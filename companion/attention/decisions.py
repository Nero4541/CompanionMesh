"""Proactive turns as an explicit decision: reply, stay silent, or remember.

The agent (and first the optional triage model) gets the event, who is around
and what has been going on, and answers with one JSON object:

    {"action": "reply", "text": "...", "reason": "..."}
    {"action": "silent", "reason": "..."}
    {"action": "remember", "note": "...", "reason": "..."}

The reply is read leniently: the first JSON object in it counts (code fences
and text around it are ignored). Anything else counts as silent and is logged
as unparsed. A bare ``[SILENT]`` (the older prompt's answer) is silent too.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

Action = Literal["reply", "silent", "remember"]
SILENT_MARK = "[SILENT]"

DECISION_PROMPT = """[Proactive check: this is not a message from the user.]
Something happened, and you may react to it. Decide what to do.

Event: {event}
{situation}
Answer with one JSON object and nothing else, one of:
{{"action": "reply", "text": "<one short, natural remark to say aloud, in the language you \
use with the user>", "reason": "<why>"}}
{{"action": "silent", "reason": "<why>"}}
{{"action": "remember", "note": "<what to keep in mind>", "reason": "<why>"}}
Choose silent when nothing is new or worth saying, or when you are unsure. Choose remember \
when it is worth knowing later but not worth saying now. Never say the same thing twice."""


@dataclass(frozen=True, slots=True)
class ProactiveDecision:
    action: Action
    text: str = ""
    note: str = ""
    reason: str = ""
    parsed: bool = True  # False: the answer was not understood (counted as silent)


def first_json_object(text: str) -> dict[str, object] | None:
    """The first ``{...}`` in ``text`` that parses as a JSON object."""
    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(value, dict):
                        return value
                    break
        start = text.find("{", start + 1)
    return None


def parse_decision(text: str) -> ProactiveDecision:
    """The decision in an agent's answer; unparsable means silent (``parsed`` False)."""
    data = first_json_object(text or "")
    if data is None:
        if SILENT_MARK in (text or "") or not (text or "").strip():
            return ProactiveDecision("silent", reason="[SILENT]" if text else "empty")
        return ProactiveDecision("silent", reason="unparsed", parsed=False)
    action = str(data.get("action") or "").strip().lower()
    reason = str(data.get("reason") or "").strip()
    if action == "reply":
        said = str(data.get("text") or "").strip()
        if said:
            return ProactiveDecision("reply", text=said, reason=reason)
    elif action == "remember":
        note = str(data.get("note") or data.get("text") or "").strip()
        if note:
            return ProactiveDecision("remember", note=note, reason=reason)
    elif action == "silent":
        return ProactiveDecision("silent", reason=reason)
    return ProactiveDecision("silent", reason="unparsed", parsed=False)


def decision_prompt(event: str, situation: list[str]) -> str:
    lines = "\n".join(situation)
    return DECISION_PROMPT.format(event=event, situation=f"{lines}\n" if lines else "")
