import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from click.testing import CliRunner
import pytest

from cairn.cli import main
from cairn.controller import kritt, kritt_probe, kritt_source
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from test_kritt import FakeAsyncConnection, patch_connection, snapshot
from test_kritt_probe import probe
from test_kritt_source import source


def test_slow_database_stops_at_total_deadline_before_source_or_agent(probe, monkeypatch):
    config, _, _, output = probe
    config.controller.kritt.probe_seconds = 1
    monkeypatch.setenv("CAIRN_KRITT_READONLY_DSN", "host=127.0.0.1")
    operations = []
    started = time.monotonic()
    actual_deadline = []
    reader = kritt_probe.read_kritt_view

    def tracked_read(*args, **kwargs):
        actual_deadline.append(kwargs["deadline"])
        return reader(*args, **kwargs)

    monkeypatch.setattr(kritt_probe, "read_kritt_view", tracked_read)

    class SlowConnection(FakeAsyncConnection):
        async def execute(self, query, params=()):
            operations.append(time.monotonic() - started)
            await asyncio.sleep(.15)
            return await super().execute(query, params)

    connection = SlowConnection(snapshot.__wrapped__())
    patch_connection(monkeypatch, connection)
    monkeypatch.setattr(kritt_probe, "verify_source", lambda *a, **kw: pytest.fail("source must not start"))
    monkeypatch.setattr(kritt_probe, "call_agent", lambda *a, **kw: pytest.fail("Agent must not start"))
    with pytest.raises(TimeoutError, match="deadline exhausted"):
        kritt_probe.run_kritt_probe(config, target_name="test", user_goal="test", output=output)
    assert connection.closed
    assert operations and max(operations) + started < actual_deadline[0]
    assert time.monotonic() - started < 1.5
    assert json.loads((output / "failure.json").read_text())["calls"] == 0


def test_budget_expiry_during_connect_cancels_attempt_without_query(monkeypatch):
    import psycopg
    stopped = []

    async def connect(*args, **kwargs):
        assert kwargs["connect_timeout"] <= 1
        try:
            await asyncio.sleep(30)
        finally:
            stopped.append(True)

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="deadline exhausted"):
        kritt.read_kritt_view("host=127.0.0.1", repo_full="example/project", deadline=started + .1)
    assert stopped == [True] and time.monotonic() - started < .6


@pytest.mark.parametrize("named_host", [False, True])
def test_native_connection_handshake_obeys_subsecond_total_budget(monkeypatch, named_host):
    # A local server accepts the connection but never completes PostgreSQL startup.
    accepted = threading.Event()
    disconnected = threading.Event()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2)

        def server():
            with listener.accept()[0] as peer:
                peer.settimeout(2)
                peer.recv(65536)
                accepted.set()
                if peer.recv(1) == b"":
                    disconnected.set()

        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        started = time.monotonic()
        host = "dns-must-not-run.invalid" if named_host else "127.0.0.1"
        address = " hostaddr=127.0.0.1" if named_host else ""
        dsn = f"host={host}{address} port={listener.getsockname()[1]} user=test dbname=test sslmode=disable"
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: pytest.fail("DNS must not start"))
        with pytest.raises(TimeoutError):
            kritt.read_kritt_view(dsn, repo_full="example/project", deadline=started + .2)
        thread.join(timeout=2)
        assert accepted.is_set() and disconnected.is_set() and not thread.is_alive()
        assert time.monotonic() - started < 1


@pytest.mark.parametrize("reason", ["deadline", "cancel"])
def test_long_query_is_interrupted_and_connection_closed(monkeypatch, reason):
    token = TaskCancellation()
    operations = []

    class BlockedConnection(FakeAsyncConnection):
        async def execute(self, query, params=()):
            operations.append(query)
            if reason == "cancel":
                asyncio.get_running_loop().call_later(.05, token.cancel, "cancel database read")
            await asyncio.sleep(30)
            return await super().execute(query, params)

    connection = BlockedConnection(snapshot.__wrapped__())
    patch_connection(monkeypatch, connection)
    started = time.monotonic()
    deadline = started + .15 if reason == "deadline" else started + 30
    with pytest.raises(TimeoutError if reason == "deadline" else InterruptedError):
        kritt.read_kritt_view("host=127.0.0.1", repo_full="example/project", deadline=deadline, cancellation=token)
    assert connection.closed and len(operations) == 1
    assert time.monotonic() - started < .6


