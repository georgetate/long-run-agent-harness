"""Contract tests for the feature list: the file that defines "done".

The list is the harness's defence against both failure modes the article
describes — declaring victory early, and marking a feature done without testing
it — so the rules about what may change in it are the rules worth testing
hardest. Assertions written from the contract before the implementation, per
`docs/project-workflow.md` §2.
"""

import copy

import pytest
from agent_harness.config import HarnessError
from agent_harness.features import (
    Evidence,
    FeatureState,
    TransitionRequirement,
    digest_paths,
    find_structural_changes,
    find_unverifiable_evidence,
    legal_targets_from,
    next_feature,
    parse_feature_list,
    parse_verification,
    set_feature_state,
    summarize,
    transition_requirement,
)

FEATURE_LIST: dict[str, object] = {
    "version": 2,
    "features": [
        {
            "id": "new-chat",
            "category": "functional",
            "priority": 1,
            "description": "New chat button creates a fresh conversation",
            "steps": ["Navigate to main interface", "Click the 'New Chat' button"],
            "state": "no-test",
        },
        {
            "id": "dark-mode",
            "category": "visual",
            "priority": 3,
            "description": "Theme toggle switches to dark mode",
            "steps": ["Click the theme toggle"],
            "state": "passing",
            "evidence": {
                "kind": "command",
                "detail": "pytest tests/test_theme.py",
                "test_paths": ["tests/test_theme.py"],
                "digest": "sha256:aaaa",
                "observed_at": "2026-08-18T10:00:00+00:00",
            },
        },
        {
            "id": "send-message",
            "category": "functional",
            "priority": 2,
            "description": "A typed message receives a response",
            "steps": ["Type a query", "Press enter"],
            "state": "no-test",
        },
    ],
}


def with_state(
    document: dict[str, object], index: int, state: str, **extra: object
) -> dict[str, object]:
    """A copy of `document` with one feature's state replaced.

    Enough of these tests move a single feature that doing it inline every time
    buried the assertion under three lines of deep-copy bookkeeping.
    """
    changed = copy.deepcopy(document)
    entry = changed["features"][index]  # type: ignore[index]
    entry["state"] = state  # type: ignore[index]
    entry.pop("evidence", None)  # type: ignore[union-attr]
    entry.update(extra)  # type: ignore[union-attr]
    return changed


COMMAND_EVIDENCE: dict[str, object] = {
    "kind": "command",
    "detail": "pytest tests/test_new_chat.py",
    "test_paths": ["tests/test_new_chat.py"],
    "digest": "sha256:beef",
    "observed_at": "2026-08-18T11:00:00+00:00",
}


class TestParseFeatureList:
    def test_reads_every_feature_in_declaration_order(self):
        features = parse_feature_list(FEATURE_LIST)
        assert [feature.id for feature in features] == [
            "new-chat",
            "dark-mode",
            "send-message",
        ]

    def test_refuses_a_document_that_is_not_an_object(self):
        with pytest.raises(HarnessError):
            parse_feature_list([{"id": "a"}])

    def test_refuses_a_document_with_no_features_key(self):
        with pytest.raises(HarnessError, match="features"):
            parse_feature_list({"version": 2})

    def test_names_the_field_a_feature_is_missing(self):
        broken = copy.deepcopy(FEATURE_LIST)
        del broken["features"][0]["description"]  # type: ignore[index]
        with pytest.raises(HarnessError, match="description"):
            parse_feature_list(broken)

    def test_refuses_duplicate_ids_because_they_make_a_feature_unaddressable(self):
        broken = copy.deepcopy(FEATURE_LIST)
        broken["features"][1]["id"] = "new-chat"  # type: ignore[index]
        with pytest.raises(HarnessError, match="new-chat"):
            parse_feature_list(broken)

    def test_refuses_an_empty_feature_list(self):
        with pytest.raises(HarnessError):
            parse_feature_list({"version": 2, "features": []})

    @pytest.mark.parametrize("state", ["no-test", "test-failing", "passing", "broken"])
    def test_reads_each_of_the_four_states(self, state):
        document = with_state(FEATURE_LIST, 0, state, evidence=COMMAND_EVIDENCE)
        features = {f.id: f for f in parse_feature_list(document)}
        assert features["new-chat"].state == state
        assert features["new-chat"].state is FeatureState(state)

    def test_refuses_a_state_that_is_not_one_of_the_four_and_names_the_legal_ones(
        self,
    ):
        broken = with_state(FEATURE_LIST, 0, "yes")
        with pytest.raises(HarnessError) as raised:
            parse_feature_list(broken)
        message = str(raised.value)
        assert "new-chat" in message
        # Naming the legal values is the whole point: the reader is a model that
        # has just guessed a state name and needs the real ones, not a scolding.
        for legal in ("no-test", "test-failing", "passing", "broken"):
            assert legal in message


