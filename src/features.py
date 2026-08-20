"""The feature list: the file that defines what "done" means for a run.

Two of the article's failure modes are really one problem — the agent gets to
decide what finished looks like — and the feature list is the fix. It is
written once by the initializer from the spec, and after that the only change
any coding session may make to it is moving a feature's `state` along an edge
the harness itself has watched happen.

That rule is enforced three times over: the prompt states it, a PreToolUse
hook denies direct writes to the file, and the loop compares the list before and
after every session. The layers overlap on purpose, because a rule that only
exists in a prompt is a rule the model may talk itself out of at three in the
morning, and the hook fails open by design.

The human is deliberately not bound by this: the hook only sees the agent's
tool calls, so editing the list by hand stays the intended escape hatch when
the spec genuinely changed.
"""

import copy
import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from agent_harness.config import HarnessError


class FeatureState(StrEnum):
    """Where a feature stands, as something the harness observed rather than
    something a session asserted.

    A `StrEnum` rather than bare string constants for two reasons: it serialises
    to its own value straight through `json.dumps`, so nothing has to remember
    to call `.value` on the write path; and a mistyped member is an
    `AttributeError` at import instead of a string literal that silently matches
    no feature anywhere.
    """

    # Nothing written. Every feature the initializer writes starts here.
    NO_TEST = "no-test"
    # The test exists and the harness watched it fail.
    TEST_FAILING = "test-failing"
    # The test exists and the harness watched it pass.
    PASSING = "passing"
    # It was passing; the recorded check now fails. A regression.
    BROKEN = "broken"


class TransitionRequirement(StrEnum):
    """What the harness must observe before it will write a state change."""

    CHECK_MUST_FAIL = "check-must-fail"
    CHECK_MUST_PASS = "check-must-pass"
    NO_CHECK = "no-check"


# Every state change a session may make, as data rather than a chain of `if`s,
# so the legal set reads in one screen and can be tested cell by cell.
#
# The governing principle: a transition that *reduces* claimed progress needs no
# proof, because no session has an incentive to falsely claim less than it
# achieved. Only movement toward `passing` needs evidence. That is why every
# edge into `no-test` is free and why `passing` is the most constrained state to
# reach.
#
# Three absences are the point of the whole table:
#   no-test      -> passing   the ordering rule itself: nothing may be recorded
#                             passing without having first been seen failing
#   no-test      -> broken    nothing has ever worked, so nothing can have broken
#   test-failing -> broken    it has never passed, so it has not regressed
LEGAL_TRANSITIONS: dict[tuple[FeatureState, FeatureState], TransitionRequirement] = {
    # Writing a test, and watching it fail before the implementation exists.
    (FeatureState.NO_TEST, FeatureState.TEST_FAILING): (
        TransitionRequirement.CHECK_MUST_FAIL
    ),
    # Re-recording after the test itself changed. Still has to be seen red.
    (FeatureState.TEST_FAILING, FeatureState.TEST_FAILING): (
        TransitionRequirement.CHECK_MUST_FAIL
    ),
    # The edge the whole mechanism exists to guard.
    (FeatureState.TEST_FAILING, FeatureState.PASSING): (
        TransitionRequirement.CHECK_MUST_PASS
    ),
    # A regression: the recorded check no longer holds.
    (FeatureState.PASSING, FeatureState.BROKEN): (
        TransitionRequirement.CHECK_MUST_FAIL
    ),
    # Fixing a regression means fixing the code, so the digest must still match.
    (FeatureState.BROKEN, FeatureState.PASSING): (
        TransitionRequirement.CHECK_MUST_PASS
    ),
    # If the test genuinely has to change, the honest path is back through red.
    (FeatureState.BROKEN, FeatureState.TEST_FAILING): (
        TransitionRequirement.CHECK_MUST_FAIL
    ),
    # Giving up a claim, from anywhere, costs nothing and drops all evidence.
    (FeatureState.NO_TEST, FeatureState.NO_TEST): TransitionRequirement.NO_CHECK,
    (FeatureState.TEST_FAILING, FeatureState.NO_TEST): TransitionRequirement.NO_CHECK,
    (FeatureState.PASSING, FeatureState.NO_TEST): TransitionRequirement.NO_CHECK,
    (FeatureState.BROKEN, FeatureState.NO_TEST): TransitionRequirement.NO_CHECK,
}


