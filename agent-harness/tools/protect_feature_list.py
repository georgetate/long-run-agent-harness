#!/usr/bin/env python3
"""PreToolUse hook: blocks a session from writing the run's ground-truth files.

Wired into a session through the generated settings file rather than through
any config in the target repository, so a repo needs no setup to be protected
and every repo gets hook upgrades the moment the harness is upgraded.

The contract with the CLI is the exit code: 0 allows the tool call, 2 blocks it
and feeds this script's stderr back to the model as the reason. All of the
judgment lives in `agent_harness.guard`, which is unit-tested; this file only
moves bytes between stdin, that function, and the exit code.
"""

import json
import sys
from pathlib import Path
from typing import Any

# The harness is not an installed package: it is a developer tool that runs
# against repositories other than its own. Bootstrapping sys.path from this
# file's location is what lets the hook be invoked by absolute path from inside
# any target repository.
HARNESS_ROOT = Path(__file__).resolve().parents[1]
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

from agent_harness.guard import decide  # noqa: E402 - must follow the path bootstrap

BLOCK_EXIT_CODE = 2


def main() -> int:
    raw = sys.stdin.read()
    try:
        event: Any = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        # Fail open, deliberately: an unreadable event must not break every tool
        # call in the session. The loop re-checks the feature list after each
        # session, which is the backstop that makes this safe.
        return 0

    decision = decide(event)
    if decision.allowed:
        return 0

    print(decision.reason, file=sys.stderr)
    return BLOCK_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
