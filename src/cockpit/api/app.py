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
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi import Path as SciezkaUrl
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from cockpit.db import session as db_session
from cockpit.db.session import get_session
from cockpit.services import (
    decyzje, dostep, katalog, kroki, odbior, odbior_dowody, pliki_runnera, projekty, sesje,
    skille_api, tickety,
)
from cockpit.services import runs as uslugi
from cockpit.services.dostep import Kontekst, Uprawnienie
from cockpit.services.errors import BrakDostepu, NieZnaleziono
from cockpit.settings import settings

from . import bus, konta
from .bledy import zarejestruj_obsluge_bledow
from .schematy import (
    CapabilitiesProjektu,
    DecyzjaIn,
    DecyzjaOdbioru,
    DecyzjaOdbioruIn,
    DecyzjaPrzyjeta,
    DiffRunu,
    DowodyRunu,
    Katalog,
    KatalogOdswiezony,
    KrokRunu,
    ListaPlikow,
    ListaSkilli,
    PlikOdczytany,
    PlikZapisany,
    PokrycieRunu,
    Projekt,
    RunnerZameldowany,
    RunnerZespolu,
    RunNaLiscie,
    RunSzczegoly,
    RunUtworzony,
    SkillSzczegoly,
    SkrotTicketu,
    TicketPobrany,
    Zacommitowano,
    ZdarzeniePrzyjete,
    ZdarzenieRunu,
    Zdrowie,
    Zintegrowano,
    ZmianaProjektu,
    odpowiedzi_bledow,
)
from .zaleznosci import (
    identyfikator_sesji,
    kontekst_sesji,
    kontekst_sesji_strumienia,
    runner_z_tokenu,
    trasa_publiczna,
)

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
    model_config = ConfigDict(extra="forbid")   # bez `command`: kokpit nie podaje argv (HAR-159)

    workflow: str = "szkielet"
    operacja: str = Field(default="szkielet", max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    ticket_id: str | None = Field(default=None, min_length=1, max_length=200)  # kolumna String(200)
    potwierdz_rownolegly: bool = False
    project_id: str | None = Field(default=None, min_length=1, max_length=36)  # kolumna String(36)


_ListaKotwic = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=1000)]], Field(max_length=1000)]
_ListaArtefaktow = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=255)]], Field(max_length=100)]


class DowodIn(BaseModel):
    """Dowod kroku w ksztalcie, jaki wysyla silnik (`runner.dowody`); `argv` i `stdout_tail`
    zostaja w pliku dowodu u runnera (sekrety, HAR-66), wiec nieznany klucz to 422 (HAR-170)."""
    model_config = ConfigDict(extra="forbid", strict=True)

    capability: str = Field(max_length=200, min_length=1)
    # None przy przekroczeniu limitu czasu; strict odrzuca bool, napis i liczbe zmiennoprzecinkowa
    exit_code: int | None = Field(ge=-2_147_483_648, le=2_147_483_647)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: str = Field(min_length=1, max_length=64)
    finished_at: str = Field(min_length=1, max_length=64)
    source: Literal["engine", "agent"]
    plik: str = Field(min_length=1, max_length=255)


class EventIn(BaseModel):
    run_id: str
    event_id: str = Field(min_length=1)    # nadaje runner; idempotencja (HAR-39)
    step_id: str = Field(min_length=1)
    agent_id: str = ""
    agent_type: str = ""
    kind: str
    text: str = ""
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)  # None = brak pomiaru, nigdy 0 (BRO-87)
    cost_source: str = "unavailable"
    tokens: int | None = None
    duration_ms: int | None = None
    wyslane_output: int | None = None      # ile output runner probowal wyslac (HAR-24)
    # pelna dlugosc tekstu przed obcieciem przez runnera; le = int4 PostgreSQL (HAR-56)
    text_full_len: int | None = Field(default=None, ge=0, le=2_147_483_647)
    # numer proby kroku; brak to None, nie 1; le = int4 PostgreSQL (HAR-78)
    attempt: int | None = Field(default=None, ge=1, le=2_147_483_647)
    # pomiar kroku z wyniku agenta (HAR-174); brak to None, nigdy 0; le = int4 PostgreSQL
    tokens_in: int | None = Field(default=None, ge=0, le=2_147_483_647)
    tokens_out: int | None = Field(default=None, ge=0, le=2_147_483_647)
    num_turns: int | None = Field(default=None, ge=0, le=2_147_483_647)
    session_id: str | None = Field(default=None, min_length=1, max_length=100)
    # kroki weryfikujace (D27) w `run_finished`; None = runner nie sklasyfikowal (HAR-178)
    kroki_weryfikujace: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(
        default=None, max_length=1000)
    # dane konca kroku (HAR-170), tylko w step_finished/step_failed; brak to None
    dowod: DowodIn | None = None
    verdict: str | None = Field(default=None, min_length=1, max_length=200)
    kotwice: _ListaKotwic | None = None
    artefakty: _ListaArtefaktow | None = None

    def dane(self) -> dict | None:
        """Podane pola dowodowe jako `Event.dane`; zadne nie podane to None (SQL NULL), nie `{}`."""
        dane: dict = {}
        if self.dowod is not None:
            dane["dowod"] = self.dowod.model_dump()
        if self.verdict is not None:
            dane["verdict"] = self.verdict
        if self.kotwice is not None:
            dane["kotwice"] = self.kotwice
        if self.artefakty is not None:
            dane["artefakty"] = self.artefakty
        return dane or None


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
          responses=odpowiedzi_bledow(401, 403, 404, 409, 422))