def transition_requirement(
    current: FeatureState, target: FeatureState
) -> TransitionRequirement | None:
    """What must be observed to move `current` -> `target`, or None if forbidden."""
    return LEGAL_TRANSITIONS.get((current, target))


def legal_targets_from(current: FeatureState) -> tuple[FeatureState, ...]:
    """Every state reachable from `current`, for an error message to name.

    A refusal that does not say where the session may go instead invites it to
    guess, and a guessing model retries the same forbidden edge.
    """
    return tuple(target for (source, target) in LEGAL_TRANSITIONS if source is current)


def is_manual_exception(
    current: FeatureState, target: FeatureState, evidence: Evidence | None
) -> bool:
    """True for the one edge the table deliberately does not carry.

    `no-test` -> `passing` on a `manual:` record is the escape hatch for a check
    no command can make, and it cannot go through red because there is no test
    to watch fail. It is deliberately outside the table so that every automated
    path has to satisfy the ordering rule, and so that a reader looking for how
    a feature could have skipped red finds exactly one answer.
    """
    return (
        current is FeatureState.NO_TEST
        and target is FeatureState.PASSING
        and evidence is not None
        and evidence.kind == "manual"
    )


# The document version this harness reads and writes. Enforced rather than
# ignored: a version-1 list predates the rule that a test must be seen failing
# before it can be recorded passing, so its statuses were never held to it.
FEATURE_LIST_VERSION = 2

# Every field a feature must carry. `state` is the only one a session may
# change, and `priority` exists so that "the highest-priority feature not yet
# done" is a computation rather than a judgment call the model re-makes every
# session with a different answer.
REQUIRED_FEATURE_FIELDS = (
    "id",
    "category",
    "priority",
    "description",
    "steps",
    "state",
)

# The two fields a session may change, both only through mark_feature.py.
# Everything else in a feature is fixed once the initializer writes it.
MUTABLE_FEATURE_FIELDS = ("state", "evidence")

# Fields compared when deciding whether an edit was a pure state change.
IMMUTABLE_FEATURE_FIELDS = tuple(
    field for field in REQUIRED_FEATURE_FIELDS if field not in MUTABLE_FEATURE_FIELDS
)


# The prefix that marks a check no automated command can make. It must carry a
# reason, because an escape hatch that costs nothing to use becomes the default
# route and the whole mechanism turns decorative.
MANUAL_VERIFICATION_PREFIX = "manual:"


@dataclass(frozen=True)
class Evidence:
    """What the harness saw when it last ran a feature's check.

    Named for what it is rather than `verified_by`, because this record now
    exists on a feature in `test-failing` too — where "verified by" would be a
    lie. It says what was run, against which files, and when.

    `kind` is "command" for something anyone can re-run, or "manual" for a check
    that genuinely cannot be automated. `detail` is the command, or the reason.
    """

    kind: str
    detail: str
    # The test files that were digested, relative to the repository root.
    test_paths: tuple[str, ...] = ()
    # sha256 over those files' contents, as `sha256:<hex>`.
    digest: str = ""
    # ISO 8601, when the check last ran.
    observed_at: str = ""

    def as_document(self) -> dict[str, Any]:
        """The JSON shape, omitting the fields a manual check never has.

        A manual record carrying `"digest": ""` would invite a reader to think
        something was hashed and came out empty, so absent means absent.
        """
        document: dict[str, Any] = {"kind": self.kind, "detail": self.detail}
        if self.test_paths:
            document["test_paths"] = list(self.test_paths)
        if self.digest:
            document["digest"] = self.digest
        if self.observed_at:
            document["observed_at"] = self.observed_at
        return document


# Commands that always succeed while testing nothing. A check that cannot fail
# is not evidence, so one offered as verification is refused where it is named
# rather than run and recorded. This is a heuristic mitigation, not a full
# defence: a session can still write a command that passes without proving the
# feature. What closes that gap is the ordering the state machine enforces —
# the same unchanged test has to be seen failing first.
NOOP_CHECK_COMMANDS = frozenset({"true", ":", "exit", "echo", "printf"})

# Shell composition (a pipe, a chain, a substitution) means the segment is more
# than one bare builtin, so the no-op test does not apply: `echo running && \
# pytest` really runs pytest.
_SHELL_COMPOSITION = re.compile(r"[|;&\n`]|\$\(")


