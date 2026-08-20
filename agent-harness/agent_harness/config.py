"""Constants and value types shared by every part of the harness.

This module is deliberately data rather than behavior. The harness is a thin
orchestrator around the `claude` CLI, so the things most likely to break under
a CLI upgrade are the flag names it passes and the JSON keys it reads back.
Naming them in exactly one place turns that breakage into a single edit, and
lets `preflight.py` assert every one of them before a run starts rather than
discovering the problem half an hour into an unattended session.
"""

from dataclasses import dataclass, field
from pathlib import Path

# ------ WORKSPACE LAYOUT ------
# Everything the harness writes into a target repository lives under one
# directory, so a repo being driven by the harness looks identical to every
# other one and nothing is scattered at the repo root.

WORKSPACE_DIR_NAME = ".agent-harness"

SPEC_FILENAME = "spec.md"
FEATURE_LIST_FILENAME = "feature_list.json"
PROGRESS_FILENAME = "progress.md"
INIT_SCRIPT_FILENAME = "init.sh"

# The way in to a *running* instance, as distinct from a build. `init.sh` brings
# the project up and runs the suite, which for a web application means the tests
# ran and exited, leaving nothing listening. A session told to verify a feature
# through its real interface has to be able to start the thing and be told
# where it is.
SERVE_SCRIPT_FILENAME = "serve.sh"

# Committed artifacts sit at the workspace root; anything machine-specific or
# regenerable sits under `local/`, which the workspace's own .gitignore excludes.
# The split exists so the agent's commits carry the state that matters to the
# next session without also carrying absolute paths and session transcripts.
LOCAL_DIR_NAME = "local"
LOGS_DIR_NAME = "logs"
GENERATED_SETTINGS_FILENAME = "settings.generated.json"

# ------ CLI CONTRACT ------
# The `claude` CLI is an external dependency with a moving surface. These pins
# exist so a flag rename shows up as a startup error naming the flag, instead of
# as a session that silently behaves differently than the prompt intends.

# The version this harness was written and probed against. A newer CLI only
# warns, because refusing to run on every upgrade would make the tool useless.
TESTED_CLI_VERSION = (2, 1, 234)

# Below this, refuse outright. Older CLIs predate flags the harness depends on.
MINIMUM_CLI_VERSION = (2, 0, 0)

# Every flag the harness puts on a command line. Preflight asserts each one is
# still present in `claude --help` before the first session starts.
REQUIRED_CLI_FLAGS = (
    "--print",
    "--output-format",
    # stream-json is refused in print mode without --verbose, and the stream is
    # the only source of a transcript, so both are load-bearing.
    "--verbose",
    "--effort",
    "--model",
    "--permission-mode",
    "--append-system-prompt",
    "--settings",
    "--session-id",
    "--max-budget-usd",
    "--add-dir",
)

# Keys the harness reads out of a `--output-format json` result. Verified
# against a real CLI 2.1.234 run; a missing key is a hard error rather than a
# default, because every one of them feeds either a stop condition or the run
# log a human reads afterwards.
REQUIRED_RESULT_KEYS = (
    "type",
    "subtype",
    "is_error",
    "session_id",
    "result",
    "num_turns",
    "total_cost_usd",
    "duration_ms",
)

# ------ RUN DEFAULTS ------

DEFAULT_MODEL = "opus"

# Reasoning effort, the CLI's `--effort`: low, medium, high, xhigh, max.
#
# This was once None, so the flag was omitted and whatever level the user had
# configured interactively stood. That deference is the wrong trade for a tool
# that runs while nobody is watching: it makes two runs of the same feature list
# behave differently for a reason that is invisible from outside the run, and
# the difference surfaces only as a worse result nobody can account for.
# Reproducibility wins here, and `--effort` remains for anyone who wants
# otherwise. None is still honoured, and still means "inherit the CLI's level".
DEFAULT_EFFORT: str | None = "high"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Unattended runs cannot answer a permission prompt, and a denied tool call
# makes the agent quietly work around the restriction rather than stop. So the
# default is full bypass, and the safety comes from the preflight requirement
# that the target repository is a clean git work tree the harness can revert.
DEFAULT_PERMISSION_MODE = "bypassPermissions"

DEFAULT_SESSION_BUDGET_USD = 10.0
DEFAULT_SESSION_TIMEOUT_SECONDS = 3600
DEFAULT_MAX_SESSIONS = 10

# A session that neither commits nor flips a feature to passing has stalled.
# Two in a row means the loop is burning money without moving, which is the
# failure the article's "declare victory early" mode looks like from outside.
MAX_CONSECUTIVE_STALLED_SESSIONS = 2


class HarnessError(Exception):
    """Raised for any condition that should stop the harness with a clear message.

    Every raise site is expected to explain what broke and what to do about it,
    because the audience for these messages is someone reading a log the morning
    after an unattended run.
    """


@dataclass(frozen=True)
class SessionRequest:
    """Everything needed to build one `claude` invocation.

    Kept as a value object so `build_argv` can be a pure function and therefore
    testable without spawning anything.
    """

    prompt: str
    repo_path: Path
    session_id: str
    model: str = DEFAULT_MODEL
    effort: str | None = DEFAULT_EFFORT
    permission_mode: str = DEFAULT_PERMISSION_MODE
    budget_usd: float = DEFAULT_SESSION_BUDGET_USD
    timeout_seconds: int = DEFAULT_SESSION_TIMEOUT_SECONDS
    system_prompt_suffix: str | None = None
    settings_path: Path | None = None
    extra_directories: tuple[Path, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SessionResult:
    """The parsed tail of a `claude --output-format json` run."""

    session_id: str
    is_error: bool
    subtype: str
    text: str
    num_turns: int
    cost_usd: float
    duration_ms: int
    permission_denials: tuple[str, ...] = field(default_factory=tuple)
    # None means the CLI did not report it, which is a different fact from a
    # session that did no thinking.
    thinking_tokens: int | None = None