class TestTheHardVersionBreak:
    """A version-1 list is refused outright rather than coerced.

    Coercing it would relabel work recorded under rules that did not exist as
    work that satisfied them, which is the exact class of false confidence the
    ordering rule was added to remove.
    """

    def test_refuses_a_version_one_document(self):
        old = copy.deepcopy(FEATURE_LIST)
        old["version"] = 1
        with pytest.raises(HarnessError) as raised:
            parse_feature_list(old)
        message = str(raised.value)
        assert "version" in message
        assert "init --force" in message

    def test_refuses_a_document_with_no_version_at_all(self):
        old = copy.deepcopy(FEATURE_LIST)
        del old["version"]
        with pytest.raises(HarnessError, match="version"):
            parse_feature_list(old)

    def test_refuses_a_feature_still_carrying_the_old_passes_field(self):
        legacy = copy.deepcopy(FEATURE_LIST)
        legacy["features"][0]["passes"] = False  # type: ignore[index]
        with pytest.raises(HarnessError) as raised:
            parse_feature_list(legacy)
        message = str(raised.value)
        assert "passes" in message
        assert "init --force" in message

    def test_refuses_a_feature_still_carrying_the_old_verified_by_field(self):
        legacy = copy.deepcopy(FEATURE_LIST)
        legacy["features"][1]["verified_by"] = {  # type: ignore[index]
            "kind": "command",
            "detail": "pytest",
        }
        with pytest.raises(HarnessError, match="verified_by"):
            parse_feature_list(legacy)


class TestSummarize:
    def test_counts_passing_and_failing_features(self):
        summary = summarize(parse_feature_list(FEATURE_LIST))
        assert (summary.total, summary.passing, summary.failing) == (3, 1, 2)

    def test_is_complete_only_when_nothing_is_still_failing(self):
        summary = summarize(parse_feature_list(FEATURE_LIST))
        assert summary.is_complete is False

        everything_passes = copy.deepcopy(FEATURE_LIST)
        for feature in everything_passes["features"]:  # type: ignore[union-attr]
            feature["state"] = "passing"  # type: ignore[index]
        assert summarize(parse_feature_list(everything_passes)).is_complete is True

    def test_is_not_complete_while_anything_is_broken(self):
        # The old boolean could not tell a regression from unstarted work, so a
        # run could report "complete" over a feature that used to work.
        regressed = copy.deepcopy(FEATURE_LIST)
        for feature in regressed["features"]:  # type: ignore[union-attr]
            feature["state"] = "passing"  # type: ignore[index]
            feature["evidence"] = dict(COMMAND_EVIDENCE)  # type: ignore[index]
        regressed["features"][2]["state"] = "broken"  # type: ignore[index]

        summary = summarize(parse_feature_list(regressed))
        assert summary.broken == 1
        assert summary.is_complete is False

    def test_counts_each_state_separately(self):
        mixed = with_state(FEATURE_LIST, 0, "test-failing", evidence=COMMAND_EVIDENCE)
        mixed = with_state(mixed, 2, "broken", evidence=COMMAND_EVIDENCE)
        summary = summarize(parse_feature_list(mixed))
        assert (summary.no_test, summary.test_failing, summary.passing) == (0, 1, 1)
        assert summary.broken == 1
        assert (
            summary.no_test + summary.test_failing + summary.broken == summary.failing
        )


