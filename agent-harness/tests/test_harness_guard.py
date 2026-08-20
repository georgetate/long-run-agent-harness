"""Contract tests for the PreToolUse guard.

The guard is the mechanism that turns "do not edit the feature list" from a
sentence in a prompt into something the session cannot do. Its decision
function is pure so these cases can be pinned without running a session.
Assertions written from the contract before the implementation, per
`docs/project-workflow.md` §2.
"""

from agent_harness.guard import decide

MARK_FEATURE_COMMAND = "/usr/bin/python3 /harness/tools/mark_feature.py new-chat pass"


def always_exists(_path: str) -> bool:
    """Stand in for a workspace whose protected files are already written."""
    return True


def never_exists(_path: str) -> bool:
    """Stand in for the initializer session, before those files exist."""
    return False


def edit_event(file_path: str, tool_name: str = "Edit") -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path, "old_string": "a", "new_string": "b"},
    }


def bash_event(command: str) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


class TestProtectedFileEdits:
    def test_blocks_a_direct_edit_of_the_feature_list(self):
        decision = decide(
            edit_event("/repo/.agent-harness/feature_list.json"), always_exists
        )
        assert decision.allowed is False
        assert "mark_feature" in decision.reason

    def test_blocks_a_write_over_the_feature_list(self):
        decision = decide(
            edit_event("/repo/.agent-harness/feature_list.json", "Write"), always_exists
        )
        assert decision.allowed is False

    def test_blocks_an_edit_of_the_spec_the_feature_list_came_from(self):
        decision = decide(edit_event("/repo/.agent-harness/spec.md"), always_exists)
        assert decision.allowed is False

    def test_blocks_a_relative_path_to_the_feature_list(self):
        decision = decide(edit_event(".agent-harness/feature_list.json"), always_exists)
        assert decision.allowed is False

    def test_allows_edits_to_ordinary_project_files(self):
        assert decide(edit_event("/repo/src/app.py"), always_exists).allowed is True

    def test_allows_the_progress_file_which_sessions_are_meant_to_append_to(self):
        assert (
            decide(
                edit_event("/repo/.agent-harness/progress.md"), always_exists
            ).allowed
            is True
        )

    def test_allows_a_same_named_file_outside_the_harness_workspace(self):
        # A target repository may have its own unrelated feature_list.json, and
        # blocking it would break a project the harness is supposed to serve.
        assert (
            decide(edit_event("/repo/src/feature_list.json"), always_exists).allowed
            is True
        )


class TestBashCommands:
    def test_allows_reading_the_feature_list(self):
        command = "cat .agent-harness/feature_list.json"
        assert decide(bash_event(command), always_exists).allowed is True

    def test_allows_grepping_the_feature_list(self):
        command = "grep -c 'passes' .agent-harness/feature_list.json"
        assert decide(bash_event(command), always_exists).allowed is True

    def test_blocks_an_in_place_edit_of_the_feature_list(self):
        command = "sed -i '' 's/false/true/g' .agent-harness/feature_list.json"
        assert decide(bash_event(command), always_exists).allowed is False

    def test_blocks_a_redirect_over_the_feature_list(self):
        command = "echo '{}' > .agent-harness/feature_list.json"
        assert decide(bash_event(command), always_exists).allowed is False

    def test_blocks_deleting_the_feature_list(self):
        command = "rm .agent-harness/feature_list.json"
        assert decide(bash_event(command), always_exists).allowed is False

    def test_allows_the_sanctioned_write_path(self):
        assert decide(bash_event(MARK_FEATURE_COMMAND), always_exists).allowed is True

    def test_allows_ordinary_commands(self):
        assert decide(bash_event("npm test"), always_exists).allowed is True


class TestMalformedEvents:
    def test_allows_tools_it_has_no_opinion_about(self):
        event = edit_event("/repo/anything", tool_name="Read")
        assert decide(event, always_exists).allowed is True

    def test_fails_open_on_an_event_it_cannot_read(self):
        # A guard that crashes closed would break every tool call in the
        # session. The loop re-checks the feature list after each session, so
        # failing open here loses a first line of defence rather than the only
        # one.
        decision = decide(
            {"tool_name": "Edit", "tool_input": "not a dict"}, always_exists
        )
        assert decision.allowed is True


