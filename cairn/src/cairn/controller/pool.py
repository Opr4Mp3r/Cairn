"""Bounded, on-demand calls for one Controller role; results return to its owner."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from cairn.dispatcher.runtime.cancellation import TaskCancellation


def run_pooled(config, phase, items, invoke, cancellation):
    """Yield (item, result); invoke(item, worker, child_token) runs in a pool slot.

    The caller consumes results on its own thread and closes this iterator when
    stopping early, so graph writes and lifecycle decisions remain serialized.
    """
    workers = config.pool_workers(phase)
    capacity = sum(worker.max_running for worker in workers)
    if not capacity:
        raise ValueError(f"empty role pool: {phase}")
    executor = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix=f"cairn-{phase}")
    pending, running = {}, {worker.name: 0 for worker in workers}
    iterator, exhausted, cursor = iter(items), False, 0
    try:
        while pending or not exhausted:
            if cancellation.is_cancelled:
                raise KeyboardInterrupt(cancellation.reason or "Controller cancelled")
            while not exhausted and len(pending) < capacity:
                try:
                    item = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                if cancellation.is_cancelled:
                    raise KeyboardInterrupt(cancellation.reason or "Controller cancelled")
                for offset in range(len(workers)):
                    index = (cursor + offset) % len(workers)
                    worker = workers[index]
                    if running[worker.name] < worker.max_running:
                        cursor = (index + 1) % len(workers)
                        break
                child = TaskCancellation()
                future = executor.submit(invoke, item, worker, child)
                pending[future] = (item, worker, child)
                running[worker.name] += 1
            if not pending:
                break
            done, _ = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
            for future in done:
                item, worker, _ = pending.pop(future)
                running[worker.name] -= 1
                yield item, future.result()
    finally:
        try:
            if pending:
                # Cancel process groups concurrently; N slots must not multiply grace time.
                with ThreadPoolExecutor(max_workers=len(pending)) as cleanup:
                    stopped = []
                    for future, (_, _, child) in pending.items():
                        future.cancel()
                        stopped.append(cleanup.submit(child.cancel, cancellation.reason or "role pool shutdown"))
                    for future in stopped:
                        future.result()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
