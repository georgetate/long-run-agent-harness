"""The `.agent-harness/` directory the harness keeps inside a target repository.

Everything the harness writes into someone else's repo goes under one directory,
for two reasons. Reuse: every repository the harness drives then looks identical
to it and to you, so the tool has nothing to discover. And blast radius: one
directory is one thing to delete when a run is abandoned.

Inside it, the split between committed and local is the load-bearing part. The
spec, the feature list, the progress log, init.sh and serve.sh are committed,
because the
next session reads them out of the repository and the article's whole premise is
that those artifacts are how one session speaks to the next. Generated settings
and session logs are not: they carry absolute paths from this machine and
transcripts nobody wants in a diff.
"""

from dataclasses import dataclass
from pathlib import Path

from agent_harness.config import (
    FEATURE_LIST_FILENAME,
    GENERATED_SETTINGS_FILENAME,
    INIT_SCRIPT_FILENAME,
    LOCAL_DIR_NAME,
    LOGS_DIR_NAME,
    PROGRESS_FILENAME,
    SERVE_SCRIPT_FILENAME,
    SPEC_FILENAME,
    WORKSPACE_DIR_NAME,
    HarnessError,
)

# What the workspace's own .gitignore holds. Written by the harness rather than
# asked of the user, so a target repository needs no setup to be driven.
WORKSPACE_GITIGNORE = """# Written by the agent harness.
# Session logs and machine-specific generated settings. The spec, feature list,
# progress log, and init.sh above them are committed on purpose: they are how
# one session tells the next what happened.
local/
"""


@dataclass(frozen=True)
class Workspace:
    """Every path the harness cares about inside one target repository."""

    repo_root: Path

    @property
    def root(self) -> Path:
        return self.repo_root / WORKSPACE_DIR_NAME

    @property
    def spec_path(self) -> Path:
        return self.root / SPEC_FILENAME

    @property
    def feature_list_path(self) -> Path:
        return self.root / FEATURE_LIST_FILENAME

    @property
    def progress_path(self) -> Path:
        return self.root / PROGRESS_FILENAME

    @property
    def init_script_path(self) -> Path:
        return self.root / INIT_SCRIPT_FILENAME

    @property
    def serve_script_path(self) -> Path:
        return self.root / SERVE_SCRIPT_FILENAME

    @property
    def local_dir(self) -> Path:
        return self.root / LOCAL_DIR_NAME

    @property
    def logs_dir(self) -> Path:
        return self.local_dir / LOGS_DIR_NAME

    @property
    def settings_path(self) -> Path:
        return self.local_dir / GENERATED_SETTINGS_FILENAME

    @property
    def exists(self) -> bool:
        return self.root.is_dir()


def find_workspace(start: Path) -> Workspace:
    """Walk up from `start` looking for a harness workspace.

    Used by the tools the agent runs, which are invoked from wherever the
    session happens to be rather than from the repository root.
    """
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / WORKSPACE_DIR_NAME / FEATURE_LIST_FILENAME).is_file():
            return Workspace(repo_root=candidate)
    raise HarnessError(
        f"No {WORKSPACE_DIR_NAME}/{FEATURE_LIST_FILENAME} found in {start} or any "
        "parent directory. Run `harness.py init` against the repository first."
    )


def prepare_local_directories(workspace: Workspace) -> None:
    """Create the gitignored side of the workspace and the ignore file itself."""
    workspace.logs_dir.mkdir(parents=True, exist_ok=True)
    gitignore_path = workspace.root / ".gitignore"
    if not gitignore_path.is_file():
        gitignore_path.write_text(WORKSPACE_GITIGNORE, encoding="utf-8")


def missing_initializer_artifacts(workspace: Workspace) -> tuple[str, ...]:
    """Name the artifacts the initializer session was supposed to leave behind.

    The initializer is a model, so "it said it was done" is not evidence. The
    run refuses to move on to coding sessions until the files it was asked for
    actually exist.

    `serve.sh` is on the list even for a project with nothing to serve. A
    library or a pipeline still gets the file; it exits saying so. Requiring it
    unconditionally keeps this check a one-liner, and letting it be an honest
    no-op keeps it truthful — which is a better trade than a per-project
    exception nobody can see the shape of from here.
    """
    expected = {
        SPEC_FILENAME: workspace.spec_path,
        FEATURE_LIST_FILENAME: workspace.feature_list_path,
        PROGRESS_FILENAME: workspace.progress_path,
        INIT_SCRIPT_FILENAME: workspace.init_script_path,
        SERVE_SCRIPT_FILENAME: workspace.serve_script_path,
    }
    return tuple(name for name, path in expected.items() if not path.is_file())
