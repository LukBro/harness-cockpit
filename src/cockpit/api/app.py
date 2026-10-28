"""API kokpitu - szkielet na wylot (BRO-99).

Warstwa HTTP: wejscie, wyjscie, kody odpowiedzi. **Zero zapytan do bazy** -
reguly i dostep do danych zyja w cockpit/services/. Pilnuje tego
`tools/check_layers.py` w CI.

Dwie powierzchnie sieciowe (BRO-76):
  * to API      - faza testowa 127.0.0.1, publicznie za Caddym
  * POST /hook  - NIE tutaj, odbiornik hookow zyje w runnerze (BRO-86)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from cockpit.db import session as db_session
from cockpit.db.session import get_session
from cockpit.services import runs as uslugi
from cockpit.settings import settings

from . import bus
from .bledy import zarejestruj_obsluge_bledow

logger = logging.getLogger(__name__)


async def sprawdz_runnery_offline(teraz: datetime | None = None) -> list[str]:
    """Jeden przebieg: przerwij runy runnerow offline i powiadom przegladarki (HAR-37)."""
    async with db_session.SessionLocal() as s:
        ids = await uslugi.przerwij_runy_offline(
            s, teraz or datetime.now(timezone.utc), bus.connected_runner_ids()
        )
        for run_id, zdarzenie in await uslugi.zdarzenia_przerwania(s, ids):
            await bus.publish_event(run_id, zdarzenie)
        return ids


async def _petla_runnerow_offline() -> None:
    while True:
        await asyncio.sleep(settings.runner_sweep_s)
        try:
            await sprawdz_runnery_offline()
        except Exception:
            # Szeroki except celowo: petla w tle musi przezyc kazdy blad jednego
            # przebiegu (np. baza chwilowo niedostepna). Blad trafia do logu.
            # CancelledError nie dziedziczy z Exception, wiec zamkniecie dziala.
            logger.exception("sprawdzanie runnerow offline nie powiodlo sie")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    zadanie = asyncio.create_task(_petla_runnerow_offline())
    try:
        yield
    finally:
        zadanie.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await zadanie


# Lifespan jest (petla runnerow offline, HAR-37), ale bez create_all:
# schemat buduje `alembic upgrade head` (HAR-1).
app = FastAPI(title="Harness Cockpit", version="0.1.0", lifespan=lifespan)
zarejestruj_obsluge_bledow(app)


class BackendIn(BaseModel):
    nazwa: str
    auth: Literal["subscription", "api_key"]


class RunnerRegister(BaseModel):
    name: str
    version: str = ""
    backends: list[BackendIn] = []


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
    wyslane_output: int | None = None      # ile output runner probowal wyslac (HAR-24)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/runners/register")
async def register_runner(
    body: RunnerRegister,
    authorization: str | None = Header(default=None),
    s: AsyncSession = Depends(get_session),
) -> dict[str, str | int]:
    r = await uslugi.runner_po_tokenie(s, authorization)
    r = await uslugi.zamelduj_runnera(
        s, r, body.name, body.version, [b.model_dump() for b in body.backends]
    )
    return {
        "runner_id": r.id,
        "tenant_id": r.tenant_id,
        "heartbeat_co_s": settings.heartbeat_co_s,
    }


@app.post("/api/runners/heartbeat")
async def runner_heartbeat(
    authorization: str | None = Header(default=None),
    s: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    r = await uslugi.runner_po_tokenie(s, authorization)
    await uslugi.odnotuj_heartbeat(s, r)
    return {"status": "ok"}


@app.get("/api/runners/stream")
async def runner_stream(
    request: Request,
    authorization: str | None = Header(default=None),
    s: AsyncSession = Depends(get_session),
) -> EventSourceResponse:
    """SSE w dol: polecenia dla runnera. Odpowiedzi wracaja POST-em."""
    r = await uslugi.runner_po_tokenie(s, authorization)
    q = bus.runner_queue(r.id)
    runner_id, tenant_id = r.id, r.tenant_id

    async def gen():
        try:
            # Oznaczenie w try, by zdjecie w finally zawsze mialo pare.
            bus.mark_runner_connected(r.id)
            # Sygnal gotowosci: gdy runner go odbierze, kokpit juz uznaje go
            # za dostepnego (runner_online wymaga otwartego strumienia).
            yield {"event": "command", "data": json.dumps({"type": "polaczono"})}
            while True:
                if await request.is_disconnected():
                    break
                polecenie = await q.get()
                # polecenie przerwanego runu nie idzie do runnera po powrocie (HAR-37)
                async with db_session.SessionLocal() as s2:
                    aktualne = await uslugi.polecenie_aktualne(
                        s2, polecenie, runner_id, tenant_id
                    )
                if not aktualne:
                    continue
                yield {"event": "command", "data": json.dumps(polecenie)}
        finally:
            bus.mark_runner_disconnected(r.id)

    return EventSourceResponse(gen())


@app.post("/api/runs")
async def create_run(
    body: RunCreate, s: AsyncSession = Depends(get_session)
) -> dict[str, str]:
    tenant = await uslugi.domyslny_tenant(s)
    run = await uslugi.utworz_run(
        s, tenant.id, body.workflow, polaczone=bus.connected_runner_ids(),
    )
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
    run = await uslugi.run_runnera(s, body.run_id, r)
    ev = await uslugi.zapisz_zdarzenie(
        s, run, kind=body.kind, step_id=body.step_id, agent_id=body.agent_id,
        text=body.text, cost_usd=body.cost_usd, cost_source=body.cost_source,
        tokens=body.tokens, duration_ms=body.duration_ms,
        wyslane_output=body.wyslane_output,
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
        "reason": run.reason,
        "events": [{"seq": e.seq, "kind": e.kind, "text": e.text, "step_id": e.step_id}
                   for e in rows],
    }


@app.get("/api/runs/{run_id}/stream")
async def run_stream(run_id: str, request: Request) -> EventSourceResponse:
    async def gen():
        q = None
        try:
            # Subskrypcja w try, bo niewystartowany generator nie wykona finally.
            q = bus.subscribe_run(run_id)
            while True:
                if await request.is_disconnected():
                    break
                ev = await q.get()
                yield {"id": str(ev["seq"]), "event": "run", "data": json.dumps(ev)}
        finally:
            if q is not None:
                bus.unsubscribe_run(run_id, q)

    return EventSourceResponse(gen())


STATIC = Path(__file__).resolve().parent.parent / "static"


def zamontuj_front(aplikacja: FastAPI, katalog: Path) -> bool:
    """Front z `npm run build` na tym samym porcie (HAR-2). Bez builda - nic."""
    if not katalog.is_dir():
        return False
    aplikacja.mount("/", StaticFiles(directory=katalog, html=True), name="front")
    return True


# Na samym koncu: mount "/" po wszystkich trasach, wiec /api/* ma pierwszenstwo.
zamontuj_front(app, STATIC)
