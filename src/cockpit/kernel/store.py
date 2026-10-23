"""Run store: katalog na run, zapis wylacznie dopisywaniem.

    <root>/<session_id>/
        events.ndjson
        spec.yaml        (snapshot specu - dokladany przez generator)
        score.json       (wynik evals - dokladany przez optymalizator)

Domyslny root pochodzi z COCKPIT_RUN_STORE, bo artefakty runu nigdy nie leza
w repo (regula skilla). Tylko stdlib.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

from .events import Event

DEFAULT_ROOT = "~/.claude/skills/implementacja-lukaszka-workspace/run_store"


def default_root() -> Path:
    return Path(os.environ.get("COCKPIT_RUN_STORE", DEFAULT_ROOT)).expanduser()


class RunStore:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root).expanduser() if root else default_root()

    def run_dir(self, session_id: str) -> Path:
        if "/" in session_id or session_id in ("", ".", ".."):
            raise ValueError(f"niepoprawny session_id: {session_id!r}")
        return self.root / session_id

    def append(self, event: Event) -> Path:
        d = self.run_dir(event.session_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "events.ndjson"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        return path

    def read(self, session_id: str) -> list[dict]:
        path = self.run_dir(session_id) / "events.ndjson"
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def runs(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            (p.name for p in self.root.iterdir() if (p / "events.ndjson").is_file()),
            reverse=True,
        )

    def tail(self, session_id: str, offset: int = 0) -> Iterator[dict]:
        """Zdarzenia od podanej linii. Podstawa dla SSE - bez czekania."""
        for i, rec in enumerate(self.read(session_id)):
            if i >= offset:
                yield rec
