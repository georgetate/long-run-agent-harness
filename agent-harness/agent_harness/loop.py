"""The orchestration: one initializer session, then coding sessions until a stop.

This is the part the article is really about. The two failure modes it names,
trying to one-shot the whole project and declaring victory early, are both
failures of the loop rather than of the model, and both are answered here by
refusing to let a session decide when the run ends.

Every stop condition is explicit and every one of them prints why. A long run
that ends quietly is indistinguishable from a long run that succeeded, and the
difference matters most at the moment you are least able to check: the morning
after.
"""

import json
import shlex
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agent_harness import prompts
from agent_harness.config import (
    DEFAULT_EFFORT,
    DEFAULT_MAX_SESSIONS,
    DEFAULT_MODEL,
    DEFAULT_PERMISSION_MODE,
    DEFAULT_SESSION_BUDGET_USD,
    DEFAULT_SESSION_TIMEOUT_SECONDS,
    MAX_CONSECUTIVE_STALLED_SESSIONS,
    HarnessError,
    SessionRequest,
    SessionResult,
)
from agent_harness.features import (
    Feature,
    find_structural_changes,
    find_unverifiable_evidence,
    next_feature,
    parse_feature_list,
    read_document,
    summarize,
    write_document,
)
from agent_harness.preflight import find_uncommitted_changes, run_preflight
from agent_harness.records import next_session_number
from agent_harness.session import build_argv, run_session
from agent_harness.workspace import (
    Workspace,
    missing_initializer_artifacts,
    prepare_local_directories,
)

# The default filling for the {{VERIFICATION_GUIDANCE}} slot. The article's
# finding was that Claude marks features done without end-to-end testing unless
# explicitly told to test as a user, and that giving it browser automation
# transformed the results. Browser automation is specific to web apps, so what
# generalises is the demand rather than the tool: exercise the real thing, by
# the route a user takes. A project with a better answer supplies its own with
# --verification-notes.
COMPLETE_STOP_REASON = "every feature in the list is marked passing"

DEFAULT_VERIFICATION_GUIDANCE = """Verify by exercising the running system the way a user would reach it, not by inspecting the code that implements it. Start it with the init script and drive it through its real interface: the browser for a web app, the actual command for a CLI, a real input file for a pipeline, an HTTP request for a service. If browser automation is available to you, use it and look at what renders rather than at what the response body contains.

The test you record is the durable half of this and it is real evidence — it is what tells the next session, thirty sessions from now, that the feature still works. What it cannot do on its own is tell you the feature works *now*, because a test exercises the units you wrote against the assumptions you had while writing them, which is exactly the pair that fails together. So do both: record the test, and separately drive the running system by hand and look at what it actually does."""


@dataclass(frozen=True)
class RunSettings:
    """Everything the loop needs that a human might want to change per run."""

    repo_path: Path
    model: str = DEFAULT_MODEL
    effort: str | None = DEFAULT_EFFORT
    permission_mode: str = DEFAULT_PERMISSION_MODE
    session_budget_usd: float = DEFAULT_SESSION_BUDGET_USD
    total_budget_usd: float | None = None
    session_timeout_seconds: int = DEFAULT_SESSION_TIMEOUT_SECONDS
    max_sessions: int = DEFAULT_MAX_SESSIONS
    require_clean_tree: bool = True
    verification_guidance: str = DEFAULT_VERIFICATION_GUIDANCE
    dry_run: bool = False


@dataclass
class RunReport:
    """What happened, in the form the run log and the closing summary need."""

    sessions_run: int = 0
    features_passed: int = 0
    total_cost_usd: float = 0.0
    stop_reason: str = "not started"
    session_ids: list[str] = field(default_factory=list)


# ------ SHARED SESSION PLUMBING ------


def _tool_command(script_name: str) -> str:
    """The shell command that runs one of the harness's tool scripts.

    Built from the interpreter currently running the harness rather than from
    the string "python3". The target repository may have no python on PATH, a
    different one, or one inside a virtualenv the session has not activated,
    and none of that should be the agent's problem to solve.

    Both halves are quoted: these strings are parsed by a shell, and a space in
    a checkout path ("Application Support", a user with a space in their name)
    would otherwise split the command silently.
    """
    tool_path = Path(__file__).resolve().parents[1] / "tools" / script_name
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(tool_path))}"


def _mark_feature_command() -> str:
    """The exact command a session must run to record a verified feature."""
    return _tool_command("mark_feature.py")


def _hook_command() -> str:
    return _tool_command("protect_feature_list.py")