def _is_noop_check(command: str) -> bool:
    """True when the check is a lone builtin that cannot fail.

    Deliberately narrow. It matches `true`, `:`, `exit 0`, and a bare `echo` or
    `printf` standing alone, because those are what a session offers when it
    wants the record without the work. Anything composed with a real command,
    or any command that could actually fail, is left for `run_check` to run.
    """
    stripped = command.strip().rstrip(";").strip()
    if _SHELL_COMPOSITION.search(stripped):
        return False
    try:
        tokens = shlex.split(stripped, comments=True)
    except ValueError:
        return False
    if not tokens:
        return False
    head = os.path.basename(tokens[0])
    if head not in NOOP_CHECK_COMMANDS:
        return False
    # `exit N` with a non-zero N fails the check honestly and records nothing,
    # so only the always-succeeding forms are worthless as evidence.
    exits_nonzero = head == "exit" and len(tokens) > 1 and tokens[1] not in ("0", "")
    return not exits_nonzero


def parse_verification(raw: str) -> Evidence:
    """Read what a session offers as proof, before anything is run.

    A command is the expected answer. Prose in the progress log describing a
    check somebody ran once in a shell nobody kept is not evidence that survives
    the session, and after fifty sessions the progress log is the only thing
    claiming the project works.

    The returned `Evidence` carries no digest and no timestamp: those are facts
    about a check that has actually been run, and nothing has run yet here.
    """
    value = raw.strip()
    if not value:
        raise HarnessError(
            "A feature cannot be recorded without saying how it was checked. "
            "Give the command that reproduces the check, or "
            f'"{MANUAL_VERIFICATION_PREFIX} <reason>" if it genuinely cannot be '
            "automated."
        )

    if value.lower().startswith(MANUAL_VERIFICATION_PREFIX):
        reason = value[len(MANUAL_VERIFICATION_PREFIX) :].strip()
        if not reason:
            raise HarnessError(
                "A manual verification has to say why the check cannot be "
                "automated. Without a reason it is indistinguishable from not "
                "having written a test."
            )
        return Evidence(kind="manual", detail=reason)

    if _is_noop_check(value):
        raise HarnessError(
            "A check that always succeeds proves nothing: true, :, exit 0 and a "
            "bare echo are not verifications. Give the command that actually "
            "exercises the feature (usually the test you wrote for it), or "
            f'"{MANUAL_VERIFICATION_PREFIX} <reason>" if it genuinely cannot be '
            "automated."
        )
    return Evidence(kind="command", detail=value)


@dataclass(frozen=True)
class Feature:
    id: str
    category: str
    priority: int
    description: str
    steps: tuple[str, ...]
    state: FeatureState
    # Absent while the state is `no-test`, which is every feature the
    # initializer writes.
    evidence: Evidence | None = None


@dataclass(frozen=True)
class FeatureSummary:
    total: int
    passing: int
    # Everything not passing. Kept under its old name so the loop's arithmetic
    # and every existing caller carry over unchanged.
    failing: int
    # Of the passing features, how many carry a command anyone can re-run, and
    # how many rest on a human having looked once.
    automated: int = 0
    manual: int = 0
    # The breakdown the old boolean could not express. `broken` is the one worth
    # printing loudly: it means something that used to work does not.
    no_test: int = 0
    test_failing: int = 0
    broken: int = 0

    @property
    def is_complete(self) -> bool:
        return self.failing == 0