class TestCreatingTheWorkspaceForTheFirstTime:
    """The initializer session has to be able to write the files it is asked for.

    Found by running a real initializer session: the guard blocked it from
    creating feature_list.json, and rather than stopping, the model worked
    around the block and finished the session without the file. Hence the rule
    the guard actually wants: it protects a file that exists, and never stands
    between the initializer and a file that does not.
    """

    def test_allows_creating_the_feature_list_when_there_is_none(self):
        event = edit_event("/repo/.agent-harness/feature_list.json", "Write")
        assert decide(event, never_exists).allowed is True

    def test_allows_writing_the_feature_list_from_a_shell_heredoc_when_there_is_none(
        self,
    ):
        command = "cat > .agent-harness/feature_list.json <<'JSON'\n{}\nJSON"
        assert decide(bash_event(command), never_exists).allowed is True

    def test_still_blocks_a_rewrite_once_the_file_exists(self):
        event = edit_event("/repo/.agent-harness/feature_list.json", "Write")
        assert decide(event, always_exists).allowed is False


class TestCommandsThatOnlyLookLikeWrites:
    """Found in a real session: `git add` was blocked by the substring "dd ".

    A guard that blocks the commit is worse than no guard, because committing
    the flipped status is exactly what the session was told to do. These cases
    pin the difference between a command that changes the file's contents and
    one that merely names it.
    """

    def test_allows_staging_and_committing_the_feature_list(self):
        command = (
            "git add .agent-harness/feature_list.json .agent-harness/progress.md "
            "&& git commit -m 'record verification'"
        )
        assert decide(bash_event(command), always_exists).allowed is True

    def test_allows_a_commit_message_containing_an_arrow(self):
        command = "git commit -m 'feature_list.json: false -> true'"
        assert decide(bash_event(command), always_exists).allowed is True

    def test_allows_diffing_the_feature_list(self):
        command = "git diff HEAD~1 -- .agent-harness/feature_list.json"
        assert decide(bash_event(command), always_exists).allowed is True

    def test_blocks_reverting_the_feature_list_to_undo_a_recorded_result(self):
        command = "git checkout -- .agent-harness/feature_list.json"
        assert decide(bash_event(command), always_exists).allowed is False

    def test_blocks_a_mutating_segment_hidden_behind_a_harmless_one(self):
        command = "echo starting && sed -i '' 's/false/true/' .agent-harness/feature_list.json"
        assert decide(bash_event(command), always_exists).allowed is False

    def test_blocks_a_mutating_segment_at_the_end_of_a_pipeline(self):
        command = "cat other.json | tee .agent-harness/feature_list.json"
        assert decide(bash_event(command), always_exists).allowed is False


class TestBypassesFoundByReview:
    """Bypass shapes found by an adversarial review of a real run's guard.

    Each of these was demonstrated against the hook before being fixed, and
    none of them is exotic: they are ordinary shell syntax that happened to
    fall outside the first version's patterns.
    """

    def test_blocks_a_relative_write_from_inside_the_workspace_directory(self):
        # The session's cwd is the workspace itself, so the path never contains
        # the directory name the guard used to look for.
        event = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "cwd": "/repo/.agent-harness",
            "tool_input": {"file_path": "feature_list.json", "content": "{}"},
        }
        assert decide(event, always_exists).allowed is False

    def test_a_comment_naming_the_sanctioned_script_is_not_a_sanction(self):
        command = "cp /tmp/fake.json .agent-harness/feature_list.json # mark_feature.py"
        assert decide(bash_event(command), always_exists).allowed is False

    def test_an_argument_naming_the_sanctioned_script_is_not_a_sanction(self):
        command = "echo mark_feature.py > .agent-harness/feature_list.json"
        assert decide(bash_event(command), always_exists).allowed is False

    def test_blocks_a_perl_in_place_edit(self):
        command = "perl -pi -e 's/false/true/' .agent-harness/feature_list.json"
        assert decide(bash_event(command), always_exists).allowed is False

    def test_blocks_a_gawk_in_place_edit(self):
        command = (
            "gawk -i inplace '{gsub(/false/, \"true\")}1' "
            ".agent-harness/feature_list.json"
        )
        assert decide(bash_event(command), always_exists).allowed is False

    def test_blocks_an_fd_numbered_redirect_into_the_feature_list(self):
        command = "echo '{}' 1>.agent-harness/feature_list.json"
        assert decide(bash_event(command), always_exists).allowed is False

    def test_still_allows_a_read_that_silences_stderr(self):
        # The fd-redirect rule is by target, not by shape: 2>/dev/null on a
        # read is everyday syntax and blocking it would get the guard ignored.
        command = "grep passes .agent-harness/feature_list.json 2>/dev/null"
        assert decide(bash_event(command), always_exists).allowed is True

    def test_still_sanctions_the_real_mark_command(self):
        command = (
            "/usr/bin/python3 /harness/tools/mark_feature.py new-chat pass "
            "--test 'pytest -q'"
        )
        assert decide(bash_event(command), always_exists).allowed is True

    def test_sanctions_the_mark_command_with_a_quoted_interpreter_path(self):
        # What _mark_feature_command() renders when the harness lives under a
        # path with a space in it.
        command = (
            "'/my venv/bin/python3.14' '/h arness/tools/mark_feature.py' "
            "new-chat pass --test 'pytest -q'"
        )
        assert decide(bash_event(command), always_exists).allowed is True