def write_generated_settings(workspace: Workspace) -> Path:
    """Write the per-run settings file that wires the guard hook into a session.

    Generated at run time rather than committed, because it holds absolute
    paths belonging to this machine and this checkout of the harness. The
    upside is that every repository the harness drives gets the current hook
    without anyone re-installing anything.
    """
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": _hook_command(),
                            "timeout": 30,
                        }
                    ],
                }
            ]
        }
    }
    workspace.settings_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.settings_path.write_text(json.dumps(settings, indent=2) + "\n", "utf-8")
    return workspace.settings_path


def _common_replacements(workspace: Workspace, settings: RunSettings) -> dict[str, str]:
    """Fill the prompt templates for this workspace.

    Workspace paths are rendered relative to the repository root, which is the
    session's working directory. Absolute paths would work too, but they are
    long, they differ per machine, and a prompt full of them reads as noise to
    the model that has to follow it. The mark command is the exception and stays
    absolute: the harness lives outside the repository being worked on.
    """

    def relative(path: Path) -> str:
        return str(path.relative_to(workspace.repo_root))

    return {
        "SPEC_PATH": relative(workspace.spec_path),
        "FEATURE_LIST_PATH": relative(workspace.feature_list_path),
        "PROGRESS_PATH": relative(workspace.progress_path),
        "INIT_SCRIPT_PATH": "./" + relative(workspace.init_script_path),
        "SERVE_SCRIPT_PATH": "./" + relative(workspace.serve_script_path),
        "MARK_FEATURE_COMMAND": _mark_feature_command(),
        "VERIFICATION_GUIDANCE": settings.verification_guidance,
    }


def _build_request(
    workspace: Workspace,
    settings: RunSettings,
    prompt: str,
    replacements: dict[str, str],
) -> SessionRequest:
    return SessionRequest(
        prompt=prompt,
        repo_path=workspace.repo_root,
        session_id=str(uuid.uuid4()),
        model=settings.model,
        effort=settings.effort,
        permission_mode=settings.permission_mode,
        budget_usd=settings.session_budget_usd,
        timeout_seconds=settings.session_timeout_seconds,
        system_prompt_suffix=prompts.render("system_contract", replacements),
        settings_path=workspace.settings_path,
    )


def _existing_log_filenames(workspace: Workspace) -> list[str]:
    if not workspace.logs_dir.is_dir():
        return []
    return [path.name for path in workspace.logs_dir.iterdir()]


def describe_session_model(settings: RunSettings) -> str:
    """How a session will be run, for the console line and the record.

    Effort is named explicitly, including when it is not set. "cli default" is a
    real answer and a more useful one than silence, since the level in force is
    otherwise invisible from the outside.
    """
    return f"{settings.model}, effort {settings.effort or 'cli default'}"


def _run_and_record(
    workspace: Workspace, label: str, request: SessionRequest
) -> SessionResult:
    """Run one session and leave three artifacts behind, named alike.

    The stream is the forensic copy, the transcript is the readable one, and the
    record is the structured summary. All three share the label and session id
    so a single grep finds every trace of one session.
    """
    stem = f"{label}-{request.session_id}"
    result = run_session(
        request,
        stream_path=workspace.logs_dir / f"{stem}.jsonl",
        transcript_path=workspace.logs_dir / f"{stem}.md",
    )
    _record_session(workspace, label, request, result)
    return result


