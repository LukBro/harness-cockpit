"""HAR-39: zdarzenie ponowione przez runnera (ten sam `event_id`) nie tworzy duplikatu,
zdarzenie od runnera ma `step_id`, a kazde niesie `agent_type`.

Skutek czytamy z bazy (nowa sesja), przez prawdziwa aplikacje i prawdziwy serwis.
Wyscig o to samo zdarzenie jest odtwarzany deterministycznie: SQLite nie pozwala
na dwa rownolegle zapisy ("database is locked"), wiec konkurenta wstawiamy
w chwili commitu. Test migracji jest synchroniczny i wymaga PostgreSQL.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, select, text, update
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, Runner, RunStatus, Tenant
from cockpit.services import runs as uslugi_runow
from sesja_testowa import zaloguj_klienta, zaloz_wlasciciela
from runner.client import Runner as RunnerKlienta

KORZEN = Path(__file__).resolve().parent.parent
URL = os.environ.get("COCKPIT_TEST_DATABASE_URL", "")
TOKEN = "token-wlasciciela"

tylko_postgres = pytest.mark.skipif(
    not URL.startswith("postgresql"),
    reason="test migracji wymaga PostgreSQL: ustaw "
    "COCKPIT_TEST_DATABASE_URL=postgresql+asyncpg://.../cockpit_test",
)


# --- swiat: baza SQLite w pliku, dwa zespoly ---

@dataclass
class Swiat:
    klient: AsyncClient
    run_id: str          # run zespolu Browarek, zlecony runnerowi `wlasciciel`
    drugi_run_id: str    # inny run tego samego zespolu i runnera
    browarek_id: str     # tenant_id zespolu Browarek
    piekarnia_id: str    # tenant_id zespolu Piekarnia


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Klient ma sesje wlasciciela Browarka: GET /api/runs/{id} czyta run zespolu z sesji
    wlasciciel_konta, browarek_id = await zaloz_wlasciciela(maker, nazwa_zespolu="Browarek")
    async with maker() as s:
        piekarnia = Tenant(name="Piekarnia")
        s.add(piekarnia)
        await s.flush()
        wlasciciel = Runner(
            tenant_id=browarek_id, name="wlasciciel", last_seen_at=datetime.now(timezone.utc),
            token_hash=hashlib.sha256(TOKEN.encode()).hexdigest())
        s.add(wlasciciel)
        await s.flush()
        run = Run(tenant_id=browarek_id, runner_id=wlasciciel.id, status=RunStatus.running)
        drugi = Run(tenant_id=browarek_id, runner_id=wlasciciel.id, status=RunStatus.running)
        s.add_all([run, drugi])
        await s.commit()
        ids = (run.id, drugi.id, browarek_id, piekarnia.id)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            await zaloguj_klienta(c, maker, wlasciciel_konta)
            yield Swiat(c, *ids)
    finally:
        await engine.dispose()


def _tresc(run_id: str, **pola) -> dict:
    return {"run_id": run_id, "kind": "output", "step_id": "s0",
            "event_id": str(uuid.uuid4()), **pola}


def _post(swiat: Swiat, tresc: dict):
    return swiat.klient.post("/api/events", json=tresc,
                             headers={"Authorization": f"Bearer {TOKEN}"})


async def _zdarzenia_w_bazie(run_id: str) -> list[Event]:
    async with db_session.SessionLocal() as s:  # nowa sesja: odczyt z bazy
        return list((await s.execute(
            select(Event).where(Event.run_id == run_id).order_by(Event.seq))).scalars().all())


async def _run_w_bazie(run_id: str) -> Run:
    async with db_session.SessionLocal() as s:
        return (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()


async def _ustaw_run(run_id: str, **pola) -> None:
    """Zmiana stanu runu poza zdarzeniami: odtwarza stan, w ktorym ponowne
    przetworzenie zdarzenia dalo by inny wynik niz obecny."""
    async with db_session.SessionLocal() as s:
        await s.execute(update(Run).where(Run.id == run_id).values(**pola))
        await s.commit()


# --- ponowione zdarzenie nie tworzy duplikatu ---

async def test_to_samo_zdarzenie_wyslane_dwa_razy_zapisuje_sie_raz_z_tym_samym_numerem(swiat):
    tresc = _tresc(swiat.run_id, text="linia")

    pierwsze = await _post(swiat, tresc)
    drugie = await _post(swiat, tresc)

    assert pierwsze.status_code == 200 and drugie.status_code == 200
    assert pierwsze.json() == drugie.json() == {"seq": 1}
    zapisane = await _zdarzenia_w_bazie(swiat.run_id)
    assert [(e.seq, e.event_id, e.text) for e in zapisane] == [(1, tresc["event_id"], "linia")]


async def test_ponowione_zdarzenie_nie_zuzywa_numeru_kolejne_nowe_dostaje_nastepny(swiat):
    tresc = _tresc(swiat.run_id)
    await _post(swiat, tresc)
    await _post(swiat, tresc)

    nowe = await _post(swiat, _tresc(swiat.run_id))

    assert nowe.json() == {"seq": 2}
    assert [e.seq for e in await _zdarzenia_w_bazie(swiat.run_id)] == [1, 2]


async def test_ponowione_zdarzenie_nie_nadpisuje_tresci_zapisanej_za_pierwszym_razem(swiat):
    event_id = str(uuid.uuid4())
    await _post(swiat, _tresc(swiat.run_id, event_id=event_id, text="pierwsza tresc"))

    await _post(swiat, _tresc(swiat.run_id, event_id=event_id, text="inna tresc"))

    assert [e.text for e in await _zdarzenia_w_bazie(swiat.run_id)] == ["pierwsza tresc"]


async def test_ponowiony_koniec_kroku_nie_zmienia_statusu_runu_ustawionego_po_pierwszym_zapisie(swiat):
    tresc = _tresc(swiat.run_id, kind="step_finished")
    await _post(swiat, tresc)
    assert (await _run_w_bazie(swiat.run_id)).status == RunStatus.to_accept
    await _ustaw_run(swiat.run_id, status=RunStatus.running)  # ponowne przetworzenie dalo by to_accept

    r = await _post(swiat, tresc)

    assert r.status_code == 200
    assert (await _run_w_bazie(swiat.run_id)).status == RunStatus.running


async def test_ponowiona_porazka_kroku_nie_zmienia_statusu_runu_ustawionego_po_pierwszym_zapisie(swiat):
    tresc = _tresc(swiat.run_id, kind="step_failed")
    await _post(swiat, tresc)
    assert (await _run_w_bazie(swiat.run_id)).status == RunStatus.failed
    await _ustaw_run(swiat.run_id, status=RunStatus.running)  # ponowne przetworzenie dalo by failed

    await _post(swiat, tresc)

    assert (await _run_w_bazie(swiat.run_id)).status == RunStatus.running


async def test_ponowiony_koniec_kroku_nie_zmienia_powodu_runu(swiat):
    tresc = _tresc(swiat.run_id, kind="step_finished", wyslane_output=3)
    await _post(swiat, tresc)
    assert (await _run_w_bazie(swiat.run_id)).reason == \
        uslugi_runow.komunikat_niepelnego_strumienia(0, 3)
    await _ustaw_run(swiat.run_id, reason=None)  # ponowne przetworzenie ustawiloby komunikat

    await _post(swiat, tresc)

    assert (await _run_w_bazie(swiat.run_id)).reason is None


async def test_ponowione_zdarzenie_nie_jest_publikowane_na_szynie_drugi_raz(swiat):
    kolejka = bus.subscribe_run(swiat.run_id)
    try:
        tresc = _tresc(swiat.run_id, text="raz")
        await _post(swiat, tresc)
        await _post(swiat, tresc)

        assert kolejka.qsize() == 1
        assert kolejka.get_nowait()["seq"] == 1
    finally:
        bus.unsubscribe_run(swiat.run_id, kolejka)


async def test_nowe_zdarzenie_jest_publikowane_na_szynie(swiat):
    """Strazuje poprzedni test: szyna dziala, a cisza po duplikacie nie jest jej awaria."""
    kolejka = bus.subscribe_run(swiat.run_id)
    try:
        await _post(swiat, _tresc(swiat.run_id))
        await _post(swiat, _tresc(swiat.run_id))

        assert kolejka.qsize() == 2
    finally:
        bus.unsubscribe_run(swiat.run_id, kolejka)


async def test_ten_sam_event_id_w_dwoch_runach_daje_dwa_zdarzenia(swiat):
    """Zakres unikalnosci to run, nie caly log."""
    event_id = str(uuid.uuid4())

    a = await _post(swiat, _tresc(swiat.run_id, event_id=event_id))
    b = await _post(swiat, _tresc(swiat.drugi_run_id, event_id=event_id))

    assert a.json() == b.json() == {"seq": 1}
    for run_id in (swiat.run_id, swiat.drugi_run_id):
        assert [e.event_id for e in await _zdarzenia_w_bazie(run_id)] == [event_id]


async def test_zdarzenia_kokpitu_bez_event_id_dostaja_kazde_wlasny_identyfikator(swiat):
    """Zdarzenia nadawane przez kokpit (np. przerwanie) nie podaja `event_id`."""
    async with db_session.SessionLocal() as s:
        s.add_all([
            Event(tenant_id=swiat.browarek_id, run_id=swiat.run_id, seq=1, kind="run_aborted"),
            Event(tenant_id=swiat.browarek_id, run_id=swiat.run_id, seq=2, kind="run_aborted"),
        ])
        await s.commit()

    zapisane = await _zdarzenia_w_bazie(swiat.run_id)
    identyfikatory = [e.event_id for e in zapisane]
    assert len(set(identyfikatory)) == 2
    assert all(len(i) == 36 and i for i in identyfikatory)
    assert [e.agent_type for e in zapisane] == ["", ""]


# --- zespol: obce zdarzenie nie jest duplikatem ---

async def _zapis_serwisem(swiat: Swiat, event_id: str, **pola):
    async with db_session.SessionLocal() as s:
        run = await s.get(Run, swiat.run_id)
        return await uslugi_runow.zapisz_zdarzenie(
            s, run, event_id=event_id, kind="output", step_id="s0", **pola)


async def _zapis_lub_wyjatek(swiat: Swiat, event_id: str):
    try:
        return await _zapis_serwisem(swiat, event_id, text="wlasne")
    except Exception as wyjatek:  # noqa: BLE001 - badamy tylko, czy wynik nie jest cudzy
        return wyjatek


async def _obce_zdarzenie(swiat: Swiat, event_id: str) -> None:
    """Wiersz zespolu Piekarnia z run_id runu Browarka (Event.tenant_id nie ma klucza obcego)."""
    async with db_session.SessionLocal() as s:
        s.add(Event(tenant_id=swiat.piekarnia_id, run_id=swiat.run_id, seq=5, step_id="s0",
                    kind="output", text="obce", event_id=event_id))
        await s.commit()


def _sprawdz_ze_obce_nie_uznano_za_duplikat(wynik, obce_przed: list[Event]) -> None:
    # Ograniczenie (run_id, event_id) bez tenant_id sprawia, ze zapis i tak moze sie
    # zderzyc z obcym wierszem - wolno wtedy zwrocic blad, ale nigdy cudze zdarzenie.
    if isinstance(wynik, tuple):
        ev, _nowe = wynik
        assert ev.tenant_id != obce_przed[0].tenant_id
        assert ev.text != "obce"


async def test_zdarzenie_innego_zespolu_o_tym_samym_run_id_i_event_id_nie_jest_uznane_za_duplikat(swiat):
    event_id = str(uuid.uuid4())
    await _obce_zdarzenie(swiat, event_id)
    obce_przed = await _zdarzenia_w_bazie(swiat.run_id)

    wynik = await _zapis_lub_wyjatek(swiat, event_id)

    _sprawdz_ze_obce_nie_uznano_za_duplikat(wynik, obce_przed)
    po = await _zdarzenia_w_bazie(swiat.run_id)
    assert [(e.tenant_id, e.seq, e.text) for e in po if e.tenant_id == swiat.piekarnia_id] == [
        (swiat.piekarnia_id, 5, "obce")]  # obce nietkniete
    assert not any(e.tenant_id == swiat.browarek_id and e.text == "obce" for e in po)


@pytest.fixture
def konkurent_przy_commicie(monkeypatch):
    """Podstawia pierwszy `commit` sesji serwisu: tuz przed nim konkurent zapisuje
    (we wlasnej sesji) wiersz `konkurent`, a potem commit zglasza `blad`.
    Bez konkurenta commit tylko zglasza `blad`. Kolejne commity sa prawdziwe."""
    oryginal = AsyncSession.commit
    stan = {"wywolania": 0}

    def ustaw(blad: IntegrityError, konkurent: Event | None = None) -> dict:
        async def podmieniony(self):
            stan["wywolania"] += 1
            if stan["wywolania"] > 1:
                return await oryginal(self)
            if konkurent is not None:
                async with db_session.SessionLocal() as inna:
                    inna.add(konkurent)
                    await oryginal(inna)  # prawdziwy zapis konkurenta
            raise blad

        monkeypatch.setattr(AsyncSession, "commit", podmieniony)
        return stan

    return ustaw


def _blad_sqlite(komunikat: str) -> IntegrityError:
    return IntegrityError("INSERT INTO event ...", {}, sqlite3.IntegrityError(komunikat))


class _BladSterownikaPg(Exception):
    def __init__(self, sqlstate: str, constraint_name: str):
        super().__init__("blad pg")
        self.sqlstate = sqlstate
        self.constraint_name = constraint_name


def _blad_pg(sqlstate: str, constraint_name: str) -> IntegrityError:
    orig = AsyncAdapt_asyncpg_dbapi.IntegrityError(
        "blad pg", _BladSterownikaPg(sqlstate, constraint_name))
    return IntegrityError("INSERT INTO event ...", {}, orig)


async def test_wyscig_o_obce_zdarzenie_nie_zwraca_zdarzenia_innego_zespolu(
        swiat, konkurent_przy_commicie):
    event_id = str(uuid.uuid4())
    obce = Event(tenant_id=swiat.piekarnia_id, run_id=swiat.run_id, seq=5, step_id="s0",
                 kind="output", text="obce", event_id=event_id)
    konkurent_przy_commicie(
        _blad_sqlite("UNIQUE constraint failed: event.run_id, event.event_id"), obce)

    wynik = await _zapis_lub_wyjatek(swiat, event_id)

    _sprawdz_ze_obce_nie_uznano_za_duplikat(wynik, [obce])
    assert not any(e.tenant_id == swiat.browarek_id for e in await _zdarzenia_w_bazie(swiat.run_id))


# --- wyscig o to samo zdarzenie ---

@pytest.mark.parametrize("blad", [
    _blad_sqlite("UNIQUE constraint failed: event.run_id, event.event_id"),
    _blad_sqlite("UNIQUE constraint failed: event.event_id, event.run_id"),  # kolejnosc bez znaczenia
    _blad_pg("23505", "uq_event_run_event"),
], ids=["sqlite", "sqlite-odwrocona-kolejnosc", "postgres"])
async def test_kolizja_zdarzenia_przy_zapisie_zwraca_zdarzenie_zapisane_przez_konkurenta(
        swiat, konkurent_przy_commicie, blad):
    event_id = str(uuid.uuid4())
    konkurent = Event(tenant_id=swiat.browarek_id, run_id=swiat.run_id, seq=1, step_id="s0",
                      kind="output", text="zapisane pierwsze", event_id=event_id)
    konkurent_przy_commicie(blad, konkurent)

    ev, nowe = await _zapis_serwisem(swiat, event_id, text="spozniona kopia")

    assert nowe is False
    assert (ev.seq, ev.text, ev.event_id) == (1, "zapisane pierwsze", event_id)
    zapisane = await _zdarzenia_w_bazie(swiat.run_id)
    assert [(e.seq, e.text) for e in zapisane] == [(1, "zapisane pierwsze")]


async def test_kolizja_zdarzenia_przy_zapisie_nie_zmienia_statusu_runu(
        swiat, konkurent_przy_commicie):
    event_id = str(uuid.uuid4())
    konkurent = Event(tenant_id=swiat.browarek_id, run_id=swiat.run_id, seq=1, step_id="s0",
                      kind="step_failed", event_id=event_id)
    konkurent_przy_commicie(
        _blad_sqlite("UNIQUE constraint failed: event.run_id, event.event_id"), konkurent)

    async with db_session.SessionLocal() as s:
        run = await s.get(Run, swiat.run_id)
        _ev, nowe = await uslugi_runow.zapisz_zdarzenie(
            s, run, event_id=event_id, kind="step_failed", step_id="s0")

    assert nowe is False
    # konkurent zapisal samo zdarzenie (bez zmiany runu): status z nieudanej proby nie zostaje
    assert (await _run_w_bazie(swiat.run_id)).status == RunStatus.running


@pytest.mark.parametrize("blad", [
    _blad_sqlite("UNIQUE constraint failed: event.id"),
    _blad_sqlite("UNIQUE constraint failed: event.run_id"),
    _blad_sqlite("UNIQUE constraint failed: event.run_id, event.event_id, event.step_id"),
    _blad_sqlite("UNIQUE constraint failed: events.run_id, events.event_id"),
    _blad_sqlite("NOT NULL constraint failed: event.kind"),
    _blad_pg("23505", "inna_nazwa"),
    _blad_pg("23503", "uq_event_run_event"),  # nazwa zgodna, rozstrzyga sqlstate
], ids=["sqlite-id", "sqlite-podzbior", "sqlite-nadzbior", "sqlite-inna-tabela",
        "sqlite-not-null", "pg-inna-nazwa", "pg-klucz-obcy"])
async def test_inny_blad_integralnosci_niz_kolizja_zdarzenia_nie_jest_ukrywany(
        swiat, konkurent_przy_commicie, blad):
    stan = konkurent_przy_commicie(blad)

    with pytest.raises(IntegrityError):
        await _zapis_serwisem(swiat, str(uuid.uuid4()))  # wejscie poprawne: blad tylko z commitu

    assert stan["wywolania"] == 1  # zadnego ponawiania
    assert await _zdarzenia_w_bazie(swiat.run_id) == []


async def test_kolizja_numeru_nadal_jest_ponawiana_a_nie_traktowana_jak_duplikat(
        swiat, konkurent_przy_commicie):
    """Kolizja `uq_event_run_seq`: konkurent zajal numer 1 innym zdarzeniem."""
    konkurent = Event(tenant_id=swiat.browarek_id, run_id=swiat.run_id, seq=1, step_id="s0",
                      kind="output", text="cudze", event_id=str(uuid.uuid4()))
    konkurent_przy_commicie(
        _blad_sqlite("UNIQUE constraint failed: event.run_id, event.seq"), konkurent)

    ev, nowe = await _zapis_serwisem(swiat, str(uuid.uuid4()), text="moje")

    assert nowe is True
    assert (ev.seq, ev.text) == (2, "moje")
    assert [(e.seq, e.text) for e in await _zdarzenia_w_bazie(swiat.run_id)] == [
        (1, "cudze"), (2, "moje")]


async def test_serwis_zwraca_nowe_true_dla_pierwszego_zapisu_i_false_dla_ponowienia(swiat):
    event_id = str(uuid.uuid4())

    pierwsze, nowe1 = await _zapis_serwisem(swiat, event_id)
    ponowione, nowe2 = await _zapis_serwisem(swiat, event_id)

    assert (nowe1, nowe2) == (True, False)
    assert ponowione.seq == pierwsze.seq == 1
    assert ponowione.id == pierwsze.id


# --- step_id wymagany ---

@pytest.mark.parametrize("step_id", ["", None], ids=["pusty", "brak"])
async def test_zdarzenie_bez_step_id_jest_odrzucane_i_nic_nie_powstaje(swiat, step_id):
    tresc = _tresc(swiat.run_id)
    if step_id is None:
        del tresc["step_id"]
    else:
        tresc["step_id"] = step_id

    r = await _post(swiat, tresc)

    assert r.status_code == 422
    assert await _zdarzenia_w_bazie(swiat.run_id) == []  # w tym zadne z pustym step_id


async def test_zdarzenie_z_step_id_jest_przyjmowane_i_zapisane_z_tym_krokiem(swiat):
    r = await _post(swiat, _tresc(swiat.run_id, step_id="krok-7"))

    assert r.status_code == 200
    assert [e.step_id for e in await _zdarzenia_w_bazie(swiat.run_id)] == ["krok-7"]


# --- event_id wymagany ---

@pytest.mark.parametrize("event_id", ["", None], ids=["pusty", "brak"])
async def test_zdarzenie_bez_event_id_jest_odrzucane_i_nic_nie_powstaje(swiat, event_id):
    tresc = _tresc(swiat.run_id)
    if event_id is None:
        del tresc["event_id"]
    else:
        tresc["event_id"] = event_id

    r = await _post(swiat, tresc)

    assert r.status_code == 422
    assert await _zdarzenia_w_bazie(swiat.run_id) == []


# --- agent_type ---

async def test_typ_agenta_zdarzenia_trafia_do_bazy_i_do_historii_runu(swiat):
    r = await _post(swiat, _tresc(swiat.run_id, agent_type="claude-code"))

    assert r.status_code == 200
    assert [e.agent_type for e in await _zdarzenia_w_bazie(swiat.run_id)] == ["claude-code"]
    historia = (await swiat.klient.get(f"/api/runs/{swiat.run_id}")).json()["events"]
    assert [e["agent_type"] for e in historia] == ["claude-code"]


async def test_zdarzenie_bez_typu_agenta_ma_w_bazie_i_w_historii_pusty_napis(swiat):
    r = await _post(swiat, _tresc(swiat.run_id))

    assert r.status_code == 200
    assert [e.agent_type for e in await _zdarzenia_w_bazie(swiat.run_id)] == [""]
    historia = (await swiat.klient.get(f"/api/runs/{swiat.run_id}")).json()["events"]
    assert [e["agent_type"] for e in historia] == [""]


async def test_serwis_zapisuje_typ_agenta_podany_przez_wolajacego(swiat):
    ev, _nowe = await _zapis_serwisem(swiat, str(uuid.uuid4()), agent_type="codex")

    assert ev.agent_type == "codex"
    assert [e.agent_type for e in await _zdarzenia_w_bazie(swiat.run_id)] == ["codex"]


async def test_serwis_wolany_bez_typu_agenta_zapisuje_w_bazie_pusty_napis(swiat):
    ev, _nowe = await _zapis_serwisem(swiat, str(uuid.uuid4()))  # bez agent_type

    assert ev.agent_type == ""
    assert [e.agent_type for e in await _zdarzenia_w_bazie(swiat.run_id)] == [""]


async def test_ponowione_zdarzenie_nie_probuje_zapisu_do_bazy(swiat, monkeypatch):
    """Duplikat jest rozpoznany przed zapisem: zadnego commitu, a wiec zadnego
    nieudanego INSERT-a i bledu `duplicate key` w logu bazy."""
    event_id = str(uuid.uuid4())
    pierwsze, _ = await _zapis_serwisem(swiat, event_id, text="pierwsze")
    oryginal = AsyncSession.commit
    proby = {"commit": 0}

    async def zliczajacy_commit(self):
        proby["commit"] += 1
        return await oryginal(self)

    monkeypatch.setattr(AsyncSession, "commit", zliczajacy_commit)

    ponowione, nowe = await _zapis_serwisem(swiat, event_id, text="pierwsze")

    assert nowe is False
    assert ponowione.id == pierwsze.id
    assert proby["commit"] == 0  # ani jednej proby zapisu


# --- runner nadaje event_id ---

async def _wyslane_przez_runnera(*wywolania: dict) -> list[dict]:
    wyslane: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wyslane.append(json.loads(request.content))
        return httpx.Response(200)

    runner = RunnerKlienta("http://kokpit.test", "tajny-token")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for pola in wywolania:
            await runner.send_event(client, **pola)
    return wyslane


async def test_runner_nadaje_kazdemu_zdarzeniu_inny_identyfikator_w_formacie_uuid():
    zdarzenie = {"run_id": "run-1", "step_id": "s0", "kind": "output", "text": "x"}

    a, b = await _wyslane_przez_runnera(zdarzenie, zdarzenie)

    for wyslane in (a, b):
        assert str(uuid.UUID(wyslane["event_id"], version=4)) == wyslane["event_id"]
    assert a["event_id"] != b["event_id"]


async def test_runner_zachowuje_identyfikator_zdarzenia_podany_przez_wolajacego():
    (wyslane,) = await _wyslane_przez_runnera(
        {"run_id": "run-1", "step_id": "s0", "kind": "output", "event_id": "ponowienie-1"})

    assert wyslane["event_id"] == "ponowienie-1"


# --- migracja (PostgreSQL) ---

@pytest.fixture
def cfg():
    c = Config(str(KORZEN / "alembic.ini"))
    c.set_main_option("script_location", str(KORZEN / "alembic"))
    c.set_main_option("sqlalchemy.url", URL.replace("%", "%%"))
    command.downgrade(c, "base")
    yield c
    command.downgrade(c, "base")


def _z_polaczeniem(funkcja, zatwierdz: bool = False):
    async def _biegnij():
        engine = create_async_engine(URL)
        try:
            async with (engine.begin() if zatwierdz else engine.connect()) as conn:
                return await conn.run_sync(funkcja)
        finally:
            await engine.dispose()

    return asyncio.run(_biegnij())


def _wstaw_zdarzenia_sprzed_zmiany() -> None:
    def wstaw(c):
        c.execute(text("INSERT INTO tenant (id, name, created_at) VALUES ('t1', 'Browarek', now())"))
        c.execute(text(
            "INSERT INTO run (id, tenant_id, runner_kind, workflow, status, created_at) "
            "VALUES ('r1', 't1', 'local', 'szkielet', 'running', now())"))
        for seq, id_ in ((1, "e1"), (2, "e2")):
            c.execute(text(
                "INSERT INTO event (id, tenant_id, run_id, seq, step_id, agent_id, kind, "
                "text, cost_source, at) VALUES (:id, 't1', 'r1', :seq, '', '', 'output', "
                "'tresc', 'unavailable', now())"), {"id": id_, "seq": seq})

    _z_polaczeniem(wstaw, zatwierdz=True)


def _wiersze_zdarzen(kolumny: str) -> list[tuple]:
    return _z_polaczeniem(lambda c: [tuple(w) for w in c.execute(
        text(f"SELECT {kolumny} FROM event ORDER BY seq")).all()])


def _kolumny_event() -> set[str]:
    return _z_polaczeniem(lambda c: {k["name"] for k in inspect(c).get_columns("event")})


def _unikalne_event() -> dict[str, list[str]]:
    return _z_polaczeniem(lambda c: {
        u["name"]: u["column_names"] for u in inspect(c).get_unique_constraints("event")})


@pytest.mark.baza
@tylko_postgres
def test_migracja_nadaje_istniejacym_zdarzeniom_event_id_rowny_ich_id_i_nie_kasuje_danych(cfg):
    command.upgrade(cfg, "0004")
    assert "event_id" not in _kolumny_event()
    _wstaw_zdarzenia_sprzed_zmiany()

    command.upgrade(cfg, "head")

    assert _wiersze_zdarzen("id, event_id, agent_type, text") == [
        ("e1", "e1", "", "tresc"), ("e2", "e2", "", "tresc")]
    assert _unikalne_event().get("uq_event_run_event") == ["run_id", "event_id"]


@pytest.mark.baza
@tylko_postgres
def test_cofniecie_migracji_usuwa_event_id_typ_agenta_i_ograniczenie_ale_zostawia_zdarzenia(cfg):
    command.upgrade(cfg, "0004")
    _wstaw_zdarzenia_sprzed_zmiany()
    command.upgrade(cfg, "head")

    command.downgrade(cfg, "0004")

    kolumny = _kolumny_event()
    assert "event_id" not in kolumny and "agent_type" not in kolumny
    assert "uq_event_run_event" not in _unikalne_event()
    assert _wiersze_zdarzen("id, text") == [("e1", "tresc"), ("e2", "tresc")]
