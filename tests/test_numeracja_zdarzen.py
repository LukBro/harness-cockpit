"""HAR-25: numer zdarzenia (`seq`) jest unikalny w obrebie runu, a gwarantuje to baza.

Skutek czytamy z bazy (nowa sesja), nie z kodu odpowiedzi. Testy migracji sa
synchroniczne (env.py Alembica sam uruchamia petle zdarzen) i wymagaja PostgreSQL.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event as sa_event
from sqlalchemy import inspect, select, text
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, Runner, RunStatus, Tenant
from cockpit.services import runs as uslugi_runow
from cockpit.services.errors import StanNiemozliwy
from cockpit.services.runs import _kolizja_numeru
from sesja_testowa import zaloguj_klienta, zaloz_wlasciciela

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
    drugi_run_id: str    # inny run tego samego zespolu
    run_obcego_id: str   # run zespolu Piekarnia
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
    teraz = datetime.now(timezone.utc)
    # Klient ma sesje wlasciciela Browarka: GET /api/runs/{id} czyta run zespolu z sesji
    wlasciciel_konta, browarek_id = await zaloz_wlasciciela(maker, nazwa_zespolu="Browarek")
    async with maker() as s:
        piekarnia = Tenant(name="Piekarnia")
        s.add(piekarnia)
        await s.flush()
        wlasciciel = Runner(
            tenant_id=browarek_id, name="wlasciciel", last_seen_at=teraz,
            token_hash=hashlib.sha256(TOKEN.encode()).hexdigest())
        obcy = Runner(
            tenant_id=piekarnia.id, name="obcy", last_seen_at=teraz,
            token_hash=hashlib.sha256(b"token-obcego").hexdigest())
        s.add_all([wlasciciel, obcy])
        await s.flush()
        run = Run(tenant_id=browarek_id, runner_id=wlasciciel.id, status=RunStatus.running)
        drugi = Run(tenant_id=browarek_id, runner_id=wlasciciel.id, status=RunStatus.running)
        run_obcego = Run(tenant_id=piekarnia.id, runner_id=obcy.id, status=RunStatus.running)
        s.add_all([run, drugi, run_obcego])
        await s.commit()
        ids = (run.id, drugi.id, run_obcego.id, browarek_id, piekarnia.id)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            await zaloguj_klienta(c, maker, wlasciciel_konta)
            yield Swiat(c, *ids)
    finally:
        await engine.dispose()


def _zdarzenie_bazy(run_id: str, seq: int, tenant_id: str, kind: str = "output",
                    step_id: str = "") -> Event:
    return Event(tenant_id=tenant_id, run_id=run_id, seq=seq, kind=kind, step_id=step_id)


async def _zapisz_wprost(*zdarzenia: Event) -> None:
    async with db_session.SessionLocal() as s:
        s.add_all(zdarzenia)
        await s.commit()


async def _numery_w_bazie(run_id: str) -> list[int]:
    async with db_session.SessionLocal() as s:  # nowa sesja: odczyt z bazy
        return sorted((await s.execute(
            select(Event.seq).where(Event.run_id == run_id))).scalars().all())


async def _run_w_bazie(run_id: str) -> Run:
    async with db_session.SessionLocal() as s:
        return (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()


def _post(klient: AsyncClient, run_id: str, kind: str = "output", **extra):
    tresc = {"run_id": run_id, "kind": kind, "step_id": "s0",
             "event_id": str(uuid.uuid4()), **extra}
    return klient.post("/api/events", json=tresc,
                       headers={"Authorization": f"Bearer {TOKEN}"})


@pytest.fixture
def kolizja(monkeypatch):
    """Podstawia `_nastepny_seq`: pierwsze `ile_zajetych` wywolan zwraca numer
    `zajety` (juz zapisany w bazie), kolejne ida do prawdziwej implementacji."""
    stan = {"wywolania": 0}

    def ustaw(zajety: int, ile_zajetych: int | None = None) -> dict:
        oryginal = uslugi_runow._nastepny_seq

        async def podmieniony(s, run_id, tenant_id):
            stan["wywolania"] += 1
            if ile_zajetych is None or stan["wywolania"] <= ile_zajetych:
                return zajety
            return await oryginal(s, run_id, tenant_id)

        monkeypatch.setattr(uslugi_runow, "_nastepny_seq", podmieniony)
        return stan

    return ustaw


# --- ograniczenie w bazie ---

async def test_dwa_zdarzenia_runu_o_tym_samym_numerze_odrzuca_baza(swiat):
    await _zapisz_wprost(_zdarzenie_bazy(swiat.run_id, 1, swiat.browarek_id))

    with pytest.raises(IntegrityError):
        await _zapisz_wprost(_zdarzenie_bazy(swiat.run_id, 1, swiat.browarek_id, kind="step_failed"))

    assert await _numery_w_bazie(swiat.run_id) == [1]


async def test_ten_sam_numer_w_roznych_runach_jest_dozwolony(swiat):
    """Zakres unikalnosci to run, nie caly log."""
    await _zapisz_wprost(
        _zdarzenie_bazy(swiat.run_id, 1, swiat.browarek_id),
        _zdarzenie_bazy(swiat.drugi_run_id, 1, swiat.browarek_id),
        _zdarzenie_bazy(swiat.run_obcego_id, 1, swiat.piekarnia_id),
    )

    for run_id in (swiat.run_id, swiat.drugi_run_id, swiat.run_obcego_id):
        assert await _numery_w_bazie(run_id) == [1]


# --- ponawianie po kolizji ---

async def test_kolizja_numeru_zdarzenia_jest_ponawiana_z_nastepnym_wolnym_numerem(swiat, kolizja):
    r1 = await _post(swiat.klient, swiat.run_id)
    assert r1.status_code == 200 and r1.json() == {"seq": 1}
    stan = kolizja(zajety=1, ile_zajetych=1)

    r2 = await _post(swiat.klient, swiat.run_id, text="drugie")

    assert r2.status_code == 200
    assert stan["wywolania"] == 2  # pierwsza proba zderzyla sie, druga przeszla
    assert await _numery_w_bazie(swiat.run_id) == [1, 2]
    historia = (await swiat.klient.get(f"/api/runs/{swiat.run_id}")).json()["events"]
    assert [(e["seq"], e["text"]) for e in historia] == [(1, ""), (2, "drugie")]


async def test_kilka_kolejnych_kolizji_jest_ponawianych_do_skutku(swiat, kolizja):
    """Dwie porazki z rzedu nadal mieszcza sie w limicie prob."""
    await _post(swiat.klient, swiat.run_id)
    stan = kolizja(zajety=1, ile_zajetych=2)

    r = await _post(swiat.klient, swiat.run_id)

    assert r.status_code == 200
    assert stan["wywolania"] == 3
    assert await _numery_w_bazie(swiat.run_id) == [1, 2]


async def test_po_wyczerpaniu_prob_kolizji_zdarzenie_nie_powstaje_i_jest_blad_stanu(swiat, kolizja):
    await _post(swiat.klient, swiat.run_id)
    stan = kolizja(zajety=1)  # zawsze zajety

    async with db_session.SessionLocal() as s:
        run = await uslugi_runow.run_dla_najemcy(
            s, swiat.run_id, swiat.browarek_id)
        with pytest.raises(StanNiemozliwy):
            await uslugi_runow.zapisz_zdarzenie(
            s, run, event_id=str(uuid.uuid4()), kind="output")

    assert stan["wywolania"] == uslugi_runow._PROBY_SEQ  # dokladnie tyle, nie wiecej, nie mniej
    assert await _numery_w_bazie(swiat.run_id) == [1]


async def test_po_wyczerpaniu_prob_kolizji_blad_stanu_mowi_uzytkownikowi_co_zrobic(swiat, kolizja):
    await _post(swiat.klient, swiat.run_id)
    kolizja(zajety=1)

    async with db_session.SessionLocal() as s:
        run = await uslugi_runow.run_dla_najemcy(
            s, swiat.run_id, swiat.browarek_id)
        with pytest.raises(StanNiemozliwy) as wyjatek:
            await uslugi_runow.zapisz_zdarzenie(
            s, run, event_id=str(uuid.uuid4()), kind="output")

    assert str(wyjatek.value) == (
        f"nie udalo sie nadac numeru zdarzenia po {uslugi_runow._PROBY_SEQ} probach - ponow zapis")


async def test_po_wyczerpaniu_prob_kolizji_api_odpowiada_konfliktem_i_nie_zapisuje(swiat, kolizja):
    await _post(swiat.klient, swiat.run_id)
    stan = kolizja(zajety=1)

    r = await _post(swiat.klient, swiat.run_id, kind="step_failed")

    assert r.status_code == 409
    assert stan["wywolania"] == uslugi_runow._PROBY_SEQ
    assert await _numery_w_bazie(swiat.run_id) == [1]
    assert (await _run_w_bazie(swiat.run_id)).status == RunStatus.running


async def test_status_runu_po_ponowionej_probie_odpowiada_zdarzeniu_ktore_sie_zapisalo(swiat, kolizja):
    await _post(swiat.klient, swiat.run_id)
    kolizja(zajety=1, ile_zajetych=1)

    r = await _post(swiat.klient, swiat.run_id, kind="step_failed")

    assert r.status_code == 200
    assert await _numery_w_bazie(swiat.run_id) == [1, 2]
    assert (await _run_w_bazie(swiat.run_id)).status == RunStatus.failed


async def test_komunikat_niepelnego_strumienia_przezywa_ponowienie_po_kolizji(swiat, kolizja):
    await _zapisz_wprost(_zdarzenie_bazy(swiat.run_id, 1, swiat.browarek_id, kind="output", step_id="k1"))
    kolizja(zajety=1, ile_zajetych=1)

    r = await _post(swiat.klient, swiat.run_id, kind="step_finished",
                    step_id="k1", wyslane_output=2)

    assert r.status_code == 200
    run = await _run_w_bazie(swiat.run_id)
    assert run.status == RunStatus.to_accept
    assert run.reason == uslugi_runow.komunikat_niepelnego_strumienia(1, 2)


async def test_numer_zdarzenia_liczy_sie_w_obrebie_runu_a_nie_calego_logu(swiat):
    await _zapisz_wprost(_zdarzenie_bazy(swiat.drugi_run_id, 7, swiat.browarek_id),
                         _zdarzenie_bazy(swiat.run_obcego_id, 9, swiat.piekarnia_id))

    r = await _post(swiat.klient, swiat.run_id)

    assert r.json() == {"seq": 1}
    assert await _numery_w_bazie(swiat.run_id) == [1]
    assert await _numery_w_bazie(swiat.drugi_run_id) == [7]  # cudze runy nietkniete


async def test_zdarzenie_innego_zespolu_w_tym_samym_runie_nie_zmienia_numeru(swiat):
    """Obce zdarzenie (seq=5, tenant Piekarni) z run_id runu Browarka: bez filtra
    tenant_id w _nastepny_seq numer wyszedlby 6, z filtrem jest 1."""
    await _zapisz_wprost(_zdarzenie_bazy(swiat.run_id, 5, swiat.piekarnia_id))

    r = await _post(swiat.klient, swiat.run_id)

    assert r.status_code == 200
    assert r.json() == {"seq": 1}
    async with db_session.SessionLocal() as s:  # skutek w bazie: zdarzenie Browarka
        wlasne = (await s.execute(select(Event.seq).where(
            Event.run_id == swiat.run_id,
            Event.tenant_id == swiat.browarek_id))).scalars().all()
    assert wlasne == [1]
    assert await _numery_w_bazie(swiat.run_id) == [1, 5]  # obce nietkniete


async def test_ponowienie_po_kolizji_tez_liczy_numer_w_obrebie_runu(swiat, kolizja):
    await _zapisz_wprost(_zdarzenie_bazy(swiat.run_id, 1, swiat.browarek_id),
                         _zdarzenie_bazy(swiat.drugi_run_id, 7, swiat.browarek_id))
    kolizja(zajety=1, ile_zajetych=1)

    r = await _post(swiat.klient, swiat.run_id)

    assert r.status_code == 200
    assert await _numery_w_bazie(swiat.run_id) == [1, 2]  # nie 8


async def test_licznik_output_kroku_pomija_inne_runy_inne_kroki_i_inne_rodzaje(swiat):
    """Kazdy z trzech wabikow zmienilby licznik z 1 na 2 (== wyslane), gdyby
    odpowiadajacy mu filtr zniknal; wtedy komunikat by nie powstal."""
    b = swiat.browarek_id
    await _zapisz_wprost(
        _zdarzenie_bazy(swiat.run_id, 1, b, kind="output", step_id="k1"),        # liczone
        _zdarzenie_bazy(swiat.run_id, 2, b, kind="output", step_id="k2"),        # inny krok
        _zdarzenie_bazy(swiat.run_id, 3, b, kind="step_started", step_id="k1"),  # inny rodzaj
        _zdarzenie_bazy(swiat.drugi_run_id, 1, b, kind="output", step_id="k1"),  # inny run
    )

    r = await _post(swiat.klient, swiat.run_id, kind="step_finished",
                    step_id="k1", wyslane_output=2)

    assert r.status_code == 200
    run = await _run_w_bazie(swiat.run_id)
    assert run.reason == uslugi_runow.komunikat_niepelnego_strumienia(1, 2)


# --- wezszy except: ponawiamy tylko kolizje numeru (HAR-138) ---

@pytest.fixture
async def sesje_z_kluczami_obcymi():
    """Druga fabryka sesji na tym samym pliku SQLite, ale z egzekwowanymi kluczami
    obcymi (SQLite domyslnie ich nie pilnuje)."""
    engine = create_async_engine(db_session.engine.url)

    @sa_event.listens_for(engine.sync_engine, "connect")
    def _wlacz_klucze_obce(dbapi_conn, _rekord):
        kursor = dbapi_conn.cursor()
        kursor.execute("PRAGMA foreign_keys=ON")
        kursor.close()

    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_naruszenie_klucza_obcego_przy_zapisie_zdarzenia_wychodzi_od_razu_bez_ponowien(
        swiat, sesje_z_kluczami_obcymi, monkeypatch):
    liczba_prob = {"n": 0}
    prawdziwy = uslugi_runow._nastepny_seq

    async def liczony(s, run_id, tenant_id):
        liczba_prob["n"] += 1
        return await prawdziwy(s, run_id, tenant_id)

    monkeypatch.setattr(uslugi_runow, "_nastepny_seq", liczony)
    # zespol istnieje; jedyna wada to run_id, ktorego nie ma w tabeli run
    widmo = Run(id="runa-ktorego-nie-ma", tenant_id=swiat.browarek_id,
                status=RunStatus.running)

    async with sesje_z_kluczami_obcymi() as s:
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            await uslugi_runow.zapisz_zdarzenie(
                s, widmo, event_id=str(uuid.uuid4()), kind="output")

    assert liczba_prob["n"] == 1  # zadnego ponawiania: to nie jest kolizja numeru
    assert await _numery_w_bazie("runa-ktorego-nie-ma") == []
    assert await _numery_w_bazie(swiat.run_id) == []  # nic nie powstalo w innych runach


# --- rozpoznanie kolizji numeru zdarzenia ---

def _blad_sqlite(komunikat: str) -> IntegrityError:
    return IntegrityError("INSERT INTO event ...", {}, sqlite3.IntegrityError(komunikat))


class _BladSterownikaPg(Exception):
    """Atrapa wyjatku asyncpg: tylko pola, z ktorych korzysta adapter SQLAlchemy."""

    def __init__(self, sqlstate: str, constraint_name: str, komunikat: str):
        super().__init__(komunikat)
        self.sqlstate = sqlstate
        self.constraint_name = constraint_name


def _blad_pg(sqlstate: str, constraint_name: str,
             komunikat: str = "blad pg") -> IntegrityError:
    orig = AsyncAdapt_asyncpg_dbapi.IntegrityError(
        komunikat, _BladSterownikaPg(sqlstate, constraint_name, komunikat))
    return IntegrityError("INSERT INTO event ...", {}, orig)


@pytest.mark.parametrize("komunikat", [
    "UNIQUE constraint failed: event.run_id, event.seq",
    "UNIQUE constraint failed: event.seq, event.run_id",  # kolejnosc kolumn bez znaczenia
])
def test_sqlite_kolizja_numeru_zdarzenia_w_runie_jest_rozpoznana(komunikat):
    assert _kolizja_numeru(_blad_sqlite(komunikat)) is True


@pytest.mark.parametrize("komunikat", [
    "UNIQUE constraint failed: event.id",                                # inne ograniczenie
    "UNIQUE constraint failed: event.run_id",                            # podzbior kolumn
    "UNIQUE constraint failed: event.run_id, event.seq, event.step_id",  # nadzbior kolumn
    "UNIQUE constraint failed: events.run_id, events.seq",               # inna tabela
    "NOT NULL constraint failed: event.kind",
    "FOREIGN KEY constraint failed",
])
def test_sqlite_inne_naruszenia_niz_kolizja_numeru_nie_sa_kolizja(komunikat):
    assert _kolizja_numeru(_blad_sqlite(komunikat)) is False


def test_postgres_unikalnosc_numeru_zdarzenia_w_runie_jest_kolizja():
    assert _kolizja_numeru(_blad_pg("23505", "uq_event_run_seq")) is True


def test_postgres_inne_ograniczenie_unikalnosci_nie_jest_kolizja_numeru():
    # sqlstate zgodny, rozstrzyga wylacznie nazwa ograniczenia
    assert _kolizja_numeru(_blad_pg("23505", "inna_nazwa")) is False


def test_postgres_klucz_obcy_nie_jest_kolizja_numeru_nawet_z_nazwa_ograniczenia_numeru():
    # nazwa zgodna, rozstrzyga wylacznie sqlstate (23503 = naruszenie klucza obcego)
    assert _kolizja_numeru(_blad_pg("23503", "uq_event_run_seq")) is False


def test_postgres_komunikat_podobny_do_sqlite_nie_jest_kolizja_numeru():
    """Rozpoznanie PG nie moze isc po tresci komunikatu."""
    blad = _blad_pg("23503", "fk_event_run",
                    komunikat="UNIQUE constraint failed: event.run_id, event.seq")
    assert _kolizja_numeru(blad) is False


class _BladSterownikaBezNazwyOgraniczenia(Exception):
    """Atrapa wyjatku sterownika: ma kod bledu, ale nie niesie nazwy ograniczenia."""

    sqlstate = "23505"


def test_postgres_unikalnosc_bez_nazwy_ograniczenia_nie_jest_kolizja_numeru_i_nie_rzuca():
    orig = AsyncAdapt_asyncpg_dbapi.IntegrityError(
        "blad pg", _BladSterownikaBezNazwyOgraniczenia("blad pg"))
    blad = IntegrityError("INSERT INTO event ...", {}, orig)

    assert _kolizja_numeru(blad) is False


class _BladSterownikaBezWewnetrznegoWyjatku(Exception):
    """Atrapa w stylu psycopg: kod bledu bez zagniezdzonego wyjatku `orig`."""

    sqlstate = "23505"


def test_postgres_blad_bez_zagniezdzonego_wyjatku_sterownika_nie_jest_kolizja_numeru_i_nie_rzuca():
    blad = IntegrityError("INSERT INTO event ...", {}, _BladSterownikaBezWewnetrznegoWyjatku())

    assert _kolizja_numeru(blad) is False


# --- prawdziwa rownoleglosc ---

async def test_dwa_rownolegle_zdarzenia_jednego_runu_dostaja_rozne_numery(swiat):
    async def zapis(kind: str, tekst: str):
        async with db_session.SessionLocal() as s:  # kazdy zapis we wlasnej sesji
            run = await s.get(Run, swiat.run_id)
            return await uslugi_runow.zapisz_zdarzenie(
                s, run, event_id=str(uuid.uuid4()), kind=kind, text=tekst)

    await asyncio.gather(zapis("output", "a"), zapis("output", "b"))

    assert len(set(await _numery_w_bazie(swiat.run_id))) == 2
    historia = (await swiat.klient.get(f"/api/runs/{swiat.run_id}")).json()["events"]
    assert sorted(e["text"] for e in historia) == ["a", "b"]
    assert len({e["seq"] for e in historia}) == 2


async def test_dwa_rownolegle_zadania_http_na_jeden_run_trafiaja_oba_do_historii(swiat):
    odpowiedzi = await asyncio.gather(
        _post(swiat.klient, swiat.run_id, text="a"),
        _post(swiat.klient, swiat.run_id, text="b"),
    )

    assert [o.status_code for o in odpowiedzi] == [200, 200]
    assert len(set(await _numery_w_bazie(swiat.run_id))) == 2
    historia = (await swiat.klient.get(f"/api/runs/{swiat.run_id}")).json()["events"]
    assert sorted(e["text"] for e in historia) == ["a", "b"]


# --- pola zdarzenia trafiaja do bazy ---

async def _zdarzenie_z_bazy(run_id: str) -> Event:
    async with db_session.SessionLocal() as s:  # nowa sesja: odczyt z bazy
        return (await s.execute(select(Event).where(Event.run_id == run_id))).scalar_one()


async def test_zapis_zdarzenia_bez_opcjonalnych_pol_zostawia_w_bazie_puste_teksty_i_brak_pomiaru(swiat):
    async with db_session.SessionLocal() as s:
        run = await s.get(Run, swiat.run_id)
        await uslugi_runow.zapisz_zdarzenie(
            s, run, event_id=str(uuid.uuid4()), kind="output")

    ev = await _zdarzenie_z_bazy(swiat.run_id)
    assert ev.step_id == ""
    assert ev.agent_id == ""
    assert ev.text == ""
    assert ev.cost_usd is None
    assert ev.cost_source == "unavailable"
    assert ev.tokens is None
    assert ev.duration_ms is None


async def test_zapis_zdarzenia_z_pomiarem_i_agentem_zachowuje_w_bazie_kazde_pole(swiat):
    async with db_session.SessionLocal() as s:
        run = await s.get(Run, swiat.run_id)
        await uslugi_runow.zapisz_zdarzenie(
            s, run, event_id=str(uuid.uuid4()), kind="output", step_id="k1",
            agent_id="agent-1", text="tresc", cost_usd=0.25, cost_source="measured", tokens=123, duration_ms=456)

    ev = await _zdarzenie_z_bazy(swiat.run_id)
    assert ev.tenant_id == swiat.browarek_id
    assert ev.kind == "output"
    assert ev.step_id == "k1"
    assert ev.agent_id == "agent-1"
    assert ev.text == "tresc"
    assert ev.cost_usd == 0.25
    assert ev.cost_source == "measured"
    assert ev.tokens == 123
    assert ev.duration_ms == 456


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


def _unikalne_ograniczenia_event() -> dict[str, list[str]]:
    return _z_polaczeniem(lambda c: {
        u["name"]: u["column_names"] for u in inspect(c).get_unique_constraints("event")})


def _indeksy_event() -> dict[str, bool]:
    return _z_polaczeniem(lambda c: {
        i["name"]: bool(i["unique"]) for i in inspect(c).get_indexes("event")})


def _wersja_alembica() -> str:
    return _z_polaczeniem(
        lambda c: c.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _liczba_zdarzen() -> int:
    return _z_polaczeniem(lambda c: c.execute(text("SELECT count(*) FROM event")).scalar_one())


def _wstaw_run_z_duplikatem_seq() -> None:
    def wstaw(c):
        c.execute(text("INSERT INTO tenant (id, name, created_at) VALUES ('t1', 'Browarek', now())"))
        c.execute(text(
            "INSERT INTO run (id, tenant_id, runner_kind, workflow, status, created_at) "
            "VALUES ('r1', 't1', 'local', 'szkielet', 'running', now())"))
        for id_ in ("e1", "e2"):
            c.execute(text(
                "INSERT INTO event (id, tenant_id, run_id, seq, step_id, agent_id, kind, "
                "text, cost_source, at) VALUES (:id, 't1', 'r1', 1, '', '', 'output', '', "
                "'unavailable', now())"), {"id": id_})

    _z_polaczeniem(wstaw, zatwierdz=True)


@pytest.mark.baza
@tylko_postgres
def test_migracja_tworzy_unikalne_ograniczenie_numeru_zdarzenia_w_runie(cfg):
    command.upgrade(cfg, "head")

    assert _unikalne_ograniczenia_event().get("uq_event_run_seq") == ["run_id", "seq"]
    assert "ix_event_run_seq" not in _indeksy_event()
    roznice = _z_polaczeniem(
        lambda c: compare_metadata(MigrationContext.configure(c), Base.metadata))
    assert roznice == [], f"schemat po migracji rozni sie od modelu: {roznice}"


@pytest.mark.baza
@tylko_postgres
def test_migracja_z_duplikatami_numerow_konczy_sie_bledem_i_nie_kasuje_danych(cfg):
    command.upgrade(cfg, "0001")
    _wstaw_run_z_duplikatem_seq()

    with pytest.raises(Exception, match="(?i)duplikat"):
        command.upgrade(cfg, "head")

    assert _liczba_zdarzen() == 2  # nic nie skasowane
    assert "uq_event_run_seq" not in _unikalne_ograniczenia_event()
    assert _wersja_alembica() == "0001"


@pytest.mark.baza
@tylko_postgres
def test_cofniecie_migracji_przywraca_zwykly_indeks_i_znow_pozwala_na_duplikat(cfg):
    command.upgrade(cfg, "head")

    command.downgrade(cfg, "0001")

    assert "uq_event_run_seq" not in _unikalne_ograniczenia_event()
    assert _indeksy_event().get("ix_event_run_seq") is False  # istnieje, nieunikalny
    _wstaw_run_z_duplikatem_seq()  # nie rzuca
    assert _liczba_zdarzen() == 2
