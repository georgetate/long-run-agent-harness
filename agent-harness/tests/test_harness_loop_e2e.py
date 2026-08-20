"""End-to-end tests for the loop: real harness runs against a fake claude CLI.

Everything here goes through `harness.py` as a subprocess, exactly the way a
user runs it — preflight, generated settings, sessions, commits, stop
conditions. The fake CLI (see conftest.py) plays one failure shape per mode,
so each stop condition the loop promises is demonstrated rather than assumed.

These exist because the loop shipped with zero automated coverage and three of
the bugs found by review lived exactly there: a declare-victory flip the
backstop missed, a timed-out session that left no artifacts, and early stops
that stranded the next run on a dirty tree.
"""

import json
import os
from pathlib import Path

from conftest import git_in, run_harness


def initialized_repo(env: dict[str, str], repo: Path, spec: Path) -> None:
    env = {**env, "FAKE_CLAUDE_MODE": "init"}
    completed = run_harness(env, "init", "--repo", str(repo), "--spec", str(spec))
    assert completed.returncode == 0, completed.stderr
    assert (repo / ".agent-harness" / "feature_list.json").is_file()


def read_features(repo: Path) -> list[dict[str, object]]:
    document = json.loads(
        (repo / ".agent-harness" / "feature_list.json").read_text(encoding="utf-8")
    )
    return document["features"]


def working_tree_is_clean(repo: Path) -> bool:
    return git_in(repo, "status", "--porcelain").stdout.strip() == ""


class TestHappyPath:
    def test_a_run_completes_when_sessions_verify_and_commit(
        self, fake_claude_env, target_repo, spec_file
    ):
        initialized_repo(fake_claude_env, target_repo, spec_file)
        env = {**fake_claude_env, "FAKE_CLAUDE_MODE": "mark-commit"}

        completed = run_harness(
            env, "run", "--repo", str(target_repo), "--sessions", "5"
        )

        assert completed.returncode == 0, completed.stderr
        assert "every feature in the list is marked passing" in completed.stdout
        features = read_features(target_repo)
        assert all(feature["state"] == "passing" for feature in features)
        # The evidence travels with the move: this is what separates a verified
        # list from a declared-victory one. It carries the digest of the test
        # files, which is what says the test that went green is the one the
        # harness watched go red.
        assert all("evidence" in feature for feature in features)
        assert all(feature["evidence"]["digest"] for feature in features)
        assert all(feature["evidence"]["test_paths"] for feature in features)
        assert working_tree_is_clean(target_repo)

    def test_sessions_are_numbered_across_the_run(
        self, fake_claude_env, target_repo, spec_file
    ):
        initialized_repo(fake_claude_env, target_repo, spec_file)
        env = {**fake_claude_env, "FAKE_CLAUDE_MODE": "mark-commit"}
        run_harness(env, "run", "--repo", str(target_repo), "--sessions", "5")

        logs = os.listdir(target_repo / ".agent-harness" / "local" / "logs")
        assert any(name.startswith("session-001-") for name in logs)
        assert any(name.startswith("session-002-") for name in logs)


class TestDeclareVictoryBypass:
    def test_a_direct_jump_to_passing_is_detected_and_reverted(
        self, fake_claude_env, target_repo, spec_file
    ):
        initialized_repo(fake_claude_env, target_repo, spec_file)
        env = {**fake_claude_env, "FAKE_CLAUDE_MODE": "flip-direct"}

        completed = run_harness(
            env, "run", "--repo", str(target_repo), "--sessions", "3"
        )

        assert "only a human may change it" in completed.stdout
        assert "no recorded evidence" in completed.stdout
        # The tampered list is not the run's ground truth: it is restored to
        # the pre-session state, not committed as if the work had happened.
        features = read_features(target_repo)
        assert all(feature["state"] == "no-test" for feature in features)
        assert working_tree_is_clean(target_repo)


