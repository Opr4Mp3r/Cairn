"""Read-only operational details for the existing task runtime."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shlex

from fastapi import HTTPException

from cairn.dispatcher.config import DispatchConfig
from cairn.server import db


def startup_instruction(service):
    if service is None:
        return "在终端使用原配置和数据库执行 cairn up，再回到此页面重试连接。"
    project = Path(__file__).resolve().parents[3]
    return shlex.join(["uv", "run", "--project", str(project), "cairn", "up", "--config",
                       str(service.directory / "config.yaml"), "--db-path", str(db._db_path)])


def panel_snapshot(conn, metadata, project, data, service):
    row = conn.execute("SELECT * FROM audit_submissions WHERE project_id=?", (project["id"],)).fetchone()
    config = DispatchConfig.model_validate_json(row["execution_json"]) if row else None
    pools = config.role_pools if config else None
    if config and pools is None:
        pools = {}
        for worker in config.workers:
            for role in worker.task_types:
                harnesses = pools.setdefault(role, {})
                harnesses[worker.type] = harnesses.get(worker.type, 0) + worker.max_running
    health = service.health() if service else {"managed": False, "ready": False}
    known = bool(health["ready"] and hasattr(service, "active_calls"))
    slots = {role: {harness: 0 for harness in counts} for role, counts in (pools or {}).items()} if known else None
    if known:
        for call in service.active_calls(project["id"], metadata.workspace_path, row["id"] if row else None):
            harnesses = slots.setdefault(call["role"], {})
            harnesses[call["harness"]] = harnesses.get(call["harness"], 0) + call["count"]
    terminal = project["status"] in ("completed", "budget_exhausted")
    actions, blocked = [], None
    if terminal:
        blocked = "任务已结束，可查看或导出结果。"
    elif service is None:
        blocked = "当前是历史数据查看服务；在线恢复需要人工启动 Cairn 执行服务。"
    elif not health["ready"]:
        blocked = health.get("error") or "执行组件未就绪，请人工检查或重启 Cairn。"
    elif data["state"] in ("pausing", "resuming"):
        blocked = "正在收敛调用。" if data["state"] == "pausing" else "正在恢复原任务与调度。"
    elif project["id"] in getattr(service, "_pause_cleanups", {}):
        blocked = "上一轮进程清理尚未完成。"
    elif data["state"] in ("running", "preparing"):
        actions = ["pause"]
    else:
        job = service.jobs.get(row["id"]) if row else None
        if job and not job[0].done():
            blocked = "上一轮调用正在停止，结束后可恢复。"
        else:
            actions = ["resume"]
    budget = metadata.budget
    data.update(project_status=project["status"], managed=service is not None, service_ready=health["ready"],
                role_pools=pools, slots_in_use=slots, occupancy_known=known,
                updated_at=datetime.now(timezone.utc).isoformat(), available_actions=actions,
                blocked_reason=blocked, last_error=(row["error"] if row else None) or data.get("reason"),
                needs_budget=not terminal and data["clock_mode"] == "legacy",
                budget=budget.model_dump(),
                budget_total_seconds=budget.research_seconds+budget.drain_seconds+budget.final_review_seconds,
                run_dir=row["run_dir"] if row else str(db._db_path.parent), submission_id=row["id"] if row else None,
                manual_start=startup_instruction(service))
    return data


def project_logs(metadata, log_id=None, prepared_run=None):
    """Only saved execution receipts under this project's workspace are exposed."""
    workspace = Path(metadata.workspace_path).resolve() if metadata.workspace_path else None
    files = {}
    roots = [(workspace, "research", ("*.json",), ""),
             (workspace, "controller-reviews", ("result.json", "failure.json"), "")]
    if prepared_run:
        roots.append((Path(prepared_run).resolve(), "controller", ("result.json", "failure.json"), "preparation/"))
    for root, folder, patterns, prefix in roots:
        if root is None:
            continue
        base = root / folder
        if not base.is_dir() or not base.resolve().is_relative_to(root):
            continue
        for pattern in patterns:
            for candidate in base.rglob(pattern):
                if candidate.is_file() and candidate.resolve().is_relative_to(root):
                    files[prefix+candidate.relative_to(root).as_posix()] = candidate
    ordered = sorted(files, key=lambda name: files[name].stat().st_mtime, reverse=True)[:50]
    if log_id is not None and log_id not in files:
        raise HTTPException(404, "Log is not a saved receipt for this project")
    selected = log_id if log_id is not None else next(iter(ordered), None)
    text = ""
    if selected:
        # Bounded tail, including malformed/partial receipts left by a crash.
        with files[selected].open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size-32768))
            text = handle.read(32768).decode("utf-8", errors="replace")
    return {"files": [{"id": name, "updated_at": datetime.fromtimestamp(files[name].stat().st_mtime, timezone.utc).isoformat()}
                      for name in ordered], "selected": selected, "text": text,
            "note": "保存的调用回执（最多显示末尾 32 KiB），在途调用的完整输出将在调用结束后保存。"}