class TestNextFeature:
    def test_picks_the_lowest_priority_number_still_failing(self):
        chosen = next_feature(parse_feature_list(FEATURE_LIST))
        assert chosen is not None
        assert chosen.id == "new-chat"

    def test_skips_features_that_already_pass(self):
        nearly_done = with_state(FEATURE_LIST, 0, "passing", evidence=COMMAND_EVIDENCE)
        chosen = next_feature(parse_feature_list(nearly_done))
        assert chosen is not None
        assert chosen.id == "send-message"

    def test_breaks_ties_by_declaration_order_so_the_choice_is_reproducible(self):
        tied = copy.deepcopy(FEATURE_LIST)
        tied["features"][2]["priority"] = 1  # type: ignore[index]
        chosen = next_feature(parse_feature_list(tied))
        assert chosen is not None
        assert chosen.id == "new-chat"

    def test_returns_nothing_when_every_feature_passes(self):
        everything_passes = copy.deepcopy(FEATURE_LIST)
        for feature in everything_passes["features"]:  # type: ignore[union-attr]
            feature["state"] = "passing"  # type: ignore[index]
        assert next_feature(parse_feature_list(everything_passes)) is None

    def test_prefers_a_regression_over_higher_priority_work_never_started(self):
        # dark-mode is priority 3, the lowest-ranked feature in the list, and
        # new-chat at priority 1 has not been started. The regression still wins:
        # everything built from here would otherwise sit on top of it.
        regressed = with_state(FEATURE_LIST, 1, "broken", evidence=COMMAND_EVIDENCE)
        chosen = next_feature(parse_feature_list(regressed))
        assert chosen is not None
        assert chosen.id == "dark-mode"

    def test_orders_several_regressions_among_themselves_by_priority(self):
        regressed = with_state(FEATURE_LIST, 1, "broken", evidence=COMMAND_EVIDENCE)
        regressed = with_state(regressed, 2, "broken", evidence=COMMAND_EVIDENCE)
        chosen = next_feature(parse_feature_list(regressed))
        assert chosen is not None
        # send-message is priority 2, dark-mode is 3.
        assert chosen.id == "send-message"


class TestDigestPaths:
    """The digest is what forces the final test to exist before the code does."""

    def _write(self, root, name: str, body: str):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def test_is_stable_regardless_of_the_order_the_paths_are_listed_in(self, tmp_path):
        self._write(tmp_path, "tests/test_a.py", "assert 1\n")
        self._write(tmp_path, "tests/test_b.py", "assert 2\n")
        first = digest_paths(["tests/test_a.py", "tests/test_b.py"], tmp_path)
        second = digest_paths(["tests/test_b.py", "tests/test_a.py"], tmp_path)
        assert first == second
        assert first.startswith("sha256:")

    def test_changes_when_a_single_byte_of_a_test_changes(self, tmp_path):
        path = self._write(tmp_path, "tests/test_a.py", "assert 1\n")
        before = digest_paths(["tests/test_a.py"], tmp_path)
        path.write_text("assert 2\n", encoding="utf-8")
        assert digest_paths(["tests/test_a.py"], tmp_path) != before

    def test_distinguishes_two_file_sets_that_would_otherwise_serialise_alike(
        self, tmp_path
    ):
        # The hash is fed a stream of (path, length, bytes) per file. Drop the
        # length and the stream becomes ambiguous: a file whose contents happen
        # to spell out the next file's header can absorb it, and two different
        # sets of test files hash the same. These two are exactly that pair —
        # both serialise to "a.py\0" + "1b.py\0" + "b.py\0" + "2" without it.
        paths = ["a.py", "b.py"]

        (tmp_path / "a.py").write_bytes(b"1")
        (tmp_path / "b.py").write_bytes(b"b.py\x002")
        first = digest_paths(paths, tmp_path)

        (tmp_path / "a.py").write_bytes(b"1b.py\x00")
        (tmp_path / "b.py").write_bytes(b"2")
        assert digest_paths(paths, tmp_path) != first

    def test_refuses_a_path_outside_the_repository(self, tmp_path):
        outside = tmp_path.parent / "outside.py"
        outside.write_text("assert 1\n", encoding="utf-8")
        repo = tmp_path / "repo"
        repo.mkdir()
        with pytest.raises(HarnessError, match="outside"):
            digest_paths(["../outside.py"], repo)

    def test_refuses_a_directory(self, tmp_path):
        (tmp_path / "tests").mkdir()
        with pytest.raises(HarnessError, match="not a file"):
            digest_paths(["tests"], tmp_path)

    def test_refuses_a_path_that_does_not_exist(self, tmp_path):
        with pytest.raises(HarnessError, match="not a file"):
            digest_paths(["tests/nope.py"], tmp_path)

    def test_refuses_an_empty_set_of_paths(self, tmp_path):
        with pytest.raises(HarnessError, match="nothing to digest"):
            digest_paths([], tmp_path)

    def test_digests_a_symlink_under_the_file_it_actually_points_at(self, tmp_path):
        self._write(tmp_path, "tests/test_real.py", "assert 1\n")
        (tmp_path / "tests" / "link.py").symlink_to(tmp_path / "tests/test_real.py")
        # Naming the link and naming its target have to agree, or a session
        # could swing the link at a different file between red and green.
        assert digest_paths(["tests/link.py"], tmp_path) == digest_paths(
            ["tests/test_real.py"], tmp_path
        )

    def test_refuses_a_symlink_that_escapes_the_repository(self, tmp_path):
        outside = tmp_path / "outside.py"
        outside.write_text("assert 1\n", encoding="utf-8")
        repo = tmp_path / "repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "tests" / "link.py").symlink_to(outside)
        with pytest.raises(HarnessError, match="outside"):
            digest_paths(["tests/link.py"], repo)


