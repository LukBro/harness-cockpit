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
from typing import Any, Literal

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from cockpit.db import session as db_session
from cockpit.db.session import get_session
from cockpit.services import dostep, katalog, pliki_runnera, sesje
from cockpit.services import runs as uslugi
from cockpit.services.dostep import Kontekst, Uprawnienie
from cockpit.services.errors import BrakDostepu, NieZnaleziono
from cockpit.settings import settings

from . import bus, konta
from .bledy import zarejestruj_obsluge_bledow
from .schematy import (
    Katalog,
    KatalogOdswiezony,
    ListaPlikow,
    PlikOdczytany,
    PlikZapisany,
    RunnerZameldowany,
    RunnerZespolu,
    RunSzczegoly,
    RunUtworzony,
    ZdarzeniePrzyjete,
    ZdarzenieRunu,
    Zdrowie,
    odpowiedzi_bledow,
)
from .zaleznosci import identyfikator_sesji, kontekst_sesji, runner_z_tokenu, trasa_publiczna

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
    ticket_id: str | None = Field(default=None, min_length=1, max_length=200)  # kolumna String(200)
    potwierdz_rownolegly: bool = False


class EventIn(BaseModel):
    run_id: str
    event_id: str = Field(min_length=1)    # nadaje runner; idempotencja (HAR-39)
    step_id: str = Field(min_length=1)
    agent_id: str = ""
    agent_type: str = ""
    kind: str
    text: str = ""
    cost_usd: float | None = None          # None = brak pomiaru, nigdy 0 (BRO-87)
    cost_source: str = "unavailable"
    tokens: int | None = None
    duration_ms: int | None = None
    wyslane_output: int | None = None      # ile output runner probowal wyslac (HAR-24)
    # pelna dlugosc tekstu przed obcieciem przez runnera; le = int4 PostgreSQL (HAR-56)
    text_full_len: int | None = Field(default=None, ge=0, le=2_147_483_647)


class OdpowiedzRunneraIn(BaseModel):
    req_id: str = Field(min_length=1, max_length=64)
    ok: bool
    wynik: Any = None
    blad: str | None = Field(default=None, max_length=2000)
    kod: str | None = Field(default=None, max_length=64)


@app.get("/api/health", dependencies=[Depends(trasa_publiczna)], response_model=Zdrowie,
         responses=odpowiedzi_bledow())
async def health() -> Zdrowie:
    return Zdrowie(status="ok")


@app.post("/api/runners/register", response_model=RunnerZameldowany,
          responses=odpowiedzi_bledow(401, 422))
async def register_runner(
    body: RunnerRegister,
    r=Depends(runner_z_tokenu),
    s: AsyncSession = Depends(get_session),
) -> RunnerZameldowany:
    r = await uslugi.zamelduj_runnera(
        s, r, body.name, body.version, [b.model_dump() for b in body.backends]
    )
    return RunnerZameldowany(
        runner_id=r.id, tenant_id=r.tenant_id, heartbeat_co_s=settings.heartbeat_co_s)


@app.post("/api/runners/heartbeat", response_model=Zdrowie,
          responses=odpowiedzi_bledow(401, 422))
async def runner_heartbeat(
    r=Depends(runner_z_tokenu),
    s: AsyncSession = Depends(get_session),
) -> Zdrowie:
    await uslugi.odnotuj_heartbeat(s, r)
    return Zdrowie(status="ok")


@app.post("/api/runners/odpowiedzi", status_code=204, response_model=None,
          responses=odpowiedzi_bledow(401, 404, 422))
async def runner_odpowiedz(
    body: OdpowiedzRunneraIn,
    r=Depends(runner_z_tokenu),
    s: AsyncSession = Depends(get_session),
) -> Response:
    """Odpowiedz runnera na zapytanie z `req_id`; cudze albo nieznane zapytanie to 404."""
    przyjeta = bus.odpowiedz(
        r.id, body.req_id,
        {"ok": body.ok, "wynik": body.wynik, "blad": body.blad, "kod": body.kod},
    )
    if not przyjeta:
        raise NieZnaleziono("nieznane zapytanie")
    return Response(status_code=204)


