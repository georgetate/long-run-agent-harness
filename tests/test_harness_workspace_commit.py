"""commit_workspace must commit only the harness workspace, never project code.

The loop calls commit_workspace on its early-stop paths (error, timeout, stall),
which are exactly the moments a session is most likely to have left files staged
in the index. A session that ran `git add -A` before crashing would, without a
pathspec on the commit, get its project files swept into the harness's own
"recording workspace" commit -- breaking the function's stated contract and
blinding the next run's uncommitted-changes preflight.
"""

from pathlib import Path

from agent_harness.loop import commit_workspace
from agent_harness.workspace import Workspace
from conftest import git_in


def _make_workspace(repo: Path) -> Workspace:
    workspace = Workspace(repo_root=repo)
    workspace.root.mkdir(parents=True, exist_ok=True)
    return workspace


def test_commit_workspace_ignores_a_staged_project_file(target_repo: Path) -> None:
    workspace = _make_workspace(target_repo)

    # The harness has an uncommitted workspace file to record...
    (workspace.root / "progress.md").write_text("session ran\n", encoding="utf-8")

    # ...while the session left a project file staged, as `git add -A` would.
    (target_repo / "README.md").write_text(
        "tampered by the session\n", encoding="utf-8"
    )
    git_in(target_repo, "add", "README.md")

    did_commit = commit_workspace(workspace, "chore: recording harness workspace")
    assert did_commit is True

    # The new commit carries the workspace file but NOT the project file.
    committed_files = git_in(
        target_repo, "show", "--name-only", "--pretty=format:", "HEAD"
    ).stdout.split()
    assert ".agent-harness/progress.md" in committed_files
    assert "README.md" not in committed_files

    # The project change is still staged and uncommitted for the loop to see.
    still_staged = git_in(target_repo, "diff", "--cached", "--name-only").stdout.split()
    assert "README.md" in still_staged


def test_commit_workspace_is_a_noop_when_only_project_files_are_staged(
    target_repo: Path,
) -> None:
    workspace = _make_workspace(target_repo)

    # Nothing new under the workspace, but a project file is staged.
    (target_repo / "README.md").write_text("tampered\n", encoding="utf-8")
    git_in(target_repo, "add", "README.md")

    head_before = git_in(target_repo, "rev-parse", "HEAD").stdout.strip()
    did_commit = commit_workspace(workspace, "chore: recording harness workspace")
    head_after = git_in(target_repo, "rev-parse", "HEAD").stdout.strip()

    # A staged project file must not trick the harness into making a commit.
    assert did_commit is False
    assert head_before == head_after