class TestFindStructuralChanges:
    def test_allows_an_unchanged_file(self):
        assert find_structural_changes(FEATURE_LIST, copy.deepcopy(FEATURE_LIST)) == ()

    def test_allows_moving_a_feature_forward_with_its_evidence(self):
        after = with_state(FEATURE_LIST, 0, "test-failing", evidence=COMMAND_EVIDENCE)
        assert find_structural_changes(FEATURE_LIST, after) == ()

    def test_rejects_a_state_move_that_carries_no_evidence(self):
        # The sanctioned write path always records what it saw, so a bare move
        # can only be a direct edit that got past the hook — the declare-victory
        # move this check exists to catch.
        after = with_state(FEATURE_LIST, 0, "passing")
        reasons = find_structural_changes(FEATURE_LIST, after)
        assert any(
            "new-chat" in reason and "no recorded evidence" in reason
            for reason in reasons
        )

    def test_allows_dropping_a_feature_back_to_no_test(self):
        # Reducing a claim needs no proof: no session has an incentive to
        # falsely claim less than it achieved.
        after = with_state(FEATURE_LIST, 1, "no-test")
        assert find_structural_changes(FEATURE_LIST, after) == ()

    def test_rejects_a_reworded_description(self):
        after = copy.deepcopy(FEATURE_LIST)
        after["features"][0]["description"] = "something easier"  # type: ignore[index]
        reasons = find_structural_changes(FEATURE_LIST, after)
        assert any(
            "new-chat" in reason and "description" in reason for reason in reasons
        )

    def test_rejects_weakened_test_steps(self):
        after = copy.deepcopy(FEATURE_LIST)
        after["features"][2]["steps"] = []  # type: ignore[index]
        reasons = find_structural_changes(FEATURE_LIST, after)
        assert any("send-message" in reason and "steps" in reason for reason in reasons)

    def test_rejects_a_deleted_feature(self):
        after = copy.deepcopy(FEATURE_LIST)
        del after["features"][2]  # type: ignore[arg-type]
        reasons = find_structural_changes(FEATURE_LIST, after)
        assert any("send-message" in reason for reason in reasons)

    def test_rejects_an_added_feature_because_the_list_defines_the_scope(self):
        after = copy.deepcopy(FEATURE_LIST)
        after["features"].append(  # type: ignore[union-attr]
            {
                "id": "trivial",
                "category": "functional",
                "priority": 9,
                "description": "trivially true",
                "steps": ["do nothing"],
                "state": "passing",
                "evidence": dict(COMMAND_EVIDENCE),
            }
        )
        reasons = find_structural_changes(FEATURE_LIST, after)
        assert any("trivial" in reason for reason in reasons)

    def test_rejects_a_reprioritised_feature(self):
        after = copy.deepcopy(FEATURE_LIST)
        after["features"][0]["priority"] = 99  # type: ignore[index]
        assert find_structural_changes(FEATURE_LIST, after) != ()

    def test_rejects_a_file_it_cannot_read_rather_than_letting_it_through(self):
        assert find_structural_changes(FEATURE_LIST, "not a feature list") != ()

    def test_rejects_a_proposal_that_reverts_the_document_to_version_one(self):
        after = copy.deepcopy(FEATURE_LIST)
        after["version"] = 1
        assert find_structural_changes(FEATURE_LIST, after) != ()


class TestSetFeatureState:
    def test_moves_only_the_named_feature(self):
        after = set_feature_state(
            FEATURE_LIST,
            "send-message",
            state=FeatureState.PASSING,
            evidence=Evidence(kind="command", detail="pytest -q"),
        )
        states = {
            feature["id"]: feature["state"]
            for feature in after["features"]  # type: ignore[union-attr]
        }
        assert states == {
            "new-chat": "no-test",
            "dark-mode": "passing",
            "send-message": "passing",
        }

    def test_leaves_the_original_document_untouched(self):
        before = copy.deepcopy(FEATURE_LIST)
        set_feature_state(before, "new-chat", state=FeatureState.PASSING)
        assert before == FEATURE_LIST

    def test_refuses_an_id_that_is_not_in_the_list(self):
        with pytest.raises(HarnessError, match="invented-feature"):
            set_feature_state(
                FEATURE_LIST, "invented-feature", state=FeatureState.PASSING
            )

    def test_produces_a_document_the_tamper_check_accepts(self):
        # The evidence travels with the move: a state without one is the
        # unevidenced claim the tamper check rejects.
        after = set_feature_state(
            FEATURE_LIST,
            "new-chat",
            state=FeatureState.TEST_FAILING,
            evidence=Evidence(
                kind="command",
                detail="pytest -q",
                test_paths=("tests/test_new_chat.py",),
                digest="sha256:beef",
                observed_at="2026-08-18T12:00:00+00:00",
            ),
        )
        assert find_structural_changes(FEATURE_LIST, after) == ()

    def test_writes_the_state_as_a_plain_json_string(self):
        after = set_feature_state(
            FEATURE_LIST,
            "new-chat",
            state=FeatureState.TEST_FAILING,
            evidence=Evidence(kind="command", detail="pytest -q"),
        )
        entry = after["features"][0]  # type: ignore[index]
        assert entry["state"] == "test-failing"
        assert type(entry["state"]) is str or isinstance(entry["state"], str)