class TestGitSubcommandMatching:
    """The restore check reads the subcommand token, not the whole segment."""

    def test_allows_a_commit_message_containing_the_word_restore(self):
        command = (
            "git commit -am 'fix: restore the feature_list.json status after crash'"
        )
        assert decide(bash_event(command), always_exists).allowed is True

    def test_still_blocks_git_checkout_of_the_feature_list(self):
        command = "git checkout -- .agent-harness/feature_list.json"
        assert decide(bash_event(command), always_exists).allowed is False

    def test_still_blocks_git_restore_of_the_feature_list(self):
        command = "git restore .agent-harness/feature_list.json"
        assert decide(bash_event(command), always_exists).allowed is False

    def test_blocks_git_apply_aimed_at_the_feature_list(self):
        command = "git apply --include=.agent-harness/feature_list.json fix.patch"
        assert decide(bash_event(command), always_exists).allowed is False

    def test_skips_global_options_to_find_the_subcommand(self):
        command = "git -C /repo checkout .agent-harness/feature_list.json"
        assert decide(bash_event(command), always_exists).allowed is False


class TestPentestRegressions:
    """Holes a red-team pass drove through the guard after the first fixes.

    Each was reproduced end to end before being closed here; the pure-decision
    form is pinned so a later refactor cannot quietly reopen it.
    """

    def test_blocks_a_command_substitution_rider_on_the_sanctioned_script(self):
        # mark_feature.py is exempt, but a `$(...)` rider travels inside the same
        # segment (the separators do not split on it) and does the real write.
        command = (
            "/usr/bin/python3 /harness/tools/mark_feature.py feat-a pass "
            "--test true $(cp /tmp/forged.json .agent-harness/feature_list.json)"
        )
        assert decide(bash_event(command), always_exists).allowed is False

    def test_blocks_a_backtick_rider_on_the_sanctioned_script(self):
        command = (
            "python3 /harness/tools/mark_feature.py feat-a pass "
            "--test `cp /tmp/forged.json .agent-harness/feature_list.json`"
        )
        assert decide(bash_event(command), always_exists).allowed is False

    def test_blocks_a_node_inline_write(self):
        command = (
            'node -e \'require("fs").writeFileSync'
            '(".agent-harness/feature_list.json","x")\''
        )
        assert decide(bash_event(command), always_exists).allowed is False

    def test_blocks_a_ruby_inline_write(self):
        command = 'ruby -e \'File.write(".agent-harness/feature_list.json", "x")\''
        assert decide(bash_event(command), always_exists).allowed is False

    def test_blocks_a_php_inline_write(self):
        command = (
            'php -r \'file_put_contents(".agent-harness/feature_list.json", "x");\''
        )
        assert decide(bash_event(command), always_exists).allowed is False

    def test_blocks_creating_a_symlink_that_names_the_feature_list(self):
        command = "ln -s .agent-harness/feature_list.json evil-link"
        assert decide(bash_event(command), always_exists).allowed is False

    def test_blocks_a_write_through_a_symlink_to_the_feature_list(self):
        # The textual path is an innocent name; only resolving the symlink shows
        # it lands on the protected file.
        def resolves_to_feature_list(_path: str) -> str:
            return "/repo/.agent-harness/feature_list.json"

        decision = decide(
            edit_event("/repo/evil-link", "Write"),
            always_exists,
            resolve_path=resolves_to_feature_list,
        )
        assert decision.allowed is False

    def test_still_allows_a_plain_sanctioned_command_with_no_substitution(self):
        assert decide(bash_event(MARK_FEATURE_COMMAND), always_exists).allowed is True

    def test_still_allows_reading_the_list_through_a_runtime(self):
        # node/ruby matched by their inline-eval flag, so a runtime merely named
        # alongside the file for a read is not swept up.
        command = "node scripts/report.js .agent-harness/feature_list.json"
        assert decide(bash_event(command), always_exists).allowed is True
