"""End-to-end tests for mark_feature.py, the only sanctioned write path.

Run as a real subprocess with a real workspace on disk, because this script is
the one thing standing between "the check passed" and "the model said so", and
its argument handling, check execution, and refusal behavior are the contract
the whole guard design leans on.

The ordering rule is what these tests are mostly about: a feature only reaches
`passing` if this script has already watched the same, unchanged test fail.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from agent_harness.features import digest_paths
from conftest import HARNESS_ROOT

MARK = HARNESS_ROOT / "tools" / "mark_feature.py"

# The check a fixture feature is verified by: a shell script that succeeds only
# once the "implementation" file exists. Small, real, and it genuinely fails
# before the feature is built, which is the property every test here needs.
CHECK_A = "sh checks/a.sh"
CHECK_B = "sh checks/b.sh"


def run_mark(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MARK), *arguments, "--repo", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture()
def workspace_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    workspace = repo / ".agent-harness"
    workspace.mkdir(parents=True)

    (repo / "checks").mkdir()
    (repo / "impl").mkdir()
    (repo / "checks" / "a.sh").write_text("#!/bin/sh\ntest -f impl/a.txt\n")
    (repo / "checks" / "b.sh").write_text("#!/bin/sh\ntest -f impl/b.txt\n")
    # feat-b is already passing, so its implementation is present.
    (repo / "impl" / "b.txt").write_text("built\n")

    document = {
        "version": 2,
        "features": [
            {
                "id": "feat-a",
                "category": "functional",
                "priority": 1,
                "description": "thing A works",
                "steps": ["run it"],
                "state": "no-test",
            },
            {
                "id": "feat-b",
                "category": "functional",
                "priority": 2,
                "description": "thing B works",
                "steps": ["run it"],
                "state": "passing",
                "evidence": {
                    "kind": "command",
                    "detail": CHECK_B,
                    "test_paths": ["checks/b.sh"],
                    "digest": digest_paths(["checks/b.sh"], repo),
                    "observed_at": "2026-08-18T09:00:00+00:00",
                },
            },
        ],
    }
    (workspace / "feature_list.json").write_text(json.dumps(document, indent=2))
    return repo


def read_feature(repo: Path, feature_id: str) -> dict[str, object]:
    document = json.loads(
        (repo / ".agent-harness" / "feature_list.json").read_text(encoding="utf-8")
    )
    return next(f for f in document["features"] if f["id"] == feature_id)


def record_red(repo: Path) -> subprocess.CompletedProcess[str]:
    """Put feat-a into test-failing the way a coding session would."""
    return run_mark(
        repo,
        "feat-a",
        "test-failing",
        "--test",
        CHECK_A,
        "--test-path",
        "checks/a.sh",
    )


def build_feature_a(repo: Path) -> None:
    (repo / "impl" / "a.txt").write_text("built\n")


class TestRecordingTheRed:
    def test_records_the_command_the_paths_and_the_digest(self, workspace_repo):
        completed = record_red(workspace_repo)
        assert completed.returncode == 0, completed.stderr

        feature = read_feature(workspace_repo, "feat-a")
        assert feature["state"] == "test-failing"
        evidence = feature["evidence"]
        assert evidence["kind"] == "command"
        assert evidence["detail"] == CHECK_A
        assert evidence["test_paths"] == ["checks/a.sh"]
        assert evidence["digest"] == digest_paths(["checks/a.sh"], workspace_repo)
        assert evidence["observed_at"]

    def test_is_refused_when_the_check_already_passes_and_says_why(
        self, workspace_repo
    ):
        # The implementation exists, so the "test" passes before the feature was
        # built — which means it is not testing the feature.
        build_feature_a(workspace_repo)
        completed = record_red(workspace_repo)

        assert completed.returncode == 1
        assert read_feature(workspace_repo, "feat-a")["state"] == "no-test"
        # The message has to explain the refusal, because it is what the model
        # reads and acts on; "refused" alone gets the same command retried.
        assert "exited 0" in completed.stderr
        assert "not testing the feature" in completed.stderr

    def test_needs_the_test_paths_to_hash(self, workspace_repo):
        completed = run_mark(
            workspace_repo, "feat-a", "test-failing", "--test", CHECK_A
        )
        assert completed.returncode == 1
        assert "--test-path" in completed.stderr
        assert read_feature(workspace_repo, "feat-a")["state"] == "no-test"

    def test_needs_a_check_to_run(self, workspace_repo):
        completed = run_mark(
            workspace_repo, "feat-a", "test-failing", "--test-path", "checks/a.sh"
        )
        assert completed.returncode == 1
        assert "--test" in completed.stderr

    def test_refuses_a_test_path_that_does_not_exist(self, workspace_repo):
        completed = run_mark(
            workspace_repo,
            "feat-a",
            "test-failing",
            "--test",
            CHECK_A,
            "--test-path",
            "checks/imaginary.sh",
        )
        assert completed.returncode == 1
        assert "not a file" in completed.stderr

    def test_refuses_a_test_path_outside_the_repository(self, workspace_repo):
        completed = run_mark(
            workspace_repo,
            "feat-a",
            "test-failing",
            "--test",
            CHECK_A,
            "--test-path",
            "../escape.sh",
        )
        assert completed.returncode == 1
        assert read_feature(workspace_repo, "feat-a")["state"] == "no-test"

    def test_records_a_symlink_under_the_file_it_points_at(self, workspace_repo):
        (workspace_repo / "checks" / "alias.sh").symlink_to(
            workspace_repo / "checks" / "a.sh"
        )
        completed = run_mark(
            workspace_repo,
            "feat-a",
            "test-failing",
            "--test",
            CHECK_A,
            "--test-path",
            "checks/alias.sh",
        )
        assert completed.returncode == 0, completed.stderr
        # What is recorded is what was actually hashed, so a reviewer sees the
        # real file rather than a name that can be repointed later.
        assert read_feature(workspace_repo, "feat-a")["evidence"]["test_paths"] == [
            "checks/a.sh"
        ]


class TestReachingPassing:
    def test_is_refused_straight_from_no_test(self, workspace_repo):
        build_feature_a(workspace_repo)
        completed = run_mark(workspace_repo, "feat-a", "passing", "--test", CHECK_A)

        assert completed.returncode == 1
        assert read_feature(workspace_repo, "feat-a")["state"] == "no-test"
        assert "not something the harness can watch happen" in completed.stderr
        # The refusal names where the session may go instead.
        assert "test-failing" in completed.stderr

    def test_succeeds_when_the_same_test_goes_green(self, workspace_repo):
        assert record_red(workspace_repo).returncode == 0
        build_feature_a(workspace_repo)
        completed = run_mark(workspace_repo, "feat-a", "passing", "--test", CHECK_A)

        assert completed.returncode == 0, completed.stderr
        feature = read_feature(workspace_repo, "feat-a")
        assert feature["state"] == "passing"
        assert feature["evidence"]["detail"] == CHECK_A
        assert feature["evidence"]["digest"] == digest_paths(
            ["checks/a.sh"], workspace_repo
        )

    def test_is_refused_when_the_named_command_is_not_the_recorded_one(
        self, workspace_repo
    ):
        assert record_red(workspace_repo).returncode == 0
        build_feature_a(workspace_repo)
        # A command that passes, but not the one whose failure was watched.
        completed = run_mark(
            workspace_repo, "feat-a", "passing", "--test", "test -f impl/a.txt"
        )
        assert completed.returncode == 1
        assert read_feature(workspace_repo, "feat-a")["state"] == "test-failing"
        assert "not the one the harness watched fail" in completed.stderr

    def test_is_refused_when_the_test_file_changed_since_the_red(self, workspace_repo):
        assert record_red(workspace_repo).returncode == 0
        # The dodge: observe red on a stub, then rewrite the test into something
        # that passes against nothing.
        (workspace_repo / "checks" / "a.sh").write_text("#!/bin/sh\nexit 0\n")
        completed = run_mark(workspace_repo, "feat-a", "passing", "--test", CHECK_A)

        assert completed.returncode == 1
        assert read_feature(workspace_repo, "feat-a")["state"] == "test-failing"
        assert "test files have changed" in completed.stderr
        # The message has to cover the other way this happens: the test file was
        # never committed and the tree was cleaned under an interrupted session.
        assert "never committed" in completed.stderr
        assert "Record 'test-failing' again" in completed.stderr

    def test_is_refused_when_the_check_still_fails(self, workspace_repo):
        assert record_red(workspace_repo).returncode == 0
        completed = run_mark(workspace_repo, "feat-a", "passing", "--test", CHECK_A)
        assert completed.returncode == 1
        assert read_feature(workspace_repo, "feat-a")["state"] == "test-failing"

    def test_refuses_a_fresh_set_of_test_paths(self, workspace_repo):
        # Choosing what to hash after the fact is choosing what to prove after
        # the fact.
        assert record_red(workspace_repo).returncode == 0
        build_feature_a(workspace_repo)
        completed = run_mark(
            workspace_repo,
            "feat-a",
            "passing",
            "--test",
            CHECK_A,
            "--test-path",
            "checks/b.sh",
        )
        assert completed.returncode == 1
        assert "--test-path is not accepted" in completed.stderr

    def test_does_not_run_the_check_when_the_digest_already_disagrees(
        self, workspace_repo
    ):
        # The two cheap comparisons come first, so the message the session has
        # to act on is not buried under a whole suite's output.
        assert record_red(workspace_repo).returncode == 0
        (workspace_repo / "checks" / "a.sh").write_text(
            "#!/bin/sh\ntouch ran.marker\nexit 0\n"
        )
        completed = run_mark(workspace_repo, "feat-a", "passing", "--test", CHECK_A)
        assert completed.returncode == 1
        assert not (workspace_repo / "ran.marker").exists()
        assert "running check" not in completed.stdout


class TestRegressions:
    def test_broken_re_runs_the_recorded_check_and_records_the_regression(
        self, workspace_repo
    ):
        (workspace_repo / "impl" / "b.txt").unlink()
        completed = run_mark(workspace_repo, "feat-b", "broken")

        assert completed.returncode == 0, completed.stderr
        feature = read_feature(workspace_repo, "feat-b")
        assert feature["state"] == "broken"
        # The evidence survives, because fixing a regression has to be measured
        # against the same test that was passing before it.
        assert feature["evidence"]["detail"] == CHECK_B
        assert feature["evidence"]["digest"] == digest_paths(
            ["checks/b.sh"], workspace_repo
        )

    def test_broken_is_refused_while_the_recorded_check_still_passes(
        self, workspace_repo
    ):
        completed = run_mark(workspace_repo, "feat-b", "broken")
        assert completed.returncode == 1
        assert read_feature(workspace_repo, "feat-b")["state"] == "passing"
        assert "has not regressed" in completed.stderr

    def test_broken_takes_no_check_of_its_own(self, workspace_repo):
        (workspace_repo / "impl" / "b.txt").unlink()
        completed = run_mark(workspace_repo, "feat-b", "broken", "--test", "false")
        assert completed.returncode == 1
        assert "--test is not accepted" in completed.stderr

    def test_a_regression_is_fixed_by_fixing_the_code_not_the_test(
        self, workspace_repo
    ):
        (workspace_repo / "impl" / "b.txt").unlink()
        assert run_mark(workspace_repo, "feat-b", "broken").returncode == 0

        # Rewriting the test to go green is refused; the digest still has to hold.
        (workspace_repo / "checks" / "b.sh").write_text("#!/bin/sh\nexit 0\n")
        refused = run_mark(workspace_repo, "feat-b", "passing", "--test", CHECK_B)
        assert refused.returncode == 1
        assert "test files have changed" in refused.stderr

        # Restoring the test and fixing the code is the route that works.
        (workspace_repo / "checks" / "b.sh").write_text(
            "#!/bin/sh\ntest -f impl/b.txt\n"
        )
        (workspace_repo / "impl" / "b.txt").write_text("rebuilt\n")
        fixed = run_mark(workspace_repo, "feat-b", "passing", "--test", CHECK_B)
        assert fixed.returncode == 0, fixed.stderr
        assert read_feature(workspace_repo, "feat-b")["state"] == "passing"

    def test_a_feature_that_never_passed_cannot_have_broken(self, workspace_repo):
        assert record_red(workspace_repo).returncode == 0
        completed = run_mark(workspace_repo, "feat-a", "broken")
        assert completed.returncode == 1
        assert read_feature(workspace_repo, "feat-a")["state"] == "test-failing"


class TestGivingUpAClaim:
    def test_no_test_from_passing_drops_the_evidence(self, workspace_repo):
        completed = run_mark(workspace_repo, "feat-b", "no-test")
        assert completed.returncode == 0, completed.stderr
        feature = read_feature(workspace_repo, "feat-b")
        assert feature["state"] == "no-test"
        assert "evidence" not in feature

    def test_no_test_from_test_failing_drops_the_evidence(self, workspace_repo):
        assert record_red(workspace_repo).returncode == 0
        completed = run_mark(workspace_repo, "feat-a", "no-test")
        assert completed.returncode == 0, completed.stderr
        assert "evidence" not in read_feature(workspace_repo, "feat-a")

    def test_no_test_runs_no_check_at_all(self, workspace_repo):
        completed = run_mark(workspace_repo, "feat-b", "no-test")
        assert "running check" not in completed.stdout

    def test_no_test_takes_no_arguments(self, workspace_repo):
        completed = run_mark(workspace_repo, "feat-b", "no-test", "--test", CHECK_B)
        assert completed.returncode == 1
        assert "--test is not accepted" in completed.stderr


class TestTheManualHatch:
    def test_records_a_manual_check_with_its_reason(self, workspace_repo):
        completed = run_mark(
            workspace_repo,
            "feat-a",
            "passing",
            "--test",
            "manual: requires a colour-calibrated monitor",
        )
        assert completed.returncode == 0, completed.stderr
        feature = read_feature(workspace_repo, "feat-a")
        assert feature["state"] == "passing"
        assert feature["evidence"]["kind"] == "manual"
        assert feature["evidence"]["detail"] == "requires a colour-calibrated monitor"
        # No digest to misread as something that was hashed and came out empty.
        assert "digest" not in feature["evidence"]

    def test_refuses_a_manual_claim_with_no_reason(self, workspace_repo):
        completed = run_mark(workspace_repo, "feat-a", "passing", "--test", "manual:")
        assert completed.returncode == 1
        assert read_feature(workspace_repo, "feat-a")["state"] == "no-test"

    def test_a_manual_claim_cannot_be_marked_broken(self, workspace_repo):
        # The recorded "check" is a sentence, and a sentence run through a
        # shell fails the way any prose fails. Without this refusal that
        # failure reads as a confirmed regression, so `broken` would mean
        # nothing at all on a manually verified feature.
        run_mark(
            workspace_repo,
            "feat-a",
            "passing",
            "--test",
            "manual: requires a colour-calibrated monitor",
        )

        completed = run_mark(workspace_repo, "feat-a", "broken")

        assert completed.returncode == 1
        assert "verified by hand" in completed.stderr
        assert read_feature(workspace_repo, "feat-a")["state"] == "passing"

    def test_cannot_be_used_to_record_a_failing_test(self, workspace_repo):
        completed = run_mark(
            workspace_repo,
            "feat-a",
            "test-failing",
            "--test",
            "manual: no test to watch",
            "--test-path",
            "checks/a.sh",
        )
        assert completed.returncode == 1
        assert "no test for the harness to watch fail" in completed.stderr


class TestUnknownIds:
    def test_a_typoed_id_fails_before_the_check_is_run(self, workspace_repo):
        # The check must not run for a feature that does not exist: a typo
        # would otherwise cost a full test-suite run and print "check passed"
        # right next to the error, inviting the model to read it as success.
        marker = workspace_repo / "check-ran.marker"
        completed = run_mark(
            workspace_repo,
            "no-such-feature",
            "test-failing",
            "--test",
            f"touch {marker}",
            "--test-path",
            "checks/a.sh",
        )
        assert completed.returncode == 1
        assert "No feature with id" in completed.stderr
        assert not marker.exists()
        assert "check passed" not in completed.stdout


class TestWorthlessEvidence:
    """Finding A: a check that cannot fail is not evidence, and is refused.

    Still a mitigation rather than the whole answer, but it is no longer the
    only defence: a no-op check now also has to have been watched failing, which
    by definition it cannot be.
    """

    @pytest.mark.parametrize("check", ["true", ":", "exit 0", "echo done", "printf hi"])
    def test_refuses_a_no_op_check_as_evidence(self, workspace_repo, check):
        completed = run_mark(
            workspace_repo,
            "feat-a",
            "test-failing",
            "--test",
            check,
            "--test-path",
            "checks/a.sh",
        )
        assert completed.returncode == 1
        assert read_feature(workspace_repo, "feat-a")["state"] == "no-test"
        assert "proves nothing" in completed.stderr

    def test_a_real_check_composed_with_a_no_op_is_still_allowed(self, workspace_repo):
        # `echo` chained to a command that can actually fail is a real check.
        check = "echo running && test -f impl/a.txt"
        completed = run_mark(
            workspace_repo,
            "feat-a",
            "test-failing",
            "--test",
            check,
            "--test-path",
            "checks/a.sh",
        )
        assert completed.returncode == 0, completed.stderr
        assert read_feature(workspace_repo, "feat-a")["state"] == "test-failing"


class TestTheLegacyBreak:
    def test_a_version_one_list_is_refused_rather_than_coerced(self, workspace_repo):
        path = workspace_repo / ".agent-harness" / "feature_list.json"
        document = json.loads(path.read_text())
        document["version"] = 1
        path.write_text(json.dumps(document, indent=2))

        completed = run_mark(workspace_repo, "feat-a", "no-test")
        assert completed.returncode == 1
        assert "init --force" in completed.stderr