VERIFIED_LIST: dict[str, object] = {
    "version": 2,
    "features": [
        {
            "id": "new-chat",
            "category": "functional",
            "priority": 1,
            "description": "New chat button creates a fresh conversation",
            "steps": ["Click the 'New Chat' button"],
            "state": "passing",
            "evidence": {
                "kind": "command",
                "detail": "pytest tests/test_chat.py",
                "test_paths": ["tests/test_chat.py"],
                "digest": "sha256:1111",
                "observed_at": "2026-08-18T09:00:00+00:00",
            },
        },
        {
            "id": "dark-mode",
            "category": "visual",
            "priority": 3,
            "description": "Theme toggle switches to dark mode",
            "steps": ["Click the theme toggle"],
            "state": "passing",
            "evidence": {
                "kind": "manual",
                "detail": "colour judgement, no assertion",
            },
        },
        {
            "id": "send-message",
            "category": "functional",
            "priority": 2,
            "description": "A typed message receives a response",
            "steps": ["Press enter"],
            "state": "no-test",
        },
    ],
}


class TestParseVerification:
    """What a session must hand over to record a check, before anything runs.

    Prose in the progress log describing a check that was run once, by hand, in
    a shell nobody kept, is not evidence anyone can act on later. A command is.
    """

    def test_reads_a_command_that_reproduces_the_check(self):
        evidence = parse_verification("uv run pytest tests/test_chat.py::test_new")
        assert evidence.kind == "command"
        assert evidence.detail == "uv run pytest tests/test_chat.py::test_new"

    def test_reads_an_explicitly_manual_check_with_its_reason(self):
        evidence = parse_verification("manual: the check is a colour judgement")
        assert evidence.kind == "manual"
        assert "colour judgement" in evidence.detail

    def test_refuses_a_manual_claim_with_no_reason_behind_it(self):
        # The escape hatch has to cost something to use, or it becomes the
        # default route and the whole mechanism is decorative.
        with pytest.raises(HarnessError):
            parse_verification("manual:")

    def test_refuses_an_empty_claim(self):
        with pytest.raises(HarnessError):
            parse_verification("   ")

    def test_carries_no_digest_because_nothing_has_been_run_yet(self):
        evidence = parse_verification("pytest -q")
        assert evidence.digest == ""
        assert evidence.test_paths == ()
        assert evidence.observed_at == ""


class TestEvidenceOnTheFeatureList:
    def test_reads_the_evidence_recorded_against_a_feature(self):
        features = {f.id: f for f in parse_feature_list(VERIFIED_LIST)}
        assert features["new-chat"].evidence is not None
        assert features["new-chat"].evidence.kind == "command"

    def test_reads_the_test_paths_and_digest_the_check_was_taken_against(self):
        features = {f.id: f for f in parse_feature_list(VERIFIED_LIST)}
        evidence = features["new-chat"].evidence
        assert evidence is not None
        assert evidence.test_paths == ("tests/test_chat.py",)
        assert evidence.digest == "sha256:1111"
        assert evidence.observed_at == "2026-08-18T09:00:00+00:00"

    def test_an_unstarted_feature_simply_has_none(self):
        features = {f.id: f for f in parse_feature_list(VERIFIED_LIST)}
        assert features["send-message"].evidence is None

    def test_counts_how_much_of_the_passing_work_is_actually_reproducible(self):
        summary = summarize(parse_feature_list(VERIFIED_LIST))
        assert (summary.passing, summary.automated, summary.manual) == (2, 1, 1)

    def test_recording_evidence_alongside_a_move_is_an_allowed_change(self):
        after = with_state(
            VERIFIED_LIST,
            2,
            "test-failing",
            evidence={
                "kind": "command",
                "detail": "pytest tests/test_send.py",
                "test_paths": ["tests/test_send.py"],
                "digest": "sha256:2222",
                "observed_at": "2026-08-18T13:00:00+00:00",
            },
        )
        assert find_structural_changes(VERIFIED_LIST, after) == ()

    def test_rewriting_a_description_is_still_blocked(self):
        after = copy.deepcopy(VERIFIED_LIST)
        after["features"][0]["description"] = "easier"  # type: ignore[index]
        assert find_structural_changes(VERIFIED_LIST, after) != ()

    def test_refuses_evidence_of_an_unknown_kind(self):
        broken = copy.deepcopy(VERIFIED_LIST)
        broken["features"][0]["evidence"]["kind"] = "vibes"  # type: ignore[index]
        with pytest.raises(HarnessError, match="unknown kind"):
            parse_feature_list(broken)


