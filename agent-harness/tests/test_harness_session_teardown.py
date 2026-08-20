"""The harness reaps what a session leaves behind, from a real `finally`.

A session that starts a dev server and dies leaves that server holding a port,
and the failure that produces lands somewhere else entirely: the *next* run's
`serve.sh` cannot bind, for reasons that look nothing like the real cause. This
file drives real subprocesses that really outlive their parent, because the
whole point is what happens to a process the harness never directly started.

Every exit is covered — success, error, timeout, KeyboardInterrupt — since three
of those four are the ones that leak.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from agent_harness.config import HarnessError, SessionRequest
from agent_harness.session import run_session

SESSION_ID = "99999999-8888-7777-6666-555555555555"

RESULT_EVENT = (
    '{"type": "result", "subtype": "success", "is_error": false, '
    f'"session_id": "{SESSION_ID}", "result": "done", "num_turns": 1, '
    '"total_cost_usd": 0.1, "duration_ms": 10}'
)

# A fake CLI that leaves a child running after it exits: the shape of a session
# that started a dev server and did not stop it. The pid is written out so the
# test can ask, afterwards, whether anything reaped it.
FAKE_BODY = f"""
import json, os, subprocess, sys, time
from pathlib import Path

mode = os.environ.get("FAKE_MODE", "success")
pid_file = Path(os.environ["FAKE_PID_FILE"])

if mode == "stubborn-child":
    # Ignores SIGTERM, so only the SIGKILL escalation can stop it. The ready
    # marker removes the race: without it the handler may not be installed when
    # the signal lands, and the child dies quietly like any other.
    ready = pid_file.with_suffix(".ready")
    child = subprocess.Popen([
        sys.executable, "-c",
        "import signal, sys, time\\n"
        "from pathlib import Path\\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
        "Path(sys.argv[1]).write_text('ready')\\n"
        "time.sleep(300)\\n",
        str(ready),
    ])
    for _ in range(500):
        if ready.is_file():
            break
        time.sleep(0.02)
else:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])

pid_file.write_text(str(child.pid))

if mode == "no-output":
    sys.exit(3)

if mode == "hang":
    time.sleep(300)

print({RESULT_EVENT!r}, flush=True)
sys.exit(0)
"""


def process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - alive, just not ours
        return True
    return True


def wait_for(path: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text().strip():
            return int(path.read_text().strip())
        time.sleep(0.02)
    raise AssertionError(f"the fake session never wrote {path}")


def wait_until_dead(pid: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_is_alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture()
def leaky_claude(tmp_path: Path, monkeypatch):
    """Put a `claude` on PATH that leaves a child running behind it."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(f"#!{sys.executable}\n{FAKE_BODY}", encoding="utf-8")
    fake.chmod(0o755)

    pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PID_FILE", str(pid_file))
    return pid_file


def make_request(tmp_path: Path, timeout_seconds: int = 60) -> SessionRequest:
    return SessionRequest(
        prompt="do the thing",
        repo_path=tmp_path,
        session_id=SESSION_ID,
        timeout_seconds=timeout_seconds,
    )


class TestTheProcessGroupIsReapedOnEveryExit:
    def test_after_a_session_that_succeeded(self, tmp_path, leaky_claude, monkeypatch):
        monkeypatch.setenv("FAKE_MODE", "success")
        result = run_session(make_request(tmp_path))
        assert result.session_id == SESSION_ID

        child = wait_for(leaky_claude)
        assert wait_until_dead(child), (
            "the session exited cleanly and its orphan survived, which is the "
            "leak that strands the next run's serve.sh on a held port"
        )

    def test_after_a_session_that_produced_nothing(
        self, tmp_path, leaky_claude, monkeypatch
    ):
        monkeypatch.setenv("FAKE_MODE", "no-output")
        with pytest.raises(HarnessError):
            run_session(make_request(tmp_path))

        child = wait_for(leaky_claude)
        assert wait_until_dead(child)

    def test_after_a_session_that_timed_out(self, tmp_path, leaky_claude, monkeypatch):
        monkeypatch.setenv("FAKE_MODE", "hang")
        with pytest.raises(HarnessError, match="timeout"):
            run_session(make_request(tmp_path, timeout_seconds=1))

        child = wait_for(leaky_claude)
        assert wait_until_dead(child)

    def test_after_a_keyboard_interrupt(self, tmp_path, leaky_claude, monkeypatch):
        # Interrupting a run is a normal way to end one, and it is the exit most
        # likely to skip a cleanup that is not in a `finally`.
        pid_file = leaky_claude

        def interrupt_once_the_child_exists(self, *arguments, **keywords):
            wait_for(pid_file)
            raise KeyboardInterrupt

        monkeypatch.setenv("FAKE_MODE", "success")
        monkeypatch.setattr(subprocess.Popen, "wait", interrupt_once_the_child_exists)
        with pytest.raises(KeyboardInterrupt):
            run_session(make_request(tmp_path))
        monkeypatch.undo()

        assert wait_until_dead(wait_for(pid_file))


class TestTheTeardownSaysWhatItDid:
    def test_reports_a_group_that_ignored_sigterm(
        self, tmp_path, leaky_claude, monkeypatch, capsys
    ):
        # A server that ignores SIGTERM is a fact about the project worth
        # knowing, so the escalation is never silent.
        monkeypatch.setenv("FAKE_MODE", "stubborn-child")
        run_session(make_request(tmp_path))

        child = wait_for(leaky_claude)
        assert wait_until_dead(child)
        assert "needed SIGKILL" in capsys.readouterr().out

    def test_says_nothing_when_a_session_left_nothing_behind(
        self, tmp_path, monkeypatch, capsys
    ):
        bin_dir = tmp_path / "tidy-bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.write_text(
            f"#!{sys.executable}\nprint({RESULT_EVENT!r})\n", encoding="utf-8"
        )
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

        run_session(make_request(tmp_path))
        assert "process group" not in capsys.readouterr().out
