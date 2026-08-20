"""End-to-end tests for the PreToolUse hook's stdin / exit-code contract.

The guard's decision function is unit-tested in test_harness_guard.py; this
file drives `tools/protect_feature_list.py` the way the claude CLI does — a
JSON event on stdin, a verdict as an exit code, the denial reason on stderr —
against a real workspace on disk, because the wrapper and the existence checks
are exactly the parts a pure unit test cannot vouch for.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import HARNESS_ROOT

HOOK = HARNESS_ROOT / "tools" / "protect_feature_list.py"

ALLOW = 0
BLOCK = 2


def run_hook(event: object, *arguments: str) -> subprocess.CompletedProcess[str]:
    raw = event if isinstance(event, str) else json.dumps(event)
    return subprocess.run(
        [sys.executable, str(HOOK), *arguments],
        input=raw,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture()
def workspace_repo(tmp_path: Path) -> Path:
    """A repository whose protected files really exist on disk."""
    repo = tmp_path / "repo"
    workspace = repo / ".agent-harness"
    workspace.mkdir(parents=True)
    (workspace / "feature_list.json").write_text('{"features": []}\n')
    (workspace / "spec.md").write_text("# spec\n")
    return repo


def bash_event(command: str, cwd: Path = Path("/tmp")) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "cwd": str(cwd),
        "tool_input": {"command": command},
    }


def write_event(file_path: str, cwd: Path) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "cwd": str(cwd),
        "tool_input": {"file_path": file_path, "content": "{}"},
    }


class TestFileToolEvents:
    def test_blocks_writing_the_existing_feature_list(self, workspace_repo):
        event = write_event(
            str(workspace_repo / ".agent-harness" / "feature_list.json"),
            workspace_repo,
        )
        completed = run_hook(event)
        assert completed.returncode == BLOCK
        assert "mark_feature" in completed.stderr

    def test_blocks_a_relative_write_from_inside_the_workspace(self, workspace_repo):
        # The session's cwd is the workspace itself; the raw path never names
        # the directory. This is the bypass the review demonstrated.
        event = write_event("feature_list.json", workspace_repo / ".agent-harness")
        assert run_hook(event).returncode == BLOCK

    def test_allows_creating_the_feature_list_when_none_exists(self, tmp_path):
        empty_repo = tmp_path / "empty"
        empty_repo.mkdir()
        event = write_event(
            str(empty_repo / ".agent-harness" / "feature_list.json"), empty_repo
        )
        assert run_hook(event).returncode == ALLOW

    def test_allows_ordinary_project_files(self, workspace_repo):
        event = write_event(str(workspace_repo / "src" / "app.py"), workspace_repo)
        assert run_hook(event).returncode == ALLOW


class TestBashEvents:
    def test_blocks_a_sed_in_place_edit(self, workspace_repo):
        command = "sed -i '' 's/false/true/' .agent-harness/feature_list.json"
        assert run_hook(bash_event(command, workspace_repo)).returncode == BLOCK

    def test_blocks_a_comment_that_name_drops_the_sanctioned_script(
        self, workspace_repo
    ):
        command = "cp /tmp/fake.json .agent-harness/feature_list.json # mark_feature.py"
        assert run_hook(bash_event(command, workspace_repo)).returncode == BLOCK

    def test_blocks_a_perl_in_place_edit(self, workspace_repo):
        command = "perl -pi -e 's/false/true/' .agent-harness/feature_list.json"
        assert run_hook(bash_event(command, workspace_repo)).returncode == BLOCK

    def test_blocks_an_fd_numbered_redirect(self, workspace_repo):
        command = "echo '{}' 1>.agent-harness/feature_list.json"
        assert run_hook(bash_event(command, workspace_repo)).returncode == BLOCK

    def test_allows_the_sanctioned_mark_command(self, workspace_repo):
        command = (
            f"{sys.executable} {HARNESS_ROOT / 'tools' / 'mark_feature.py'} "
            "feat-a pass --test 'pytest -q'"
        )
        assert run_hook(bash_event(command, workspace_repo)).returncode == ALLOW

    def test_allows_a_commit_message_containing_restore(self, workspace_repo):
        command = "git commit -am 'fix: restore the feature_list.json status'"
        assert run_hook(bash_event(command, workspace_repo)).returncode == ALLOW

    def test_blocks_git_checkout_of_the_feature_list(self, workspace_repo):
        command = "git checkout -- .agent-harness/feature_list.json"
        assert run_hook(bash_event(command, workspace_repo)).returncode == BLOCK

    def test_allows_commands_that_never_mention_protected_files(self, workspace_repo):
        assert run_hook(bash_event("npm test", workspace_repo)).returncode == ALLOW


class TestContractEdges:
    def test_fails_open_on_garbage_stdin(self):
        assert run_hook("this is not json").returncode == ALLOW

    def test_fails_open_on_empty_stdin(self):
        assert run_hook("").returncode == ALLOW


class TestPentestBypasses:
    """The red-team bypasses, driven through the real hook against a real disk."""

    def test_blocks_a_command_substitution_rider(self, workspace_repo):
        command = (
            f"{sys.executable} {HARNESS_ROOT / 'tools' / 'mark_feature.py'} "
            "feat-a pass --test true "
            "$(cp /tmp/forged.json .agent-harness/feature_list.json)"
        )
        assert run_hook(bash_event(command, workspace_repo)).returncode == BLOCK

    def test_blocks_a_node_inline_write(self, workspace_repo):
        command = (
            'node -e \'require("fs").writeFileSync'
            '(".agent-harness/feature_list.json","x")\''
        )
        assert run_hook(bash_event(command, workspace_repo)).returncode == BLOCK

    def test_blocks_a_ruby_inline_write(self, workspace_repo):
        command = 'ruby -e \'File.write(".agent-harness/feature_list.json","x")\''
        assert run_hook(bash_event(command, workspace_repo)).returncode == BLOCK

    def test_blocks_a_write_through_a_symlink(self, workspace_repo):
        # A session points an innocent name at the protected file and writes the
        # name. Only resolving the link shows where the bytes land.
        link = workspace_repo / "evil-link"
        link.symlink_to(workspace_repo / ".agent-harness" / "feature_list.json")
        event = write_event(str(link), workspace_repo)
        assert run_hook(event).returncode == BLOCK

    def test_blocks_creating_a_symlink_that_names_the_feature_list(
        self, workspace_repo
    ):
        command = "ln -s .agent-harness/feature_list.json evil-link"
        assert run_hook(bash_event(command, workspace_repo)).returncode == BLOCK
