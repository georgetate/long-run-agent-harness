"""Numbering sessions, and turning a session's event stream into something readable.

Two things live here, both of which exist because of what a real log directory
looked like after a few runs.

Sessions used to be numbered from one on every `run` invocation, so a
repository with five completed sessions held two `session-001`s and two
`session-002`s. Nothing was lost, but nothing could be read either: the
directory said sessions had gone missing. Numbering now continues from whatever
the directory already holds, so a session number means the same thing across
every run against that repository.

And the only artifact kept per session was the final result object, which is
the one part of a session that tells you nothing about what it did. The
transcript renderer is the answer to that: the model's reasoning, every tool
call with its input, and what came back, in the order it happened.
"""

import json
import re
from typing import Any, cast

SESSION_FILENAME_PATTERN = re.compile(r"^session-(\d+)-")

# How much of a single tool result to keep. Reading a file returns its entire
# contents, and a transcript that pastes three copies of a source file is one
# nobody scrolls through. Generous enough to hold a stack trace or a test run.
MAX_TOOL_RESULT_CHARACTERS = 2000

# The same, for the model's own text and reasoning. Thinking blocks can be very
# long, and the useful part is nearly always the beginning.
MAX_TEXT_CHARACTERS = 4000


def next_session_number(existing_filenames: list[str]) -> int:
    """The number the next coding session should take, given what is already logged.

    Continues past gaps rather than filling them. A gap means a run was
    interrupted, and reusing its number would overwrite the record of whatever
    ran under it, which is exactly the record worth keeping.
    """
    numbers = [
        int(match.group(1))
        for match in (
            SESSION_FILENAME_PATTERN.match(name) for name in existing_filenames
        )
        if match is not None
    ]
    if not numbers:
        return 1
    return max(numbers) + 1


# ------ TRANSCRIPT RENDERING ------


def render_transcript(events: list[Any]) -> str:
    """Render a session's event stream as a readable Markdown transcript.

    Defensive throughout, and never raises. This is a debugging aid written
    after the session has already finished; a renderer that threw on an
    unfamiliar event shape would destroy the artifact precisely when the session
    did something unexpected, which is the only time anybody opens it.
    """
    lines: list[str] = ["# Session transcript", ""]

    for event in events:
        try:
            lines.extend(_render_event(event))
        # Broad by design: see the note about never raising in the docstring.
        except Exception as error:
            lines.append(f"_(could not render an event: {error})_")
            lines.append("")

    return "\n".join(lines)


def _render_event(event: Any) -> list[str]:
    if not isinstance(event, dict):
        return []

    # Cast after the isinstance, as everywhere else json crosses in: a checked
    # dict is dict[Unknown, Unknown] and every read off it reports as unknown.
    payload = cast(dict[str, Any], event)
    event_type = payload.get("type")

    if event_type == "system" and payload.get("subtype") == "init":
        return [
            f"**Session** `{payload.get('session_id', 'unknown')}`  ",
            f"**Working directory** `{payload.get('cwd', 'unknown')}`",
            "",
            "---",
            "",
        ]

    if event_type in ("assistant", "user"):
        return _render_message(payload)

    if event_type == "result":
        return _render_result(payload)

    return []


def _render_message(payload: dict[str, Any]) -> list[str]:
    message = payload.get("message")
    if not isinstance(message, dict):
        return []

    message_payload = cast(dict[str, Any], message)
    content = message_payload.get("content")
    if not isinstance(content, list):
        return []

    lines: list[str] = []
    for block in cast(list[Any], content):
        if isinstance(block, dict):
            lines.extend(_render_block(cast(dict[str, Any], block)))
    return lines


def _render_block(block: dict[str, Any]) -> list[str]:
    block_type = block.get("type")

    if block_type == "thinking":
        return [
            "### Thinking",
            "",
            _clip(str(block.get("thinking", "")), MAX_TEXT_CHARACTERS),
            "",
        ]

    if block_type == "text":
        return [
            "### Assistant",
            "",
            _clip(str(block.get("text", "")), MAX_TEXT_CHARACTERS),
            "",
        ]

    if block_type == "tool_use":
        rendered_input = json.dumps(block.get("input", {}), indent=2, default=str)
        return [
            f"### Tool call: {block.get('name', 'unknown')}",
            "",
            "```json",
            _clip(rendered_input, MAX_TOOL_RESULT_CHARACTERS),
            "```",
            "",
        ]

    if block_type == "tool_result":
        content = block.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, default=str)
        return [
            "### Tool result",
            "",
            "```",
            _clip(content, MAX_TOOL_RESULT_CHARACTERS),
            "```",
            "",
        ]

    return []


def _render_result(payload: dict[str, Any]) -> list[str]:
    usage = payload.get("usage")
    thinking_tokens = "unknown"
    if isinstance(usage, dict):
        details = cast(dict[str, Any], usage).get("output_tokens_details")
        if isinstance(details, dict):
            thinking_tokens = str(
                cast(dict[str, Any], details).get("thinking_tokens", "unknown")
            )

    return [
        "---",
        "",
        "## Result",
        "",
        f"- outcome: `{payload.get('subtype', 'unknown')}`"
        + (" (error)" if payload.get("is_error") else ""),
        f"- turns: {payload.get('num_turns', 'unknown')}",
        f"- thinking tokens: {thinking_tokens}",
        f"- cost: ${payload.get('total_cost_usd', 0):.4f}",
        f"- duration: {payload.get('duration_ms', 0) / 1000:.1f}s",
        "",
        "### Closing message",
        "",
        _clip(str(payload.get("result") or ""), MAX_TEXT_CHARACTERS),
        "",
    ]


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"{text[:limit]}\n… truncated, {dropped} more characters"