class TestSetFeatureStateWithEvidence:
    def test_records_the_evidence_alongside_the_state(self):
        evidence = Evidence(
            kind="command",
            detail="pytest -k send",
            test_paths=("tests/test_send.py",),
            digest="sha256:3333",
            observed_at="2026-08-18T14:00:00+00:00",
        )
        after = set_feature_state(
            VERIFIED_LIST,
            "send-message",
            state=FeatureState.TEST_FAILING,
            evidence=evidence,
        )
        entry = after["features"][2]  # type: ignore[index]
        assert entry["state"] == "test-failing"
        assert entry["evidence"] == {
            "kind": "command",
            "detail": "pytest -k send",
            "test_paths": ["tests/test_send.py"],
            "digest": "sha256:3333",
            "observed_at": "2026-08-18T14:00:00+00:00",
        }

    def test_a_manual_record_carries_no_empty_digest_to_misread(self):
        after = set_feature_state(
            VERIFIED_LIST,
            "send-message",
            state=FeatureState.PASSING,
            evidence=Evidence(kind="manual", detail="colour judgement"),
        )
        entry = after["features"][2]  # type: ignore[index]
        assert entry["evidence"] == {"kind": "manual", "detail": "colour judgement"}

    def test_dropping_a_feature_to_no_test_clears_the_proof_that_no_longer_holds(self):
        # A feature back at no-test is one the list no longer claims has a test,
        # so a record of a check against that test would say something false.
        after = set_feature_state(VERIFIED_LIST, "new-chat", state=FeatureState.NO_TEST)
        assert "evidence" not in after["features"][0]  # type: ignore[index]
        assert after["features"][0]["state"] == "no-test"  # type: ignore[index]


class TestNoopCheckRejection:
    """Finding A at the source: parse_verification refuses a check that cannot fail."""

    @pytest.mark.parametrize(
        "check", ["true", ":", "exit 0", "exit", "echo hi", "  true  "]
    )
    def test_rejects_a_no_op_command(self, check):
        with pytest.raises(HarnessError, match="proves nothing"):
            parse_verification(check)

    @pytest.mark.parametrize(
        "check",
        ["pytest -q", "test -f README.md", "exit 1", "echo run && pytest", "./run.sh"],
    )
    def test_accepts_a_command_that_can_fail(self, check):
        evidence = parse_verification(check)
        assert evidence.kind == "command"
        assert evidence.detail == check.strip()


# ------ THE TRANSITION TABLE ------

ALL_STATES = (
    FeatureState.NO_TEST,
    FeatureState.TEST_FAILING,
    FeatureState.PASSING,
    FeatureState.BROKEN,
)

# Exactly the table in the plan, restated here rather than imported, so that a
# change to LEGAL_TRANSITIONS has to be made in two places by someone who meant
# it. A test that reads its expectations out of the code under test asserts
# nothing.
LEGAL_EDGES: dict[tuple[str, str], str] = {
    ("no-test", "test-failing"): "check-must-fail",
    ("test-failing", "test-failing"): "check-must-fail",
    ("test-failing", "passing"): "check-must-pass",
    ("passing", "broken"): "check-must-fail",
    ("broken", "passing"): "check-must-pass",
    ("broken", "test-failing"): "check-must-fail",
    ("no-test", "no-test"): "no-check",
    ("test-failing", "no-test"): "no-check",
    ("passing", "no-test"): "no-check",
    ("broken", "no-test"): "no-check",
}

FORBIDDEN_EDGES = tuple(
    (source.value, target.value)
    for source in ALL_STATES
    for target in ALL_STATES
    if (source.value, target.value) not in LEGAL_EDGES
)


