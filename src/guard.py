"""The decision behind the PreToolUse hook that protects the run's ground truth.

Two files in the workspace are the run's ground truth: the spec, which says
what was asked for, and the feature list, which says what is left. A session
that can rewrite either one can also make itself finished, and the article's
own defence against that is a strongly-worded prompt.

This module is the enforcement instead. It is pure and knows nothing about
stdin or exit codes; `tools/protect_feature_list.py` is the thin wrapper that
gives it an event and turns its verdict into the exit code the CLI expects.

Two deliberate limits, stated plainly because a security-shaped mechanism that
oversells itself is worse than none:

  * Bash matching is a heuristic. It reads the command string, so a sufficiently
    creative command (base64, an interpreter heredoc, a helper script) gets past
    it. It is a guardrail against drift, not a sandbox against intent.
  * The guard fails open. If it cannot understand an event it allows the call,
    because a guard that crashes closed breaks every tool call in the session.
    The loop's own before-and-after comparison of the feature list is the
    backstop that makes that trade safe.

It protects a file that exists and never stands between the initializer and a
file that does not. That rule came from a real initializer session: the guard
blocked it from creating the feature list, and instead of stopping it worked
around the block and finished without the file. A guard that makes the thing it
protects impossible to create does not get obeyed, it gets routed around.
"""

import os
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, cast

from agent_harness.config import (
    FEATURE_LIST_FILENAME,
    SPEC_FILENAME,
    WORKSPACE_DIR_NAME,
)

# Files inside the workspace that no session may write to directly.
PROTECTED_FILENAMES = (FEATURE_LIST_FILENAME, SPEC_FILENAME)

# Tools that write a file identified by `file_path`.
FILE_WRITING_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

# The sanctioned write path. A Bash command that runs this is allowed through
# whatever else it mentions, because this script is itself incapable of doing
# anything but flipping one status.
SANCTIONED_WRITE_SCRIPT = "mark_feature.py"

# A command is judged one segment at a time, because the interesting case is a
# harmless-looking command with a mutating one chained onto it.
SEGMENT_SEPARATORS = re.compile(r"&&|\|\||;|\||\n")

# Patterns that mean a segment intends to change a file rather than read it.
# Word-bounded rather than plain substrings, which is not pedantry: the first
# version matched "dd " inside "git add" and blocked a session from committing
# the very status change it had just been told to record.
MUTATING_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        # A redirect, but not the arrow in "false -> true" and not an
        # fd-numbered one like 2>/dev/null, which routinely rides along on a
        # read. Redirects that number a descriptor are judged separately below,
        # by where they point.
        r"(?<![-=0-9])>",
        r"\bsed\b[^\n]*\s-i\b",
        r"\btee\b",
        r"\b(mv|cp|rm|dd|truncate|patch|sponge|install|ln)\b",
        r"\bpython3?\b[^\n]*\s-c\b",
        r"\bperl\b[^\n]*\s-[a-zA-Z]*i\b",  # perl -i / -pi and friends edit in place
        r"\bg?awk\b[^\n]*\s-i\b",  # gawk -i inplace
        # Scripting interpreters running code inline. Same class as the
        # `python3 -c` line above: a one-liner that writes the file. The
        # earlier fix modelled perl and awk and stopped there; a session on
        # a web-app repo reaches for whatever runtime is already on PATH, so
        # the common ones are named here too. Matched by their inline-eval
        # flag, not the bare name, so `node build.js feature_list.json` that
        # only reads the file is not swept up.
        r"\bnode\b[^\n]*\s-(e|-eval|p|-print)\b",
        r"\b(ruby|bun)\b[^\n]*\s-e\b",
        r"\bphp\b[^\n]*\s-r\b",
        r"\bdeno\s+eval\b",
    )
)

# An fd-numbered redirect (1>, 2>>) is mutating only when it points at a
# protected file. Treating every one as a write would block commands like
# `grep pattern feature_list.json 2>/dev/null`, which is a read.
FD_REDIRECT_INTO_PROTECTED = tuple(
    re.compile(r"\d>>?\s*\S*" + re.escape(name)) for name in PROTECTED_FILENAMES
)