def parse_feature_list(raw: object) -> tuple[Feature, ...]:
    """Validate a loaded feature-list document and return its features.

    Validation is strict and every message names the offending feature, because
    the most likely reader of the message is someone looking at a run that
    stopped before it started.
    """
    if not isinstance(raw, dict):
        raise HarnessError(
            "The feature list must be a JSON object with a 'features' key, "
            f"not a {type(raw).__name__}."
        )

    # Cast after the isinstance rather than relying on narrowing: a checked
    # `dict` out of json is `dict[Unknown, Unknown]`, and every read off it then
    # reports as an unknown type.
    document = cast(dict[str, Any], raw)

    _require_supported_version(document)

    features_raw = document.get("features")
    if not isinstance(features_raw, list):
        raise HarnessError("The feature list has no 'features' array.")
    if not features_raw:
        raise HarnessError(
            "The feature list is empty. A run with no features has no definition "
            "of done, which is the failure the list exists to prevent."
        )

    features: list[Feature] = []
    seen_ids: set[str] = set()

    for index, raw_entry in enumerate(cast(list[Any], features_raw)):
        if not isinstance(raw_entry, dict):
            raise HarnessError(f"Feature at index {index} is not an object.")

        entry = cast(dict[str, Any], raw_entry)
        label = str(entry.get("id", f"index {index}"))
        _refuse_legacy_feature(entry, label)

        missing = [field for field in REQUIRED_FEATURE_FIELDS if field not in entry]
        if missing:
            raise HarnessError(f"Feature {label} is missing: {', '.join(missing)}.")

        feature_id = entry["id"]
        if not isinstance(feature_id, str) or not feature_id:
            raise HarnessError(f"Feature at index {index} has no usable string id.")
        if feature_id in seen_ids:
            raise HarnessError(
                f"Duplicate feature id {feature_id!r}. Ids address a feature when "
                "marking it, so they have to be unique."
            )
        seen_ids.add(feature_id)

        state = _read_state(entry["state"], feature_id)

        if not isinstance(entry["priority"], int) or isinstance(
            entry["priority"], bool
        ):
            raise HarnessError(f"Feature {feature_id} has a non-integer 'priority'.")

        raw_steps = entry["steps"]
        if not isinstance(raw_steps, list):
            raise HarnessError(f"Feature {feature_id} has a non-list 'steps'.")
        steps = cast(list[Any], raw_steps)
        if not all(isinstance(step, str) for step in steps):
            raise HarnessError(
                f"Feature {feature_id} has 'steps' that are not strings."
            )

        features.append(
            Feature(
                evidence=_read_evidence(entry.get("evidence"), feature_id),
                id=feature_id,
                category=str(entry["category"]),
                priority=entry["priority"],
                description=str(entry["description"]),
                steps=tuple(str(step) for step in steps),
                state=state,
            )
        )

    return tuple(features)


def _require_supported_version(document: dict[str, Any]) -> None:
    """Refuse any document this harness did not write, rather than coercing it.

    A quiet coercion would turn "this list predates the ordering rule" into
    "these features were verified under rules that did not exist", which is
    exactly the class of false confidence the ordering rule exists to remove.
    The break is loud and the way out is to re-initialise.
    """
    version = document.get("version")
    if version == FEATURE_LIST_VERSION:
        return
    raise HarnessError(
        f"This feature list is version {version!r}, but the harness reads "
        f"version {FEATURE_LIST_VERSION}. Version 1 predates the rule that a "
        "test has to be seen failing before a feature can be recorded passing, "
        "so nothing in it was ever held to that rule. Re-initialise the "
        "repository with `harness.py init --force` rather than editing the "
        "version number: coercing the old list would relabel work that was "
        "never checked as work that was."
    )


def _refuse_legacy_feature(entry: dict[str, Any], label: str) -> None:
    """Name the old field explicitly, because the fix differs from a typo's."""
    if "passes" in entry:
        raise HarnessError(
            f"Feature {label} carries a 'passes' field, which the four-state "
            "model replaced with 'state'. A boolean cannot say whether a test "
            "exists and fails, which is the state the ordering rule turns on. "
            "Re-initialise the repository with `harness.py init --force`."
        )
    if "verified_by" in entry:
        raise HarnessError(
            f"Feature {label} carries a 'verified_by' field, which is now "
            "'evidence' and holds the test paths and digest as well. "
            "Re-initialise the repository with `harness.py init --force`."
        )


def _read_state(raw: object, feature_id: str) -> FeatureState:
    try:
        return FeatureState(raw)
    except ValueError:
        legal = ", ".join(state.value for state in FeatureState)
        raise HarnessError(
            f"Feature {feature_id} has an unknown 'state' {raw!r}. "
            f"It must be one of: {legal}."
        ) from None


def _read_evidence(raw: object, feature_id: str) -> Evidence | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise HarnessError(f"Feature {feature_id} has a malformed 'evidence'.")
    entry = cast(dict[str, Any], raw)
    kind = str(entry.get("kind", ""))
    if kind not in ("command", "manual"):
        raise HarnessError(
            f"Feature {feature_id} has an 'evidence' of unknown kind {kind!r}."
        )
    raw_paths = entry.get("test_paths", [])
    if not isinstance(raw_paths, list):
        raise HarnessError(
            f"Feature {feature_id} has an 'evidence' whose 'test_paths' is not a list."
        )
    return Evidence(
        kind=kind,
        detail=str(entry.get("detail", "")),
        test_paths=tuple(str(path) for path in cast(list[Any], raw_paths)),
        digest=str(entry.get("digest", "")),
        observed_at=str(entry.get("observed_at", "")),
    )


