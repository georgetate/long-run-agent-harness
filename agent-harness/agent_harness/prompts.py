"""Loading and rendering the prompt templates in `prompts/`.

Prompts are files rather than string literals in code because they are the part
of this harness most likely to be tuned, and tuning them should not look like a
code change or require reading Python to find them.

Substitution is a literal replace of `{{TOKEN}}` markers rather than
`str.format`. The prompts contain JSON examples full of braces, and format()
would either choke on them or force every example to be double-escaped.
"""

import re
from pathlib import Path

from agent_harness.config import HarnessError

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


def render(name: str, replacements: dict[str, str]) -> str:
    """Load `prompts/<name>.md` and substitute its `{{TOKEN}}` markers.

    An unreplaced marker is an error rather than a curiosity that ships in the
    prompt: a session told to run `{{INIT_SCRIPT}}` will do something creative
    instead of stopping, and that is exactly the class of silent failure this
    harness exists to remove.
    """
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise HarnessError(f"No prompt template at {path}.")

    text = path.read_text(encoding="utf-8")
    for token, value in replacements.items():
        text = text.replace("{{" + token + "}}", value)

    leftovers = _find_unreplaced_markers(text)
    if leftovers:
        raise HarnessError(
            f"Prompt {name} still contains unreplaced markers: {', '.join(leftovers)}."
        )
    return text


def _find_unreplaced_markers(text: str) -> tuple[str, ...]:
    return tuple(sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", text))))