# git is allowed to touch the protected files. A session is required to commit
# the status change it records, so blocking git would block the instruction.
# The exceptions are the subcommands that overwrite a working file with an
# older version (or a patch), which is how a recorded result would get quietly
# undone. Matched against the subcommand token, not the whole segment: a commit
# message that happens to contain the word "restore" is not a restore.
GIT_COMMAND = re.compile(r"^\s*git\b")
GIT_DENIED_SUBCOMMANDS = frozenset({"checkout", "restore", "stash", "apply"})
GIT_RESTORING_SUBCOMMANDS = re.compile(r"\b(checkout|restore|stash|apply)\b")

# Global git options that take a separate value, which have to be skipped to
# find the actual subcommand token.
GIT_OPTIONS_WITH_VALUES = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--exec-path", "--namespace"}
)

# What an interpreter in front of the sanctioned script may look like.
PYTHON_INTERPRETER = re.compile(r"^python[\d.]*$")

# Command or process substitution inside a segment. SEGMENT_SEPARATORS does
# not split on these, so a `$(...)` or backtick rider rides inside a segment
# that otherwise looks sanctioned. Their presence voids the mark_feature.py
# exemption: the segment is then guarded on its literal text, which denies it
# the moment that text names a protected file with a mutating command.
COMMAND_SUBSTITUTION = re.compile(r"\$\(|`|<\(|>\(")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


ALLOWED = Decision(allowed=True, reason="")


def _protected_path(file_path: str) -> str | None:
    """Return the protected filename this path refers to, if any.

    The `.agent-harness` directory has to be in the path. A target repository
    may legitimately have its own `feature_list.json` somewhere, and blocking
    writes to it would break the project the harness is supposed to be serving.
    """
    path = PurePosixPath(file_path.replace("\\", "/"))
    if WORKSPACE_DIR_NAME not in path.parts:
        return None
    if path.name in PROTECTED_FILENAMES:
        return path.name
    return None


def _deny_file_edit(protected_name: str) -> Decision:
    if protected_name == FEATURE_LIST_FILENAME:
        return Decision(
            allowed=False,
            reason=(
                f"Editing {FEATURE_LIST_FILENAME} directly is blocked by the "
                "harness. The feature list defines what done means for this run, "
                "so it is not editable by the session being measured against it. "
                "To record a feature as verified, run the mark_feature.py command "
                "named in your instructions. If a feature is genuinely wrong or "
                "impossible, say so in the progress file and leave it failing; a "
                "human will decide."
            ),
        )
    return Decision(
        allowed=False,
        reason=(
            f"Editing {SPEC_FILENAME} is blocked by the harness. It is the "
            "statement of what was asked for, and rewriting it would change the "
            "goal rather than meet it. Record disagreements with the spec in the "
            "progress file instead."
        ),
    )


def decide(
    event: Any,
    path_exists: Callable[[str], bool] = os.path.exists,
    resolve_path: Callable[[str], str] = os.path.realpath,
) -> Decision:
    """Allow or deny one PreToolUse event.

    `path_exists` is injected rather than called directly so the decision stays
    testable without a filesystem, and so a test can state which side of the
    "does this file exist yet" rule it is pinning.
    """
    try:
        return _decide(event, path_exists, resolve_path)
    # Broad by design: any failure to understand an event must not break the
    # session. See the fail-open note in the module docstring.
    except Exception as error:
        return Decision(
            allowed=True,
            reason=f"guard could not read this event and allowed it: {error}",
        )


def _decide(
    event: Any,
    path_exists: Callable[[str], bool],
    resolve_path: Callable[[str], str],
) -> Decision:
    if not isinstance(event, dict):
        return ALLOWED

    hook_event = cast(dict[str, Any], event)
    tool_name = hook_event.get("tool_name")
    raw_input = hook_event.get("tool_input")
    if not isinstance(raw_input, dict):
        return ALLOWED

    tool_input = cast(dict[str, Any], raw_input)
    working_directory = str(hook_event.get("cwd", ""))

    if tool_name in FILE_WRITING_TOOLS:
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        # Joined with the session's cwd before the check, not after. A relative
        # `feature_list.json` written while the session sits inside
        # `.agent-harness/` names the protected file without ever containing
        # the directory name.
        absolute_path = _absolute(str(file_path), working_directory)
        # Resolve symlinks before judging the path. A session can point an
        # innocent-looking name at the protected file (`ln -s ... link`; then
        # write `link`); the textual path would not contain `.agent-harness`,
        # but the bytes still land on the ground-truth file.
        resolved_path = resolve_path(absolute_path)
        protected_name = _protected_path(resolved_path)
        if protected_name is None:
            return ALLOWED
        if not path_exists(resolved_path):
            return ALLOWED
        return _deny_file_edit(protected_name)

    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        return _decide_bash(command, working_directory, path_exists)

    return ALLOWED


