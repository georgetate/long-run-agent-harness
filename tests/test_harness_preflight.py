"""Contract tests for the preflight checks.

These assertions were written against the documented contract before the
implementation existed, per `docs/project-workflow.md` §2: a test generated
alongside the code it checks agrees with the code's misunderstandings.
"""

import pytest
from agent_harness.config import HarnessError
from agent_harness.preflight import (
    check_cli_version,
    find_missing_flags,
    parse_cli_version,
    summarize_authentication,
)


class TestParseCliVersion:
    def test_reads_the_version_out_of_the_real_cli_banner(self):
        assert parse_cli_version("2.1.234 (Claude Code)") == (2, 1, 234)

    def test_tolerates_surrounding_whitespace_and_newlines(self):
        assert parse_cli_version("\n  2.1.234 (Claude Code)\n") == (2, 1, 234)

    def test_reads_a_bare_version_string(self):
        assert parse_cli_version("3.0.1") == (3, 0, 1)

    def test_refuses_output_with_no_version_in_it(self):
        with pytest.raises(HarnessError):
            parse_cli_version("command not found")


class TestCheckCliVersion:
    def test_accepts_the_tested_version_without_comment(self):
        assert (
            check_cli_version((2, 1, 234), tested=(2, 1, 234), minimum=(2, 0, 0))
            is None
        )

    def test_rejects_a_version_below_the_minimum(self):
        with pytest.raises(HarnessError):
            check_cli_version((1, 9, 9), tested=(2, 1, 234), minimum=(2, 0, 0))

    def test_warns_but_allows_a_newer_version_than_the_one_tested(self):
        warning = check_cli_version((2, 2, 0), tested=(2, 1, 234), minimum=(2, 0, 0))
        assert warning is not None
        assert "2.2.0" in warning

    def test_accepts_a_version_between_the_minimum_and_the_tested_one(self):
        assert (
            check_cli_version((2, 0, 5), tested=(2, 1, 234), minimum=(2, 0, 0)) is None
        )


class TestFindMissingFlags:
    HELP_TEXT = """
      -p, --print                     Print response and exit
      --output-format <format>        Output format
      --model <model>                 Model for the current session
    """

    def test_reports_nothing_when_every_flag_is_present(self):
        required = ("--print", "--output-format", "--model")
        assert find_missing_flags(self.HELP_TEXT, required) == ()

    def test_names_each_flag_the_cli_no_longer_documents(self):
        required = ("--print", "--session-id", "--max-budget-usd")
        assert find_missing_flags(self.HELP_TEXT, required) == (
            "--session-id",
            "--max-budget-usd",
        )

    def test_does_not_count_a_flag_that_only_appears_as_a_prefix_of_another(self):
        # "--output-format" must not satisfy a requirement for "--output".
        assert find_missing_flags(self.HELP_TEXT, ("--output",)) == ("--output",)


# Exactly what a real `claude auth status` returned on CLI 2.1.234.
CLAUDE_AI_STATUS: dict[str, object] = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "subscriptionType": "max",
}


class TestSummarizeAuthentication:
    """How a session will be paid for is a fact worth printing before a long run.

    The harness never chooses an authentication method: it inherits whatever the
    CLI and the environment already decided. That makes it worth reporting, since
    the difference between subscription usage and metered API billing is invisible
    from the output of a run and is not something to discover afterwards.
    """

    def test_reports_a_subscription_login(self):
        summary = summarize_authentication(CLAUDE_AI_STATUS, api_key=None)
        assert "subscription" in summary
        assert "max" in summary

    def test_an_api_key_in_the_environment_wins_and_says_so(self):
        # Sessions inherit the environment, so an exported key silently changes
        # who pays. It outranks whatever the CLI is logged in as.
        summary = summarize_authentication(CLAUDE_AI_STATUS, api_key="sk-ant-x")
        assert "ANTHROPIC_API_KEY" in summary

    def test_never_echoes_the_account_email(self):
        status = dict(CLAUDE_AI_STATUS, email="someone@example.com")
        assert "example.com" not in summarize_authentication(status, api_key=None)

    def test_says_so_plainly_when_the_status_cannot_be_read(self):
        assert "could not" in summarize_authentication("not json", api_key=None)

    def test_reports_an_unrecognised_method_verbatim_rather_than_guessing(self):
        status = {"loggedIn": True, "authMethod": "bedrock", "apiProvider": "bedrock"}
        summary = summarize_authentication(status, api_key=None)
        assert "bedrock" in summary
