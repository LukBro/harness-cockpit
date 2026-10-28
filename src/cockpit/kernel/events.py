"""Normalizacja zdarzen z hookow Claude Code do jednego schematu.

Schemat (jedna linia NDJSON = jedno zdarzenie):

    {ts, session_id, phase, agent_id, agent_type, event, tool, status, ms}

Klucz tozsamosci: (session_id, agent_id). Watek glowny ma agent_id = None,
subagent wlasne id - stad drzewo zagniezdzen bez parsowania tekstu.

Tylko stdlib. Kernel nie zalezy od FastAPI ani od transportu.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

# Fazy pipeline'u, w kolejnosci wyswietlania na tablicy.
PHASES: tuple[str, ...] = (
    "0", "A", "classify", "B", "B.2", "B.3", "C", "D", "E", "E.2", "F", "G",
)

# `phase_timer.py <RUN_DIR> mark <phase> start|end` - jedyne zrodlo nazw faz.
_PHASE_MARK = re.compile(
    r"phase_timer\.py\s+\S+\s+mark\s+(?P<phase>[\w.]+)\s+(?P<edge>start|end)"
)

# (hook, krawedz znacznika) -> zdarzenie fazy. Fixture'y HAR-40 (fazy-atrapa.ndjson):
# `mark X end` konczy sie PostToolUse, a `mark X end && false` - PostToolUseFailure
# z `error` i bez `tool_response`; PreToolUse + end to jeszcze przed wykonaniem
# komendy, wiec jest zdarzeniem surowym. Brak w tabeli = zdarzenie surowe.
_PHASE_EVENT: dict[tuple[str, str], str] = {
    ("PreToolUse", "start"): "phase_start",
    ("PostToolUse", "end"): "phase_end",
    ("PostToolUseFailure", "end"): "phase_failed",
}


@dataclass(slots=True)
class Event:
    ts: float
    session_id: str
    event: str
    phase: str | None = None
    agent_id: str | None = None
    agent_type: str | None = None
    tool: str | None = None
    status: str | None = None
    ms: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if not d["extra"]:
            d.pop("extra")
        return {k: v for k, v in d.items() if v is not None}


def parse_phase_mark(command: str | None) -> tuple[str | None, str | None]:
    """Wyciaga (faza, start|end) z komendy Bash. (None, None) gdy nie pasuje."""
    if not command:
        return None, None
    m = _PHASE_MARK.search(command)
    if not m:
        return None, None
    return m.group("phase"), m.group("edge")


def normalize(payload: dict[str, Any]) -> Event:
    """Zamienia surowy JSON z hooka na Event.

    Nieznane zdarzenia przechodza z nazwa hooka i bez pol opcjonalnych -
    kolektor nigdy nie odrzuca zdarzenia, ktorego nie rozumie.
    """
    name = payload.get("hook_event_name") or "unknown"
    ev = Event(
        ts=payload.get("ts") or time.time(),
        session_id=payload.get("session_id") or "unknown",
        event=name,
        agent_id=payload.get("agent_id"),
        agent_type=payload.get("agent_type"),
    )

    tool_input = payload.get("tool_input") or {}
    ev.tool = payload.get("tool_name")

    if name in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        phase, edge = parse_phase_mark(tool_input.get("command"))
        phase_event = _PHASE_EVENT.get((name, edge)) if phase else None
        if phase_event:
            ev.phase = phase
            ev.event = phase_event
            ev.tool = None
            # Powod tylko z payloadu (brak pola = brak klucza); status zostaje None.
            if phase_event == "phase_failed" and "error" in payload:
                ev.extra["error"] = payload["error"]

    return ev