def _record_session(
    workspace: Workspace, label: str, request: SessionRequest, result: SessionResult
) -> None:
    record = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "label": label,
        "session_id": result.session_id,
        "model": request.model,
        "effort": request.effort or "cli default",
        "thinking_tokens": result.thinking_tokens,
        "transcript": f"{label}-{result.session_id}.md",
        "stream": f"{label}-{result.session_id}.jsonl",
        "is_error": result.is_error,
        "subtype": result.subtype,
        "num_turns": result.num_turns,
        "cost_usd": result.cost_usd,
        "duration_ms": result.duration_ms,
        "permission_denials": list(result.permission_denials),
        "final_message": result.text,
    }
    path = workspace.logs_dir / f"{label}-{result.session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def commit_workspace(workspace: Workspace, message: str) -> bool:
    """Commit whatever is uncommitted under the workspace directory.

    The harness commits its own files rather than asking the session to. Two
    reasons, both learned from a real run. The harness writes the spec and the
    ignore file itself, so leaving them uncommitted means it dirties the very
    work tree it just insisted was clean. And an initializer told to "commit
    everything you created" does not count files it did not create, so the
    feature list defining the whole run was left untracked.

    Project code is never committed here. That stays the session's job, because
    an uncommitted change to the project is a signal the loop needs to see.
    """
    subprocess.run(
        ["git", "-C", str(workspace.repo_root), "add", "--", str(workspace.root)],
        capture_output=True,
        text=True,
        check=False,
    )
    # Scope both the staged-check and the commit to the workspace path. Without
    # the pathspec, a session that left files staged (e.g. `git add -A` before a
    # crash) would have them swept into this commit, breaking the "project code
    # is never committed here" contract above and blinding the next run's
    # uncommitted-changes preflight.
    staged = subprocess.run(
        [
            "git",
            "-C",
            str(workspace.repo_root),
            "diff",
            "--cached",
            "--quiet",
            "--",
            str(workspace.root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if staged.returncode == 0:
        return False

    committed = subprocess.run(
        [
            "git",
            "-C",
            str(workspace.repo_root),
            "commit",
            "-m",
            message,
            "--",
            str(workspace.root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if committed.returncode != 0:
        raise HarnessError(
            "Could not commit the harness workspace: "
            f"{committed.stderr.strip() or committed.stdout.strip()}"
        )
    return True


def _git_head(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip()


# ------ INITIALIZER ------


def initialize(settings: RunSettings, spec_path: Path, *, force: bool = False) -> None:
    """Run the one session that builds the environment every other session uses."""
    report = run_preflight(
        settings.repo_path, require_clean_tree=settings.require_clean_tree
    )
    for warning in report.warnings:
        print(f"warning: {warning}")
    print(f"harness: auth, {report.authentication}")

    workspace = Workspace(repo_root=report.repo_root)
    if workspace.feature_list_path.is_file() and not force:
        raise HarnessError(
            f"{workspace.feature_list_path} already exists. Initializing again would "
            "replace the definition of done for work already in progress. Pass "
            "--force if that is genuinely what you want."
        )

    if not spec_path.is_file():
        raise HarnessError(f"No specification file at {spec_path}.")

    replacements = _common_replacements(workspace, settings)
    prompt = prompts.render("initializer", replacements)

    if settings.dry_run:
        # Deliberately before anything is written. A dry run that leaves a
        # workspace behind also leaves the repository dirty, which makes the
        # next real command refuse to start.
        _print_dry_run(_build_request(workspace, settings, prompt, replacements))
        return

    workspace.root.mkdir(parents=True, exist_ok=True)
    prepare_local_directories(workspace)

    # The spec is copied into the repository rather than passed as a prompt
    # string, so that every later session reads the same source of truth from
    # disk and the record of what was asked for lives in the repo's history.
    workspace.spec_path.write_text(spec_path.read_text(encoding="utf-8"), "utf-8")

    write_generated_settings(workspace)

    # Before the session, not after: the harness just required a clean work
    # tree and has now written two files into it. Starting a session on a tree
    # the harness itself dirtied would make the session's own diff unreadable.
    commit_workspace(
        workspace,
        "chore: recording the harness workspace so the run starts from a known "
        "commit and the spec is part of the repository's history",
    )

    request = _build_request(workspace, settings, prompt, replacements)

    print(
        f"harness: initializing {workspace.repo_root} "
        f"({describe_session_model(settings)}, session {request.session_id})"
    )
    result = _run_and_record(workspace, "init", request)

    if result.is_error:
        raise HarnessError(
            f"The initializer session failed ({result.subtype}). Its log is in "
            f"{workspace.logs_dir}."
        )

    missing = missing_initializer_artifacts(workspace)
    if missing:
        raise HarnessError(
            "The initializer session ended without leaving: "
            f"{', '.join(missing)}. The run cannot continue, because every coding "
            "session depends on those files existing. Read its log in "
            f"{workspace.logs_dir} and re-run init with --force."
        )

    commit_workspace(
        workspace,
        "chore: recording the feature list and progress log the initializer "
        "produced, so the next session reads them from a committed state",
    )

    features = parse_feature_list(read_document(workspace.feature_list_path))
    summary = summarize(features)
    print(
        f"harness: initialized. {summary.total} features defined, "
        f"{summary.passing} already passing. Cost ${result.cost_usd:.2f}."
    )


# ------ CODING LOOP ------


def run(settings: RunSettings) -> RunReport:
    """Run coding sessions until a stop condition fires, and say which one."""
    preflight = run_preflight(
        settings.repo_path, require_clean_tree=settings.require_clean_tree
    )
    for warning in preflight.warnings:
        print(f"warning: {warning}")
    print(f"harness: auth, {preflight.authentication}")

    workspace = Workspace(repo_root=preflight.repo_root)
    missing = missing_initializer_artifacts(workspace)
    if missing:
        raise HarnessError(
            f"This repository is missing {', '.join(missing)}. Run "
            "`harness.py init --spec <file>` against it first."
        )

    if not settings.dry_run:
        prepare_local_directories(workspace)
        write_generated_settings(workspace)

    report = RunReport(stop_reason="reached the session limit")
    stalled_sessions = 0

    print(f"harness: session records in {workspace.logs_dir}")

    for index in range(1, settings.max_sessions + 1):
        document_before = read_document(workspace.feature_list_path)
        features_before = parse_feature_list(document_before)
        summary_before = summarize(features_before)

        if summary_before.is_complete:
            report.stop_reason = COMPLETE_STOP_REASON
            break

        if (
            settings.total_budget_usd is not None
            and report.total_cost_usd >= settings.total_budget_usd
        ):
            report.stop_reason = (
                f"the run budget of ${settings.total_budget_usd:.2f} is spent"
            )
            break

        target = next_feature(features_before)
        if target is None:  # pragma: no cover - is_complete already covers this
            report.stop_reason = "no failing feature left to work on"
            break

        head_before = _git_head(workspace.repo_root)

        replacements = _common_replacements(workspace, settings)
        replacements["FEATURE_SUMMARY"] = (
            f"{summary_before.passing} of {summary_before.total} features pass"
        )
        replacements["TARGET_FEATURE"] = _describe_feature(target)

        prompt = prompts.render("coding", replacements)
        request = _build_request(workspace, settings, prompt, replacements)

        if settings.dry_run:
            _print_dry_run(request)
            report.stop_reason = "dry run, nothing was executed"
            break

        number = next_session_number(_existing_log_filenames(workspace))
        label = f"session-{number:03d}"
        print(
            f"harness: {label} on {target.id!r} "
            f"({describe_session_model(settings)}) — "
            f"{summary_before.passing}/{summary_before.total} passing, "
            f"{index} of {settings.max_sessions} this run"
        )
        try:
            result = _run_and_record(workspace, label, request)
        except HarnessError as error:
            # A session that timed out or produced nothing parseable. Its
            # artifacts are already on disk; what remains is to leave the
            # repository in a state the next `run` will accept, and to end
            # with the summary rather than a traceback.
            report.sessions_run += 1
            report.session_ids.append(request.session_id)
            _restore_feature_list_if_tampered(workspace, document_before)
            report.stop_reason = f"the session could not finish: {error}"
            _commit_before_stopping(workspace, index)
            break

        report.sessions_run += 1
        report.total_cost_usd += result.cost_usd
        report.session_ids.append(result.session_id)

        # Defence in depth behind the hook. The hook can be bypassed by a
        # creative enough command and fails open when it cannot read an event,
        # so the state that matters is re-checked from disk after every session.
        tampering = _detect_tampering(workspace, document_before)
        if tampering:
            report.stop_reason = (
                "the feature list was changed in a way only a human may change it: "
                + "; ".join(tampering)
            )
            _restore_feature_list_if_tampered(workspace, document_before)
            _commit_before_stopping(workspace, index)
            break

        if result.is_error:
            report.stop_reason = f"the session ended in an error ({result.subtype})"
            _commit_before_stopping(workspace, index)
            break

        summary_after = summarize(
            parse_feature_list(read_document(workspace.feature_list_path))
        )
        report.features_passed += summary_after.passing - summary_before.passing

        # A session that wrote a test and recorded it failing has done real
        # work, and it is the half of the shift that cannot be faked. It
        # usually commits too, so the HEAD check would normally catch it — but
        # a session that runs out of context between the red and the commit
        # must not read as stalled and cost the run one of its two lives.
        made_progress = (
            summary_after.passing > summary_before.passing
            or summary_after.test_failing > summary_before.test_failing
            or _git_head(workspace.repo_root) != head_before
        )
        if made_progress:
            stalled_sessions = 0
        else:
            stalled_sessions += 1
            print(
                f"harness: session {index} neither committed nor passed a feature "
                f"({stalled_sessions} in a row)"
            )
            if stalled_sessions >= MAX_CONSECUTIVE_STALLED_SESSIONS:
                report.stop_reason = (
                    f"{stalled_sessions} sessions in a row made no commit and passed "
                    "no feature, so the loop was spending money without moving"
                )
                _commit_before_stopping(workspace, index)
                break

        commit_workspace(
            workspace,
            f"chore: recording harness workspace changes from session {index}",
        )

        uncommitted = find_uncommitted_changes(workspace.repo_root)
        if uncommitted:
            print(
                f"harness: warning, session {index} left {len(uncommitted)} "
                "uncommitted change(s) behind"
            )

    print(
        f"harness: stopped because {report.stop_reason}. "
        f"{report.sessions_run} session(s), {report.features_passed} feature(s) "
        f"newly passing, ${report.total_cost_usd:.2f} spent."
    )

    return report


def _detect_tampering(
    workspace: Workspace, document_before: dict[str, object]
) -> tuple[str, ...]:
    """Everything the loop can still check once the session is over.

    Two halves, and they catch different things. Comparing the documents catches
    a list that changed shape — a feature added, a description softened, a claim
    with no record behind it. Re-hashing catches a record that is no longer true
    of the repository: a digest that was invented, a test path naming a file
    that is not there, a test rewritten after the harness watched it fail. The
    document comparison cannot see any of the second kind, because a forged
    record looks exactly like an honest one on paper.

    Only the features this session moved are re-hashed, and that bound is
    load-bearing rather than an optimisation. Test files hold more than one
    test in every real suite, so session six adding its own test to
    `tests/test_api.py` changes the bytes that features one through five were
    digested against. Re-hashing all of them would read that as tampering,
    restore the list, and stop the run — and stop it again on the next
    invocation, since nothing about the repository has changed. The forgery
    this check exists for always shows up in the feature it was written for, so
    scoping it to what moved loses nothing worth keeping. `status` re-hashes
    everything and prints it, which is where a test quietly weakened later
    surfaces, for a human rather than as a halt.
    """
    try:
        document_after = read_document(workspace.feature_list_path)
    except HarnessError as error:
        return (f"the feature list is no longer readable: {error}",)

    reasons = find_structural_changes(document_before, document_after)
    if reasons:
        return reasons

    return find_unverifiable_evidence(
        _features_moved_this_session(document_before, document_after),
        workspace.repo_root,
    )


def _features_moved_this_session(
    document_before: dict[str, object], document_after: dict[str, object]
) -> tuple[Feature, ...]:
    """The features whose state or evidence the session changed.

    An unparseable *before* document means the comparison cannot be made at
    all, so every feature is treated as moved: erring toward checking more is
    the safe direction for a tamper check.
    """
    features_after = parse_feature_list(document_after)
    try:
        before = {
            feature.id: feature for feature in parse_feature_list(document_before)
        }
    except HarnessError:
        return features_after

    return tuple(
        feature
        for feature in features_after
        if feature.id not in before
        or before[feature.id].state is not feature.state
        or before[feature.id].evidence != feature.evidence
    )


def _restore_feature_list_if_tampered(
    workspace: Workspace, document_before: dict[str, object]
) -> None:
    """Put the feature list back the way the session found it, if it was tampered.

    Detection alone is not enough: whatever commits next would enshrine the
    tampered list as the run's ground truth, and the next run would read a
    definition of done the session wrote for itself. The harness held the
    pre-session document, so the honest state is one write away.
    """
    if _detect_tampering(workspace, document_before):
        write_document(workspace.feature_list_path, document_before)
        print("harness: the feature list was restored to its pre-session state")


def _commit_before_stopping(workspace: Workspace, index: int) -> None:
    """Commit workspace files before an early stop, so the next run starts clean.

    Every break skips the end-of-iteration commit, and by the time a session
    has failed it has usually appended to the progress log. Leaving that
    uncommitted strands an unattended setup: the next `run` refuses to start
    on a tree the harness itself dirtied.
    """
    commit_workspace(
        workspace,
        f"chore: recording harness workspace state after session {index} "
        "stopped early, so the next run starts from a clean tree",
    )


def _describe_feature(feature: Feature) -> str:
    steps = "\n".join(
        f"{number}. {step}" for number, step in enumerate(feature.steps, 1)
    )
    return (
        f"**Feature `{feature.id}`** ({feature.category}, priority {feature.priority})\n\n"
        f"{feature.description}\n\n"
        f"Its steps, which are the acceptance criteria:\n\n{steps}"
    )


def _print_dry_run(request: SessionRequest) -> None:
    print("harness: dry run, nothing was executed.\n")
    print("--- command ---")
    print(" ".join(shlex.quote(part) for part in build_argv(request)[:-1]))
    print("<prompt passed as the final positional argument>\n")
    print("--- appended system prompt ---")
    print(request.system_prompt_suffix)
    print("\n--- prompt ---")
    print(request.prompt)
