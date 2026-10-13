"""A manually started local Cairn instance; submissions never start services."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
import logging
from pathlib import Path
import threading
import uuid
import shutil

from fastapi import HTTPException
from cairn.audit_evidence import assert_repository_unchanged
from cairn.server import runtime
from cairn.server.services import audit_metadata, get_project_or_404, save_audit_metadata

from cairn.controller.audit import PreflightNeedsRevision
from cairn.controller.monitor import monitor_audit
from cairn.controller.research import import_audit_plan, save_results
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.owned_process import renew_owner
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server.db import get_conn
from cairn.server import submissions
from cairn.server.services import clear_project_reason, utcnow

LOG = logging.getLogger(__name__)


class ManagedService:
    def __init__(self, config: DispatchConfig, config_path: Path, directory: Path):
        self.config, self.directory = config, directory
        self.service_id = uuid.uuid4().hex
        self.runtime_enabled = True
        runtime.INSTANCE = self.service_id
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO service_identity VALUES (1,?)", (uuid.uuid4().hex,))
            self.database_id = conn.execute("SELECT database_id FROM service_identity WHERE singleton=1").fetchone()[0]
            runtime.restart(conn)
            # Reopening storage must not silently resume previously active work.
            for row in conn.execute("SELECT id FROM projects WHERE mode='audit' AND status IN ('active','draining')"):
                self._stop_project(conn, row[0])
            conn.execute("UPDATE audit_submissions SET status='interrupted',worker_token=NULL,error='Service restarted; explicit task recovery is required',updated_at=? WHERE status IN ('pending','preparing','running','resuming','pausing')", (utcnow(),))
        self.dispatcher = DispatcherLoop(config_path, managed=True)
        self._stop = threading.Event()
        self.error = None
        self.jobs = {}
        self._pause_cleanups = {}
        self.executor = ThreadPoolExecutor(max_workers=config.runtime.max_running_projects, thread_name_prefix="cairn-project")
        self.dispatch_thread = threading.Thread(target=self._dispatch, name="cairn-dispatcher", daemon=True)
        self.clock_thread = threading.Thread(target=self._heartbeat, name="cairn-budget-clock", daemon=True)
        self.control_thread = threading.Thread(target=self._control, name="cairn-controller", daemon=True)

    @staticmethod
    def _stop_project(conn, project_id):
        row = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()
        if row and row[0] in ("active", "draining", "stopped"):
            conn.execute("UPDATE projects SET status='stopped' WHERE id=?", (project_id,))
            conn.execute("UPDATE intents SET worker=NULL WHERE project_id=? AND to_fact_id IS NULL", (project_id,))
            clear_project_reason(conn, project_id)

    def start(self):
        renew_owner(self.directory / "agent-processes", self.service_id, runtime.LEASE_SECONDS)
        self.dispatch_thread.start()
        self.control_thread.start()
        self.clock_thread.start()

    def health(self):
        dispatcher_ready = (self.dispatch_thread.is_alive() and self.dispatcher._settings_checked
                            and not self.dispatcher._stopping)
        controller_ready = self.control_thread.is_alive() and self.clock_thread.is_alive()
        return {"managed": True, "service_id": self.service_id, "database_id": self.database_id,
                "ready": bool(dispatcher_ready and controller_ready and not self._stop.is_set() and not self.error),
                "dispatcher_ready": dispatcher_ready, "controller_ready": controller_ready, "error": self.error}

    def _dispatch(self):
        self.dispatcher.on_failure = lambda exc: self._fail(f"Dispatcher stopped: {type(exc).__name__}: {exc}")
        try:
            self.dispatcher.run()
        except BaseException as exc:
            self._fail(f"Dispatcher stopped: {type(exc).__name__}: {exc}")
            LOG.exception("managed Dispatcher stopped")

    def _fail(self, error):
        if self.error is not None:
            return
        self.error = error
        try:
            with get_conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                for row in conn.execute("SELECT project_id FROM audit_submissions WHERE status IN ('pending','preparing','running','resuming','pausing') AND project_id IS NOT NULL"):
                    self._stop_project(conn, row[0])
                    status = get_project_or_404(conn, row[0])["status"]
                    runtime.freeze(conn, row[0], "finished" if status in ("completed", "budget_exhausted") else "interrupted", error)
                conn.execute("UPDATE audit_submissions SET status='error',worker_token=NULL,error=?,updated_at=? WHERE status IN ('pending','preparing','running','resuming','pausing')",
                             (error, utcnow()))
        finally:
            tokens = [token for _, token in list(self.jobs.values())]
            with ThreadPoolExecutor(max_workers=len(tokens) + 1) as cleanup:
                futures = [cleanup.submit(self.dispatcher.cancel_running_tasks, error)]
                futures.extend(cleanup.submit(token.cancel, error) for token in tokens)
                for future in futures:
                    future.result()

    def _control(self):
        try:
            while not self._stop.wait(.2):
                with get_conn() as conn:
                    pausing = [row[0] for row in conn.execute("SELECT project_id FROM audit_submissions WHERE status='pausing'")]
                for project_id in pausing:
                    self._pause_calls(project_id)
                for key, (future, _) in list(self.jobs.items()):
                    if future.done():
                        future.result()
                        del self.jobs[key]
                while len(self.jobs) < self.config.runtime.max_running_projects and not self.error:
                    row = submissions.claim_next()
                    if row is None:
                        break
                    if self._stop.is_set() or self.error:
                        submissions.finish(row["id"], "interrupted", error=self.error or "Cairn service shutdown")
                        break
                    token = TaskCancellation()
                    self.jobs[row["id"]] = (self.executor.submit(self._run_submission, row, token), token)
        except BaseException as exc:
            self._fail(f"Controller stopped: {type(exc).__name__}: {exc}")
            LOG.exception("managed Controller stopped")

    def _heartbeat(self):
        try:
            while not self._stop.wait(runtime.HEARTBEAT_SECONDS):
                with get_conn() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    runtime.heartbeat(conn)
                if self.error is None:
                    renew_owner(self.directory / "agent-processes", self.service_id, runtime.LEASE_SECONDS)
        except BaseException as exc:
            self._fail(f"Budget heartbeat stopped: {exc}")

    def pause(self, project_id, expected_generation=None):
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            get_project_or_404(conn, project_id)
            if expected_generation is not None and expected_generation != str(runtime.load(conn, project_id)["generation"]):
                raise HTTPException(409, "Runtime generation changed; old attempt cannot control this task")
            state = runtime.pause(conn, project_id)
            if state["state"] == "pausing":
                conn.execute("UPDATE audit_submissions SET status='pausing' WHERE project_id=?", (project_id,))

    def _pause_calls(self, project_id):
        cleanups = self.__dict__.setdefault("_pause_cleanups", {})
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = runtime.load(conn, project_id)
            if current is None or current["state"] != "pausing" or project_id in cleanups:
                return
            generation = current["generation"]
            cleanups[project_id] = generation
        try:
            self._clean_paused_calls(project_id, generation)
        finally:
            cleanups.pop(project_id, None)

    def _clean_paused_calls(self, project_id, generation):
        self.dispatcher.cancel_project_tasks(project_id, "Task paused")
        for key, (future, token) in list(self.jobs.items()):
            with get_conn() as conn:
                row = submissions.get_row(conn, key)
            if row["project_id"] == project_id and not future.done():
                token.cancel("Task paused")
                return
        with get_conn() as conn:
            metadata = audit_metadata(conn, project_id)
        if metadata is None:
            return
        from cairn.controller.local import stop_agent_groups
        stop_agent_groups(self.directory / "agent-processes", {os.getpid()}, {metadata.workspace_path})
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = runtime.load(conn, project_id)
            if current and current["generation"] == generation and current["state"] == "pausing":
                runtime.freeze(conn, project_id, "paused", "User paused task")
                conn.execute("UPDATE audit_submissions SET status='paused' WHERE project_id=? AND status='pausing'", (project_id,))

    def resume(self, project_id, remaining_seconds=None, expected_generation=None):
        with get_conn() as conn:
            metadata = audit_metadata(conn, project_id)
            project = get_project_or_404(conn, project_id)
            if project["status"] in ("completed", "budget_exhausted"):
                raise HTTPException(409, "Finished projects cannot resume")
            row = conn.execute("SELECT * FROM audit_submissions WHERE project_id=?", (project_id,)).fetchone()
            if metadata is None:
                raise HTTPException(404, "Task is not registered in this Cairn database")
            if row is None and remaining_seconds is None and metadata.started_at is not None:
                raise HTTPException(409, {"status": "needs_budget", "phase": metadata.phase,
                                          "reason": "Legacy task needs its remaining stage budget"})
            key = row["id"] if row else None
            current = runtime.load(conn, project_id)
            if expected_generation is not None and current and expected_generation != str(current["generation"]):
                raise HTTPException(409, "Runtime generation changed; old attempt cannot control this task")
            if current and (runtime.live(current) or current["state"] == "resuming"):
                if remaining_seconds is not None:
                    raise HTTPException(409, "Budget migration is one-time")
                return
            job = self.jobs.get(key)
            if job and not job[0].done():
                raise HTTPException(409, "Previous task calls are still stopping")
        try:
            assert_repository_unchanged(Path(metadata.repo_path), metadata.baseline, Path(metadata.workspace_root))
        except (OSError, ValueError, RuntimeError) as exc:
            with get_conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                runtime.freeze(conn, project_id, "blocked", str(exc))
            raise HTTPException(409, f"Cannot resume the fixed source: {exc}") from exc
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            metadata = audit_metadata(conn, project_id)
            current = runtime.load(conn, project_id)
            if project_id in getattr(self, "_pause_cleanups", {}):
                raise HTTPException(409, "Previous pause cleanup is still in progress")
            if expected_generation is not None and current and expected_generation != str(current["generation"]):
                raise HTTPException(409, "Runtime generation changed; old attempt cannot control this task")
            if current and (runtime.live(current) or current["state"] == "resuming"):
                if remaining_seconds is not None:
                    raise HTTPException(409, "Budget migration is one-time")
                return
            runtime.resume(conn, metadata, remaining_seconds)
            if key is None:
                key = submissions.adopt_legacy(conn, self, metadata)["id"]
            conn.execute("UPDATE audit_submissions SET status='resuming',worker_token=NULL,error=NULL WHERE id=?", (key,))

    def _restore_workspace(self, config, prepared, project):
        from cairn.controller.kritt_query import write_json
        metadata = project.project.audit
        workspace = Path(metadata.workspace_root) / project.project.id
        workspace.mkdir(parents=True, exist_ok=True)
        repo = workspace / "repo"
        if repo.exists() or repo.is_symlink():
            if repo.resolve(strict=True) != Path(metadata.repo_path):
                raise ValueError("Existing workspace source does not match this project")
        else:
            repo.symlink_to(metadata.repo_path, target_is_directory=True)
        context_path = workspace / "input.json"
        if context_path.exists():
            context = json.loads(context_path.read_text())
            if any(context.get(k) != v for k, v in {"project_id": project.project.id,
                    "repo_path": metadata.repo_path, "policy_digest": metadata.policy_digest}.items()):
                raise ValueError("Existing workspace context does not match this project")
            if "kritt-materials.json" in context.get("materials", {}) and all(Path(path).is_file() for path in context["materials"].values()):
                return
        materials = {"kritt-materials.json": prepared["materials"], "requirements.json": prepared["requirements"],
                     "references.json": prepared["references"], "query-receipts.json": prepared.get("query_receipts")}
        research_context = {k: prepared[k] for k in ("scan_id", "commit_sha", "repo_full", "source_root", "user_goal")}
        goal = next(f.description for f in project.facts if f.id == "goal")
        write_json(workspace / "research-context.json", {**research_context, "goal": goal})
        copied = {"research-context.json": str(workspace / "research-context.json")}
        for name, path in materials.items():
            if path is None:
                path = str(Path(prepared["run_dir"]) / name)
            destination = workspace / name
            shutil.copyfile(path, destination)
            copied[name] = str(destination)
        write_json(context_path, {"project_id": project.project.id, "repo_path": metadata.repo_path,
                   "policy_digest": metadata.policy_digest, "materials": copied})

    @staticmethod
    def _restore_preparation(client, prepared, project_id):
        from cairn.audit_evidence import json_digest
        from cairn.controller.models import AgentCallResult
        from cairn.server.models import AuditRecordRequest
        folder = Path(prepared["run_dir"])
        locator = json.loads((folder / "project.json").read_text())
        controller_run = Path(locator["controller_run"])
        if locator["project_id"] != project_id or not controller_run.resolve().is_relative_to((folder / "controller").resolve()):
            raise ValueError("Original preparation receipts do not identify this project")
        saved = client.get_audit(project_id).records
        artifacts = {path for record in saved for path in record.artifacts}
        for artifact in sorted(controller_run.rglob("result.json")):
            result = AgentCallResult.model_validate_json(artifact.read_text())
            if str(artifact) in artifacts or not result.phase.startswith(("preflight", "seed_review")):
                continue
            payload = json.loads(artifact.with_name("input.json").read_text())
            success = result.status == "success"
            body = AuditRecordRequest(record_key="recovered-preparation:" + json_digest(str(artifact)),
                object_type="project", object_id=project_id, input_digest=json_digest(payload),
                phase=result.phase, session=result.session, attempt_status="success" if success else "error",
                error=None if success else result.error or result.status,
                decision=("approved" if result.data["approved"] else "rejected") if success and result.phase.startswith("preflight") else None,
                result=result.data, artifacts=[str(artifact)])
            response = client.record_audit_review(project_id, body)
            if not response.ok:
                raise ValueError(f"Cannot restore preparation receipt: {response.text}")
        records = client.get_audit(project_id).records
        if not any(r.phase.startswith("preflight") and r.decision == "approved" for r in records):
            raise ValueError("Approved preparation receipt is missing; cannot activate this project")

    def _run_submission(self, row, token):
        if self._stop.is_set() or self.error:
            token.cancel(self.error or "Cairn service shutdown")
        config = DispatchConfig.model_validate_json(row["execution_json"])
        config.server = self.config.server
        prepared = json.loads(row["prepared_json"])
        backend = LocalBackend(config.local)
        project_id = row["project_id"]
        restoring = project_id is not None
        with closing(CairnClient(config.server, trust_env=False,
            headers={"X-Cairn-Submission-Token": row["worker_token"]})) as client:
            try:
                if project_id is None:
                    result = import_audit_plan(config, client, backend, prepared, cancellation=token, external_builder=True)
                    project_id = result["project_id"]
                else:
                    project = client.get_project(project_id)
                    self._restore_workspace(config, prepared, project)
                    if project.project.audit.started_at is None:
                        self._restore_preparation(client, prepared, project_id)
                        result = client.start_audit(project_id)
                        if not result.ok:
                            raise RuntimeError(result.text)
                    else:
                        with get_conn() as conn:
                            conn.execute("BEGIN IMMEDIATE")
                            metadata = audit_metadata(conn, project_id)
                            runtime.activate(conn, project_id)
                            state = "active" if metadata.phase == "researching" else "draining"
                            conn.execute("UPDATE projects SET status=? WHERE id=?", (state, project_id))
                            from cairn.server.audit_budget import advance_budget
                            advance_budget(conn, project_id)
                current = client.get_runtime(project_id)
                restoring = False
                client.set_generation(current["generation"])
                submissions.finish(row["id"], "running")
                from cairn.controller.kritt_query import write_json
                write_json(Path(prepared["run_dir"]) / "research.json", {"status": "running",
                    "project_id": project_id, "generation": current["generation"], "server": config.server,
                    "service_id": self.service_id, "database_id": self.database_id})
                LOG.info("managed submission running submission=%s project=%s generation=%s", row["id"], project_id, current["generation"])
                terminal = monitor_audit(config, client, backend, project_id, cancellation=token)
                if token.is_cancelled:
                    raise KeyboardInterrupt(token.reason)
                self.dispatcher.cancel_project_tasks(project_id, "Task ended")
                results = save_results(client, project_id, Path(prepared["run_dir"]))
                write_json(Path(prepared["run_dir"]) / "research.json", {"status": terminal["status"],
                    "project_id": project_id, "server": config.server, "results": results,
                    "generation": current["generation"],
                    "service_id": self.service_id, "database_id": self.database_id})
                with get_conn() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    current = runtime.load(conn, project_id)
                    if current and current["state"] == "pausing":
                        runtime.freeze(conn, project_id, "paused", "User paused task")
                        conn.execute("UPDATE audit_submissions SET status='paused' WHERE id=?", (row["id"],))
                    elif current and current["state"] == "interrupted":
                        conn.execute("UPDATE audit_submissions SET status='interrupted',error=? WHERE id=?", (current["reason"], row["id"]))
                    else:
                        runtime.freeze(conn, project_id, "finished", terminal["status"])
                        conn.execute("UPDATE audit_submissions SET status=?,error=NULL WHERE id=? AND status!='deleted'", (terminal["status"], row["id"]))
            except PreflightNeedsRevision as exc:
                submissions.finish(row["id"], "needs_revision", issues=exc.issues)
            except BaseException as exc:
                if project_id is not None:
                    with get_conn() as conn:
                        metadata = audit_metadata(conn, project_id)
                    if metadata is not None:
                        from cairn.controller.local import stop_agent_groups
                        stop_agent_groups(self.directory / "agent-processes", {os.getpid()}, {metadata.workspace_path})
                with get_conn() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    saved = submissions.get_row(conn, row["id"])
                    if saved["project_id"]:
                        self._stop_project(conn, saved["project_id"])
                        current = runtime.load(conn, saved["project_id"])
                        paused = current and current["state"] == "pausing" and not self._stop.is_set()
                        state = "paused" if paused else "blocked" if restoring and not token.is_cancelled else "interrupted"
                        status = get_project_or_404(conn, saved["project_id"])["status"]
                        runtime.freeze(conn, saved["project_id"], "finished" if status in ("completed", "budget_exhausted") else state,
                                       "Task paused" if paused else str(exc))
                        conn.execute("UPDATE audit_submissions SET status=?,error=? WHERE id=? AND status!='deleted'",
                                     (state, None if paused else str(exc), row["id"]))
                    else:
                        conn.execute("UPDATE audit_submissions SET status='error',error=? WHERE id=? AND status!='deleted'", (str(exc), row["id"]))
                LOG.warning("submission stopped submission=%s error=%s", row["id"], exc)
            finally:
                backend.close()

    def close(self):
        if self._stop.is_set():
            return
        self._stop.set()
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in conn.execute("SELECT project_id FROM project_runtime").fetchall():
                current = runtime.load(conn, row[0])
                if current["state"] == "running":
                    runtime.pause(conn, row[0])
            conn.execute("UPDATE audit_submissions SET status='interrupted',worker_token=NULL,error='Cairn service shutdown' WHERE status IN ('pending','preparing')")
        if self.clock_thread.ident is not None:
            self.clock_thread.join(timeout=3)
        if self.control_thread.ident is not None:
            self.control_thread.join(timeout=2)
        tokens = [token for _, token in self.jobs.values()]
        if tokens:
            with ThreadPoolExecutor(max_workers=len(tokens)) as cleanup:
                list(cleanup.map(lambda token: token.cancel("Cairn service shutdown"), tokens))
        self.dispatcher.cancel_running_tasks("Cairn service shutdown")
        if self.dispatch_thread.ident is not None:
            self.dispatch_thread.join(timeout=30)
        else:
            self.dispatcher.close()
        self.executor.shutdown(wait=True, cancel_futures=True)
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in conn.execute("SELECT project_id FROM project_runtime").fetchall():
                current = runtime.load(conn, row[0])
                if current["state"] not in ("paused", "finished", "blocked"):
                    runtime.freeze(conn, row[0], "interrupted", "Cairn service shutdown")
                    self._stop_project(conn, row[0])
        if self.dispatch_thread.is_alive() or self.control_thread.is_alive():
            raise RuntimeError("service threads did not stop")