@app.get("/api/runners/stream", response_model=None,
         responses=odpowiedzi_bledow(401, 422))
async def runner_stream(
    request: Request,
    r=Depends(runner_z_tokenu),
    s: AsyncSession = Depends(get_session),
) -> EventSourceResponse:
    """SSE w dol: polecenia dla runnera. Odpowiedzi wracaja POST-em."""
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


@app.post("/api/runs", response_model=RunUtworzony,
          responses=odpowiedzi_bledow(401, 403, 409, 422))
async def create_run(
    body: RunCreate,
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> RunUtworzony:
    dostep.wymagaj(k, Uprawnienie.uruchamianie)
    run = await uslugi.utworz_run(
        s, k.tenant_id, body.workflow, polaczone=bus.connected_runner_ids(),
        ticket_id=body.ticket_id, potwierdz_rownolegly=body.potwierdz_rownolegly,
    )
    await bus.send_to_runner(
        run.runner_id,
        {"type": "run_step", "run_id": run.id, "step_id": "s0", "command": body.command},
    )
    return RunUtworzony(run_id=run.id)


@app.post("/api/events", response_model=ZdarzeniePrzyjete,
          responses=odpowiedzi_bledow(401, 404, 409, 422))
async def ingest_event(
    body: EventIn,
    r=Depends(runner_z_tokenu),
    s: AsyncSession = Depends(get_session),
) -> ZdarzeniePrzyjete:
    run = await uslugi.run_runnera(s, body.run_id, r)
    ev, nowe = await uslugi.zapisz_zdarzenie(
        s, run, event_id=body.event_id, kind=body.kind, step_id=body.step_id,
        agent_id=body.agent_id, agent_type=body.agent_type,
        text=body.text, cost_usd=body.cost_usd, cost_source=body.cost_source,
        tokens=body.tokens, duration_ms=body.duration_ms,
        wyslane_output=body.wyslane_output, text_full_len=body.text_full_len,
    )
    if nowe:
        await bus.publish_event(run.id, {"seq": ev.seq, "kind": ev.kind, "text": ev.text,
                                         "step_id": ev.step_id,
                                         "text_truncated": ev.text_truncated,
                                         "text_full_len": ev.text_full_len})
    return ZdarzeniePrzyjete(seq=ev.seq)


@app.get("/api/runs/{run_id}", response_model=RunSzczegoly,
         responses=odpowiedzi_bledow(401, 404, 422))
async def get_run(
    run_id: str,
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> RunSzczegoly:
    run = await uslugi.run_dla_najemcy(s, run_id, k.tenant_id)
    rows = await uslugi.zdarzenia_runu(s, run)
    return RunSzczegoly(
        id=run.id,
        workflow=run.workflow,
        status=run.status,
        runner_kind=run.runner_kind,
        reason=run.reason,
        events=[ZdarzenieRunu(
            seq=e.seq, kind=e.kind, text=e.text, step_id=e.step_id, agent_type=e.agent_type,
            text_truncated=e.text_truncated, text_full_len=e.text_full_len) for e in rows],
    )


@app.get("/api/runs/{run_id}/stream", response_model=None,
         responses=odpowiedzi_bledow(401, 404, 422))
async def run_stream(
    run_id: str,
    request: Request,
    k: Kontekst = Depends(kontekst_sesji),
    identyfikator: str = Depends(identyfikator_sesji),
    s: AsyncSession = Depends(get_session),
) -> EventSourceResponse:
    # cudzy lub nieznany run to 404 przed otwarciem strumienia
    await uslugi.run_dla_najemcy(s, run_id, k.tenant_id)

    async def gen():
        q = None
        try:
            # Subskrypcja w try, bo niewystartowany generator nie wykona finally.
            q = bus.subscribe_run(run_id)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    # Limit czytany przy kazdym obrocie: cisza w szynie nie moze odroczyc kontroli sesji.
                    ev = await asyncio.wait_for(q.get(), settings.sesja_kontrola_s)
                except TimeoutError:
                    ev = None
                # Kontrola przed wyslaniem: zdarzenie po uniewaznieniu sesji nie trafia do klienta.
                try:
                    async with db_session.SessionLocal() as s2:
                        await sesje.rozstrzygnij(s2, identyfikator)
                except BrakDostepu:
                    yield {
                        "event": "sesja_wygasla",
                        "data": json.dumps({"komunikat": sesje.KOMUNIKAT_SESJI}),
                    }
                    break
                if ev is None:
                    continue
                yield {"id": str(ev["seq"]), "event": "run", "data": json.dumps(ev)}
        finally:
            if q is not None:
                bus.unsubscribe_run(run_id, q)

    return EventSourceResponse(gen())


class PlikIn(BaseModel):
    sciezka: str = Field(min_length=1, max_length=4096)
    tresc: str = Field(max_length=3 * 1024 * 1024)


@app.get("/api/pliki", response_model=PlikOdczytany,
         responses=odpowiedzi_bledow(401, 403, 409, 422))
async def czytaj_plik(
    sciezka: str = Query(max_length=4096),
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> Any:
    return await pliki_runnera.czytaj(
        s, k.tenant_id, sciezka, bus.connected_runner_ids(), bus.zapytaj)


@app.get("/api/pliki/lista", response_model=ListaPlikow,
         responses=odpowiedzi_bledow(401, 403, 409, 422))
async def listuj_pliki(
    sciezka: str = Query(max_length=4096),
    wzorzec: str = Query(default="*", max_length=512),
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> Any:
    return await pliki_runnera.listuj(
        s, k.tenant_id, sciezka, wzorzec, bus.connected_runner_ids(), bus.zapytaj)


@app.put("/api/pliki", response_model=PlikZapisany,
         responses=odpowiedzi_bledow(401, 403, 409, 422))
async def zapisz_plik(
    body: PlikIn,
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> Any:
    dostep.wymagaj(k, Uprawnienie.edycja_definicji)  # przed zapytaniem: viewer nie budzi runnera
    return await pliki_runnera.zapisz(
        s, k.tenant_id, body.sciezka, body.tresc, bus.connected_runner_ids(), bus.zapytaj)


@app.get("/api/runners", response_model=list[RunnerZespolu],
         responses=odpowiedzi_bledow(401))
async def odczytaj_runnery(
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> Any:
    dostep.wymagaj(k, Uprawnienie.podglad)
    return await uslugi.runnery_zespolu(
        s, k.tenant_id, bus.connected_runner_ids(), datetime.now(timezone.utc),
    )


@app.get("/api/katalog", response_model=Katalog,
         responses=odpowiedzi_bledow(401))
async def odczytaj_katalog(
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> Any:
    dostep.wymagaj(k, Uprawnienie.podglad)
    return await katalog.katalog(s, k.tenant_id)


@app.post("/api/katalog/odswiez", response_model=KatalogOdswiezony,
          responses=odpowiedzi_bledow(401, 403, 409))
async def odswiez_katalog(
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> Any:
    dostep.wymagaj(k, Uprawnienie.podglad)   # odswiezenie nie zmienia plikow uzytkownika
    return await katalog.odswiez_katalog(s, k.tenant_id, bus.connected_runner_ids(), bus.zapytaj)


STATIC = Path(__file__).resolve().parent.parent / "static"


def zamontuj_front(aplikacja: FastAPI, katalog: Path) -> bool:
    """Front z `npm run build` na tym samym porcie (HAR-2). Bez builda - nic."""
    if not katalog.is_dir():
        return False
    aplikacja.mount("/", StaticFiles(directory=katalog, html=True), name="front")
    return True


app.include_router(konta.router)

# Na samym koncu: mount "/" po wszystkich trasach, wiec /api/* ma pierwszenstwo.
zamontuj_front(app, STATIC)
