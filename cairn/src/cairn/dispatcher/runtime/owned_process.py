"""启动器专用的本地进程登记；不改变 harness 的 argv 或输出。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

REGISTRY_ENV = "CAIRN_OWNED_PROCESS_DIR"


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
    command = sys.argv[3:]
    if os.getppid() != parent:
        raise SystemExit(130)
    pid = os.getpid()
    identity = process_identities([pid]).get(pid)
    if identity is None or os.getpgrp() != pid:
        raise RuntimeError("owned Agent must have a verifiable independent process group")
    registry.mkdir(parents=True, exist_ok=True)
    temporary = registry / f".{pid}.tmp"
    temporary.write_text(json.dumps({"pid": pid, "parent_pid": parent, "identity": identity}), encoding="utf-8")
    temporary.replace(registry / f"{pid}.json")
    # 父进程在登记窗口被强杀时，不再启动实际 Agent；登记后的组仍可由启动器回收。
    if os.getppid() != parent:
        raise SystemExit(130)
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