def _absolute(file_path: str, working_directory: str) -> str:
    if os.path.isabs(file_path) or not working_directory:
        return file_path
    return os.path.join(working_directory, file_path)


def _decide_bash(
    command: str, working_directory: str, path_exists: Callable[[str], bool]
) -> Decision:
    for segment in SEGMENT_SEPARATORS.split(command):
        if not _segment_is_allowed(segment, working_directory, path_exists):
            return _deny_bash()
    return ALLOWED


def _segment_is_allowed(
    segment: str, working_directory: str, path_exists: Callable[[str], bool]
) -> bool:
    if _invokes_sanctioned_script(segment) and not COMMAND_SUBSTITUTION.search(segment):
        return True

    mentioned = [name for name in PROTECTED_FILENAMES if name in segment]
    if not mentioned:
        return True

    if GIT_COMMAND.search(segment):
        subcommand = _git_subcommand(segment)
        if subcommand is None:
            # The segment would not tokenize (an unbalanced quote, usually), so
            # fall back to scanning the whole segment. That can false-positive
            # on a commit message, but only for a segment that is already
            # malformed enough that the shell may not accept it either.
            return GIT_RESTORING_SUBCOMMANDS.search(segment) is None
        return subcommand not in GIT_DENIED_SUBCOMMANDS

    is_mutating = any(pattern.search(segment) for pattern in MUTATING_PATTERNS) or any(
        pattern.search(segment) for pattern in FD_REDIRECT_INTO_PROTECTED
    )
    if not is_mutating:
        return True

    # A command naming a protected file that does not exist yet is the
    # initializer writing it for the first time. The workspace path is
    # reconstructed from the session's working directory because a shell
    # command carries no structured path to inspect.
    already_written = [
        name
        for name in mentioned
        if path_exists(os.path.join(working_directory, WORKSPACE_DIR_NAME, name))
    ]
    return not already_written


def _invokes_sanctioned_script(segment: str) -> bool:
    """True only when the segment actually runs mark_feature.py.

    A plain substring test would let any command smuggle itself past the guard
    by naming the script in a comment or an argument. So the segment is
    tokenized with comments stripped, and the script has to sit where a command
    can actually sit: first, or second behind a python interpreter, which is
    exactly the shape of the command the prompts hand the session.
    """
    try:
        tokens = shlex.split(segment, comments=True)
    except ValueError:
        # Untokenizable: not sanctioned. Falling through to the ordinary checks
        # can only deny, never allow, so this stays fail-safe for the file.
        return False

    candidates = tokens[:1]
    if len(tokens) > 1 and PYTHON_INTERPRETER.match(os.path.basename(tokens[0])):
        candidates.append(tokens[1])
    return any(
        token == SANCTIONED_WRITE_SCRIPT
        or token.replace("\\", "/").endswith("/" + SANCTIONED_WRITE_SCRIPT)
        for token in candidates
    )


def _git_subcommand(segment: str) -> str | None:
    """The subcommand token of a git segment, or None when it will not tokenize."""
    try:
        tokens = shlex.split(segment, comments=True)
    except ValueError:
        return None
    if not tokens or tokens[0] != "git":
        return None

    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in GIT_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return None


def _deny_bash() -> Decision:
    return Decision(
        allowed=False,
        reason=(
            "This command looks like it modifies a protected harness file "
            f"({', '.join(PROTECTED_FILENAMES)}). Those files are the run's "
            "ground truth and are not writable from a session. Read them freely; "
            "to record a verified feature, use the mark_feature.py command named "
            "in your instructions."
        ),
    )
