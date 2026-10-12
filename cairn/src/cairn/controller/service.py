"""A manually started local Cairn instance; submissions never start services."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import logging
from pathlib import Path
import threading
import uuid

from cairn.controller.audit import PreflightNeedsRevision
from cairn.controller.monitor import monitor_audit
from cairn.controller.research import import_audit_plan, save_results
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server.db import get_conn
from cairn.server import submissions
from cairn.server.services import clear_project_reason, utcnow

LOG = logging.getLogger(__name__)


class ManagedService:
    def __init__(self, config: DispatchConfig, config_path: Path, directory: Path):
        self.config, self.directory = config, directory
        self.service_id = uuid.uuid4().hex
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO service_identity VALUES (1,?)", (uuid.uuid4().hex,))
            self.database_id = conn.execute("SELECT database_id FROM service_identity WHERE singleton=1").fetchone()[0]
            # Reopening storage must not silently resume previously active work.
            for row in conn.execute("SELECT id FROM projects WHERE mode='audit' AND status IN ('active','draining')"):
                self._stop_project(conn, row[0])
            conn.execute("UPDATE audit_submissions SET status='interrupted',worker_token=NULL,error='Service restarted; explicit task recovery is required',updated_at=? WHERE status IN ('pending','preparing','running')", (utcnow(),))
        self.dispatcher = DispatcherLoop(config_path, managed=True)
        self._stop = threading.Event()
        self.error = None
        self.jobs = {}
        self.executor = ThreadPoolExecutor(max_workers=config.runtime.max_running_projects, thread_name_prefix="cairn-project")
        self.dispatch_thread = threading.Thread(target=self._dispatch, name="cairn-dispatcher", daemon=True)
        self.control_thread = threading.Thread(target=self._control, name="cairn-controller", daemon=True)

    @staticmethod
    def _stop_project(conn, project_id):
        row = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()
        if row and row[0] in ("active", "draining", "stopped"):
            conn.execute("UPDATE projects SET status='stopped' WHERE id=?", (project_id,))
            conn.execute("UPDATE intents SET worker=NULL WHERE project_id=? AND to_fact_id IS NULL", (project_id,))
            clear_project_reason(conn, project_id)

    def start(self):
        self.dispatch_thread.start()
        self.control_thread.start()

    def health(self):
        dispatcher_ready = (self.dispatch_thread.is_alive() and self.dispatcher._settings_checked
                            and not self.dispatcher._stopping)
        controller_ready = self.control_thread.is_alive()
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
                for row in conn.execute("SELECT project_id FROM audit_submissions WHERE status IN ('pending','preparing','running') AND project_id IS NOT NULL"):
                    self._stop_project(conn, row[0])
                conn.execute("UPDATE audit_submissions SET status='error',worker_token=NULL,error=?,updated_at=? WHERE status IN ('pending','preparing','running')",
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

    def _run_submission(self, row, token):
        if self._stop.is_set() or self.error:
            token.cancel(self.error or "Cairn service shutdown")
        config = DispatchConfig.model_validate_json(row["execution_json"])
        prepared = json.loads(row["prepared_json"])
        backend = LocalBackend(config.local)
        project_id = None
        with closing(CairnClient(config.server, trust_env=False,
            headers={"X-Cairn-Submission-Token": row["worker_token"]})) as client:
            try:
                result = import_audit_plan(config, client, backend, prepared, cancellation=token, external_builder=True)
                project_id = result["project_id"]
                submissions.finish(row["id"], "running")
                LOG.info("managed submission running submission=%s project=%s", row["id"], project_id)
                terminal = monitor_audit(config, client, backend, project_id, cancellation=token)
                if token.is_cancelled:
                    raise KeyboardInterrupt(token.reason)
                results = save_results(client, project_id, Path(prepared["run_dir"]))
                from cairn.controller.kritt_query import write_json
                write_json(Path(prepared["run_dir"]) / "research.json", {"status": terminal["status"],
                    "project_id": project_id, "server": config.server, "results": results,
                    "service_id": self.service_id, "database_id": self.database_id})
                submissions.finish(row["id"], terminal["status"])
            except PreflightNeedsRevision as exc:
                submissions.finish(row["id"], "needs_revision", issues=exc.issues)
            except BaseException as exc:
                with get_conn() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    current = submissions.get_row(conn, row["id"])
                    if current["project_id"]:
                        self._stop_project(conn, current["project_id"])
                submissions.finish(row["id"], "interrupted" if token.is_cancelled else "error",
                    error=f"{type(exc).__name__}: {exc}")
                LOG.warning("submission stopped submission=%s error=%s", row["id"], exc)
            finally:
                backend.close()

    def close(self):
        self._stop.set()
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
        if self.dispatch_thread.is_alive() or self.control_thread.is_alive():
            raise RuntimeError("service threads did not stop")