async def create_run(
    body: RunCreate,
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> RunUtworzony:
    dostep.wymagaj(k, Uprawnienie.uruchamianie)
    run = await uslugi.utworz_run(
        s, k.tenant_id, body.workflow, polaczone=bus.connected_runner_ids(),
        ticket_id=body.ticket_id, potwierdz_rownolegly=body.potwierdz_rownolegly,
        project_id=body.project_id,
    )
    if body.project_id is None:
        polecenie = {
            "type": "run_step", "run_id": run.id, "step_id": "s0", "operacja": body.operacja,
        }
    else:
        sciezka = await projekty.sciezka_projektu(s, k.tenant_id, body.project_id)
        polecenie = {
            "type": "run_workflow", "run_id": run.id, "workflow": body.workflow,
            "sciezka": sciezka,
        }
    await bus.send_to_runner(run.runner_id, polecenie)
    return RunUtworzony(run_id=run.id)


@app.post("/api/runs/{run_id}/decision", response_model=DecyzjaPrzyjeta,
          responses=odpowiedzi_bledow(401, 403, 404, 409, 422))
async def podejmij_decyzje(
    run_id: str,
    body: DecyzjaIn,
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> DecyzjaPrzyjeta:
    dostep.wymagaj(k, Uprawnienie.zatwierdzanie)
    wynik = await decyzje.podejmij_decyzje(s, run_id, k.tenant_id, body.decyzja, body.powod)
    ev = wynik.zdarzenie
    if wynik.polecenie is not None:
        await bus.send_to_runner(wynik.run.runner_id, wynik.polecenie)
    await bus.publish_event(wynik.run.id, uslugi.zdarzenie_na_szyne(ev))
    return DecyzjaPrzyjeta(run_id=wynik.run.id, status=wynik.run.status,
                           step_id=wynik.step_id, reason=wynik.run.reason)


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
        attempt=body.attempt, tokens_in=body.tokens_in, tokens_out=body.tokens_out,
        num_turns=body.num_turns, session_id=body.session_id,
        kroki_weryfikujace=body.kroki_weryfikujace, dane=body.dane(),
    )
    if nowe:
        await bus.publish_event(run.id, uslugi.zdarzenie_na_szyne(ev))
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
    wiersze_krokow = await kroki.kroki_runu(s, run)
    pokrycie = uslugi.pokrycie_z_kolumn(run)
    return RunSzczegoly(
        id=run.id,
        workflow=run.workflow,
        status=run.status,
        runner_kind=run.runner_kind,
        reason=run.reason,
        events=[ZdarzenieRunu(
            seq=e.seq, kind=e.kind, text=e.text, step_id=e.step_id, agent_type=e.agent_type,
            text_truncated=e.text_truncated, text_full_len=e.text_full_len,
            at=uslugi.czas_zdarzenia(e)) for e in rows],
        steps=[KrokRunu(
            step_id=k.step_id, agent_id=k.agent_id, attempt=k.attempt, status=k.status.value,
            reason=k.reason, cost_usd=k.cost_usd, tokens_in=k.tokens_in,
            tokens_out=k.tokens_out, num_turns=k.num_turns, session_id=k.session_id,
        ) for k in wiersze_krokow],
        pokrycie=None if pokrycie is None else PokrycieRunu(**pokrycie),
    )


@app.get("/api/runs/{run_id}/diff", response_model=DiffRunu,
         responses=odpowiedzi_bledow(401, 404, 409, 422))
async def diff_runu(
    run_id: str,
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> DiffRunu:
    diff = await odbior.diff_runu(s, run_id, k.tenant_id, bus.connected_runner_ids(), bus.zapytaj)
    return DiffRunu(diff=diff)


@app.post("/api/runs/{run_id}/integrate", response_model=Zintegrowano,
          responses=odpowiedzi_bledow(401, 403, 404, 409, 422))
async def integruj_run(
    run_id: str,
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> Zintegrowano:
    dostep.wymagaj(k, Uprawnienie.zatwierdzanie)
    wynik = await odbior.integruj_run(
        s, run_id, k.tenant_id, k.user_id, bus.connected_runner_ids(), bus.zapytaj)
    ev = wynik.zdarzenie
    await bus.publish_event(run_id, uslugi.zdarzenie_na_szyne(ev))
    return Zintegrowano(run_id=run_id, at=ev.at, pliki=wynik.pliki)


@app.post("/api/runs/{run_id}/commit", response_model=Zacommitowano,
          responses=odpowiedzi_bledow(401, 403, 404, 409, 422))
async def commituj_run(
    run_id: str,
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> Zacommitowano:
    dostep.wymagaj(k, Uprawnienie.zatwierdzanie)
    wynik = await odbior.commituj_run(
        s, run_id, k.tenant_id, k.user_id, bus.connected_runner_ids(), bus.zapytaj)
    ev = wynik.zdarzenie
    await bus.publish_event(run_id, uslugi.zdarzenie_na_szyne(ev))
    return Zacommitowano(run_id=run_id, at=ev.at, sha=wynik.sha)


@app.get("/api/runs/{run_id}/dowody", response_model=DowodyRunu,
         responses=odpowiedzi_bledow(401, 404, 422))
async def dowody_runu(
    run_id: str,
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> DowodyRunu:
    return DowodyRunu(**await odbior_dowody.dowody_runu(s, run_id, k.tenant_id))


@app.post("/api/runs/{run_id}/decyzja-odbioru", response_model=DecyzjaOdbioru,
          responses=odpowiedzi_bledow(401, 403, 404, 409, 422))
async def decyzja_odbioru(
    run_id: str,
    body: DecyzjaOdbioruIn,
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> DecyzjaOdbioru:
    dostep.wymagaj(k, Uprawnienie.zatwierdzanie)
    zapisane = await odbior_dowody.podejmij_decyzje_odbioru(
        s, run_id, k.tenant_id, k.user_id, body.decyzja, body.uzasadnienie)
    await bus.publish_event(run_id, uslugi.zdarzenie_na_szyne(zapisane))
    return DecyzjaOdbioru(**odbior_dowody.decyzja_z_zdarzenia(zapisane))


@app.get("/api/runs/{run_id}/stream", response_model=None,
         responses=odpowiedzi_bledow(401, 404, 422))
async def run_stream(
    run_id: str,
    request: Request,
    k: Kontekst = Depends(kontekst_sesji_strumienia),
    identyfikator: str = Depends(identyfikator_sesji),
) -> EventSourceResponse:
    # cudzy lub nieznany run to 404 przed otwarciem strumienia; sesja bazy
    # tylko na to sprawdzenie, zamknieta przed zwroceniem strumienia (HAR-147)
    async with db_session.SessionLocal() as s:
        await uslugi.run_dla_najemcy(s, run_id, k.tenant_id)

    async def gen():
        q = None
        try:
            # Subskrypcja w try, bo niewystartowany generator nie wykona finally.
            q = bus.subscribe_run(run_id)
            # Otwarcie strumienia rozstrzygnelo sesje w zaleznosci kontekst_sesji_strumienia.
            ostatnia = time.monotonic()
            while True:
                if await request.is_disconnected():
                    break
                # Kontrola raz na okno zamiast przy kazdym zdarzeniu (HAR-199): kazda to UPDATE
                # + commit, a 2000 linii x 3 karty dawalo ~6000 UPDATE (audyt 2026-11-03).
                # Czekamy tylko reszte okna i limit czytamy przy kazdym obrocie, wiec ani
                # ciagly ruch, ani cisza w szynie nie odraczaja kontroli dluzej niz o okno.
                pozostalo = sesje.pozostalo_do_kontroli(
                    ostatnia, time.monotonic(), settings.sesja_kontrola_s)
                ev = None
                if pozostalo > 0:
                    try:
                        ev = await asyncio.wait_for(q.get(), pozostalo)
                    except TimeoutError:
                        # cisza przez reszte okna: ev zostaje None, nizej kontrola
                        pass
                if sesje.pozostalo_do_kontroli(
                        ostatnia, time.monotonic(), settings.sesja_kontrola_s) <= 0:
                    # Zdarzenie sprzed uplywu okna po uniewaznieniu sesji moze dotrzec do
                    # klienta (decyzja HAR-199, zastepuje gwarancje HAR-70); strumien zamyka
                    # sie najpozniej po oknie.
                    try:
                        async with db_session.SessionLocal() as s2:
                            await sesje.rozstrzygnij(s2, identyfikator)
                    except BrakDostepu:
                        yield {
                            "event": "sesja_wygasla",
                            "data": json.dumps({"komunikat": sesje.KOMUNIKAT_SESJI}),
                        }
                        break
                    ostatnia = time.monotonic()
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


@app.get("/api/runs", response_model=list[RunNaLiscie],
         responses=odpowiedzi_bledow(401, 422))
async def lista_runow(
    project: str | None = Query(default=None, max_length=36),
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> Any:
    dostep.wymagaj(k, Uprawnienie.podglad)
    return await uslugi.runy_zespolu(s, k.tenant_id, project_id=project)


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


class NowyProjekt(BaseModel):
    runner_id: str = Field(min_length=1, max_length=36)
    path: str = Field(min_length=1, max_length=4096)


@app.get("/api/projects", response_model=list[Projekt],
         responses=odpowiedzi_bledow(401))
async def lista_projektow(
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> Any:
    dostep.wymagaj(k, Uprawnienie.podglad)
    return await projekty.projekty_zespolu(s, k.tenant_id)


@app.post("/api/projects", response_model=Projekt, status_code=201,
          responses=odpowiedzi_bledow(401, 403, 404, 409, 422))
async def podlacz_projekt(
    body: NowyProjekt,
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> Any:
    dostep.wymagaj(k, Uprawnienie.podlaczanie_projektow)   # przed zapytaniem runnera
    return await projekty.podlacz_projekt(
        s, k.tenant_id, body.runner_id, body.path, bus.connected_runner_ids(), bus.zapytaj)


@app.patch("/api/projects/{project_id}", response_model=CapabilitiesProjektu,
           responses=odpowiedzi_bledow(401, 403, 404, 409, 422))
async def zmien_projekt(
    project_id: Annotated[str, SciezkaUrl(max_length=36)],
    body: ZmianaProjektu,
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> Any:
    # capabilities to komendy w .cockpit/project.yaml, obok skilli i workflow: edycja definicji
    dostep.wymagaj(k, Uprawnienie.edycja_definicji)   # przed zapytaniem runnera
    return await projekty.zmien_projekt(
        s, k.tenant_id, project_id, body.capabilities, bus.connected_runner_ids(), bus.zapytaj)


@app.get("/api/skills", response_model=ListaSkilli,
         responses=odpowiedzi_bledow(401, 404, 422))
async def lista_skilli(
    project: str = Query(max_length=36),
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> Any:
    dostep.wymagaj(k, Uprawnienie.podglad)
    return await skille_api.rejestr_projektu(s, k.tenant_id, project)


@app.get("/api/skills/{skill_id}", response_model=SkillSzczegoly,
         responses=odpowiedzi_bledow(401, 403, 404, 409, 422))
async def szczegoly_skilla(
    skill_id: str,
    project: str = Query(max_length=36),
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> Any:
    dostep.wymagaj(k, Uprawnienie.podglad)
    return await skille_api.skill_projektu(
        s, k.tenant_id, project, skill_id, bus.connected_runner_ids(), bus.zapytaj)


@app.get("/api/tickety", response_model=list[SkrotTicketu],
         responses=odpowiedzi_bledow(401, 403, 404, 409, 422))
async def lista_ticketow(
    projekt: str = Query(max_length=36),
    filtr: str | None = Query(default=None, max_length=500),
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> Any:
    dostep.wymagaj(k, Uprawnienie.podglad)
    return await tickety.lista(
        s, k.tenant_id, projekt, filtr, bus.connected_runner_ids(), bus.zapytaj)


# `:path`: identyfikator GitHub ma ukosnik (`owner/repo#12`), klient koduje `#` jako `%23`.
@app.get("/api/tickety/{id_ticketu:path}", response_model=TicketPobrany,
         responses=odpowiedzi_bledow(401, 403, 404, 409, 422))
async def pobierz_ticket(
    id_ticketu: str,
    projekt: str = Query(max_length=36),
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> Any:
    dostep.wymagaj(k, Uprawnienie.podglad)
    return await tickety.pobierz(
        s, k.tenant_id, projekt, id_ticketu, bus.connected_runner_ids(), bus.zapytaj)


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
