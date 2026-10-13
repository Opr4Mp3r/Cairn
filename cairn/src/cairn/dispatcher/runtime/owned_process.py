"""启动器专用的本地进程登记；不改变 harness 的 argv 或输出。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import signal
import time
from contextlib import suppress

REGISTRY_ENV = "CAIRN_OWNED_PROCESS_DIR"


def owner_lease_path(registry, parent):
    return Path(registry) / "owners" / f"{parent}.json"


def renew_owner(registry, owner, seconds):
    path = owner_lease_path(registry, os.getpid())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"owner": owner, "valid_until": time.monotonic() + seconds}))
    temporary.replace(path)


def _owned_group(pid, identity):
    if os.getppid() != pid:
        current = process_identities([pid]).get(pid)
        if current is not None and current != identity:
            return False
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _guard_group(pid, identity, parent, parent_identity, path, owner, grace, ready):
    # A separate session survives TERM of the Agent and can enforce KILL if
    # the harness or one of its descendants ignores the first signal.
    os.setsid()
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    if null > 2:
        os.close(null)
    try:
        os.write(ready, b"1")
        os.close(ready)
        while _owned_group(pid, identity):
            expired = os.getppid() != pid
            if owner is not None:
                try:
                    lease = json.loads(path.read_text())
                    expired |= lease["owner"] != owner or time.monotonic() >= lease["valid_until"]
                except (OSError, ValueError, KeyError):
                    expired = True
            else:
                expired |= process_identities([parent]).get(parent) != parent_identity
            if expired:
                with suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGTERM)
                deadline = time.monotonic() + grace
                while time.monotonic() < deadline and _owned_group(pid, identity):
                    time.sleep(.1)
                if _owned_group(pid, identity):
                    with suppress(ProcessLookupError):
                        os.killpg(pid, signal.SIGKILL)
                return
            time.sleep(.25)
    finally:
        os._exit(0)


def process_identities(pids: list[int]) -> dict[int, str]:
    if not pids:
        return {}
    result = subprocess.run(["ps", "-o", "pid=,pgid=,lstart=", "-p", ",".join(map(str, pids))],
                            capture_output=True, text=True, timeout=1,
                            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    if result.returncode not in (0, 1):
        raise RuntimeError("cannot verify owned process identities")
    return {int(line.split()[0]): " ".join(line.split()) for line in result.stdout.splitlines() if line.split()}


def main() -> None:
    parent = int(sys.argv[1])
    registry = Path(sys.argv[2])
    grace = float(sys.argv[3])
    command = sys.argv[4:]
    if os.getppid() != parent:
        raise SystemExit(130)
    pid = os.getpid()
    identity = process_identities([pid]).get(pid)
    parent_identity = process_identities([parent]).get(parent)
    if identity is None or os.getpgrp() != pid:
        raise RuntimeError("owned Agent must have a verifiable independent process group")
    registry.mkdir(parents=True, exist_ok=True)
    temporary = registry / f".{pid}.tmp"
    record = {"pid": pid, "parent_pid": parent, "identity": identity, "cwd": os.getcwd()}
    temporary.write_text(json.dumps(record), encoding="utf-8")
    temporary.replace(registry / f"{pid}.json")
    # 父进程在登记窗口被强杀时，不再启动实际 Agent；登记后的组仍可由启动器回收。
    if os.getppid() != parent:
        raise SystemExit(130)
    path = owner_lease_path(registry, parent)
    lease = json.loads(path.read_text()) if path.exists() else None
    owner = lease["owner"] if lease else None
    if lease and time.monotonic() >= lease["valid_until"]:
        raise SystemExit(130)
    ready_read, ready_write = os.pipe()
    guardian = os.fork()
    if guardian == 0:
        os.close(ready_read)
        _guard_group(pid, identity, parent, parent_identity, path, owner, grace, ready_write)
    os.close(ready_write)
    ready = os.read(ready_read, 1)
    os.close(ready_read)
    if ready != b"1":
        raise RuntimeError("Agent watchdog did not start")
    record["guardian_pid"] = guardian
    record["guardian_identity"] = process_identities([guardian]).get(guardian)
    temporary.write_text(json.dumps(record), encoding="utf-8")
    temporary.replace(registry / f"{pid}.json")
    if os.getppid() != parent:
        raise SystemExit(130)
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