def summarize(features: tuple[Feature, ...]) -> FeatureSummary:
    passing_features = [
        feature for feature in features if feature.state is FeatureState.PASSING
    ]
    automated = sum(
        1
        for feature in passing_features
        if feature.evidence is not None and feature.evidence.kind == "command"
    )
    manual = sum(
        1
        for feature in passing_features
        if feature.evidence is not None and feature.evidence.kind == "manual"
    )

    def count(state: FeatureState) -> int:
        return sum(1 for feature in features if feature.state is state)

    return FeatureSummary(
        total=len(features),
        passing=len(passing_features),
        failing=len(features) - len(passing_features),
        automated=automated,
        manual=manual,
        no_test=count(FeatureState.NO_TEST),
        test_failing=count(FeatureState.TEST_FAILING),
        broken=count(FeatureState.BROKEN),
    )


def next_feature(features: tuple[Feature, ...]) -> Feature | None:
    """The feature a session should work on, chosen deterministically.

    A `broken` feature outranks everything, then the lowest priority number,
    then declaration order. The harness picks rather than the agent so that two
    runs over the same list do the same work in the same order, and so a session
    does not spend its first thousand tokens re-deciding what matters most.

    `broken` comes first because it means something that used to work does not,
    and every feature built on top of it from here is built on a known
    regression — which is the exact failure `coding.md` step 2 asks each session
    to look for. Until the four-state model there was no way to say a feature
    had regressed, so the loop could not act on one even when a session found
    it. Priority alone would bury a regression under any lower-numbered feature
    nobody had started.
    """
    unfinished = [
        feature for feature in features if feature.state is not FeatureState.PASSING
    ]
    if not unfinished:
        return None
    return min(
        unfinished,
        key=lambda feature: (
            0 if feature.state is FeatureState.BROKEN else 1,
            feature.priority,
            features.index(feature),
        ),
    )


# ------ THE TEST DIGEST ------


