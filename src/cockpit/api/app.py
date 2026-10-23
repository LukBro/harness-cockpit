"""API kokpitu - szkielet na wylot (BRO-99).

Warstwa HTTP: wejscie, wyjscie, kody odpowiedzi. **Zero zapytan do bazy** -
reguly i dostep do danych zyja w cockpit/services/. Pilnuje tego
`tools/check_layers.py` w CI.

Dwie powierzchnie sieciowe (BRO-76):
  * to API      - faza testowa 127.0.0.1, publicznie za Caddym
  * POST /hook  - NIE tutaj, odbiornik hookow zyje w runnerze (BRO-86)
"""
from __future__ import annotations

import json

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from cockpit.db.models import RunStatus
from cockpit.db.session import get_session
from cockpit.services import runs as uslugi
from cockpit.services.errors import (
    BladDziedziny,
    BrakDostepu,
    NaruszonyNiezmiennik,
    NieZnaleziono,
    StanNiemozliwy,
)

from . import bus

_KODY = {BrakDostepu: 401, NieZnaleziono: 404, StanNiemozliwy: 409,
         NaruszonyNiezmiennik: 422}


# Brak lifespan z create_all: schemat buduje `alembic upgrade head` (HAR-1).
app = FastAPI(title="Harness Cockpit", version="0.1.0")


@app.exception_handler(BladDziedziny)
async def _blad_dziedziny(_: Request, exc: BladDziedziny):
    from fastapi.responses import JSONResponse

    kod = next((v for k, v in _KODY.items() if isinstance(exc, k)), 400)
    return JSONResponse(status_code=kod, content={"detail": str(exc)})


class RunnerRegister(BaseModel):
    name: str
    version: str = ""
    backends: list[str] = []


class RunCreate(BaseModel):
    workflow: str = "szkielet"
    command: list[str] = ["echo", "szkielet dziala"]


class EventIn(BaseModel):
    run_id: str
    step_id: str = ""
    agent_id: str = ""
    kind: str
    text: str = ""
    cost_usd: float | None = None          # None = brak pomiaru, nigdy 0 (BRO-87)
    cost_source: str = "unavailable"
    tokens: int | None = None
    duration_ms: int | None = None
    status: RunStatus | None = None


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/runners/register")
async def register_runner(
    body: RunnerRegister,
    authorization: str | None = Header(default=None),
    s: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    r = await uslugi.runner_po_tokenie(s, authorization)
    r = await uslugi.zamelduj_runnera(s, r, body.name, body.version)
    return {"runner_id": r.id, "tenant_id": r.tenant_id}


@app.get("/api/runners/stream")
async def runner_stream(
    request: Request,
    authorization: str | None = Header(default=None),
    s: AsyncSession = Depends(get_session),
) -> EventSourceResponse:
    """SSE w dol: polecenia dla runnera. Odpowiedzi wracaja POST-em."""
    r = await uslugi.runner_po_tokenie(s, authorization)
    q = bus.runner_queue(r.id)

    async def gen():
        while True:
            if await request.is_disconnected():
                break
            yield {"event": "command", "data": json.dumps(await q.get())}

    return EventSourceResponse(gen())


@app.post("/api/runs")
async def create_run(
    body: RunCreate, s: AsyncSession = Depends(get_session)
) -> dict[str, str]:
    tenant = await uslugi.domyslny_tenant(s)
    run = await uslugi.utworz_run(s, tenant.id, body.workflow)
    await bus.send_to_runner(
        run.runner_id,
        {"type": "run_step", "run_id": run.id, "step_id": "s0", "command": body.command},
    )
    return {"run_id": run.id}


@app.post("/api/events")
async def ingest_event(
    body: EventIn,
    authorization: str | None = Header(default=None),
    s: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    r = await uslugi.runner_po_tokenie(s, authorization)
    run = await uslugi.run_dla_najemcy(s, body.run_id, r.tenant_id)
    ev = await uslugi.zapisz_zdarzenie(
        s, run, kind=body.kind, step_id=body.step_id, agent_id=body.agent_id,
        text=body.text, cost_usd=body.cost_usd, cost_source=body.cost_source,
        tokens=body.tokens, duration_ms=body.duration_ms, status=body.status,
    )
    await bus.publish_event(run.id, {"seq": ev.seq, "kind": ev.kind, "text": ev.text,
                                     "step_id": ev.step_id})
    return {"seq": ev.seq}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, s: AsyncSession = Depends(get_session)) -> dict:
    tenant = await uslugi.domyslny_tenant(s)
    run = await uslugi.run_dla_najemcy(s, run_id, tenant.id)
    rows = await uslugi.zdarzenia_runu(s, run)
    return {
        "id": run.id,
        "workflow": run.workflow,
        "status": run.status.value,
        "runner_kind": run.runner_kind.value,
        "events": [{"seq": e.seq, "kind": e.kind, "text": e.text, "step_id": e.step_id}
                   for e in rows],
    }


@app.get("/api/runs/{run_id}/stream")
async def run_stream(run_id: str, request: Request) -> EventSourceResponse:
    q = bus.subscribe_run(run_id)

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                ev = await q.get()
                yield {"id": str(ev["seq"]), "event": "run", "data": json.dumps(ev)}
        finally:
            bus.unsubscribe_run(run_id, q)

    return EventSourceResponse(gen())