class TestSessionFailures:
    def test_a_timed_out_session_leaves_its_artifacts_and_the_summary(
        self, fake_claude_env, target_repo, spec_file
    ):
        initialized_repo(fake_claude_env, target_repo, spec_file)
        env = {**fake_claude_env, "FAKE_CLAUDE_MODE": "timeout"}

        completed = run_harness(
            env,
            "run",
            "--repo",
            str(target_repo),
            "--sessions",
            "2",
            "--session-timeout",
            "2",
        )

        # The run ends with its summary, not a traceback out of the loop.
        assert "stopped because" in completed.stdout
        assert "could not finish" in completed.stdout

        # The hour-long hung session is exactly the one whose log matters, so
        # the partial stream and transcript must exist.
        logs_dir = target_repo / ".agent-harness" / "local" / "logs"
        streams = [name for name in os.listdir(logs_dir) if name.endswith(".jsonl")]
        session_streams = [name for name in streams if name.startswith("session-")]
        assert session_streams, f"no session stream in {sorted(os.listdir(logs_dir))}"
        stream_text = (logs_dir / session_streams[0]).read_text(encoding="utf-8")
        assert '"type"' in stream_text
        assert working_tree_is_clean(target_repo)

    def test_an_error_session_leaves_a_tree_the_next_run_accepts(
        self, fake_claude_env, target_repo, spec_file
    ):
        initialized_repo(fake_claude_env, target_repo, spec_file)
        env = {**fake_claude_env, "FAKE_CLAUDE_MODE": "error-dirty"}

        completed = run_harness(
            env, "run", "--repo", str(target_repo), "--sessions", "3"
        )
        assert "ended in an error" in completed.stdout
        assert working_tree_is_clean(target_repo)

        # The morning-after re-run must start, not refuse on the harness's own
        # uncommitted progress log.
        env["FAKE_CLAUDE_MODE"] = "stall"
        rerun = run_harness(env, "run", "--repo", str(target_repo), "--sessions", "1")
        assert "uncommitted changes" not in rerun.stderr
        assert rerun.returncode == 0, rerun.stderr


class TestStallDetection:
    def test_sessions_that_do_nothing_stop_the_loop(
        self, fake_claude_env, target_repo, spec_file
    ):
        initialized_repo(fake_claude_env, target_repo, spec_file)
        env = {**fake_claude_env, "FAKE_CLAUDE_MODE": "stall"}

        completed = run_harness(
            env, "run", "--repo", str(target_repo), "--sessions", "5"
        )

        assert completed.returncode == 0, completed.stderr
        assert "no commit and passed no feature" in completed.stdout
        assert working_tree_is_clean(target_repo)


class TestCommandQuoting:
    """A space in the harness's own path must not split the generated commands.

    The rendered mark command and the generated hook command are both parsed by
    a shell. This machine's paths have no spaces, so the round-trip is pinned
    with a deliberately hostile interpreter path instead.
    """

    def test_the_mark_command_survives_a_spaced_interpreter_path(self, monkeypatch):
        import shlex

        import agent_harness.loop as loop_module

        monkeypatch.setattr(loop_module.sys, "executable", "/tmp/my venv/bin/python3")
        parts = shlex.split(loop_module._mark_feature_command())
        assert parts[0] == "/tmp/my venv/bin/python3"
        assert parts[1].endswith("mark_feature.py")

    def test_the_hook_command_survives_a_spaced_interpreter_path(self, monkeypatch):
        import shlex

        import agent_harness.loop as loop_module

        monkeypatch.setattr(loop_module.sys, "executable", "/tmp/my venv/bin/python3")
        parts = shlex.split(loop_module._hook_command())
        assert parts[0] == "/tmp/my venv/bin/python3"
        assert parts[1].endswith("protect_feature_list.py")


class TestForgedEvidence:
    """A record that looks right on paper but is not true of the repository.

    The document comparison sees a complete, plausible evidence block and has no
    way to tell it from an honest one. Re-hashing the files it names is what
    closes that, and it is the reason the tamper check reads the working tree
    rather than only the two documents.
    """

    def test_a_fabricated_digest_is_detected_and_reverted(
        self, fake_claude_env, target_repo, spec_file
    ):
        initialized_repo(fake_claude_env, target_repo, spec_file)
        env = {**fake_claude_env, "FAKE_CLAUDE_MODE": "forge-evidence"}

        completed = run_harness(
            env, "run", "--repo", str(target_repo), "--sessions", "3"
        )

        assert "only a human may change it" in completed.stdout
        assert "no longer hash to what was recorded" in completed.stdout
        features = read_features(target_repo)
        assert all(feature["state"] == "no-test" for feature in features)
        assert working_tree_is_clean(target_repo)