def digest_paths(paths: tuple[str, ...] | list[str], repo_root: Path) -> str:
    """A sha256 over the contents of every named test file.

    This exists to stop one specific dodge: write `assert False`, record the red,
    rewrite the test into something real, record the green. Requiring the test
    files to be byte-identical between the two observations forces the final
    test to exist before the implementation does, which is the whole objective.

    Its limit, stated honestly: a session could name a decoy path and leave the
    real test file undigested. That is a deliberate act of falsification, it is
    recorded in the evidence, and it is visible to whoever reviews the PR. The
    digest raises the cost of the dodge; it does not make it impossible.

    Paths are resolved before hashing, so a symlink is digested under the file
    it actually points at and cannot be swung at a different target between the
    red and the green.
    """
    if not paths:
        raise HarnessError(
            "No test paths were given, so there is nothing to digest. Name the "
            "file or files holding the test, with --test-path."
        )

    root = repo_root.resolve()
    entries: list[tuple[str, bytes]] = []
    seen: set[str] = set()

    for raw in paths:
        candidate = Path(raw)
        absolute = candidate if candidate.is_absolute() else root / candidate
        resolved = absolute.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            raise HarnessError(
                f"The test path {raw!r} resolves to {resolved}, which is outside "
                f"the repository at {root}. A test the repository does not "
                "contain is not one the next session can re-run."
            ) from None
        if not resolved.is_file():
            raise HarnessError(
                f"The test path {raw!r} is not a file. Name the test files "
                "themselves, not a directory and not a path that does not exist."
            )
        key = relative.as_posix()
        if key in seen:
            continue
        seen.add(key)
        entries.append((key, resolved.read_bytes()))

    # Sorted, so the digest is a property of the set of files rather than of the
    # order they happened to be listed in on the command line.
    entries.sort()

    hasher = hashlib.sha256()
    for key, blob in entries:
        # The length is fed in as well, so that moving bytes across a file
        # boundary cannot leave the concatenation unchanged.
        hasher.update(key.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(str(len(blob)).encode("ascii"))
        hasher.update(b"\0")
        hasher.update(blob)
    return f"sha256:{hasher.hexdigest()}"


# ------ TAMPER DETECTION ------


def find_structural_changes(before: object, after: object) -> tuple[str, ...]:
    """Describe every change between two feature lists that is not a state move.

    An empty result means the edit is allowed. Anything else is a reason to
    block it, phrased for the model that is about to read it as a denial.

    Additions are rejected alongside removals and rewrites. A list that can grow
    can be padded with features that are already true, which is the same
    "declare victory" failure by a different route.

    A move into a state other than `no-test` that carries no `evidence` is also
    rejected. The sanctioned write path always records what it saw, so a bare
    state change can only have come from a direct edit that got past the hook —
    and an unevidenced "it passes" is precisely the claim this file exists to
    make impossible.

    **This does not enforce the transition table, and cannot.** It compares the
    list from before a session against the list after it, and one honest session
    legitimately makes several moves: `no-test` -> `test-failing` -> `passing` is
    the normal shift. Worse, the state graph is strongly connected — every state
    reaches `no-test` for free, and `no-test` reaches every other state — so
    every before/after pair is the endpoint of *some* legal path, and asking
    "is this a legal edge" at the session boundary would reject honest work
    while forbidding nothing.

    The table is enforced where a single move is actually visible: inside
    `mark_feature.py`, one call at a time, with the check run and its outcome
    observed. What this function adds on top is the part that survives the hook
    failing open — that the shape of the list is unchanged, and that every
    claim in it carries a record with something in it to re-check. The
    re-checking itself is `find_unverifiable_evidence`, which needs the working
    tree and so cannot live in a pure comparison.
    """
    try:
        before_features = {
            feature.id: feature for feature in parse_feature_list(before)
        }
    except HarnessError as error:
        return (f"the current feature list could not be read: {error}",)

    try:
        after_features = {feature.id: feature for feature in parse_feature_list(after)}
    except HarnessError as error:
        return (f"the proposed feature list is not a valid feature list: {error}",)

    reasons: list[str] = []

    for removed in sorted(set(before_features) - set(after_features)):
        reasons.append(f"feature {removed!r} was removed")
    for added in sorted(set(after_features) - set(before_features)):
        reasons.append(f"feature {added!r} was added")

    for feature_id in sorted(set(before_features) & set(after_features)):
        original = before_features[feature_id]
        proposed = after_features[feature_id]
        for field in IMMUTABLE_FEATURE_FIELDS:
            if getattr(original, field) != getattr(proposed, field):
                reasons.append(
                    f"feature {feature_id!r} had its {field} changed from "
                    f"{getattr(original, field)!r} to {getattr(proposed, field)!r}"
                )
        # Only a feature that actually changed is judged. An untouched entry
        # is not this session's doing, and flagging one would blame every
        # session after a human used the hand-edit escape hatch — and leave the
        # run permanently unable to start.
        moved = (
            original.state is not proposed.state
            or original.evidence != proposed.evidence
        )
        if not moved:
            continue

        # A same-state change is a rewrite of the evidence in place, and that is
        # its own dodge: re-digest a rewritten test against a feature already
        # marked `passing` and the red it was supposed to have gone through
        # never happened. `test-failing` -> `test-failing` is the one same-state
        # edge the table carries, because re-observing red is the honest way to
        # change a test.
        #
        # The price of this rule: a session that legitimately drops a passing
        # feature and rebuilds it from scratch within one session reads as a
        # rewrite. Nothing asks a session to do that, and the refusal explains
        # itself, so the trade is worth it against an undetected weakening.
        if (
            original.state is proposed.state
            and transition_requirement(original.state, proposed.state) is None
        ):
            reasons.append(
                f"feature {feature_id!r} kept its state {proposed.state.value!r} "
                "while its evidence was rewritten, which the sanctioned "
                "mark_feature.py path never does: it only ever records evidence "
                "alongside a state it has just watched happen"
            )
            continue

        if proposed.state is FeatureState.NO_TEST:
            continue

        if proposed.evidence is None:
            reasons.append(
                f"feature {feature_id!r} was moved to {proposed.state.value!r} with "
                "no recorded evidence, which the sanctioned mark_feature.py path "
                "always supplies"
            )
            continue

        # A fabricated record is usually a bare {kind, detail}: the fields that
        # cost something to forge are the ones naming real files and their hash.
        if proposed.evidence.kind == "command" and not (
            proposed.evidence.test_paths and proposed.evidence.digest
        ):
            reasons.append(
                f"feature {feature_id!r} is recorded {proposed.state.value!r} on "
                "evidence that names no test files and carries no digest, so "
                "there is nothing to re-check it against"
            )

    return tuple(reasons)


def find_unverifiable_evidence(
    features: tuple[Feature, ...], repo_root: Path
) -> tuple[str, ...]:
    """Re-hash every recorded test and report the claims that no longer hold up.

    This is the check with teeth, and it is deliberately separate from
    `find_structural_changes` because it needs the working tree rather than two
    documents. Comparing before and after can only see that a record exists;
    this sees whether the record is true of the repository as it now stands.

    It catches the three shapes a forged record takes, none of which the
    document comparison can see:

      - a digest invented to match nothing, so the named files hash differently
      - test paths naming files the repository does not contain
      - a test deleted or rewritten after the harness watched it fail

    Cheap enough to run after every session: it is a sha256 over the few files
    each feature named, not a test suite.

    Callers choose the scope, and it matters. The loop passes only the features
    a session moved, because a test file legitimately holds tests for several
    features and one honest addition changes the bytes every earlier claim was
    digested against. `status` passes everything, because there the answer is
    printed for a human rather than used to stop a run.
    """
    reasons: list[str] = []
    for feature in features:
        evidence = feature.evidence
        if feature.state is FeatureState.NO_TEST or evidence is None:
            continue
        # A manual record has no test to hash; `status` counts it separately so
        # that a project resting on manual claims cannot hide among checked ones.
        if evidence.kind != "command":
            continue

        try:
            actual = digest_paths(evidence.test_paths, repo_root)
        except HarnessError as error:
            reasons.append(
                f"feature {feature.id!r} is recorded {feature.state.value!r} "
                f"against a test the repository does not have: {error}"
            )
            continue

        if actual != evidence.digest:
            reasons.append(
                f"feature {feature.id!r} is recorded {feature.state.value!r} "
                f"against {', '.join(evidence.test_paths)}, but those files no "
                f"longer hash to what was recorded when the harness watched the "
                f"check run (recorded {evidence.digest}, now {actual}). Either "
                "the test changed after the fact or the record was written by "
                "something other than mark_feature.py."
            )

    return tuple(reasons)


# ------ FILE ACCESS ------


def read_document(path: Path) -> dict[str, Any]:
    """Load and validate the feature list at `path`, returning the raw document.

    The raw document rather than parsed `Feature` objects, so that a state
    change can be written back without dropping any field the initializer chose
    to add.
    """
    if not path.is_file():
        raise HarnessError(
            f"No feature list at {path}. Run `harness.py init` against this "
            "repository first."
        )
    try:
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise HarnessError(f"{path} is not valid JSON: {error}") from error

    parse_feature_list(loaded)
    if not isinstance(loaded, dict):  # pragma: no cover - parse_feature_list enforces
        raise HarnessError(f"{path} is not a JSON object.")
    return cast(dict[str, Any], loaded)


def set_feature_state(
    document: dict[str, Any],
    feature_id: str,
    *,
    state: FeatureState,
    evidence: Evidence | None = None,
) -> dict[str, Any]:
    """Return a copy of `document` with one feature's state and evidence changed.

    Deterministic, addressable by id, and incapable of touching anything else.
    This is the only write path the agent is given, which is what lets the hook
    deny direct edits outright instead of trying to judge them.

    Moving a feature back to `no-test` drops its evidence. That state means
    nothing has been written, and a record of a check against a test the list no
    longer claims exists is worse than no record at all.
    """
    updated = copy.deepcopy(document)
    for entry in cast(list[dict[str, Any]], updated.get("features", [])):
        if entry.get("id") == feature_id:
            entry["state"] = state.value
            if state is FeatureState.NO_TEST:
                entry.pop("evidence", None)
            elif evidence is not None:
                entry["evidence"] = evidence.as_document()
            return updated
    raise HarnessError(
        f"No feature with id {feature_id!r} in the list. Ids are fixed when the "
        "list is written; check the exact id rather than inventing one."
    )


def write_document(path: Path, document: dict[str, Any]) -> None:
    """Write the feature list back with stable formatting.

    Two-space indent and a trailing newline every time, so a state change is a
    one-line diff instead of a whole-file reformat.
    """
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
