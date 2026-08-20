"""The command line: `doctor`, `init`, `run`, `status`.

Four verbs, in the order you use them. `doctor` proves the environment is sane
before you have spent anything, `init` builds the workspace once, `run` is the
loop you leave going, and `status` answers "where did it get to" without
starting a session or costing a token.

Errors are printed as one line beginning `harness: error:` and exit non-zero.
The audience is a person scrolling back through a long log, or a cron job's
mail, so a stack trace would bury the sentence that matters.
"""

import argparse
import sys
from pathlib import Path

from agent_harness.config import (
    DEFAULT_EFFORT,
    DEFAULT_MAX_SESSIONS,
    DEFAULT_MODEL,
    DEFAULT_PERMISSION_MODE,
    DEFAULT_SESSION_BUDGET_USD,
    DEFAULT_SESSION_TIMEOUT_SECONDS,
    EFFORT_LEVELS,
    HarnessError,
)
from agent_harness.features import (
    FeatureState,
    find_unverifiable_evidence,
    next_feature,
    parse_feature_list,
    read_document,
    summarize,
)
from agent_harness.loop import (
    DEFAULT_VERIFICATION_GUIDANCE,
    RunSettings,
    initialize,
    run,
)
from agent_harness.preflight import format_version, run_preflight
from agent_harness.workspace import Workspace, missing_initializer_artifacts

PROGRESS_TAIL_LINES = 20


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness.py",
        description=(
            "Run long-lived Claude Code sessions against a target repository, one "
            "feature per session, until the feature list is done or a stop "
            "condition fires."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    doctor = subcommands.add_parser(
        "doctor", help="Check the CLI, git, and the target repository, then exit"
    )
    _add_repo_argument(doctor)
    doctor.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Do not require the target repository to have a clean work tree",
    )

    initializer = subcommands.add_parser(
        "init", help="Run the one session that builds the harness workspace"
    )
    _add_repo_argument(initializer)
    _add_session_arguments(initializer)
    initializer.add_argument(
        "--spec",
        type=Path,
        required=True,
        help="The specification to build from. Copied into the repository as the "
        "run's source of truth.",
    )
    initializer.add_argument(
        "--force",
        action="store_true",
        help="Re-initialize even though a feature list already exists, replacing "
        "the definition of done for work already in progress",
    )

    runner = subcommands.add_parser("run", help="Run coding sessions until a stop")
    _add_repo_argument(runner)
    _add_session_arguments(runner)
    runner.add_argument(
        "--sessions",
        type=int,
        default=DEFAULT_MAX_SESSIONS,
        help=f"Maximum sessions to run (default: {DEFAULT_MAX_SESSIONS})",
    )
    runner.add_argument(
        "--total-budget-usd",
        type=float,
        default=None,
        help="Stop once this much has been spent across the whole run",
    )
    status = subcommands.add_parser(
        "status", help="Print where a repository got to, without running anything"
    )
    _add_repo_argument(status)

    return parser


def _add_repo_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="The repository to work on (default: current directory)",
    )


def _add_session_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"(default: {DEFAULT_MODEL})"
    )
    parser.add_argument(
        "--effort",
        choices=EFFORT_LEVELS,
        default=DEFAULT_EFFORT,
        help=f"Reasoning effort for the session (default: {DEFAULT_EFFORT}). "
        "Pinned rather than inherited so that two unattended runs of the same "
        "feature list behave the same on any machine.",
    )
    parser.add_argument(
        "--permission-mode",
        default=DEFAULT_PERMISSION_MODE,
        help=f"(default: {DEFAULT_PERMISSION_MODE}, which is what unattended runs need)",
    )
    parser.add_argument(
        "--session-budget-usd",
        type=float,
        default=DEFAULT_SESSION_BUDGET_USD,
        help=f"Hard per-session ceiling, enforced by the CLI itself "
        f"(default: {DEFAULT_SESSION_BUDGET_USD})",
    )
    parser.add_argument(
        "--session-timeout",
        type=int,
        default=DEFAULT_SESSION_TIMEOUT_SECONDS,
        help=f"Wall-clock seconds before a session is killed "
        f"(default: {DEFAULT_SESSION_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--verification-notes",
        type=Path,
        default=None,
        help="A file describing how this project is verified end to end. Replaces "
        "the generic guidance in the prompts.",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Do not require the target repository to have a clean work tree",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the command and the fully rendered prompt, and run nothing",
    )