def test_sql_limits_shrink_with_remaining_budget(monkeypatch):
    class DelayedConnection(FakeAsyncConnection):
        async def execute(self, query, params=()):
            await asyncio.sleep(.002)
            return await super().execute(query, params)

    connection = DelayedConnection(snapshot.__wrapped__())
    patch_connection(monkeypatch, connection)
    view = kritt.read_kritt_view("host=127.0.0.1", repo_full="example/project", deadline=time.monotonic() + 1)
    limits = [int(params[0]) for query, params in connection.calls if "set_config" in query]
    assert view.scan["id"] == 19 and connection.closed
    assert len(limits) > 5 and 0 < min(limits) <= max(limits) <= 1000
    assert limits == sorted(limits, reverse=True)


def test_late_reader_return_cannot_enter_source_check(probe, monkeypatch):
    config, _, view_path, output = probe
    config.controller.kritt.probe_seconds = 1
    clock = [0.0]
    monkeypatch.setattr(kritt.time, "monotonic", lambda: clock[0])

    def late_read(*args, **kwargs):
        assert kwargs["deadline"] == 1 and kwargs["cancellation"] is not None
        clock[0] = 2
        return kritt.KrittView.load(view_path)

    monkeypatch.setattr(kritt_probe, "read_kritt_view", late_read)
    monkeypatch.setattr(kritt_probe, "verify_source", lambda *a, **kw: pytest.fail("source must not start"))
    with pytest.raises(TimeoutError):
        kritt_probe.run_kritt_probe(config, target_name="test", user_goal="test", output=output)
    assert not (output / "view.json").exists()


