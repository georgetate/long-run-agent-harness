"""The single boundary between the harness and the claude CLI.

Everything that knows a session is a subprocess lives in this file. That is a
deliberate constraint rather than tidiness: the CLI is the one part of the
design that might be swapped for the Agent SDK later, and confining it to one
module with two pure functions and one impure one makes that swap a rewrite of
this file instead of a rewrite of the harness.

`build_argv` and `parse_session_result` are pure so the command line and the
result contract are both testable in CI, where no CLI is authenticated.

**Process groups.** Every subprocess the harness starts is put in a session of
its own and reaped as a group. A session that starts a dev server and dies
leaves that server holding a port, and the failure that produces is nasty and
misdirected: the *next* run's `serve.sh` fails to bind, for reasons that look
nothing like the real cause. Killing the group rather than the process is what
catches the children — the actual server behind a wrapper script, the database
container, the worker.

Ownership sits here, with the harness, rather than with the session. A session
told to clean up after itself will usually do it and will occasionally die
before it gets there, and "usually" is not a lifecycle.

**POSIX only.** `start_new_session` and process groups are POSIX concepts.
Windows needs `CREATE_NEW_PROCESS_GROUP` and a different signal, and none of
that is written. The harness targets macOS and Linux; the teardown degrades to
killing the single process where the primitives are missing rather than
pretending to be portable.
"""

import contextlib
import json
import os
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, cast

from agent_harness.config import (
    REQUIRED_RESULT_KEYS,
    HarnessError,
    SessionRequest,
    SessionResult,
)
from agent_harness.records import render_transcript

# How long a process group is given to exit on SIGTERM before it is killed.
# Short, because nothing the harness starts has cleanup worth waiting on, and a
# run that hangs here hangs unattended.
TERMINATION_GRACE_SECONDS = 5.0

# The primitives exist on POSIX and not on Windows. Checked once, here, so the
# rest of the file reads as if they are simply available.
_HAS_PROCESS_GROUPS = hasattr(os, "killpg") and hasattr(os, "getpgid")


def start_in_own_process_group(
    argv: list[str], cwd: Path, **popen_arguments: Any
) -> subprocess.Popen[str]:
    """Start a process as the leader of its own group, so it can be reaped whole.

    `start_new_session` makes the child a session and group leader, which means
    its pid is also its process-group id — and that matters, because once the
    child has been waited on its pid is gone and the group can no longer be
    looked up from it. Recording the leader's pid at start is the only reliable
    handle on the group.
    """
    return subprocess.Popen(
        argv,
        cwd=str(cwd),
        text=True,
        start_new_session=_HAS_PROCESS_GROUPS,
        **popen_arguments,
    )


def terminate_process_group(pgid: int, label: str) -> str | None:
    """SIGTERM a process group, wait out the grace period, then SIGKILL.

    Returns a line describing what happened, or None when nothing was left to
    kill. Never silent when it did something: a server that ignores SIGTERM is a
    fact about the project worth knowing, and a reaped orphan is a fact about
    the session that left it.
    """
    if not _HAS_PROCESS_GROUPS:  # pragma: no cover - POSIX only, see the docstring
        return None

    if not _group_is_alive(pgid):
        return None

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError, PermissionError:
        return None

    deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not _group_is_alive(pgid):
            return f"{label}: process group {pgid} stopped on SIGTERM"
        time.sleep(0.05)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError, PermissionError:  # pragma: no cover - lost the race
        return f"{label}: process group {pgid} stopped on SIGTERM"
    return f"{label}: process group {pgid} ignored SIGTERM and needed SIGKILL"


def _group_is_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - alive, just not ours to signal
        return True
    return True


def reap(pgid: int, label: str) -> None:
    """Terminate a process group and say so on stdout if anything was there."""
    outcome = terminate_process_group(pgid, label)
    if outcome is not None:
        print(f"harness: {outcome}")


class _OutputCollector:
    """Reads a process's pipes on threads, so waiting for it cannot deadlock.

    `communicate()` waits for end-of-file on the pipes rather than for the
    process, and a session that leaves a child running hands that child its
    stdout. The pipe then stays open after the session itself has exited, and
    the harness sits there until the session timeout for a session that finished
    in a minute. Reading on threads lets the main path wait for the *process*,
    reap the group — which closes the descriptors the orphan was holding — and
    only then collect what was read.
    """

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self._chunks: dict[str, str] = {"stdout": "", "stderr": ""}
        self._threads = [
            self._reader("stdout", process.stdout),
            self._reader("stderr", process.stderr),
        ]

    def _reader(self, name: str, pipe: Any) -> threading.Thread:
        def read() -> None:
            if pipe is None:  # pragma: no cover - both pipes are always PIPE here
                return
            # Suppressed: a pipe closed under the reader is the normal way
            # this ends once the group has been reaped, not a failure.
            with contextlib.suppress(ValueError, OSError):
                self._chunks[name] = pipe.read()

        # Daemon, so a reader still blocked on a pipe nothing will ever close
        # cannot keep the interpreter alive after an interrupt.
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        return thread

    def collect(self, timeout: float = 10.0) -> tuple[str, str]:
        for thread in self._threads:
            thread.join(timeout)
        return self._chunks["stdout"], self._chunks["stderr"]


