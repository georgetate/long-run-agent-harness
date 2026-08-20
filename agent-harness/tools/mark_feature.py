#!/usr/bin/env python3
"""The only sanctioned way for a session to change the feature list.

Direct edits to the list are blocked, so this script is the whole write path.
That is the point: it can move exactly one feature along exactly one edge of the
state machine and record what the harness saw while doing it, and it cannot do
anything else, which is what makes the block on direct edits enforceable rather
than merely rude.

The ordering is the mechanism. A feature cannot be recorded `passing` unless
this script has already watched the same, unchanged test **fail** before the
implementation existed. Both observations are made here, at the moment the claim
is made, rather than believed and filed — because a session's own account of
having tested something is exactly the claim that cannot be trusted after fifty
sessions.

Run by the agent from inside the target repository, by absolute path, as named
in the coding prompt.
"""

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# See the note in protect_feature_list.py: the harness is not installed, so the
# path bootstrap is what makes it importable from another repository.
HARNESS_ROOT = Path(__file__).resolve().parents[1]
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

from agent_harness.config import HarnessError  # noqa: E402 - follows path bootstrap
from agent_harness.features import (  # noqa: E402 - follows path bootstrap
    Evidence,
    FeatureState,
    TransitionRequirement,
    digest_paths,
    is_manual_exception,
    legal_targets_from,
    parse_feature_list,
    parse_verification,
    read_document,
    set_feature_state,
    summarize,
    transition_requirement,
    write_document,
)
from agent_harness.workspace import find_workspace  # noqa: E402 - follows bootstrap

# Long enough for a real suite, short enough that a hung check does not eat the
# session's wall clock.
DEFAULT_TEST_TIMEOUT_SECONDS = 300


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Move a feature to a new state in the harness feature list, after "
            "the harness has watched its check behave the way that state means."
        )
    )
    parser.add_argument(
        "feature_id", help="The 'id' of the feature, exactly as written"
    )
    parser.add_argument(
        "state",
        choices=[state.value for state in FeatureState],
        help=(
            "no-test: nothing written yet. "
            "test-failing: the test exists and this command must watch it FAIL "
            "(--test and --test-path required). "
            "passing: the same unchanged test must now PASS (--test required, "
            "and it must be the command recorded when the test was seen failing). "
            "broken: re-runs the recorded check and requires it to fail now."
        ),
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="Any path inside the target repository (default: current directory)",
    )
    parser.add_argument(
        "--test",
        default="",
        help=(
            "The command that reproduces the check. Required for test-failing "
            'and passing. Use "manual: <reason>" only when the check genuinely '
            "cannot be automated; that is the one route to passing that does not "
            "go through a recorded failure."
        ),
    )
    parser.add_argument(
        "--test-path",
        action="append",
        default=[],
        dest="test_paths",
        metavar="PATH",
        help=(
            "A file holding the test, relative to the repository root. Repeat "
            "for several. Required for test-failing: these files are hashed, and "
            "the hash has to still match when the feature is marked passing, so "
            "that the test that went green is the same one that went red."
        ),
    )
    parser.add_argument(
        "--test-timeout",
        type=int,
        default=DEFAULT_TEST_TIMEOUT_SECONDS,
        help=f"Seconds to allow the check (default: {DEFAULT_TEST_TIMEOUT_SECONDS})",
    )
    return parser


# ------ RUNNING THE CHECK ------


