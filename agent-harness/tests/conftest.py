"""Makes the harness package importable, and holds the end-to-end rig.

The harness deliberately is not installed as a project dependency (it is a
developer tool that must run against repositories other than this one), so
there is no package metadata for pytest to resolve. Prepending the harness root
to sys.path is the smallest thing that works and keeps the tool self-contained.

The rest of this file is the rig the end-to-end tests share: a fake `claude`
CLI on PATH and a throwaway git repository for it to work in. The fake is what
lets the loop, the hook wiring, and the tools be exercised for real — actual
subprocesses, actual stdin events, actual commits — without an authenticated
CLI or a paid session.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

HARNESS_ROOT = Path(__file__).resolve().parents[1]

if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))


# ------ FAKE CLAUDE CLI ------

# The script body of the fake `claude` binary. Behavior is selected by the
# FAKE_CLAUDE_MODE environment variable, one mode per failure shape the loop
# has to survive. Every print is flushed because a timed-out session is killed
# outright, and unflushed output would vanish with it — which is precisely the
# case one of the modes exists to pin.
FAKE_CLAUDE_BODY = """
# Fake claude CLI for the harness end-to-end tests. See tests/conftest.py.
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

args = sys.argv[1:]

if args[:1] == ["--version"]:
    print("2.1.234 (Claude Code)")
    sys.exit(0)

if args[:1] == ["--help"]:
    print(
        "Usage: claude [options] [prompt]\\n"
        "  --print --output-format --verbose --effort --model\\n"
        "  --permission-mode --append-system-prompt --settings\\n"
        "  --session-id --max-budget-usd --add-dir"
    )
    sys.exit(0)

if args[:2] == ["auth", "status"]:
    print(json.dumps({"authMethod": "claude.ai", "subscriptionType": "max"}))
    sys.exit(0)


def flag_value(name, default=None):
    if name in args:
        return args[args.index(name) + 1]
    return default


session_id = flag_value("--session-id", "no-session-id")
mode = os.environ.get("FAKE_CLAUDE_MODE", "stall")
repo = Path.cwd()
workspace = repo / ".agent-harness"


def emit(event):
    print(json.dumps(event), flush=True)


def git(*argv):
    subprocess.run(["git", *argv], cwd=repo, capture_output=True, text=True)


emit({"type": "system", "subtype": "init", "session_id": session_id})
emit({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "fake session, mode " + mode}]}})

if mode == "timeout":
    time.sleep(3600)

if mode in ("init", "init-no-serve"):
    workspace.mkdir(exist_ok=True)
    features = {
        "version": 2,
        "features": [
            {"id": "feat-a", "category": "functional", "priority": 1,
             "description": "thing A works", "steps": ["run it", "see A"],
             "state": "no-test"},
            {"id": "feat-b", "category": "functional", "priority": 2,
             "description": "thing B works", "steps": ["run it", "see B"],
             "state": "no-test"},
        ],
    }
    (workspace / "feature_list.json").write_text(
        json.dumps(features, indent=2) + "\\n")
    (workspace / "progress.md").write_text("# Progress\\n\\n## init\\nSet up.\\n")
    init_script = workspace / "init.sh"
    init_script.write_text("#!/bin/sh\\necho ok\\n")
    init_script.chmod(0o755)
    if mode == "init":
        serve_script = workspace / "serve.sh"
        serve_script.write_text(
            "#!/bin/sh\\necho serving on http://localhost:9999\\n")
        serve_script.chmod(0o755)

if mode == "mark-commit":
    # Behave like a well-behaved session under the ordering rule: write the
    # test, record it FAILING, then write the implementation, then record it
    # passing. Two calls, in that order, exactly as coding.md now instructs.
    mark_command = shlex.split(os.environ["FAKE_MARK_COMMAND"])
    feature_list = json.loads((workspace / "feature_list.json").read_text())
    target = next(
        f["id"] for f in feature_list["features"] if f["state"] != "passing")

    # The "test": a script that only succeeds once the "implementation" exists.
    # Real enough to fail before the feature and pass after it, which is the
    # only property the harness is measuring here.
    (repo / "checks").mkdir(exist_ok=True)
    (repo / "impl").mkdir(exist_ok=True)
    test_path = "checks/" + target + ".sh"
    (repo / test_path).write_text(
        "#!/bin/sh\\ntest -f impl/" + target + ".txt\\n")
    check = "sh " + test_path

    red = subprocess.run(
        mark_command + [target, "test-failing", "--test", check,
                        "--test-path", test_path],
        cwd=repo, capture_output=True, text=True)

    # Only now does the implementation appear.
    (repo / "impl" / (target + ".txt")).write_text("built\\n")

    green = subprocess.run(
        mark_command + [target, "passing", "--test", check],
        cwd=repo, capture_output=True, text=True)

    emit({"type": "assistant", "message": {"content": [{"type": "text", "text":
        "red: " + red.stdout + red.stderr +
        "\\ngreen: " + green.stdout + green.stderr}]}})
    git("add", "-A")
    git("commit", "-m", "feat: " + target + " verified")

