"""HAR-25: numer zdarzenia (`seq`) jest unikalny w obrebie runu, a gwarantuje to baza.

Skutek czytamy z bazy (nowa sesja), nie z kodu odpowiedzi. Testy migracji sa
synchroniczne (env.py Alembica sam uruchamia petle zdarzen) i wymagaja PostgreSQL.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, Runner, RunStatus, Tenant
from cockpit.services import runs as uslugi_runow
from cockpit.services.errors import StanNiemozliwy

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
    async with maker() as s:
        # Browarek pierwszy: GET /api/runs/{id} wybiera pierwszy zespol
        browarek = Tenant(name="Browarek")
        s.add(browarek)
        await s.flush()
        piekarnia = Tenant(name="Piekarnia")
        s.add(piekarnia)
        await s.flush()
        wlasciciel = Runner(
            tenant_id=browarek.id, name="wlasciciel", last_seen_at=teraz,
            token_hash=hashlib.sha256(TOKEN.encode()).hexdigest())
        obcy = Runner(
            tenant_id=piekarnia.id, name="obcy", last_seen_at=teraz,
            token_hash=hashlib.sha256(b"token-obcego").hexdigest())
        s.add_all([wlasciciel, obcy])
        await s.flush()
        run = Run(tenant_id=browarek.id, runner_id=wlasciciel.id, status=RunStatus.running)
        drugi = Run(tenant_id=browarek.id, runner_id=wlasciciel.id, status=RunStatus.running)
        run_obcego = Run(tenant_id=piekarnia.id, runner_id=obcy.id, status=RunStatus.running)
        s.add_all([run, drugi, run_obcego])
        await s.commit()
        ids = (run.id, drugi.id, run_obcego.id, browarek.id, piekarnia.id)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
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
    return klient.post("/api/events", json={"run_id": run_id, "kind": kind, **extra},
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
            s, swiat.run_id, (await uslugi_runow.domyslny_tenant(s)).id)
        with pytest.raises(StanNiemozliwy):
            await uslugi_runow.zapisz_zdarzenie(s, run, kind="output")

    assert stan["wywolania"] == uslugi_runow._PROBY_SEQ  # dokladnie tyle, nie wiecej, nie mniej
    assert await _numery_w_bazie(swiat.run_id) == [1]


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


# --- prawdziwa rownoleglosc ---

async def test_dwa_rownolegle_zdarzenia_jednego_runu_dostaja_rozne_numery(swiat):
    async def zapis(kind: str, tekst: str):
        async with db_session.SessionLocal() as s:  # kazdy zapis we wlasnej sesji
            run = await s.get(Run, swiat.run_id)
            return await uslugi_runow.zapisz_zdarzenie(s, run, kind=kind, text=tekst)

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


@tylko_postgres
def test_migracja_tworzy_unikalne_ograniczenie_numeru_zdarzenia_w_runie(cfg):
    command.upgrade(cfg, "head")

    assert _unikalne_ograniczenia_event().get("uq_event_run_seq") == ["run_id", "seq"]
    assert "ix_event_run_seq" not in _indeksy_event()
    roznice = _z_polaczeniem(
        lambda c: compare_metadata(MigrationContext.configure(c), Base.metadata))
    assert roznice == [], f"schemat po migracji rozni sie od modelu: {roznice}"


@tylko_postgres
def test_migracja_z_duplikatami_numerow_konczy_sie_bledem_i_nie_kasuje_danych(cfg):
    command.upgrade(cfg, "0001")
    _wstaw_run_z_duplikatem_seq()

    with pytest.raises(Exception, match="(?i)duplikat"):
        command.upgrade(cfg, "head")

    assert _liczba_zdarzen() == 2  # nic nie skasowane
    assert "uq_event_run_seq" not in _unikalne_ograniczenia_event()
    assert _wersja_alembica() == "0001"


@tylko_postgres
def test_cofniecie_migracji_przywraca_zwykly_indeks_i_znow_pozwala_na_duplikat(cfg):
    command.upgrade(cfg, "head")

    command.downgrade(cfg, "0001")

    assert "uq_event_run_seq" not in _unikalne_ograniczenia_event()
    assert _indeksy_event().get("ix_event_run_seq") is False  # istnieje, nieunikalny
    _wstaw_run_z_duplikatem_seq()  # nie rzuca
    assert _liczba_zdarzen() == 2