def run_check(
    command: str,
    repo_root: Path,
    timeout_seconds: int,
    *,
    expected: TransitionRequirement,
    target: FeatureState,
) -> None:
    """Run the named check and raise unless it behaved the way `target` claims.

    Run through a shell rather than parsed into argv, because what a project
    needs to run its own tests is a project-specific incantation with pipes and
    environment in it, and second-guessing that is not this script's job.

    A check that fails when a failure was expected is not an error here: it is
    the observation being recorded. The refusals below are the two cases where
    what happened contradicts what is about to be written down.
    """
    print(f"running check: {command}")
    try:
        completed = subprocess.run(
            command,
            cwd=str(repo_root),
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise HarnessError(
            f"The check did not finish within {timeout_seconds}s. A verification "
            "that cannot complete is not one the next session can rely on."
        ) from error

    output = (completed.stdout + completed.stderr).strip()
    tail = f"\n--- last 2000 characters of its output ---\n{output[-2000:]}"

    if expected is TransitionRequirement.CHECK_MUST_PASS:
        if completed.returncode != 0:
            raise HarnessError(
                f"The check exited {completed.returncode}, so nothing was "
                "recorded. Fix the feature until this command passes, then mark "
                "it. Do not change the test to make it pass: the recorded hash "
                "of the test files has to still match." + tail
            )
        print(f"check passed, as {target.value} requires: {command}")
        return

    if completed.returncode == 0:
        raise HarnessError(_unexpected_pass_message(target, command) + tail)
    print(f"check failed, as {target.value} requires: {command}")


def _unexpected_pass_message(target: FeatureState, command: str) -> str:
    """Why a passing check is a refusal here, in the terms the model needs.

    This message is the one a session reads and then acts on, so it has to say
    what to do next rather than only what went wrong. A model told "the check
    passed, refused" with no reason will usually try the same command again.
    """
    if target is FeatureState.BROKEN:
        return (
            f"The recorded check still passes, so nothing was recorded. "
            f"{command!r} exited 0, which means this feature has not regressed. "
            "'broken' is for a feature that used to work and now does not; if "
            "you believe it is wrong in a way this check does not catch, the "
            "honest route is a better test, recorded through test-failing."
        )
    return (
        f"The check passed, so nothing was recorded. {command!r} exited 0 "
        "before the feature was implemented, which means it is not testing the "
        "feature — it is testing something that was already true. Recording "
        "'test-failing' is the harness watching your test fail for the right "
        "reason: the behaviour is missing. Write a test that fails because the "
        "feature does not exist yet, then record it, then build the feature."
    )


# ------ WHAT EACH TARGET STATE REQUIRES ON THE COMMAND LINE ------


def validate_arguments(
    target: FeatureState, test: str, test_paths: list[str], *, manual: bool
) -> None:
    if target is FeatureState.NO_TEST:
        _refuse_extra(target, test, test_paths)
        return

    if target is FeatureState.BROKEN:
        _refuse_extra(
            target,
            test,
            test_paths,
            because="the check is the one already recorded against the feature, "
            "and re-running exactly that command is what makes this a regression "
            "rather than a new claim",
        )
        return

    if not test:
        raise HarnessError(
            f"Recording {target.value!r} needs --test: the command that "
            "reproduces the check."
        )

    if target is FeatureState.TEST_FAILING:
        if manual:
            raise HarnessError(
                "A manual check cannot be recorded as test-failing: there is no "
                "test for the harness to watch fail. A manual reason is only "
                "accepted as \"--test 'manual: <reason>'\" when marking a "
                "feature passing directly, which is the one route that skips "
                "the recorded failure."
            )
        if not test_paths:
            raise HarnessError(
                "Recording 'test-failing' needs --test-path: the file or files "
                "holding the test. They are hashed now, and the hash has to "
                "still match when the feature is marked passing — that is what "
                "forces the test that goes green to be the same one that went "
                "red."
            )
        return

    # target is PASSING
    if test_paths and not manual:
        raise HarnessError(
            "--test-path is not accepted when marking a feature passing. The "
            "files to hash are the ones recorded when the test was seen "
            "failing; naming a different set here would be choosing what to "
            "prove after the fact."
        )
    if test_paths and manual:
        raise HarnessError("--test-path is not accepted with a manual check.")


def _refuse_extra(
    target: FeatureState, test: str, test_paths: list[str], *, because: str = ""
) -> None:
    trailer = f", because {because}" if because else ""
    if test:
        raise HarnessError(f"--test is not accepted for {target.value!r}{trailer}.")
    if test_paths:
        raise HarnessError(
            f"--test-path is not accepted for {target.value!r}{trailer}."
        )


# ------ REACHING `passing`: THE TWO CHEAP CHECKS THAT COME FIRST ------


def evidence_for_passing(
    recorded: Evidence | None, offered_command: str, repo_root: Path
) -> Evidence:
    """Confirm the green is being claimed over the same check and the same test.

    Both comparisons happen before the check is run. They are cheap, and they
    are the ones whose failure the session has to act on — running a suite first
    only buries the message that matters under its output.
    """
    if recorded is None:  # pragma: no cover - the state machine reaches here first
        raise HarnessError(
            "This feature carries no recorded evidence, so there is no failing "
            "run to compare against. Record 'test-failing' first."
        )

    if offered_command != recorded.detail:
        raise HarnessError(
            "The check named here is not the one the harness watched fail.\n"
            f"  recorded: {recorded.detail}\n"
            f"  given:    {offered_command}\n"
            "The whole point of recording the red is that the same command over "
            "the same test is then seen to go green; a different command proves "
            "something else. Use the recorded command, or record 'test-failing' "
            "again if the check genuinely has to change."
        )

    current = digest_paths(recorded.test_paths, repo_root)
    if current != recorded.digest:
        raise HarnessError(
            "The test files have changed since the failing run was recorded.\n"
            f"  files:    {', '.join(recorded.test_paths)}\n"
            f"  recorded: {recorded.digest}\n"
            f"  now:      {current}\n"
            "Either the test was edited after the harness watched it fail, or "
            "it was never committed and the working tree has since been "
            "cleaned. Either way the recorded failure no longer refers to the "
            "test that exists now, so it cannot back a claim that this test "
            "went green. Record 'test-failing' again with the test as it stands "
            "— the harness will require it to fail again — and then mark it "
            "passing."
        )

    return Evidence(
        kind=recorded.kind,
        detail=recorded.detail,
        test_paths=recorded.test_paths,
        digest=recorded.digest,
        observed_at=_now(),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ------ THE FLOW ------


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    target = FeatureState(arguments.state)

    try:
        workspace = find_workspace(arguments.repo)
        document = read_document(workspace.feature_list_path)

        # The id is validated before anything else runs, so a typo fails
        # immediately instead of after a full suite run whose output would sit
        # next to the error inviting the wrong conclusion.
        feature = _find_feature(document, arguments.feature_id)
        current = feature.state

        offered = arguments.test.strip()
        manual = bool(offered) and offered.lower().startswith("manual:")

        requirement = transition_requirement(current, target)
        if requirement is None and not is_manual_exception(
            current, target, parse_verification(offered) if manual else None
        ):
            legal = ", ".join(state.value for state in legal_targets_from(current))
            raise HarnessError(
                f"{arguments.feature_id} is {current.value!r}, and "
                f"{current.value!r} -> {target.value!r} is not something the "
                f"harness can watch happen. From here the legal targets are: "
                f"{legal}."
                + (
                    "\nReaching 'passing' means recording 'test-failing' first, "
                    "with the test written and the implementation not."
                    if target is FeatureState.PASSING
                    else ""
                )
            )

        validate_arguments(target, offered, arguments.test_paths, manual=manual)

        evidence = _observe(
            target=target,
            requirement=requirement,
            manual=manual,
            offered=offered,
            test_paths=arguments.test_paths,
            recorded=feature.evidence,
            repo_root=workspace.repo_root,
            timeout=arguments.test_timeout,
        )

        updated = set_feature_state(
            document, arguments.feature_id, state=target, evidence=evidence
        )
        write_document(workspace.feature_list_path, updated)
        summary = summarize(parse_feature_list(updated))
    except HarnessError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(
        f"{arguments.feature_id} is now {target.value}. "
        f"{summary.passing}/{summary.total} features passing "
        f"({summary.automated} backed by a command, {summary.manual} manual); "
        f"{summary.test_failing} with a failing test, {summary.no_test} not "
        f"started, {summary.broken} broken."
    )
    return 0


def _find_feature(document: dict[str, object], feature_id: str):
    for feature in parse_feature_list(document):
        if feature.id == feature_id:
            return feature
    raise HarnessError(
        f"No feature with id {feature_id!r} in the list. Ids are fixed when the "
        "list is written; check the exact id rather than inventing one."
    )


def _observe(
    *,
    target: FeatureState,
    requirement: TransitionRequirement | None,
    manual: bool,
    offered: str,
    test_paths: list[str],
    recorded: Evidence | None,
    repo_root: Path,
    timeout: int,
) -> Evidence | None:
    """Run whatever the target state claims, and return the evidence to record."""
    if target is FeatureState.NO_TEST:
        return None

    if manual:
        # The one route to `passing` that never sees a red, because there is no
        # test to watch. It records a reason and no digest, and `status` counts
        # it apart so a project where everything is manual cannot be mistaken
        # for one where everything was checked.
        claimed = parse_verification(offered)
        return Evidence(kind="manual", detail=claimed.detail, observed_at=_now())

    if target is FeatureState.PASSING:
        evidence = evidence_for_passing(recorded, offered, repo_root)
        run_check(
            evidence.detail,
            repo_root,
            timeout,
            expected=TransitionRequirement.CHECK_MUST_PASS,
            target=target,
        )
        return evidence

    if target is FeatureState.BROKEN:
        if recorded is None:  # pragma: no cover - the table reaches here first
            raise HarnessError("This feature carries no recorded check to re-run.")
        if recorded.kind != "command":
            # Otherwise the recorded "check" is a sentence explaining why no
            # command could be written, and running it through a shell fails
            # for the reason any prose fails — which the caller would then
            # read as a confirmed regression.
            raise HarnessError(
                "This feature was verified by hand, so there is no command to "
                "re-run and nothing for the harness to watch fail. Its recorded "
                f"reason was: {recorded.detail!r}. If it no longer works, move "
                "it to 'no-test', which drops the claim and needs no proof, and "
                "then either record it through 'test-failing' with a real test "
                "or verify it by hand again."
            )
        run_check(
            recorded.detail,
            repo_root,
            timeout,
            expected=TransitionRequirement.CHECK_MUST_FAIL,
            target=target,
        )
        # The recorded paths and digest are kept as they were. A regression is
        # the code moving away from a fixed test, so `broken` -> `passing` has
        # to be measured against the same test that was passing before.
        return Evidence(
            kind=recorded.kind,
            detail=recorded.detail,
            test_paths=recorded.test_paths,
            digest=recorded.digest,
            observed_at=_now(),
        )

    # target is TEST_FAILING
    claimed = parse_verification(offered)
    # Hashed before the check runs, so the digest is of the test as it stood
    # when it was watched failing rather than whatever the run left behind.
    digest = digest_paths(test_paths, repo_root)
    run_check(
        claimed.detail,
        repo_root,
        timeout,
        expected=TransitionRequirement.CHECK_MUST_FAIL,
        target=target,
    )
    return Evidence(
        kind="command",
        detail=claimed.detail,
        test_paths=_canonical_paths(test_paths, repo_root),
        digest=digest,
        observed_at=_now(),
    )


def _canonical_paths(test_paths: list[str], repo_root: Path) -> tuple[str, ...]:
    """The recorded paths, resolved and repo-relative.

    Recording what was actually hashed rather than what was typed means a
    symlink shows up in the evidence as the file it points at, which is what a
    reviewer needs to see.
    """
    root = repo_root.resolve()
    resolved = {
        (root / path).resolve().relative_to(root).as_posix() for path in test_paths
    }
    return tuple(sorted(resolved))


if __name__ == "__main__":
    raise SystemExit(main())
