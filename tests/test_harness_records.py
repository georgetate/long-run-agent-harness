"""Contract tests for how sessions are numbered and how their output is recorded.

Both came out of reading a real log directory. Sessions were numbered per `run`
invocation, so three runs over one repository produced two `session-001`s and
two `session-002`s and read as though sessions had gone missing. And the only
thing kept per session was the final result object, which is the one part of a
session that does not help when you are trying to work out what it did.

Assertions written from the contract before the implementation, per
`docs/project-workflow.md` §2.
"""

import json

import pytest
from agent_harness.config import HarnessError
from agent_harness.records import next_session_number, render_transcript
from agent_harness.session import parse_stream_result

SESSION_ID = "11111111-2222-3333-4444-555555555555"


class TestNextSessionNumber:
    def test_starts_at_one_in_an_empty_log_directory(self):
        assert next_session_number([]) == 1

    def test_ignores_the_initializer_and_anything_else_in_the_directory(self):
        existing = [f"init-{SESSION_ID}.json", "notes.txt", "session-.json"]
        assert next_session_number(existing) == 1

    def test_continues_from_the_highest_number_already_recorded(self):
        existing = [
            f"session-001-{SESSION_ID}.json",
            f"session-002-{SESSION_ID}.log",
            f"session-002-{SESSION_ID}.jsonl",
        ]
        assert next_session_number(existing) == 3

    def test_continues_past_a_gap_rather_than_filling_it(self):
        # A gap means a run was interrupted. Reusing the number would overwrite
        # the record of whatever ran under it.
        existing = [f"session-001-{SESSION_ID}.json", f"session-007-{SESSION_ID}.json"]
        assert next_session_number(existing) == 8

    def test_survives_three_digits(self):
        assert next_session_number([f"session-137-{SESSION_ID}.json"]) == 138


RESULT_EVENT: dict[str, object] = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": SESSION_ID,
    "result": "done",
    "num_turns": 4,
    "total_cost_usd": 0.25,
    "duration_ms": 9000,
    "permission_denials": [],
}

STREAM = "\n".join(
    [
        json.dumps({"type": "system", "subtype": "init", "session_id": SESSION_ID}),
        json.dumps({"type": "assistant", "message": {"content": []}}),
        json.dumps(RESULT_EVENT),
        "",
    ]
)


class TestParseStreamResult:
    def test_finds_the_result_event_at_the_end_of_the_stream(self):
        result = parse_stream_result(STREAM)
        assert result.session_id == SESSION_ID
        assert result.num_turns == 4

    def test_ignores_every_event_before_it(self):
        assert parse_stream_result(STREAM).subtype == "success"

    def test_tolerates_a_trailing_partial_line(self):
        # A killed session can leave half a line behind. The result event, if it
        # arrived at all, is still the last complete one.
        assert parse_stream_result(STREAM + '{"type": "assis').is_error is False

    def test_says_so_when_the_stream_never_produced_a_result(self):
        stream = json.dumps({"type": "system", "subtype": "init"})
        with pytest.raises(HarnessError, match="result"):
            parse_stream_result(stream)

    def test_says_so_when_there_is_no_output_at_all(self):
        with pytest.raises(HarnessError):
            parse_stream_result("")


EVENTS: list[object] = [
    {"type": "system", "subtype": "init", "session_id": SESSION_ID, "cwd": "/repo"},
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "I should check the tests first."},
                {"type": "text", "text": "Checking the suite."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Bash",
                    "input": {"command": "pytest -q"},
                },
            ]
        },
    },
    {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "3 passed",
                }
            ]
        },
    },
    RESULT_EVENT,
]


class TestRenderTranscript:
    def test_includes_the_model_reasoning(self):
        assert "I should check the tests first." in render_transcript(EVENTS)

    def test_includes_what_the_model_said(self):
        assert "Checking the suite." in render_transcript(EVENTS)

    def test_names_each_tool_call_and_shows_its_input(self):
        transcript = render_transcript(EVENTS)
        assert "Bash" in transcript
        assert "pytest -q" in transcript

    def test_includes_what_the_tool_returned(self):
        assert "3 passed" in render_transcript(EVENTS)

    def test_truncates_a_huge_tool_result_rather_than_pasting_a_whole_file(self):
        events = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t",
                            "content": "x" * 50_000,
                        }
                    ]
                },
            }
        ]
        transcript = render_transcript(events)
        assert len(transcript) < 10_000
        assert "truncated" in transcript

    def test_produces_something_readable_from_an_empty_session(self):
        assert isinstance(render_transcript([]), str)

    def test_never_raises_on_an_event_shape_it_does_not_recognise(self):
        # A transcript is a debugging aid. Failing to render one must never be
        # the reason a run stops.
        weird = [{"type": "assistant", "message": "not a dict"}, {"nope": True}, 7]
        assert isinstance(render_transcript(weird), str)
