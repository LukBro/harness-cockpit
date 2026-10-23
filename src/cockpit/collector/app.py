"""Kolektor zdarzen: przyjmuje POST-y z hookow Claude Code, streamuje do tablicy.

Nic nie uruchamia. Serwer wylaczony = run leci normalnie, tylko bez podgladu.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse

from ..kernel import PHASES, RunStore, normalize

WEB = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="harness-cockpit")
store = RunStore()


@app.post("/hook")
async def hook(request: Request) -> dict[str, Any]:
    """Odbiornik hookow. Nigdy nie zwraca bledu - hook nie moze przerwac runu."""
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "reason": "bad json"}
    try:
        event = normalize(payload)
        store.append(event)
    except Exception as exc:  # noqa: BLE001 - swiadomie polykamy
        return {"ok": False, "reason": repr(exc)}
    return {"ok": True}


@app.get("/api/phases")
def phases() -> list[str]:
    return list(PHASES)


@app.get("/api/runs")
def runs() -> list[dict[str, Any]]:
    out = []
    for sid in store.runs()[:20]:
        events = store.read(sid)
        last = events[-1] if events else {}
        out.append({
            "session_id": sid,
            "events": len(events),
            "last_ts": last.get("ts"),
            "finished": any(e.get("event") in ("SessionEnd", "Stop") for e in events[-3:]),
        })
    return out


@app.get("/api/events/{session_id}")
async def events(session_id: str, offset: int = 0) -> StreamingResponse:
    async def stream():
        sent = offset
        idle = 0
        while True:
            batch = list(store.tail(session_id, sent))
            if batch:
                idle = 0
                for rec in batch:
                    sent += 1
                    yield f"data: {json.dumps(rec, ensure_ascii=False)}\n\n"
            else:
                idle += 1
                yield ": keepalive\n\n"
                if idle > 1200:  # ~10 min bez zdarzen
                    return
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")