def solo_document(state: str, evidence: dict[str, object] | None = None):
    """A one-feature list in `state`, for exercising a single edge."""
    entry: dict[str, object] = {
        "id": "solo",
        "category": "functional",
        "priority": 1,
        "description": "the only feature",
        "steps": ["do the thing"],
        "state": state,
    }
    if evidence is not None:
        entry["evidence"] = evidence
    return {"version": 2, "features": [entry]}


def evidence_for(state: str, digest: str = "sha256:cafe") -> dict[str, object] | None:
    """The evidence a feature in `state` would legitimately be carrying."""
    if state == "no-test":
        return None
    return {
        "kind": "command",
        "detail": "pytest tests/test_solo.py",
        "test_paths": ["tests/test_solo.py"],
        "digest": digest,
        "observed_at": "2026-08-18T12:00:00+00:00",
    }


class TestTransitionRequirement:
    @pytest.mark.parametrize(("edge", "requirement"), sorted(LEGAL_EDGES.items()))
    def test_allows_each_legal_edge_and_says_what_must_be_observed(
        self, edge, requirement
    ):
        source, target = edge
        assert transition_requirement(
            FeatureState(source), FeatureState(target)
        ) == TransitionRequirement(requirement)

    @pytest.mark.parametrize(("source", "target"), FORBIDDEN_EDGES)
    def test_forbids_every_edge_not_in_the_table(self, source, target):
        assert (
            transition_requirement(FeatureState(source), FeatureState(target)) is None
        )

    def test_forbids_reaching_passing_without_having_been_seen_failing(self):
        # The single edge this entire change exists to remove.
        assert (
            transition_requirement(FeatureState.NO_TEST, FeatureState.PASSING) is None
        )

    def test_forbids_breaking_something_that_never_worked(self):
        assert transition_requirement(FeatureState.NO_TEST, FeatureState.BROKEN) is None
        assert (
            transition_requirement(FeatureState.TEST_FAILING, FeatureState.BROKEN)
            is None
        )

    def test_names_the_legal_targets_from_a_state(self):
        assert set(legal_targets_from(FeatureState.NO_TEST)) == {
            FeatureState.NO_TEST,
            FeatureState.TEST_FAILING,
        }
        assert set(legal_targets_from(FeatureState.PASSING)) == {
            FeatureState.NO_TEST,
            FeatureState.BROKEN,
        }


class TestTheSessionBoundaryComparison:
    """Layer 3: what the loop catches after the session, whatever the hook saw.

    The hook fails open by design, so this comparison is what survives it. What
    it can assert is narrower than the transition table — see the note on
    `find_structural_changes` — so the re-hashing in
    `TestFindUnverifiableEvidence` is the other half of this layer.
    """

    @pytest.mark.parametrize("edge", sorted(LEGAL_EDGES))
    def test_accepts_every_legal_edge(self, edge):
        source, target = edge
        before = solo_document(source, evidence_for(source))
        # A different digest, so the same-state edges are a real change rather
        # than an identical document the check would skip.
        after = solo_document(target, evidence_for(target, digest="sha256:f00d"))
        assert find_structural_changes(before, after) == ()

    def test_does_not_pretend_to_enforce_the_edge_table_across_a_session(self):
        # One honest session goes no-test -> test-failing -> passing, so the
        # comparison sees no-test -> passing and has to accept it. Worse, every
        # state reaches no-test for free and no-test reaches everything, so
        # every before/after pair is the endpoint of some legal path: asking
        # "is this a legal edge" here would reject real work while forbidding
        # nothing. The table is enforced one call at a time in mark_feature.py,
        # where a single move is actually visible.
        before = solo_document("no-test")
        after = solo_document("passing", evidence_for("passing"))
        assert find_structural_changes(before, after) == ()

    def test_rejects_a_claim_whose_record_names_no_test_and_no_digest(self):
        # What a forged record looks like: the two fields that cost something
        # to fabricate are the ones naming real files and their hash.
        before = solo_document("no-test")
        after = solo_document("passing", {"kind": "command", "detail": "pytest -q"})
        reasons = find_structural_changes(before, after)
        assert any("nothing to re-check it against" in reason for reason in reasons)

    def test_rejects_rewriting_the_evidence_of_a_passing_feature_in_place(self):
        # The dodge the digest exists to stop, attempted at the file level:
        # re-digest a rewritten test against a feature already marked passing
        # and the red it should have gone through never happened. `passing` ->
        # `passing` is not an edge the sanctioned path can produce, so this is
        # caught even though the state is untouched.
        before = solo_document("passing", evidence_for("passing"))
        after = solo_document("passing", evidence_for("passing", digest="sha256:f00d"))
        reasons = find_structural_changes(before, after)
        assert any("evidence was rewritten" in reason for reason in reasons)

    def test_rejects_rewriting_the_evidence_of_a_broken_feature_in_place(self):
        before = solo_document("broken", evidence_for("broken"))
        after = solo_document("broken", evidence_for("broken", digest="sha256:f00d"))
        assert find_structural_changes(before, after) != ()

    def test_allows_re_recording_a_changed_test_while_still_failing(self):
        # The honest way to change a test: observe the new one red first. This
        # is the one same-state edge the table carries, and it has to stay open
        # or a session that improves its test has nowhere legal to go.
        before = solo_document("test-failing", evidence_for("test-failing"))
        after = solo_document(
            "test-failing", evidence_for("test-failing", digest="sha256:f00d")
        )
        assert find_structural_changes(before, after) == ()

    def test_allows_the_manual_hatch_to_reach_passing_directly(self):
        # A check no command can make cannot go through red, because there is no
        # test to watch fail. This is the only way past the ordering rule and it
        # is deliberately outside the table.
        before = solo_document("no-test")
        after = solo_document(
            "passing", {"kind": "manual", "detail": "colour judgement"}
        )
        assert find_structural_changes(before, after) == ()

    def test_leaves_a_feature_nobody_touched_alone(self):
        # A human may hand-edit the list; that is the intended escape hatch.
        # Judging untouched entries would blame every session afterwards and
        # leave the run permanently unable to start.
        hand_edited = solo_document("passing")
        assert find_structural_changes(hand_edited, copy.deepcopy(hand_edited)) == ()


