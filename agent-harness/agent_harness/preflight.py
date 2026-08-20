"""Checks that must pass before a single token is spent.

The whole point of this module is to convert silent breakage into a loud one.
An unattended run that starts against a renamed CLI flag, an unexpectedly dirty
work tree, or a repository with no git history does not fail immediately: it
fails an hour later in a way that looks like the model being bad at its job. So
everything the harness assumes is asserted here, up front, by name.
"""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from agent_harness.config import (
    MINIMUM_CLI_VERSION,
    REQUIRED_CLI_FLAGS,
    TESTED_CLI_VERSION,
    HarnessError,
)

VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)")

# How long to wait on `claude --version` / `claude --help`. These return
# immediately in practice; the bound only exists so a hung binary cannot make
# the harness hang before it has printed anything.
PROBE_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class PreflightReport:
    """What preflight found, for the run log a human reads afterwards."""

    cli_version: tuple[int, int, int]
    repo_root: Path
    warnings: tuple[str, ...]
    authentication: str = "not checked"


def format_version(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


# ------ PURE CHECKS ------
# Separated from the subprocess calls so they can be tested without a CLI
# installed, which is also what lets CI check them.


def parse_cli_version(version_output: str) -> tuple[int, int, int]:
    """Pull the semantic version out of `claude --version` output."""
    match = VERSION_PATTERN.search(version_output)
    if match is None:
        raise HarnessError(
            "Could not read a version number out of `claude --version`. "
            f"Got: {version_output.strip()!r}. "
            "Is the `claude` CLI on PATH and working?"
        )
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def check_cli_version(
    version: tuple[int, int, int],
    tested: tuple[int, int, int] = TESTED_CLI_VERSION,
    minimum: tuple[int, int, int] = MINIMUM_CLI_VERSION,
) -> str | None:
    """Refuse versions that are too old; warn about ones newer than we probed.

    Returns a warning string, or None when the version needs no comment.
    Refusing every version newer than the tested one would make the harness
    break on a routine CLI upgrade, which is a worse failure than the drift it
    would be protecting against. Flag presence is asserted separately, and that
    check is the one that actually catches a breaking upgrade.
    """
    if version < minimum:
        raise HarnessError(
            f"claude CLI {format_version(version)} is older than the minimum "
            f"this harness supports ({format_version(minimum)}). Upgrade the CLI."
        )
    if version > tested:
        return (
            f"claude CLI {format_version(version)} is newer than the version this "
            f"harness was probed against ({format_version(tested)}). If sessions "
            "behave oddly, re-check the flags and result keys in config.py."
        )
    return None


def summarize_authentication(status: object, api_key: str | None) -> str:
    """Say how sessions will be paid for, from the CLI's own auth status.

    The harness never chooses an authentication method. It runs `claude`, which
    uses whatever it is already logged in as, unless an API key in the
    environment overrides that. Sessions inherit this process's environment, so
    an exported key silently changes who pays, which is why it is checked first
    and reported ahead of anything the CLI says about itself.

    The account email is deliberately not included. It is in the CLI's output,
    it identifies a person, and it has no bearing on the question being asked.
    """
    if api_key:
        return (
            "ANTHROPIC_API_KEY is set in this environment and sessions inherit it, "
            "so usage will be billed to that key rather than to a subscription"
        )

    if not isinstance(status, dict):
        return "could not be determined from `claude auth status`"

    payload = cast(dict[str, Any], status)
    method = str(payload.get("authMethod", "unknown"))
    if method == "claude.ai":
        subscription = str(payload.get("subscriptionType", "unknown"))
        return (
            f"signed in to a claude.ai account on the {subscription} plan, so "
            "sessions draw on that subscription rather than metered API credits"
        )

    provider = str(payload.get("apiProvider", "unknown"))
    return f"authMethod={method}, apiProvider={provider}"


def read_authentication() -> str:
    """Ask the CLI how it is authenticated. Never fatal.

    Informational only: a run is not worth blocking because an older CLI has no
    `auth status` subcommand or prints something unexpected.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return summarize_authentication(None, api_key)
    try:
        output = _run_probe(["claude", "auth", "status"], "claude auth status")
    except HarnessError:
        return "could not be determined from `claude auth status`"
    try:
        return summarize_authentication(json.loads(output), None)
    except json.JSONDecodeError:
        return "could not be read from `claude auth status`"


def find_missing_flags(
    help_text: str,
    required: tuple[str, ...] = REQUIRED_CLI_FLAGS,
) -> tuple[str, ...]:
    """Name every required flag that `claude --help` no longer documents.

    Matching is bounded on both sides so that `--output-format` cannot be read
    as satisfying a requirement for `--output`. A partial match here would
    defeat the purpose: the check exists to catch a flag being renamed into
    something similar.
    """
    missing: list[str] = []
    for flag in required:
        pattern = re.compile(rf"(?<![\w-]){re.escape(flag)}(?![\w-])")
        if pattern.search(help_text) is None:
            missing.append(flag)
    return tuple(missing)


# ------ ENVIRONMENT PROBES ------


def _run_probe(argv: list[str], what: str) -> str:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise HarnessError(f"Could not run {what}: {error}") from error
    if completed.returncode != 0:
        raise HarnessError(
            f"{what} exited {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout


def resolve_repo_root(repo_path: Path) -> Path:
    """Confirm the target is a git work tree and return its root.

    Git is not optional here. The harness's entire recovery story is that a
    session's work is committed and therefore revertable, and the coding prompt
    tells the agent to read git log to find out what the last session did.
    """
    if not repo_path.is_dir():
        raise HarnessError(f"Target repository {repo_path} is not a directory.")
    output = _run_probe(
        ["git", "-C", str(repo_path), "rev-parse", "--show-toplevel"],
        f"git rev-parse in {repo_path}",
    )
    return Path(output.strip())


def find_uncommitted_changes(repo_root: Path) -> tuple[str, ...]:
    output = _run_probe(
        ["git", "-C", str(repo_root), "status", "--porcelain"],
        f"git status in {repo_root}",
    )
    return tuple(line for line in output.splitlines() if line.strip())


def run_preflight(
    repo_path: Path, *, require_clean_tree: bool = True
) -> PreflightReport:
    """Assert every environmental precondition, or raise with the reason."""
    warnings: list[str] = []

    if shutil.which("claude") is None:
        raise HarnessError(
            "The `claude` CLI is not on PATH. This harness drives it as a "
            "subprocess and cannot do anything without it."
        )
    if shutil.which("git") is None:
        raise HarnessError("`git` is not on PATH. The harness needs it to commit work.")

    version = parse_cli_version(_run_probe(["claude", "--version"], "claude --version"))
    version_warning = check_cli_version(version)
    if version_warning is not None:
        warnings.append(version_warning)

    missing = find_missing_flags(_run_probe(["claude", "--help"], "claude --help"))
    if missing:
        raise HarnessError(
            "The installed claude CLI no longer documents these flags, which "
            f"this harness depends on: {', '.join(missing)}. Update the argv "
            "built in session.py and the pins in config.py before running."
        )

    repo_root = resolve_repo_root(repo_path)

    if require_clean_tree:
        changes = find_uncommitted_changes(repo_root)
        if changes:
            preview = "\n  ".join(changes[:10])
            raise HarnessError(
                f"{repo_root} has uncommitted changes:\n  {preview}\n"
                "The harness starts every session from a known-good commit so a "
                "bad session can be reverted. Commit or stash first, or pass "
                "--allow-dirty if you accept that a revert would take your work "
                "with it."
            )

    return PreflightReport(
        cli_version=version,
        repo_root=repo_root,
        warnings=tuple(warnings),
        authentication=read_authentication(),
    )