class TestASharedTestFile:
    """Two features whose tests live in one file, which is every real suite.

    The digest is a hash of whole files, so session two appending its own test
    changes the bytes session one's evidence was recorded against. Re-hashing
    every feature after every session would call that tampering, restore the
    list, and stop the run — and stop it again on the next invocation, because
    nothing about the repository has changed. The run has to survive it.
    """

    def test_a_second_feature_sharing_the_file_does_not_stop_the_run(
        self, fake_claude_env, target_repo, spec_file
    ):
        initialized_repo(fake_claude_env, target_repo, spec_file)
        env = {**fake_claude_env, "FAKE_CLAUDE_MODE": "shared-test-file"}

        completed = run_harness(
            env, "run", "--repo", str(target_repo), "--sessions", "4"
        )

        assert completed.returncode == 0, completed.stderr
        assert "only a human may change it" not in completed.stdout
        assert "every feature in the list is marked passing" in completed.stdout
        assert all(
            feature["state"] == "passing" for feature in read_features(target_repo)
        )

    def test_status_still_reports_the_stale_digest_for_a_human(
        self, fake_claude_env, target_repo, spec_file
    ):
        # The other half of the trade. The loop stops halting on this, so the
        # mismatch has to remain visible somewhere, and `status` is where a
        # person can tell "the file grew a second test" from "the test was
        # rewritten to assert less".
        initialized_repo(fake_claude_env, target_repo, spec_file)
        env = {**fake_claude_env, "FAKE_CLAUDE_MODE": "shared-test-file"}
        run_harness(env, "run", "--repo", str(target_repo), "--sessions", "4")

        completed = run_harness(env, "status", "--repo", str(target_repo))

        assert "UNVERIFIABLE" in completed.stdout
        assert "no longer hash to what was recorded" in completed.stdout


class TestRecordingARedCountsAsProgress:
    def test_a_session_that_only_records_a_failing_test_is_not_stalled(
        self, fake_claude_env, target_repo, spec_file
    ):
        # The session writes the test, records it failing, and stops without
        # committing — the shape of a session that ran out of context between
        # the red and the commit. That is real work and the half that cannot be
        # faked, so it must not cost the run one of its two lives.
        initialized_repo(fake_claude_env, target_repo, spec_file)
        env = {**fake_claude_env, "FAKE_CLAUDE_MODE": "red-only"}

        completed = run_harness(
            env, "run", "--repo", str(target_repo), "--sessions", "2"
        )

        assert completed.returncode == 0, completed.stderr
        # If the red did not count, both sessions would have stalled and the run
        # would have stopped on the stall detector instead of the session limit.
        assert "made no commit and passed no feature" not in completed.stdout
        features = read_features(target_repo)
        assert features[0]["state"] == "test-failing"
        assert features[0]["evidence"]["digest"]


class TestStatusReportsTheStates:
    def test_status_breaks_the_list_down_by_state(
        self, fake_claude_env, target_repo, spec_file
    ):
        initialized_repo(fake_claude_env, target_repo, spec_file)
        env = {**fake_claude_env, "FAKE_CLAUDE_MODE": "red-only"}
        run_harness(env, "run", "--repo", str(target_repo), "--sessions", "1")

        completed = run_harness(env, "status", "--repo", str(target_repo))
        assert completed.returncode == 0, completed.stderr
        assert "states:" in completed.stdout
        assert "with a test written and failing" in completed.stdout


class TestServeScriptIsRequired:
    """`init.sh` is the way in for a build; `serve.sh` is the way in for a
    running instance, and a session that cannot reach one falls back to reading
    the source and reasoning about what is probably wrong.
    """

    def test_a_run_refuses_to_start_without_it(
        self, fake_claude_env, target_repo, spec_file
    ):
        initialized_repo(fake_claude_env, target_repo, spec_file)
        (target_repo / ".agent-harness" / "serve.sh").unlink()
        # Committed, so the work tree is clean and the run gets past preflight.
        # Without this the test passes on the dirty-tree refusal instead, which
        # mentions serve.sh for an entirely different reason.
        git_in(target_repo, "commit", "-am", "chore: drop serve.sh")

        completed = run_harness(
            {**fake_claude_env, "FAKE_CLAUDE_MODE": "stall"},
            "run",
            "--repo",
            str(target_repo),
            "--sessions",
            "1",
        )
        assert completed.returncode == 1
        assert "is missing serve.sh" in completed.stderr

    def test_init_refuses_when_the_initializer_did_not_leave_it(
        self, fake_claude_env, target_repo, spec_file
    ):
        # The initializer is a model, so "it said it was done" is not evidence.
        env = {**fake_claude_env, "FAKE_CLAUDE_MODE": "init-no-serve"}
        completed = run_harness(
            env, "init", "--repo", str(target_repo), "--spec", str(spec_file)
        )
        assert completed.returncode == 1
        assert "serve.sh" in completed.stderr
        assert "--force" in completed.stderr