class TestFindUnverifiableEvidence:
    """The backstop with teeth: every recorded claim re-hashed against the tree.

    The document comparison can only see that a record exists. This sees whether
    it is still true of the repository, which is what catches a record that was
    written by something other than mark_feature.py.
    """

    def _repo_with(self, tmp_path, body: str = "assert 1\n"):
        (tmp_path / "tests").mkdir(exist_ok=True)
        (tmp_path / "tests" / "test_solo.py").write_text(body, encoding="utf-8")
        return tmp_path

    def _features(self, state: str, digest: str, paths=("tests/test_solo.py",)):
        return parse_feature_list(
            solo_document(
                state,
                {
                    "kind": "command",
                    "detail": "pytest tests/test_solo.py",
                    "test_paths": list(paths),
                    "digest": digest,
                    "observed_at": "2026-08-18T12:00:00+00:00",
                },
            )
        )

    def test_accepts_a_record_whose_test_still_hashes_the_same(self, tmp_path):
        repo = self._repo_with(tmp_path)
        digest = digest_paths(["tests/test_solo.py"], repo)
        assert find_unverifiable_evidence(self._features("passing", digest), repo) == ()

    def test_catches_a_digest_that_was_invented(self, tmp_path):
        repo = self._repo_with(tmp_path)
        reasons = find_unverifiable_evidence(
            self._features("passing", "sha256:notreal"), repo
        )
        assert any("no longer hash to what was recorded" in r for r in reasons)

    def test_catches_a_test_rewritten_after_the_check_was_watched(self, tmp_path):
        repo = self._repo_with(tmp_path)
        digest = digest_paths(["tests/test_solo.py"], repo)
        (repo / "tests" / "test_solo.py").write_text("assert 2\n", encoding="utf-8")
        assert find_unverifiable_evidence(self._features("passing", digest), repo) != ()

    def test_catches_test_paths_the_repository_does_not_contain(self, tmp_path):
        repo = self._repo_with(tmp_path)
        reasons = find_unverifiable_evidence(
            self._features("passing", "sha256:x", paths=("tests/decoy.py",)), repo
        )
        assert any("does not have" in reason for reason in reasons)

    def test_checks_a_test_failing_record_too(self, tmp_path):
        # A recorded red is what a later green is measured against, so a red
        # whose test has vanished is worth catching at the moment it stops
        # being true rather than one session later.
        repo = self._repo_with(tmp_path)
        assert (
            find_unverifiable_evidence(
                self._features("test-failing", "sha256:notreal"), repo
            )
            != ()
        )

    def test_leaves_an_unstarted_feature_alone(self, tmp_path):
        features = parse_feature_list(solo_document("no-test"))
        assert find_unverifiable_evidence(features, tmp_path) == ()

    def test_leaves_a_manual_record_alone_because_it_has_nothing_to_hash(
        self, tmp_path
    ):
        features = parse_feature_list(
            solo_document("passing", {"kind": "manual", "detail": "colour judgement"})
        )
        assert find_unverifiable_evidence(features, tmp_path) == ()