@pytest.mark.parametrize("reason", ["deadline", "cancel"])
def test_source_hashing_stops_between_chunks_and_files(source, monkeypatch, reason):
    repo, view, manifest, _ = source
    clock = [0.0]
    reads = []
    token = TaskCancellation()
    original = type(repo).open
    monkeypatch.setattr(kritt.time, "monotonic", lambda: clock[0])

    class SlowFile:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size):
            reads.append(clock[0])
            clock[0] += .6
            if reason == "cancel":
                token.cancel("cancel source scan")
            return self.stream.read(size)

    def slow_open(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        return SlowFile(stream) if path.is_relative_to(repo) else stream

    monkeypatch.setattr(type(repo), "open", slow_open)
    with pytest.raises(TimeoutError if reason == "deadline" else InterruptedError):
        kritt_source.verify_source(repo, view, manifest, deadline=1, cancellation=token)
    assert reads and max(reads) < 1
    assert len(reads) == (2 if reason == "deadline" else 1)


@pytest.mark.parametrize("exit_mode", ["success", "timeout", "cancel"])
def test_probe_cli_cleans_owned_children_even_after_leader_exit(probe, monkeypatch, exit_mode):
    _, config, view, output = probe
    binary = config.parent / "bin" / "claude"
    child_file = config.parent / "child.json"
    monkeypatch.setenv("PROBE_CHILD_FILE", str(child_file))
    monkeypatch.setenv("PROBE_EXIT_MODE", exit_mode)
    if exit_mode == "timeout":
        data = json.loads(config.read_text())
        data["controller"]["timeouts"] = {"build": 3}
        config.write_text(json.dumps(data))
    extra = '''
if phase == ("observe" if os.environ["PROBE_EXIT_MODE"] == "success" else "search"):
    import subprocess, signal, time
    child = subprocess.Popen([sys.executable, "-c", "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pathlib.Path(os.environ["PROBE_CHILD_FILE"]).write_text(json.dumps({"pid":child.pid,"pgid":os.getpgrp()}))
    time.sleep(.1)
    if os.environ["PROBE_EXIT_MODE"] == "cancel":
        os.kill(os.getppid(), signal.SIGTERM)
    if os.environ["PROBE_EXIT_MODE"] != "success":
        time.sleep(120)
'''
    binary.write_text(binary.read_text().replace("print(json.dumps(result))", extra + "\nprint(json.dumps(result))"))
    started = time.monotonic()
    try:
        result = CliRunner().invoke(main, ["kritt-probe", "--config", str(config), "--target", "test",
            "--goal", "Review RCE mechanisms", "--view", str(view), "--output", str(output)])
        assert result.exit_code == (0 if exit_mode == "success" else 1), result.output
        actors = json.loads(child_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(actors["pid"], 0)
        assert time.monotonic() - started < 15
    finally:
        if child_file.exists():
            actors = json.loads(child_file.read_text())
            try:
                os.killpg(actors["pgid"], signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("mode", ["deadline", "cancel"])
def test_hostname_only_is_rejected_without_starting_slow_dns(monkeypatch, mode):
    events = []

    def slow_dns(*args, **kwargs):
        events.append("started")
        time.sleep(.8)
        events.append("finished")
        return []

    monkeypatch.setattr(socket, "getaddrinfo", slow_dns)
    token = TaskCancellation()
    timer = threading.Timer(.1, token.cancel, args=("cancel DNS",)) if mode == "cancel" else None
    if timer:
        timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(kritt.KrittError, match="pre-resolved hostaddr"):
            kritt.read_kritt_view("host=delayed.invalid sslmode=disable", repo_full="example/project",
                deadline=started + (.1 if mode == "deadline" else 30), cancellation=token)
        assert time.monotonic() - started < .15
        assert events == []
    finally:
        if timer:
            timer.cancel()
            timer.join()


@pytest.mark.parametrize("dsn", ["", "host=127.0.0.1 hostaddr=delayed.invalid",
                                  "host=delayed.invalid hostaddr=,127.0.0.1", "service=unbounded"])
def test_invalid_or_hidden_resolver_paths_fail_before_connection(monkeypatch, dsn):
    import psycopg
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.delenv("PGHOSTADDR", raising=False)
    monkeypatch.delenv("PGSERVICE", raising=False)
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", lambda *a, **kw: pytest.fail("must not connect"))
    with pytest.raises(kritt.KrittError, match="requires"):
        kritt.read_kritt_view(dsn, repo_full="example/project")


def test_environment_host_cannot_bypass_address_validation(monkeypatch):
    import psycopg
    monkeypatch.setenv("PGHOST", "delayed.invalid")
    monkeypatch.delenv("PGHOSTADDR", raising=False)
    monkeypatch.delenv("PGSERVICE", raising=False)
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", lambda *a, **kw: pytest.fail("must not connect"))
    with pytest.raises(kritt.KrittError, match="pre-resolved hostaddr"):
        kritt.read_kritt_view("dbname=test", repo_full="example/project")


@pytest.mark.parametrize("host,address", [("127.0.0.1", "127.0.0.1"), ("::1", "::1"),
                                         ("/tmp", ""), ("db.invalid", "127.0.0.1")])
def test_supported_hosts_are_explicitly_passed_without_resolution(monkeypatch, host, address):
    import psycopg
    connection = FakeAsyncConnection(snapshot.__wrapped__())
    observed = []

    async def connect(*args, **kwargs):
        observed.append(kwargs)
        return connection

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    dsn = f"host={host}" + (f" hostaddr={address}" if host == "db.invalid" else "")
    assert kritt.read_kritt_view(dsn, repo_full="example/project").scan["id"] == 19
    assert observed[0]["host"] == host and observed[0]["hostaddr"] == address


@pytest.mark.parametrize("interrupt_signal", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize("initial_cancel", [False, True])
def test_cleanup_signals_cannot_skip_final_child_reaping(probe, monkeypatch, interrupt_signal, initial_cancel):
    from cairn.controller import local
    _, config, view, output = probe
    binary = config.parent / "bin" / "claude"
    child_file = config.parent / "cleanup-child.json"
    monkeypatch.setenv("CLEANUP_CHILD_FILE", str(child_file))
    monkeypatch.setenv("INITIAL_CANCEL", "1" if initial_cancel else "0")
    extra = '''
if phase == ("search" if os.environ["INITIAL_CANCEL"] == "1" else "observe"):
    import subprocess, signal, time
    child = subprocess.Popen([sys.executable, "-c", "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pathlib.Path(os.environ["CLEANUP_CHILD_FILE"]).write_text(json.dumps({"pid":child.pid,"pgid":os.getpgrp()}))
    time.sleep(.1)
    if os.environ["INITIAL_CANCEL"] == "1":
        os.kill(os.getppid(), signal.SIGTERM)
        time.sleep(120)
'''
    binary.write_text(binary.read_text().replace("print(json.dumps(result))", extra + "\nprint(json.dumps(result))"))
    original = local.stop_agent_groups
    timers = []
    saved_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    def interrupt_cleanup(*args, **kwargs):
        for delay in (.2, .35) if initial_cancel else (.2,):
            timer = threading.Timer(delay, os.kill, args=(os.getpid(), interrupt_signal))
            timers.append(timer)
            timer.start()
        return original(*args, **kwargs)

    monkeypatch.setattr(local, "stop_agent_groups", interrupt_cleanup)
    started = time.monotonic()
    try:
        result = CliRunner().invoke(main, ["kritt-probe", "--config", str(config), "--target", "test",
            "--goal", "Review RCE mechanisms", "--view", str(view), "--output", str(output)])
        assert result.exit_code == 1 and '"status": "success"' not in result.output
        actors = json.loads(child_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(actors["pid"], 0)
        assert time.monotonic() - started < 15
        assert all(signal.getsignal(sig) == handler for sig, handler in saved_handlers.items())
    finally:
        for timer in timers:
            timer.cancel()
            timer.join()
        if child_file.exists():
            actors = json.loads(child_file.read_text())
            try:
                os.killpg(actors["pgid"], signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("interrupt_signal", ["SIGINT", "SIGTERM"])
@pytest.mark.parametrize("site", ["reason", "attach_process"])
def test_signal_unwinds_cancellation_critical_section(probe, interrupt_signal, site):
    from cairn.controller.local import stop_agent_groups
    _, config, view, output = probe
    injected = config.parent / "signal-injected.json"
    code = r'''
import json, os, pathlib, signal, sys
from cairn.cli import main
from cairn.dispatcher.runtime.cancellation import TaskCancellation
site, signum, marker, config, view, output = sys.argv[1:]
target = TaskCancellation.reason.fget if site == "reason" else TaskCancellation.attach_process
sent = False
def trace(frame, event, arg):
    global sent
    if not sent and event == "line" and frame.f_code is target.__code__ and frame.f_locals["self"]._lock.locked():
        sent = True
        pathlib.Path(marker).write_text(json.dumps({"site":site,"signal":signum,"lock_held":True}))
        os.kill(os.getpid(), getattr(signal, signum))
    return trace
sys.settrace(trace)
main(args=["kritt-probe","--config",config,"--target","test","--goal","offline test","--view",view,"--output",output])
'''
    process = subprocess.Popen([sys.executable, "-c", code, site, interrupt_signal, str(injected),
        str(config), str(view), str(output)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True)
    started = time.monotonic()
    try:
        stdout, stderr = process.communicate(timeout=12)
        assert process.returncode == 1, (stdout, stderr)
        assert json.loads(injected.read_text())["lock_held"] is True
        assert time.monotonic() - started < 12
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        stop_agent_groups(output / "agent-processes", {process.pid})


@pytest.mark.parametrize("interrupt_signal", [signal.SIGINT, signal.SIGTERM])
def test_signal_during_real_timeout_term_wait_does_not_reenter_process_lock(probe, monkeypatch, interrupt_signal):
    from cairn.controller.local import stop_agent_groups
    _, config, view, output = probe
    marker = config.parent / "actual-term.json"
    binary = config.parent / "bin" / "claude"
    binary.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, signal, time
def term(signum, frame):
    pathlib.Path(os.environ["TIMEOUT_TERM_MARKER"]).write_text(json.dumps({"pid":os.getpid(),"pgid":os.getpgrp()}))
signal.signal(signal.SIGTERM, term)
while True:
    time.sleep(.1)
''')
    data = json.loads(config.read_text())
    data["controller"]["timeouts"] = {"build": 3}
    config.write_text(json.dumps(data))
    monkeypatch.setenv("TIMEOUT_TERM_MARKER", str(marker))
    process = subprocess.Popen([sys.executable, "-c", "from cairn.cli import main; main()",
        "kritt-probe", "--config", str(config), "--target", "test", "--goal", "offline test",
        "--view", str(view), "--output", str(output)], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        deadline = time.monotonic() + 8
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert marker.exists(), "Agent must receive the actual timeout TERM first"
        actor = json.loads(marker.read_text())
        signalled = time.monotonic()
        os.kill(process.pid, interrupt_signal)
        stdout, stderr = process.communicate(timeout=12)
        assert process.returncode == 1, (stdout, stderr)
        assert time.monotonic() - signalled < 12
        with pytest.raises(ProcessLookupError):
            os.kill(actor["pid"], 0)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        stop_agent_groups(output / "agent-processes", {process.pid})