def build_argv(request: SessionRequest) -> list[str]:
    """Turn a session request into the exact command line to run.

    The prompt goes last, as a positional argument. Passing it that way means a
    prompt that happens to start with a dash can never be read as a flag, which
    matters because prompts here are rendered from templates and are long.
    """
    argv = [
        "claude",
        "--print",
        # stream-json rather than json: the single result object is identical,
        # and everything before it is the only record of what the session
        # actually did. --verbose is not optional, the CLI refuses the
        # combination without it.
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        request.model,
        "--permission-mode",
        request.permission_mode,
        # A caller-assigned session id is what makes a run log actionable: every
        # session the harness starts can be resumed or read back later with
        # `claude --resume <id>` without having to hunt for it.
        "--session-id",
        request.session_id,
        # A hard per-session ceiling. The loop has its own stop conditions, but
        # this one is enforced by the CLI itself and survives a harness bug.
        "--max-budget-usd",
        str(request.budget_usd),
    ]

    # Omitted entirely when unset, so the CLI's own default effort applies
    # rather than a level chosen on the user's behalf.
    if request.effort is not None:
        argv += ["--effort", request.effort]

    if request.settings_path is not None:
        argv += ["--settings", str(request.settings_path)]

    if request.system_prompt_suffix is not None:
        argv += ["--append-system-prompt", request.system_prompt_suffix]

    for directory in request.extra_directories:
        argv += ["--add-dir", str(directory)]

    argv.append(request.prompt)
    return argv


def parse_session_result(stdout: str) -> SessionResult:
    """Read the CLI's JSON result object, or fail naming what was wrong with it.

    Every key required here feeds either a stop condition or the run log, so a
    missing one is an error rather than a default. Defaulting would produce a
    run that reports zero cost and zero turns and looks fine.
    """
    try:
        payload: Any = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise HarnessError(
            "The claude CLI did not return JSON. First 200 characters of what it "
            f"did return: {stdout[:200]!r}"
        ) from error

    if not isinstance(payload, dict):
        raise HarnessError(f"Expected a JSON object from the CLI, got {type(payload)}.")

    # Cast rather than lean on the narrowing: a checked `dict` from json is
    # `dict[Unknown, Unknown]`, and every read off it then reports as unknown.
    # The isinstance above is what makes this cast honest.
    result = cast(dict[str, Any], payload)

    missing = tuple(key for key in REQUIRED_RESULT_KEYS if key not in result)
    if missing:
        raise HarnessError(
            "The claude CLI result object is missing keys this harness reads: "
            f"{', '.join(missing)}. The CLI's output format has changed; update "
            "REQUIRED_RESULT_KEYS in config.py and this parser together."
        )

    if result["type"] != "result":
        raise HarnessError(
            f"Expected a result object from the CLI, got type={result['type']!r}."
        )

    return SessionResult(
        session_id=str(result["session_id"]),
        is_error=bool(result["is_error"]),
        subtype=str(result["subtype"]),
        text=str(result["result"] or ""),
        num_turns=int(result["num_turns"]),
        cost_usd=float(result["total_cost_usd"]),
        duration_ms=int(result["duration_ms"]),
        permission_denials=_read_denials(result.get("permission_denials")),
        thinking_tokens=_read_thinking_tokens(result.get("usage")),
    )


def _read_thinking_tokens(usage: object) -> int | None:
    """How many tokens the session spent thinking, if the CLI said.

    Read defensively and reported as None when absent. Reporting zero would
    claim a fact the CLI never stated.
    """
    if not isinstance(usage, dict):
        return None
    details = cast(dict[str, Any], usage).get("output_tokens_details")
    if not isinstance(details, dict):
        return None
    tokens = cast(dict[str, Any], details).get("thinking_tokens")
    if isinstance(tokens, int):
        return tokens
    return None


