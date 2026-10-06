"""R1 隔离夹具，只创建本次 Python 进程，不启动模型或网络。"""
import json
import os
from pathlib import Path
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from cairn.dispatcher.runtime.local_process import LocalProcess
from cairn.dispatcher.scheduler.loop import DispatcherLoop

folder = Path(sys.argv[1])
mode = sys.argv[2]
loop = DispatcherLoop.__new__(DispatcherLoop)
loop.futures = {}
loop._task_cancellations = set()
loop._stopping = False
loop.executor = ThreadPoolExecutor(max_workers=5)
loop.cleanup_executor = ThreadPoolExecutor(max_workers=1)
loop.client = loop.container_manager = SimpleNamespace(close=lambda: None)
code = """
import signal,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
heartbeat=Path(sys.argv[1])
number=0
while True:
    heartbeat.write_text(str(number))
    number+=1
    time.sleep(0.05)
"""
actors = []
for number in range(5):
    cancellation = loop._new_cancellation()
    heartbeat = folder / f"heartbeat-{number}"
    process = LocalProcess([sys.executable, "-B", "-c", code, str(heartbeat)], cwd=str(folder),
                           env=dict(os.environ), timeout_seconds=180, term_grace_seconds=5)
    process.start()
    cancellation.attach_process(process)
    future = loop.executor.submit(process.communicate, 180)
    loop.futures[future] = SimpleNamespace(project_id="owned-fixture", cancellation=cancellation)
    actors.append({"pid": process._process.pid, "heartbeat": str(heartbeat)})
deadline = time.monotonic() + 5
while not all(Path(actor["heartbeat"]).exists() for actor in actors):
    if time.monotonic() >= deadline:
        raise RuntimeError("owned actors did not become ready")
    time.sleep(0.01)

def terminate(_signum, _frame):
    loop.cancel_running_tasks("owned fixture shutdown")
    raise KeyboardInterrupt

signal.signal(signal.SIGTERM, signal.SIG_IGN if mode == "force" else terminate)
(folder / "ready.json").write_text(json.dumps({"actors": actors}))
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    loop.close()
