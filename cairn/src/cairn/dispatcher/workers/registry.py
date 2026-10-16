from __future__ import annotations

from cairn.dispatcher.workers.adapters import ClaudeCodeDriver, CodexDriver, GrokDriver, MiniMaxDriver, MockDriver, PiDriver
from cairn.dispatcher.workers.base import WorkerDriver


_CLAUDE = ClaudeCodeDriver()
_MOCK = MockDriver()

DRIVERS: dict[str, WorkerDriver] = {
    "claudecode": _CLAUDE,
    "codex": CodexDriver(),
    "pi": PiDriver(),
    "grok": GrokDriver(),
    "mock": _MOCK,
}

# Local variants invoke the host CLIs in their native configuration (no cairn provider
# injection). claudecode and mock build identical commands in both modes, so they are shared.
LOCAL_DRIVERS: dict[str, WorkerDriver] = {
    "claudecode": _CLAUDE,
    "codex": CodexDriver(local=True),
    "pi": PiDriver(local=True),
    "grok": GrokDriver(local=True),
    "minimax": MiniMaxDriver(),
    "mock": _MOCK,
}


def get_driver(name: str, execution: str = "container") -> WorkerDriver:
    if execution == "local":
        drivers = LOCAL_DRIVERS
    elif execution == "container":
        drivers = DRIVERS
    else:
        raise ValueError(f"unsupported execution mode: {execution}")
    try:
        return drivers[name]
    except KeyError:
        raise ValueError(f"worker {name!r} is not registered for {execution} execution") from None
