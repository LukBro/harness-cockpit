"""HAR-66: zdarzenia hooków idą do kokpitu tylko jako metadane, przez bufor runnera.

Ścieżka produkcyjna: prawdziwy `CzytnikHookow` na pliku kroku -> `przekazuj` ->
`Runner.send_event` (bufor + dosyłka) -> prawdziwa aplikacja kokpitu. Skutek czytamy z bazy
i z ciał żądań, które opuściły runnera. Odczekanie jest wstrzykiwane: żaden test nie śpi
realnie, a zadanie w tle jest zawsze sprzątane w `finally`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, Runner, RunStatus, Tenant
from runner.bufor import BladBufora, BrakMiejsca, Bufor
from runner.client import Runner as RunnerKlienta
from runner.hooks import CzytnikHookow, do_wysylki, plik_hookow, przekazuj

TOKEN = "token-wlasciciela-hookow-5521"
ZNACZNIK = "ZNACZNIK-TRESCI-7f3a"
KROK = "krok-ze-sciezki"
LIMIT_S = 10


# --- świat: prawdziwa aplikacja na SQLite w katalogu testu -----------------------------

@dataclass
class Swiat:
    klient: AsyncClient
    run_id: str


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with maker() as s:
        browarek = Tenant(name="Browarek")
        s.add(browarek)
        await s.flush()
        wlasciciel = Runner(
            tenant_id=browarek.id, name="wlasciciel", last_seen_at=datetime.now(timezone.utc),
            token_hash=hashlib.sha256(TOKEN.encode()).hexdigest())
        s.add(wlasciciel)
        await s.flush()
        run = Run(tenant_id=browarek.id, runner_id=wlasciciel.id, status=RunStatus.running)
        s.add(run)
        await s.commit()
        run_id = run.id
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield Swiat(c, run_id)
    finally:
        await engine.dispose()


async def _zdarzenia_w_bazie() -> list[Event]:
    async with db_session.SessionLocal() as s:
        zapytanie = select(Event).order_by(Event.run_id, Event.seq)
        return list((await s.execute(zapytanie)).scalars().all())


def _wszystkie_kolumny(zdarzenie: Event) -> list[str]:
    return [str(getattr(zdarzenie, k.key)) for k in Event.__table__.columns]


class NagrywajacyKokpit(httpx.AsyncBaseTransport):
    """Prawdziwa aplikacja za transportem, który zapisuje ciała żądań `/api/events`
    i bywa niedostępny przez pierwsze `niedostepny_przez` żądań."""

    def __init__(self, niedostepny_przez: int = 0) -> None:
        self._prawdziwy = ASGITransport(app=app)
        self.pozostalo = niedostepny_przez
        self.ciala: list[str] = []
        self.proby = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/events":
            self.ciala.append(request.content.decode())
            self.proby += 1
            if self.pozostalo > 0:
                self.pozostalo -= 1
                raise httpx.ConnectError("kokpit nie odpowiada", request=request)
        return await self._prawdziwy.handle_async_request(request)


class Odczekania:
    """Atrapa odczekania: nie śpi; przy każdym wywołaniu zapisuje, co leży w buforze."""

    def __init__(self) -> None:
        self.argumenty: list[float] = []
        self.bufor_przy_odczekaniu: list[list[dict]] = []

    async def __call__(self, sekundy: float) -> None:
        self.argumenty.append(sekundy)
        self.bufor_przy_odczekaniu.append(Bufor().niepotwierdzone())


# --- pomocnicze: ścieżka produkcyjna -----------------------------------------------------

def _payload(zdarzenie: str, **pola) -> dict:
    return {"session_id": "sesja-1", "hook_event_name": zdarzenie, **pola}


def _czytnik(tmp_path, run_id: str, krok: str = KROK) -> CzytnikHookow:
    plik = plik_hookow(tmp_path / "hooki", run_id, krok)
    return CzytnikHookow(plik, run_id, krok, interwal=0.01)


def _dopisz(czytnik: CzytnikHookow, *payloady: dict) -> list[dict]:
    with czytnik.plik.open("ab") as f:
        for p in payloady:
            f.write(json.dumps(p).encode() + b"\n")
    return czytnik.odczytaj_nowe()


@asynccontextmanager
async def _przekazywanie(czytnik: CzytnikHookow, runner: RunnerKlienta):
    async with httpx.AsyncClient(transport=runner._transport) as klient:
        zadanie = asyncio.create_task(przekazuj(czytnik, partial(runner.send_event, klient)))
        try:
            yield zadanie
        finally:
            zadanie.cancel()
            await asyncio.wait({zadanie})


async def _przeslij(czytnik: CzytnikHookow, runner: RunnerKlienta) -> None:
    async with _przekazywanie(czytnik, runner):
        await asyncio.wait_for(czytnik.kolejka.join(), LIMIT_S)


def _runner(transport, odczekania: Odczekania | None = None) -> RunnerKlienta:
    return RunnerKlienta("http://test", TOKEN, transport=transport,
                         odczekaj=odczekania or Odczekania())


# --- 1. treść artefaktów nie wychodzi -----------------------------------------------------

async def test_tresc_narzedzi_promptow_i_ostatniej_wiadomosci_nie_opuszcza_runnera(swiat, tmp_path):
    czytnik = _czytnik(tmp_path, swiat.run_id)
    polecenie_fazy = "python phase_timer.py /tmp/run mark B end && false"
    odczytane = _dopisz(
        czytnik,
        _payload("PostToolUse", tool_name="Bash",
                 tool_input={"command": f"echo {ZNACZNIK}"},
                 tool_response={"stdout": ZNACZNIK, "stderr": ZNACZNIK}),
        _payload("SubagentStop", agent_id="a-1", agent_type="reviewer",
                 last_assistant_message=f"wynik {ZNACZNIK}",
                 agent_transcript_path=f"/tmp/{ZNACZNIK}.jsonl", prompt=ZNACZNIK),
        _payload("PostToolUseFailure", tool_name="Bash",
                 tool_input={"command": polecenie_fazy}, error=f"polecenie padło {ZNACZNIK}"),
    )
    # dowód, że znacznik faktycznie dotarł do czytnika inną drogą (extra.error)
    assert any(ZNACZNIK in json.dumps(z.get("extra", {})) for z in odczytane)
    transport = NagrywajacyKokpit()

    await _przeslij(czytnik, _runner(transport))

    zapisane = await _zdarzenia_w_bazie()
    assert [e.kind for e in zapisane] == [
        "hook:PostToolUse", "hook:SubagentStop", "hook:phase_failed"]
    assert len(transport.ciala) == 3
    assert not any(ZNACZNIK in c for c in transport.ciala)
    assert not any(ZNACZNIK in kolumna for e in zapisane for kolumna in _wszystkie_kolumny(e))


# --- 2-3. tożsamość kroku i agenta -----------------------------------------------------------

async def test_poczatek_pracy_subagenta_trafia_do_bazy_z_krokiem_i_runem_ze_sciezki_pliku(
    swiat, tmp_path,
):
    czytnik = _czytnik(tmp_path, swiat.run_id)
    _dopisz(czytnik, _payload("SubagentStart", agent_id="agent-42", agent_type="author",
                              step_id="krok-z-payloadu", run_id="run-z-payloadu"))

    await _przeslij(czytnik, _runner(NagrywajacyKokpit()))

    (zdarzenie,) = await _zdarzenia_w_bazie()
    assert zdarzenie.kind == "hook:SubagentStart"
    assert zdarzenie.agent_id == "agent-42"
    assert zdarzenie.agent_type == "author"
    assert zdarzenie.run_id == swiat.run_id
    assert zdarzenie.step_id == KROK


async def test_start_sesji_bez_agenta_nie_ginie_i_ma_puste_pola_agenta(swiat, tmp_path):
    czytnik = _czytnik(tmp_path, swiat.run_id)
    _dopisz(czytnik, _payload("SessionStart"))
    transport = NagrywajacyKokpit()

    await _przeslij(czytnik, _runner(transport))

    (zdarzenie,) = await _zdarzenia_w_bazie()
    assert zdarzenie.kind == "hook:SessionStart"
    assert zdarzenie.agent_id == ""
    assert zdarzenie.agent_type == ""
    assert transport.proby == 1


# --- 4. brak pomiaru czasu ------------------------------------------------------------------

async def test_zdarzenie_bez_pomiaru_czasu_ma_w_bazie_brak_pomiaru_a_nie_zero(swiat, tmp_path):
    czytnik = _czytnik(tmp_path, swiat.run_id)
    _dopisz(czytnik, _payload("Stop"))

    await _przeslij(czytnik, _runner(NagrywajacyKokpit()))

    (zdarzenie,) = await _zdarzenia_w_bazie()
    assert zdarzenie.kind == "hook:Stop"
    assert zdarzenie.duration_ms is None


async def test_zmierzony_czas_zdarzenia_trafia_do_bazy_bez_zmian(swiat, tmp_path):
    czytnik = _czytnik(tmp_path, swiat.run_id)
    czytnik.kolejka.put_nowait({"event": "Stop", "ms": 1234, "run_id": "x", "step_id": "y"})

    await _przeslij(czytnik, _runner(NagrywajacyKokpit()))

    (zdarzenie,) = await _zdarzenia_w_bazie()
    assert zdarzenie.duration_ms == 1234
    assert zdarzenie.run_id == swiat.run_id
    assert zdarzenie.step_id == KROK


# --- 5. niedostępny kokpit --------------------------------------------------------------------

async def test_zdarzenie_hooka_przy_niedostepnym_kokpicie_czeka_w_buforze_a_po_powrocie_trafia_do_bazy(
    swiat, tmp_path,
):
    czytnik = _czytnik(tmp_path, swiat.run_id)
    _dopisz(czytnik, _payload("SubagentStart", agent_id="agent-1", agent_type="author"))
    transport = NagrywajacyKokpit(niedostepny_przez=3)
    odczekania = Odczekania()

    await _przeslij(czytnik, _runner(transport, odczekania))

    assert transport.proby == 4
    assert len(odczekania.argumenty) == 3
    for w_buforze in odczekania.bufor_przy_odczekaniu:
        assert [z["kind"] for z in w_buforze] == ["hook:SubagentStart"]
    zapisane = await _zdarzenia_w_bazie()
    assert [(e.kind, e.agent_id) for e in zapisane] == [("hook:SubagentStart", "agent-1")]
    assert Bufor().niepotwierdzone() == []


# --- 6. do_wysylki: jednostkowo -----------------------------------------------------------------

KLUCZE = {"kind", "run_id", "step_id", "agent_id", "agent_type", "duration_ms", "text"}


def _ev(**pola) -> dict:
    return {"event": "Stop", "ts": 1.0, "session_id": "s", **pola}


def test_do_wysylki_zwraca_dokladnie_pola_metadanych_z_wartosciami():
    ev = _ev(event="SubagentStop", agent_id="a-1", agent_type="reviewer", tool="Bash",
             phase="B", ms=250, status="ok", extra={"error": ZNACZNIK},
             tool_input={"command": ZNACZNIK}, run_id="run-z-ev", step_id="krok-z-ev")

    wynik = do_wysylki(ev, "run-arg", "krok-arg")

    assert wynik == {
        "kind": "hook:SubagentStop", "run_id": "run-arg", "step_id": "krok-arg",
        "agent_id": "a-1", "agent_type": "reviewer", "duration_ms": 250, "text": "Bash",
    }
    assert set(wynik) == KLUCZE
    assert ZNACZNIK not in json.dumps(wynik)


def test_narzedzie_ma_pierwszenstwo_przed_faza_w_tekscie_zdarzenia():
    assert do_wysylki(_ev(tool="Bash", phase="B"), "r", "k")["text"] == "Bash"


def test_sama_faza_jest_tekstem_zdarzenia():
    assert do_wysylki(_ev(phase="B.2"), "r", "k")["text"] == "B.2"


def test_zdarzenie_bez_narzedzia_i_fazy_ma_pusty_tekst():
    assert do_wysylki(_ev(), "r", "k")["text"] == ""


def test_zdarzenie_bez_agenta_ma_puste_pola_agenta_zamiast_braku():
    wynik = do_wysylki(_ev(), "r", "k")

    assert wynik["agent_id"] == ""
    assert wynik["agent_type"] == ""


def test_agent_none_jest_pustym_napisem():
    wynik = do_wysylki(_ev(agent_id=None, agent_type=None), "r", "k")

    assert (wynik["agent_id"], wynik["agent_type"]) == ("", "")


def test_agent_id_dluzszy_niz_kolumna_jest_obciety_do_stu_znakow():
    dlugi = "".join(chr(ord("a") + i % 26) for i in range(150))

    wynik = do_wysylki(_ev(agent_id=dlugi), "r", "k")

    assert wynik["agent_id"] == dlugi[:100]
    assert wynik["agent_type"] == ""


def test_agent_type_dluzszy_niz_kolumna_jest_obciety_do_stu_znakow():
    dlugi = "".join(chr(ord("a") + i % 26) for i in range(150))

    wynik = do_wysylki(_ev(agent_type=dlugi), "r", "k")

    assert wynik["agent_type"] == dlugi[:100]
    assert wynik["agent_id"] == ""


def test_agent_dokladnie_o_dlugosci_kolumny_zostaje_bez_zmian():
    sto = "x" * 99 + "y"

    wynik = do_wysylki(_ev(agent_id=sto, agent_type=sto), "r", "k")

    assert wynik["agent_id"] == sto
    assert wynik["agent_type"] == sto


def test_zmierzony_czas_zero_zostaje_zerem_bo_to_pomiar_a_nie_brak():
    assert do_wysylki(_ev(ms=0), "r", "k")["duration_ms"] == 0


def test_czas_dodatni_przechodzi_bez_zmian():
    assert do_wysylki(_ev(ms=1234), "r", "k")["duration_ms"] == 1234


@pytest.mark.parametrize("ms", [None, "12", True, False, 1.5])
def test_czas_ktory_nie_jest_liczba_calkowita_to_brak_pomiaru(ms):
    assert do_wysylki(_ev(ms=ms), "r", "k")["duration_ms"] is None


def test_zdarzenie_bez_pola_czasu_ma_brak_pomiaru():
    assert do_wysylki(_ev(), "r", "k")["duration_ms"] is None


def test_run_i_krok_pochodza_z_argumentow_nie_ze_zdarzenia():
    wynik = do_wysylki(_ev(run_id="obcy-run", step_id="obcy-krok"), "run-arg", "krok-arg")

    assert (wynik["run_id"], wynik["step_id"]) == ("run-arg", "krok-arg")


# --- 7. przekazuj: błędy wysyłki -------------------------------------------------------------------

class BladTestowy(Exception):
    pass


def _falszywy_czytnik(*zdarzenia: dict) -> SimpleNamespace:
    kolejka: asyncio.Queue[dict] = asyncio.Queue()
    for z in zdarzenia:
        kolejka.put_nowait(z)
    return SimpleNamespace(kolejka=kolejka, run_id="run-1", step_id="krok-1")


@pytest.mark.parametrize("klasa", [BrakMiejsca, BladBufora])
async def test_blad_bufora_przy_jednym_zdarzeniu_nie_zatrzymuje_przekazywania_nastepnych(
    klasa, capsys,
):
    czytnik = _falszywy_czytnik(
        _ev(event="PostToolUse", tool="NarzedzieA"), _ev(event="Stop", tool="NarzedzieB"))
    wyslane: list[dict] = []
    proby: list[str] = []

    async def wyslij(**kw):
        proby.append(kw["text"])
        if len(proby) == 1:
            raise klasa(f"bufor odmówił: {ZNACZNIK}")
        wyslane.append(kw)

    zadanie = asyncio.create_task(przekazuj(czytnik, wyslij))
    try:
        await asyncio.wait_for(czytnik.kolejka.join(), LIMIT_S)
        assert not zadanie.done()                        # pętla żyje
    finally:
        zadanie.cancel()
        await asyncio.wait({zadanie})

    assert proby == ["NarzedzieA", "NarzedzieB"]
    assert [w["text"] for w in wyslane] == ["NarzedzieB"]
    stderr = capsys.readouterr().err
    assert klasa.__name__ in stderr
    assert ZNACZNIK not in stderr
    assert "NarzedzieA" not in stderr


async def test_inny_wyjatek_wysylki_konczy_przekazywanie_tym_samym_wyjatkiem():
    czytnik = _falszywy_czytnik(_ev(event="Stop"))

    async def wyslij(**kw):
        raise BladTestowy("awaria")

    zadanie = asyncio.create_task(przekazuj(czytnik, wyslij))
    try:
        await asyncio.wait_for(czytnik.kolejka.join(), LIMIT_S)   # task_done także przy wyjątku
        with pytest.raises(BladTestowy):
            await asyncio.wait_for(zadanie, LIMIT_S)
    finally:
        zadanie.cancel()
        await asyncio.wait({zadanie})


async def test_przekazuj_wywoluje_wysylke_z_polami_zwroconymi_przez_do_wysylki():
    ev = _ev(event="SubagentStart", agent_id="a", agent_type="t", ms=7, tool="Bash")
    czytnik = _falszywy_czytnik(ev)
    wyslane: list[dict] = []

    async def wyslij(**kw):
        wyslane.append(kw)

    zadanie = asyncio.create_task(przekazuj(czytnik, wyslij))
    try:
        await asyncio.wait_for(czytnik.kolejka.join(), LIMIT_S)
    finally:
        zadanie.cancel()
        await asyncio.wait({zadanie})

    assert wyslane == [do_wysylki(ev, "run-1", "krok-1")]
    assert wyslane[0]["run_id"] == "run-1"
