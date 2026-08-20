"""Contract tests for the one place the harness touches the claude CLI.

`build_argv` and `parse_session_result` are pure by design precisely so this
file can pin the command line and the result contract without spawning a
session. Assertions written from the contract before the implementation, per
`docs/project-workflow.md` §2.
"""

import json
from pathlib import Path

import pytest
from agent_harness.config import HarnessError, SessionRequest
from agent_harness.session import build_argv, parse_session_result

SESSION_ID = "11111111-2222-3333-4444-555555555555"


def make_request(**overrides: object) -> SessionRequest:
    defaults: dict[str, object] = {
        "prompt": "do the thing",
        "repo_path": Path("/tmp/target-repo"),
        "session_id": SESSION_ID,
        "model": "opus",
        "permission_mode": "bypassPermissions",
        "budget_usd": 10.0,
    }
    defaults.update(overrides)
    return SessionRequest(**defaults)  # type: ignore[arg-type]


class TestBuildArgv:
    def test_builds_the_full_command_for_a_minimal_request(self):
        assert build_argv(make_request()) == [
            "claude",
            "--print",
            "--output-format",
            "stream-json",
            # The CLI refuses stream-json in print mode without it, and the
            # stream is the only way to keep a transcript of what happened.
            "--verbose",
            "--model",
            "opus",
            "--permission-mode",
            "bypassPermissions",
            "--session-id",
            SESSION_ID,
            "--max-budget-usd",
            "10.0",
            "--effort",
            "high",
            "do the thing",
        ]

    def test_puts_the_prompt_last_so_it_is_never_read_as_a_flag_value(self):
        argv = build_argv(make_request(prompt="--not-a-flag"))
        assert argv[-1] == "--not-a-flag"

    def test_passes_a_settings_file_through_when_one_is_given(self):
        argv = build_argv(make_request(settings_path=Path("/tmp/settings.json")))
        assert "--settings" in argv
        assert argv[argv.index("--settings") + 1] == "/tmp/settings.json"

    def test_appends_the_system_prompt_suffix_when_one_is_given(self):
        argv = build_argv(make_request(system_prompt_suffix="never edit the list"))
        assert argv[argv.index("--append-system-prompt") + 1] == "never edit the list"

    def test_grants_access_to_every_extra_directory(self):
        argv = build_argv(
            make_request(extra_directories=(Path("/tmp/a"), Path("/tmp/b")))
        )
        assert argv.count("--add-dir") == 2
        assert "/tmp/a" in argv
        assert "/tmp/b" in argv

    def test_omits_optional_flags_entirely_rather_than_passing_empty_values(self):
        argv = build_argv(make_request())
        assert "--settings" not in argv
        assert "--append-system-prompt" not in argv
        assert "--add-dir" not in argv

    def test_omits_effort_entirely_when_none_was_asked_for(self):
        # No longer the default, but still reachable by constructing a request
        # with effort=None, and the omission is what makes that request mean
        # "inherit the CLI's level" rather than "send an empty flag".
        assert "--effort" not in build_argv(make_request(effort=None))

    def test_defaults_to_high_effort_so_two_unattended_runs_behave_the_same(self):
        # A run nobody is watching must not vary with whatever level an
        # interactive session happened to be configured with.
        argv = build_argv(make_request())
        assert argv[argv.index("--effort") + 1] == "high"

    def test_passes_the_effort_level_through_when_one_is_given(self):
        argv = build_argv(make_request(effort="high"))
        assert argv[argv.index("--effort") + 1] == "high"


# Field-for-field the shape a real CLI 2.1.234 run returns.
VALID_RESULT: dict[str, object] = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": SESSION_ID,
    "result": "ok",
    "num_turns": 1,
    "total_cost_usd": 0.0166898,
    "duration_ms": 2071,
    "permission_denials": [],
}


class TestParseSessionResult:
    def test_reads_every_field_the_harness_reports_on(self):
        result = parse_session_result(json.dumps(VALID_RESULT))
        assert result.session_id == SESSION_ID
        assert result.is_error is False
        assert result.subtype == "success"
        assert result.text == "ok"
        assert result.num_turns == 1
        assert result.cost_usd == pytest.approx(0.0166898)
        assert result.duration_ms == 2071
        assert result.permission_denials == ()

    def test_records_denied_tool_calls_by_name(self):
        payload = dict(VALID_RESULT)
        payload["permission_denials"] = [{"tool_name": "Edit"}, {"tool_name": "Write"}]
        assert parse_session_result(json.dumps(payload)).permission_denials == (
            "Edit",
            "Write",
        )

    def test_names_the_missing_key_rather_than_defaulting_it(self):
        payload = dict(VALID_RESULT)
        del payload["total_cost_usd"]
        with pytest.raises(HarnessError, match="total_cost_usd"):
            parse_session_result(json.dumps(payload))

    def test_refuses_output_that_is_not_json(self):
        with pytest.raises(HarnessError):
            parse_session_result("Usage: claude [options]")

    def test_refuses_a_json_document_that_is_not_a_result_object(self):
        with pytest.raises(HarnessError):
            parse_session_result(json.dumps({"type": "assistant", "message": {}}))

    def test_reads_the_thinking_tokens_the_session_spent(self):
        payload = dict(VALID_RESULT)
        payload["usage"] = {"output_tokens_details": {"thinking_tokens": 812}}
        assert parse_session_result(json.dumps(payload)).thinking_tokens == 812

    def test_reports_unknown_thinking_tokens_rather_than_guessing_zero(self):
        # Zero and "the CLI did not tell us" are different facts, and the second
        # one should not be reported as though a model did no thinking.
        assert parse_session_result(json.dumps(VALID_RESULT)).thinking_tokens is None

    def test_carries_an_errored_session_through_rather_than_raising(self):
        # A session that failed is a fact the loop has to act on, not an
        # exception: the run log still needs its cost and its message.
        payload = dict(VALID_RESULT)
        payload["is_error"] = True
        payload["subtype"] = "error_max_budget"
        result = parse_session_result(json.dumps(payload))
        assert result.is_error is True
        assert result.subtype == "error_max_budget"