def parse_stream_result(stdout: str) -> SessionResult:
    """Pull the final result object out of a `--output-format stream-json` stream.

    Only the last complete `type: "result"` line is read. Everything before it
    is transcript, which is written to disk untouched rather than parsed: the
    harness depends on exactly one event shape, so a change to any of the others
    cannot break a run.

    A killed session can leave a half-written line behind, which is why lines
    that do not parse are skipped rather than treated as an error.
    """
    result_event: str | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event: Any = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if cast(dict[str, Any], event).get("type") == "result":
            result_event = stripped

    if result_event is None:
        raise HarnessError(
            "The session produced no result event. It was killed, or the CLI's "
            "stream format changed. The raw stream is in the session's .jsonl log."
        )
    return parse_session_result(result_event)


def _read_denials(raw: object) -> tuple[str, ...]:
    """Name the tools a hook or permission rule blocked during the session.

    Read defensively rather than strictly: denials are a diagnostic signal, and
    the harness should not fall over because the CLI enriched their shape.
    """
    if not isinstance(raw, list):
        return ()
    names: list[str] = []
    for entry in cast(list[Any], raw):
        if isinstance(entry, dict):
            denial = cast(dict[str, Any], entry)
            names.append(str(denial.get("tool_name", denial)))
        else:
            names.append(str(entry))
    return tuple(names)


def run_session(
    request: SessionRequest,
    stream_path: Path | None = None,
    transcript_path: Path | None = None,
) -> SessionResult:
    """Run one session to completion and return its parsed result.

    Both artifacts are written before the result is parsed. Losing the record of
    a failed hour-long session to a parse error would be the most annoying
    possible way for this tool to fail, and the sessions worth reading are
    exactly the ones that ended badly.

    `stream_path` gets every event verbatim, which is the forensic copy.
    `transcript_path` gets the readable rendering of the same thing.
    """
    argv = build_argv(request)
    try:
        process = start_in_own_process_group(
            argv,
            request.repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise HarnessError(f"Could not start the claude CLI: {error}") from error

    # Recorded now, while the leader is definitely alive. After the process has
    # been waited on its pid is gone, and with it any way to find the group its
    # children are still sitting in.
    pgid = process.pid
    label = f"session {request.session_id}"
    collector = _OutputCollector(process)
    timed_out = False

    try:
        try:
            process.wait(timeout=request.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        # A real finally, and that is the whole point. Teardown has to run on
        # success, on error, on timeout, and on KeyboardInterrupt — every one of
        # those is a normal way for a session to end here, and three of them are
        # the ones that leak. It is also what unblocks the collector, by closing
        # the descriptors an orphan was holding.
        reap(pgid, label)

    stdout, stderr = collector.collect()

    if timed_out:
        # The killed session is exactly the one whose log matters most, so its
        # partial output is written before the error is raised rather than lost
        # with it.
        _write_session_artifacts(
            argv,
            stdout,
            f"[killed after {request.timeout_seconds}s timeout]\n" + stderr,
            stream_path,
            transcript_path,
        )
        raise HarnessError(
            f"Session {request.session_id} exceeded its "
            f"{request.timeout_seconds}s timeout and was killed. Its partial "
            "stream was saved to its log. Raise --session-timeout, or narrow "
            "the work a single session is asked to do."
        )

    _write_session_artifacts(argv, stdout, stderr, stream_path, transcript_path)

    if not stdout.strip():
        raise HarnessError(
            f"The claude CLI exited {process.returncode} with no output. "
            f"stderr: {stderr.strip()[:500]}"
        )

    return parse_stream_result(stdout)


def _write_session_artifacts(
    argv: list[str],
    stdout: str,
    stderr: str,
    stream_path: Path | None,
    transcript_path: Path | None,
) -> None:
    """Write the forensic stream and the readable transcript for one session."""
    if stream_path is not None:
        stream_path.parent.mkdir(parents=True, exist_ok=True)
        stream_path.write_text(stdout, encoding="utf-8")

    if transcript_path is not None:
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(
            _build_transcript(argv, stdout, stderr), encoding="utf-8"
        )


def _build_transcript(argv: list[str], stdout: str, stderr: str) -> str:
    """Render the session for a human, with the command that produced it on top.

    The command line is included so the session can be reproduced by hand, which
    is the whole reason for driving the CLI rather than a library.
    """
    events: list[Any] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event: Any = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        events.append(event)

    parts = [
        "```",
        " ".join(shlex.quote(part) for part in argv[:-1]),
        "```",
        "",
        render_transcript(events),
    ]
    if stderr.strip():
        parts += ["", "## stderr", "", "```", stderr.strip()[:4000], "```", ""]
    return "\n".join(parts)