if mode == "shared-test-file":
    # Every session puts its test in the SAME file, which is what a real suite
    # looks like. Session two appending its own test changes the bytes session
    # one's evidence was digested against, and re-hashing every feature would
    # read that honest addition as tampering.
    mark_command = shlex.split(os.environ["FAKE_MARK_COMMAND"])
    feature_list = json.loads((workspace / "feature_list.json").read_text())
    target = next(
        f["id"] for f in feature_list["features"] if f["state"] != "passing")

    (repo / "checks").mkdir(exist_ok=True)
    (repo / "impl").mkdir(exist_ok=True)
    test_path = "checks/all.sh"
    shared_file = repo / test_path
    existing = shared_file.read_text() if shared_file.exists() else "#!/bin/sh\\n"
    shared_file.write_text(existing + "test -f impl/" + target + ".txt\\n")
    check = "sh " + test_path

    red = subprocess.run(
        mark_command + [target, "test-failing", "--test", check,
                        "--test-path", test_path],
        cwd=repo, capture_output=True, text=True)

    (repo / "impl" / (target + ".txt")).write_text("built\\n")

    green = subprocess.run(
        mark_command + [target, "passing", "--test", check],
        cwd=repo, capture_output=True, text=True)

    emit({"type": "assistant", "message": {"content": [{"type": "text", "text":
        "red: " + red.stdout + red.stderr +
        "\\ngreen: " + green.stdout + green.stderr}]}})
    git("add", "-A")
    git("commit", "-m", "feat: " + target + " verified")

if mode == "red-only":
    # A session interrupted after recording the red and before implementing.
    # It has done real work and must not read as stalled.
    mark_command = shlex.split(os.environ["FAKE_MARK_COMMAND"])
    feature_list = json.loads((workspace / "feature_list.json").read_text())
    target = next(
        f["id"] for f in feature_list["features"] if f["state"] != "passing")
    (repo / "checks").mkdir(exist_ok=True)
    test_path = "checks/" + target + ".sh"
    (repo / test_path).write_text(
        "#!/bin/sh\\ntest -f impl/" + target + ".txt\\n")
    completed = subprocess.run(
        mark_command + [target, "test-failing", "--test", "sh " + test_path,
                        "--test-path", test_path],
        cwd=repo, capture_output=True, text=True)
    emit({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "red: " + completed.stdout + completed.stderr}]}})
    # Deliberately no commit: the point is that a state change alone counts.

if mode == "flip-direct":
    # The declare-victory bypass: jump every feature straight to passing by
    # editing the file, with no evidence and without ever having been red.
    feature_list = json.loads((workspace / "feature_list.json").read_text())
    for feature in feature_list["features"]:
        feature["state"] = "passing"
    (workspace / "feature_list.json").write_text(
        json.dumps(feature_list, indent=2) + "\\n")

if mode == "forge-evidence":
    # The harder bypass: not a bare flip, but a well-formed record. Every field
    # the document comparison looks for is present and plausible, and the digest
    # is invented. Only re-hashing the named files against the repository can
    # tell the difference.
    feature_list = json.loads((workspace / "feature_list.json").read_text())
    for feature in feature_list["features"]:
        feature["state"] = "passing"
        feature["evidence"] = {
            "kind": "command",
            "detail": "sh checks/" + feature["id"] + ".sh",
            "test_paths": ["README.md"],
            "digest": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
            "observed_at": "2026-01-01T00:00:00+00:00",
        }
    (workspace / "feature_list.json").write_text(
        json.dumps(feature_list, indent=2))

if mode == "error-dirty":
    # Append to the progress log (as every session is told to), then die,
    # leaving the appended file uncommitted.
    with (workspace / "progress.md").open("a") as progress:
        progress.write("\\n## session\\nCrashed partway.\\n")

is_error = mode == "error-dirty"
emit({
    "type": "result",
    "subtype": "error_during_execution" if is_error else "success",
    "is_error": is_error,
    "session_id": session_id,
    "result": None if is_error else "did the thing",
    "num_turns": 3,
    "total_cost_usd": 1.25,
    "duration_ms": 1000,
    "permission_denials": [],
    "usage": {"output_tokens_details": {"thinking_tokens": 42}},
})
sys.exit(0)
"""


@pytest.fixture()
def fake_claude_env(tmp_path: Path) -> dict[str, str]:
    """An environment whose PATH resolves `claude` to the scripted fake.

    The shebang is the interpreter running these tests, by absolute path,
    because the fake must not depend on what `python3` on PATH happens to be.
    (A shebang cannot carry a quoted path, so a checkout under a directory
    with a space in its name would need a different launcher; the harness
    itself handles that case, this rig does not.)
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE_BODY}", encoding="utf-8")
    fake.chmod(0o755)

    # The exact command string sessions are given, so the fake exercises it
    # verbatim — including the quoting a spaced path would need.
    from agent_harness.loop import _mark_feature_command

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_MARK_COMMAND"] = _mark_feature_command()
    return env


# ------ THROWAWAY TARGET REPOSITORY ------


def git_in(repo: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *argv], capture_output=True, text=True, check=False
    )


@pytest.fixture()
def target_repo(tmp_path: Path) -> Path:
    """A minimal committed git repository for the harness to drive."""
    repo = tmp_path / "target"
    repo.mkdir()
    git_in(repo, "init", "-q", "-b", "main")
    git_in(repo, "config", "user.email", "harness-test@example.invalid")
    git_in(repo, "config", "user.name", "Harness Test")
    (repo / "README.md").write_text("a target repository\n", encoding="utf-8")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-q", "-m", "start")
    return repo


@pytest.fixture()
def spec_file(tmp_path: Path) -> Path:
    spec = tmp_path / "spec.md"
    spec.write_text("# Build a thing\n\nIt should do A and B.\n", encoding="utf-8")
    return spec


def run_harness(
    env: dict[str, str], *arguments: str, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    """Run harness.py as a real subprocess, the way a user does."""
    argv = [sys.executable, str(HARNESS_ROOT / "harness.py"), *arguments]
    return subprocess.run(
        argv, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )
