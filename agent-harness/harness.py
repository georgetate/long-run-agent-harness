#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Entry point for the agent harness. Run this; everything else is plumbing.

    uv run --script scripts/agent-harness/harness.py doctor --repo ~/code/thing
    uv run --script scripts/agent-harness/harness.py init  --repo ~/code/thing --spec idea.md
    uv run --script scripts/agent-harness/harness.py run   --repo ~/code/thing --sessions 20
    uv run --script scripts/agent-harness/harness.py status --repo ~/code/thing

The inline script metadata above is what makes `uv run --script` work from
inside any repository, including ones with no Python environment of their own.
The dependency list is empty and is meant to stay that way: a developer tool
that must run anywhere is worth more than one that is slightly nicer to write.
"""

import sys
from pathlib import Path

# Running as a script puts this file's directory on sys.path, which is what
# makes the sibling package importable. Being explicit anyway, because the
# harness is also invoked by absolute path from other repositories.
HARNESS_ROOT = Path(__file__).resolve().parent
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

from agent_harness.cli import main  # noqa: E402 - must follow the path bootstrap

if __name__ == "__main__":
    raise SystemExit(main())