def _settings_from(arguments: argparse.Namespace) -> RunSettings:
    guidance = DEFAULT_VERIFICATION_GUIDANCE
    if arguments.verification_notes is not None:
        if not arguments.verification_notes.is_file():
            raise HarnessError(
                f"No verification notes at {arguments.verification_notes}."
            )
        guidance = arguments.verification_notes.read_text(encoding="utf-8").strip()

    return RunSettings(
        repo_path=arguments.repo,
        model=arguments.model,
        effort=arguments.effort,
        permission_mode=arguments.permission_mode,
        session_budget_usd=arguments.session_budget_usd,
        total_budget_usd=getattr(arguments, "total_budget_usd", None),
        session_timeout_seconds=arguments.session_timeout,
        max_sessions=getattr(arguments, "sessions", DEFAULT_MAX_SESSIONS),
        require_clean_tree=not arguments.allow_dirty,
        verification_guidance=guidance,
        dry_run=arguments.dry_run,
    )


def _run_doctor(arguments: argparse.Namespace) -> int:
    report = run_preflight(arguments.repo, require_clean_tree=not arguments.allow_dirty)
    print(f"harness: claude CLI {format_version(report.cli_version)}")
    print(f"harness: repository {report.repo_root}")
    print(f"harness: auth, {report.authentication}")
    for warning in report.warnings:
        print(f"warning: {warning}")

    workspace = Workspace(repo_root=report.repo_root)
    missing = missing_initializer_artifacts(workspace)
    if missing:
        print(f"harness: no workspace yet (missing {', '.join(missing)}). Run init.")
    else:
        print(f"harness: workspace present at {workspace.root}")
    print("harness: ready")
    return 0


def _run_status(arguments: argparse.Namespace) -> int:
    report = run_preflight(arguments.repo, require_clean_tree=False)
    workspace = Workspace(repo_root=report.repo_root)

    missing = missing_initializer_artifacts(workspace)
    if missing:
        raise HarnessError(
            f"No harness workspace in {workspace.repo_root} (missing "
            f"{', '.join(missing)}). Run `harness.py init --spec <file>` first."
        )

    features = parse_feature_list(read_document(workspace.feature_list_path))
    summary = summarize(features)
    print(f"harness: {workspace.repo_root}")
    print(
        f"features: {summary.passing}/{summary.total} passing, {summary.failing} to go"
    )
    print(
        f"states: {summary.passing} passing, {summary.test_failing} with a test "
        f"written and failing, {summary.no_test} not started, "
        f"{summary.broken} broken"
    )
    unverified = summary.passing - summary.automated - summary.manual
    print(
        f"evidence: {summary.automated} backed by a re-runnable command, "
        f"{summary.manual} verified by hand, {unverified} passing with nothing recorded"
    )

    # A regression is the one state a human should act on immediately, so it is
    # said again, on its own line, naming the features rather than counting them.
    # Buried in a tally it reads as one number among four.
    regressed = [
        feature for feature in features if feature.state is FeatureState.BROKEN
    ]
    if regressed:
        print(
            f"\nBROKEN: {len(regressed)} feature(s) that used to pass no longer do. "
            "The next session will work these before anything else."
        )
        for feature in regressed:
            print(
                f"  {feature.id} (priority {feature.priority}) — {feature.description}"
            )

    # Cheap, and it is the difference between "the list says these pass" and
    # "these still hash to the test the harness watched run". Every feature is
    # checked here, unlike in the loop, because this output is read by a human:
    # a test file that grew a second feature's test is an explicable mismatch,
    # and a test quietly rewritten to assert less is not, and only a person can
    # tell those apart.
    unverifiable = find_unverifiable_evidence(features, workspace.repo_root)
    if unverifiable:
        print(
            f"\nUNVERIFIABLE: {len(unverifiable)} recorded claim(s) no longer match "
            "the repository. Re-record the ones whose test genuinely changed; "
            "read the rest."
        )
        for reason in unverifiable:
            print(f"  {reason}")

    upcoming = next_feature(features)
    if upcoming is None:
        print("next: nothing, every feature is marked passing")
    else:
        print(
            f"next: {upcoming.id} (priority {upcoming.priority}) — {upcoming.description}"
        )

    progress = workspace.progress_path.read_text(encoding="utf-8").splitlines()
    if progress:
        print(f"\nlast {PROGRESS_TAIL_LINES} lines of the progress log:")
        for line in progress[-PROGRESS_TAIL_LINES:]:
            print(f"  {line}")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)

    try:
        if arguments.command == "doctor":
            return _run_doctor(arguments)
        if arguments.command == "status":
            return _run_status(arguments)
        if arguments.command == "init":
            initialize(_settings_from(arguments), arguments.spec, force=arguments.force)
            return 0
        if arguments.command == "run":
            run(_settings_from(arguments))
            return 0
    except HarnessError as error:
        print(f"harness: error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Interrupting a run is a normal way to end one, not a crash. The work
        # so far is already committed by the session that did it.
        print("\nharness: interrupted", file=sys.stderr)
        return 130

    raise AssertionError(f"unhandled command {arguments.command!r}")
