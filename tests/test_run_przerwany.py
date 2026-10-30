"""HAR-37: run, ktorego runner przeszedl na offline, dostaje status `aborted`
z powodem "runner offline" i sie nie wznawia.

Online = otwarty strumien ORAZ heartbeat mlodszy niz `runner_offline_after_s` (D12).
Przerwanie ustawia kokpit (nie zdarzenie runnera), po progu + `runner_sweep_s`.
Skutki sprawdzamy odczytem z bazy; reguly uruchamiamy przez produkcyjny kod
(`sprawdz_runnery_offline`, `runner_stream`, `POST /api/events`).
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import app as app_mod
from cockpit.api import bus
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, Runner, RunStatus, Tenant
from cockpit.services import runs as uslugi
from cockpit.services.errors import StanNiemozliwy
from cockpit.settings import Settings, settings

POWOD = "runner offline"
NAGLOWEK = {"Authorization": "Bearer tajne"}

R_STARY = "r-stary"      # Browarek, token "tajne", heartbeat sprzed 10 progow
R_SWIEZY = "r-swiezy"    # Browarek, heartbeat sprzed chwili
P_SWIEZY = "p-swiezy"    # Piekarnia, heartbeat sprzed chwili
P_STARY = "p-stary"      # Piekarnia, heartbeat sprzed 10 progow


def _teraz() -> datetime:
    return datetime.now(timezone.utc)


def _stary() -> datetime:
    return _teraz() - timedelta(seconds=settings.runner_offline_after_s * 10)


def _utc(chwila: datetime) -> datetime:
    """SQLite oddaje daty bez strefy; kokpit zapisuje UTC, wiec tak je czytamy."""
    return chwila if chwila.tzinfo else chwila.replace(tzinfo=timezone.utc)


# --- stan globalny szyny sprzatany po kazdym tescie -----------------------------

@pytest.fixture
def polaczenia():
    """Ewidencja polaczen to stan globalny szyny - sprzatamy to, co dopisal test."""
    zanotowane: list[str] = []
    yield zanotowane
    for runner_id in zanotowane:
        while runner_id in bus.connected_runner_ids():
            bus.mark_runner_disconnected(runner_id)
        bus._rozlaczone.pop(runner_id, None)


@pytest.fixture(autouse=True)
def _sprzatnij_kolejki_runnerow():
    przed = set(bus._runner_queues)
    yield
    for runner_id in set(bus._runner_queues) - przed:
        bus._runner_queues.pop(runner_id, None)


def _polacz(polaczenia: list[str], *runner_ids: str) -> None:
    for rid in runner_ids:
        polaczenia.append(rid)
        bus.mark_runner_connected(rid)


# --- swiat: dwa zespoly, po dwa runnery -----------------------------------------

@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    def runner(rid: str, tenant_id: str, token: str, widziany: datetime) -> Runner:
        return Runner(id=rid, tenant_id=tenant_id, name=rid,
                      token_hash=hashlib.sha256(token.encode()).hexdigest(),
                      last_seen_at=widziany)

    async with maker() as s:
        browarek, piekarnia = Tenant(name="Browarek"), Tenant(name="Piekarnia")
        s.add(browarek)
        await s.flush()
        s.add(piekarnia)
        await s.flush()
        s.add(runner(R_STARY, browarek.id, "tajne", _stary()))
        s.add(runner(R_SWIEZY, browarek.id, "tajne-swiezy", _teraz()))
        s.add(runner(P_SWIEZY, piekarnia.id, "tajne-p1", _teraz()))
        s.add(runner(P_STARY, piekarnia.id, "tajne-p2", _stary()))
        await s.commit()
        ids = SimpleNamespace(browarek=browarek.id, piekarnia=piekarnia.id)
    try:
        async with AsyncClient(transport=ASGITransport(app=app_mod.app),
                               base_url="http://test") as c:
            ids.klient = c
            yield ids
    finally:
        await engine.dispose()


async def _run(tenant_id: str, runner_id: str | None, status: RunStatus = RunStatus.running,
               reason: str | None = None, run_id: str | None = None) -> str:
    async with db_session.SessionLocal() as s:
        run = Run(tenant_id=tenant_id, runner_id=runner_id, status=status, reason=reason)
        if run_id is not None:
            run.id = run_id
        s.add(run)
        await s.commit()
        return run.id


async def _dopisz_zdarzenia(run_id: str, tenant_id: str, ile: int, kind: str = "output") -> None:
    async with db_session.SessionLocal() as s:
        for seq in range(1, ile + 1):
            s.add(Event(tenant_id=tenant_id, run_id=run_id, seq=seq, kind=kind, text=f"l{seq}"))
        await s.commit()


async def _w_bazie(run_id: str) -> tuple[RunStatus, str | None]:
    async with db_session.SessionLocal() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        return run.status, run.reason


async def _zdarzenia(run_id: str) -> list[Event]:
    async with db_session.SessionLocal() as s:
        return list((await s.execute(
            select(Event).where(Event.run_id == run_id).order_by(Event.seq))).scalars().all())


async def _przerwania(run_id: str) -> list[Event]:
    return [e for e in await _zdarzenia(run_id) if e.kind == "run_aborted"]


async def _sprawdz(teraz: datetime | None = None) -> list[str]:
    return await app_mod.sprawdz_runnery_offline(teraz)


async def _ustaw_widziany(runner_id: str, chwila: datetime) -> None:
    async with db_session.SessionLocal() as s:
        runner = await s.get(Runner, runner_id)
        runner.last_seen_at = chwila
        await s.commit()


# --- kryterium 1: run runnera offline jest przerwany z powodem -------------------

async def test_run_runnera_bez_strumienia_i_heartbeatu_zostaje_przerwany_z_powodem_runner_offline(
    swiat,
):
    run_id = await _run(swiat.browarek, R_STARY)

    przerwane = await _sprawdz()

    assert przerwane == [run_id]
    assert await _w_bazie(run_id) == (RunStatus.aborted, POWOD)
    r = await swiat.klient.get(f"/api/runs/{run_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "aborted"
    assert r.json()["reason"] == POWOD
    assert [e["kind"] for e in r.json()["events"]] == ["run_aborted"]


async def test_run_oczekujacy_na_runnerze_offline_tez_zostaje_przerwany(swiat):
    run_id = await _run(swiat.browarek, R_STARY, RunStatus.waiting)

    assert await _sprawdz() == [run_id]
    assert await _w_bazie(run_id) == (RunStatus.aborted, POWOD)


@pytest.mark.parametrize("stan", [RunStatus.to_accept, RunStatus.failed, RunStatus.pending,
                                  RunStatus.skipped, RunStatus.aborted])
async def test_run_w_innym_stanie_niz_w_toku_lub_oczekujacy_nie_jest_ruszany_mimo_offline(
    swiat, stan,
):
    run_id = await _run(swiat.browarek, R_STARY, stan)

    assert await _sprawdz() == []
    assert await _w_bazie(run_id) == (stan, None)
    assert await _zdarzenia(run_id) == []


async def test_run_bez_runnera_nie_jest_przerywany(swiat):
    run_id = await _run(swiat.browarek, None)

    assert await _sprawdz() == []
    assert await _w_bazie(run_id) == (RunStatus.running, None)
    assert await _zdarzenia(run_id) == []


# --- kryterium: run runnera online nie jest przerywany --------------------------

async def test_run_runnera_ze_strumieniem_i_swiezym_heartbeatem_trwa_dalej(swiat, polaczenia):
    _polacz(polaczenia, R_SWIEZY)
    run_id = await _run(swiat.browarek, R_SWIEZY)

    assert await _sprawdz() == []
    assert await _w_bazie(run_id) == (RunStatus.running, None)
    assert await _zdarzenia(run_id) == []


async def test_run_runnera_z_otwartym_strumieniem_ale_starym_heartbeatem_zostaje_przerwany(
    swiat, polaczenia,
):
    _polacz(polaczenia, R_STARY)
    run_id = await _run(swiat.browarek, R_STARY)

    assert await _sprawdz() == [run_id]
    assert await _w_bazie(run_id) == (RunStatus.aborted, POWOD)


async def test_run_runnera_ze_swiezym_heartbeatem_ale_bez_strumienia_zostaje_przerwany(swiat):
    run_id = await _run(swiat.browarek, R_SWIEZY)      # R_SWIEZY nie jest w `polaczone`

    assert await _sprawdz() == [run_id]
    assert await _w_bazie(run_id) == (RunStatus.aborted, POWOD)


# --- kryterium 2: granica czasu wynika z konfiguracji ---------------------------

async def test_run_jest_przerwany_dokladnie_po_progu_offline_z_konfiguracji_a_nie_sekunde_wczesniej(
    swiat, polaczenia,
):
    _polacz(polaczenia, R_SWIEZY)
    run_id = await _run(swiat.browarek, R_SWIEZY)
    widziany = _utc(_teraz()) - timedelta(minutes=30)
    await _ustaw_widziany(R_SWIEZY, widziany)
    prog = timedelta(seconds=settings.runner_offline_after_s)

    assert await _sprawdz(widziany + prog - timedelta(seconds=1)) == []
    assert await _w_bazie(run_id) == (RunStatus.running, None)

    assert await _sprawdz(widziany + prog) == [run_id]
    assert await _w_bazie(run_id) == (RunStatus.aborted, POWOD)


async def test_prog_przerwania_wynika_z_konfiguracji_a_nie_ze_stalej_w_kodzie(
    swiat, polaczenia, monkeypatch,
):
    _polacz(polaczenia, R_SWIEZY)
    run_id = await _run(swiat.browarek, R_SWIEZY)
    widziany = _utc(_teraz()) - timedelta(minutes=30)
    await _ustaw_widziany(R_SWIEZY, widziany)

    monkeypatch.setattr(settings, "runner_offline_after_s", 3600)
    assert await _sprawdz(widziany + timedelta(minutes=45)) == []
    assert await _w_bazie(run_id) == (RunStatus.running, None)

    monkeypatch.setattr(settings, "runner_offline_after_s", 600)
    assert await _sprawdz(widziany + timedelta(minutes=45)) == [run_id]


# --- zespoly: przerwanie nie miesza zespolow -------------------------------------

async def test_przerwanie_runu_jednego_zespolu_nie_rusza_runu_drugiego_zespolu_na_runnerze_online(
    swiat, polaczenia,
):
    _polacz(polaczenia, P_SWIEZY)
    wlasny = await _run(swiat.browarek, R_STARY)
    obcy = await _run(swiat.piekarnia, P_SWIEZY)

    assert await _sprawdz() == [wlasny]

    assert await _w_bazie(wlasny) == (RunStatus.aborted, POWOD)
    assert await _w_bazie(obcy) == (RunStatus.running, None)
    assert await _zdarzenia(obcy) == []


async def test_zdarzenie_przerwania_niesie_tenant_id_zespolu_runu_a_nie_innego_zespolu(swiat):
    browarkowy = await _run(swiat.browarek, R_STARY)
    piekarniany = await _run(swiat.piekarnia, P_STARY)

    assert set(await _sprawdz()) == {browarkowy, piekarniany}

    assert [e.tenant_id for e in await _przerwania(browarkowy)] == [swiat.browarek]
    assert [e.tenant_id for e in await _przerwania(piekarniany)] == [swiat.piekarnia]


async def test_runner_obcego_zespolu_nie_decyduje_o_przerwaniu_runu_nawet_gdy_jest_offline(swiat):
    """Run Browarka wskazuje runnera Piekarni (offline). Tylko warunek zespolu w
    laczeniu run-runner sprawia, ze run zostaje pominiety."""
    run_id = await _run(swiat.browarek, P_STARY)

    assert await _sprawdz() == []
    assert await _w_bazie(run_id) == (RunStatus.running, None)
    assert await _zdarzenia(run_id) == []


# --- zdarzenie przerwania: dane i szyna ----------------------------------------

async def test_zdarzenie_przerwania_dostaje_kolejny_numer_w_obrebie_runu(swiat):
    run_id = await _run(swiat.browarek, R_STARY)
    inny = await _run(swiat.browarek, R_SWIEZY, RunStatus.to_accept)
    await _dopisz_zdarzenia(run_id, swiat.browarek, 2)
    await _dopisz_zdarzenia(inny, swiat.browarek, 5)      # inny run nie przesuwa numeracji

    await _sprawdz()

    zdarzenia = await _zdarzenia(run_id)
    assert [(e.seq, e.kind) for e in zdarzenia] == [(1, "output"), (2, "output"), (3, "run_aborted")]
    assert zdarzenia[-1].text == POWOD
    assert zdarzenia[-1].tenant_id == swiat.browarek


async def test_przerwanie_jest_publikowane_przegladarce_z_numerem_zdarzenia_i_statusem(swiat):
    run_id = await _run(swiat.browarek, R_STARY)
    await _dopisz_zdarzenia(run_id, swiat.browarek, 2)
    q = bus.subscribe_run(run_id)
    try:
        await _sprawdz()
        ev = await asyncio.wait_for(q.get(), 2)
        assert q.empty()
    finally:
        bus.unsubscribe_run(run_id, q)
        bus._run_subscribers.pop(run_id, None)

    assert ev == {"seq": 3, "kind": "run_aborted", "text": POWOD, "step_id": "",
                  "status": "aborted", "reason": POWOD,
                  "text_truncated": False, "text_full_len": None}


async def test_przerwanie_nie_jest_publikowane_subskrybentom_innego_runu(swiat):
    run_id = await _run(swiat.browarek, R_STARY)
    inny = await _run(swiat.browarek, R_SWIEZY, RunStatus.to_accept)
    q = bus.subscribe_run(inny)
    try:
        await _sprawdz()
        assert q.empty()
    finally:
        bus.unsubscribe_run(inny, q)
        bus._run_subscribers.pop(inny, None)
    assert await _w_bazie(run_id) == (RunStatus.aborted, POWOD)


async def test_drugie_sprawdzenie_przerwanego_runu_nic_nie_zmienia_i_nie_dopisuje_zdarzenia(swiat):
    run_id = await _run(swiat.browarek, R_STARY)
    assert await _sprawdz() == [run_id]

    assert await _sprawdz() == []

    assert len(await _przerwania(run_id)) == 1
    assert await _w_bazie(run_id) == (RunStatus.aborted, POWOD)


async def test_sprawdzenie_bez_zadnych_runow_zwraca_pusta_liste(swiat):
    assert await _sprawdz() == []


# --- zdarzenia_przerwania wprost ------------------------------------------------

async def test_zdarzenia_przerwania_zwracaja_ostatnie_zdarzenie_przerwania_kazdego_runu(swiat):
    a = await _run(swiat.browarek, R_STARY, RunStatus.aborted, POWOD)
    b = await _run(swiat.browarek, R_STARY, RunStatus.aborted, POWOD)
    bez = await _run(swiat.browarek, R_STARY, RunStatus.running)
    await _dopisz_zdarzenia(a, swiat.browarek, 2, kind="run_aborted")
    await _dopisz_zdarzenia(b, swiat.browarek, 1, kind="run_aborted")
    await _dopisz_zdarzenia(bez, swiat.browarek, 3, kind="output")

    async with db_session.SessionLocal() as s:
        wynik = await uslugi.zdarzenia_przerwania(s, [a, b, bez, "nieznany-run"])

    assert dict(wynik) == {
        a: {"seq": 2, "kind": "run_aborted", "text": "l2", "step_id": "",
            "status": "aborted", "reason": POWOD,
            "text_truncated": False, "text_full_len": None},
        b: {"seq": 1, "kind": "run_aborted", "text": "l1", "step_id": "",
            "status": "aborted", "reason": POWOD,
            "text_truncated": False, "text_full_len": None},
    }
    assert len(wynik) == 2


# --- kryterium 3: zdarzenie runnera po przerwaniu nie zmienia statusu -----------

async def test_zdarzenie_konca_kroku_po_przerwaniu_trafia_do_logu_a_run_zostaje_przerwany(swiat):
    run_id = await _run(swiat.browarek, R_STARY)
    await _sprawdz()
    przed = len(await _zdarzenia(run_id))

    r = await swiat.klient.post("/api/events", json={"run_id": run_id, "kind": "step_finished",
                                      "step_id": "s0", "event_id": str(uuid.uuid4())},
                                headers=NAGLOWEK)

    assert r.status_code == 200
    zdarzenia = await _zdarzenia(run_id)
    assert len(zdarzenia) == przed + 1
    assert zdarzenia[-1].kind == "step_finished"
    assert await _w_bazie(run_id) == (RunStatus.aborted, POWOD)


async def test_zdarzenie_porazki_kroku_po_przerwaniu_nie_zmienia_przerwanego_runu(swiat):
    run_id = await _run(swiat.browarek, R_STARY)
    await _sprawdz()

    r = await swiat.klient.post("/api/events", json={"run_id": run_id, "kind": "step_failed",
                                      "step_id": "s0", "event_id": str(uuid.uuid4())},
                                headers=NAGLOWEK)

    assert r.status_code == 200
    assert (await _zdarzenia(run_id))[-1].kind == "step_failed"
    assert await _w_bazie(run_id) == (RunStatus.aborted, POWOD)


# --- kryterium 4: runner po powrocie nie dostaje polecenia przerwanego runu -----

async def _pierwsze_po_polaczeniu(strumien) -> dict:
    pierwsze = await asyncio.wait_for(strumien.__anext__(), 2)
    assert json.loads(pierwsze["data"]) == {"type": "polaczono"}
    nastepne = await asyncio.wait_for(strumien.__anext__(), 2)
    return json.loads(nastepne["data"])


async def _otworz_strumien():
    async def nie_rozlaczony() -> bool:
        return False

    async with db_session.SessionLocal() as s:
        odpowiedz = await app_mod.runner_stream(
            request=SimpleNamespace(is_disconnected=nie_rozlaczony),
            authorization="Bearer tajne", s=s)
    return odpowiedz.body_iterator


async def test_runner_po_powrocie_nie_dostaje_polecenia_przerwanego_runu_i_run_sie_nie_wznawia(
    swiat, polaczenia,
):
    przerwany = await _run(swiat.browarek, R_STARY)
    assert await _sprawdz() == [przerwany]
    aktualny = await _run(swiat.browarek, R_STARY)     # nowy run po przerwaniu poprzedniego
    await bus.send_to_runner(
        R_STARY, {"type": "run_step", "run_id": przerwany, "step_id": "s0", "command": ["echo"]})
    await bus.send_to_runner(
        R_STARY, {"type": "run_step", "run_id": aktualny, "step_id": "s0", "command": ["echo"]})
    zdarzen_przed = len(await _zdarzenia(przerwany))
    polaczenia.append(R_STARY)

    r = await swiat.klient.post("/api/runners/register", json={"name": "macbook"},
                                headers=NAGLOWEK)
    assert r.status_code == 200
    strumien = await _otworz_strumien()
    try:
        polecenie = await _pierwsze_po_polaczeniu(strumien)
    finally:
        await strumien.aclose()

    assert polecenie["type"] == "run_step"
    assert polecenie["run_id"] == aktualny
    assert bus.runner_queue(R_STARY).empty()
    assert await _w_bazie(przerwany) == (RunStatus.aborted, POWOD)
    assert len(await _zdarzenia(przerwany)) == zdarzen_przed


async def test_runner_po_powrocie_dostaje_polecenia_ktore_nie_sa_krokami_runu(swiat, polaczenia):
    polaczenia.append(R_STARY)
    await bus.send_to_runner(R_STARY, {"type": "ping"})

    strumien = await _otworz_strumien()
    try:
        polecenie = await _pierwsze_po_polaczeniu(strumien)
    finally:
        await strumien.aclose()

    assert polecenie == {"type": "ping"}


# --- polecenie_aktualne wprost: ktore polecenia wolno jeszcze wyslac -------------

async def _aktualne(polecenie: dict, runner_id: str, tenant_id: str) -> bool:
    async with db_session.SessionLocal() as s:
        return await uslugi.polecenie_aktualne(s, polecenie, runner_id, tenant_id)


def _krok(run_id: str | None) -> dict:
    polecenie: dict = {"type": "run_step", "step_id": "s0", "command": ["echo"]}
    if run_id is not None:
        polecenie["run_id"] = run_id
    return polecenie


async def test_polecenie_ktore_nie_jest_krokiem_runu_jest_zawsze_aktualne(swiat):
    assert await _aktualne({"type": "polaczono"}, R_STARY, swiat.browarek) is True
    assert await _aktualne({"type": "ping", "run_id": "nieznany"}, R_STARY, swiat.browarek) is True


async def test_krok_runu_w_toku_na_tym_runnerze_jest_aktualny(swiat):
    run_id = await _run(swiat.browarek, R_STARY)

    assert await _aktualne(_krok(run_id), R_STARY, swiat.browarek) is True


@pytest.mark.parametrize("stan", [RunStatus.waiting, RunStatus.aborted, RunStatus.to_accept,
                                  RunStatus.failed, RunStatus.pending, RunStatus.skipped])
async def test_krok_runu_ktory_nie_jest_w_toku_nie_jest_aktualny(swiat, stan):
    run_id = await _run(swiat.browarek, R_STARY, stan)

    assert await _aktualne(_krok(run_id), R_STARY, swiat.browarek) is False


async def test_krok_bez_identyfikatora_runu_albo_nieznanego_runu_nie_jest_aktualny(swiat):
    assert await _aktualne(_krok(None), R_STARY, swiat.browarek) is False
    assert await _aktualne(_krok("nieznany-run"), R_STARY, swiat.browarek) is False


async def test_krok_runu_innego_zespolu_nie_jest_aktualny_choc_runner_sie_zgadza(swiat):
    run_id = await _run(swiat.piekarnia, P_SWIEZY)

    assert await _aktualne(_krok(run_id), P_SWIEZY, swiat.piekarnia) is True
    assert await _aktualne(_krok(run_id), P_SWIEZY, swiat.browarek) is False


async def test_krok_runu_innego_runnera_tego_samego_zespolu_nie_jest_aktualny(swiat):
    run_id = await _run(swiat.browarek, R_SWIEZY)

    assert await _aktualne(_krok(run_id), R_SWIEZY, swiat.browarek) is True
    assert await _aktualne(_krok(run_id), R_STARY, swiat.browarek) is False


# --- petla w tle w lifespan ------------------------------------------------------

async def _poczekaj(warunek, limit: float = 5.0) -> None:
    koniec = time.monotonic() + limit
    while not await warunek():
        assert time.monotonic() < koniec, "warunek nie spelnil sie w limicie czasu"
        await asyncio.sleep(0.01)


async def test_aplikacja_po_starcie_sama_przerywa_runy_runnera_offline_co_interwal_z_konfiguracji(
    swiat, monkeypatch,
):
    monkeypatch.setattr(settings, "runner_sweep_s", 0.05)
    run_id = await _run(swiat.browarek, R_STARY)

    async with app_mod.app.router.lifespan_context(app_mod.app):
        async def przerwany() -> bool:
            return (await _w_bazie(run_id))[0] == RunStatus.aborted
        await _poczekaj(przerwany)

    assert await _w_bazie(run_id) == (RunStatus.aborted, POWOD)
    assert len(await _przerwania(run_id)) == 1


async def test_blad_jednego_przebiegu_sprawdzania_nie_zabija_petli_a_zamkniecie_ja_konczy(
    swiat, monkeypatch, caplog,
):
    monkeypatch.setattr(settings, "runner_sweep_s", 0.02)
    wywolania: list[int] = []

    async def sprawdz_z_awaria(teraz=None):
        wywolania.append(1)
        if len(wywolania) == 1:
            raise RuntimeError("awaria pierwszego przebiegu")
        return []

    monkeypatch.setattr(app_mod, "sprawdz_runnery_offline", sprawdz_z_awaria)
    przed = asyncio.all_tasks()
    with caplog.at_level(logging.ERROR):
        async with app_mod.app.router.lifespan_context(app_mod.app):
            async def trzy_przebiegi() -> bool:
                return len(wywolania) >= 3
            await _poczekaj(trzy_przebiegi)
        po_zamknieciu = len(wywolania)
        await asyncio.sleep(0.2)

    assert len(wywolania) == po_zamknieciu          # po zamknieciu petla milczy
    assert asyncio.all_tasks() - przed == set()     # i nie zostawia zadania
    assert "awaria pierwszego przebiegu" in caplog.text or any(
        r.exc_info for r in caplog.records)         # blad przebiegu jest zalogowany
    zalogowane = [r for r in caplog.records if r.exc_info]
    assert [r.getMessage() for r in zalogowane] == [
        "sprawdzanie runnerow offline nie powiodlo sie"]


async def test_petla_sprawdzania_najpierw_czeka_interwal_a_dopiero_potem_sprawdza(
    swiat, monkeypatch,
):
    monkeypatch.setattr(settings, "runner_sweep_s", 3600)
    wywolania: list[int] = []

    async def sprawdz(teraz=None):
        wywolania.append(1)
        return []

    monkeypatch.setattr(app_mod, "sprawdz_runnery_offline", sprawdz)
    async with app_mod.app.router.lifespan_context(app_mod.app):
        await asyncio.sleep(0.2)

    assert wywolania == []


# --- konfiguracja ----------------------------------------------------------------

@pytest.fixture
def czyste_srodowisko(monkeypatch):
    monkeypatch.delenv("COCKPIT_RUNNER_SWEEP_S", raising=False)


def test_interwal_sprawdzania_runnerow_domyslnie_wynosi_10_sekund(czyste_srodowisko):
    assert Settings(_env_file=None).runner_sweep_s == 10


def test_interwal_sprawdzania_runnerow_mozna_ustawic_zmienna_srodowiskowa(
    czyste_srodowisko, monkeypatch,
):
    monkeypatch.setenv("COCKPIT_RUNNER_SWEEP_S", "3")
    assert Settings(_env_file=None).runner_sweep_s == 3


@pytest.mark.parametrize("wartosc", [0, -5])
def test_interwal_sprawdzania_runnerow_zero_albo_ujemny_jest_odrzucony(czyste_srodowisko, wartosc):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, runner_sweep_s=wartosc)


def test_interwal_sprawdzania_runnerow_zero_ze_zmiennej_srodowiskowej_jest_odrzucony(
    czyste_srodowisko, monkeypatch,
):
    monkeypatch.setenv("COCKPIT_RUNNER_SWEEP_S", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# --- maszyna przejsc: aborted jest koncowy ---------------------------------------

@pytest.mark.parametrize("kind", ["step_finished", "step_failed"])
def test_zdarzenie_runnera_nie_wyprowadza_runu_ze_stanu_przerwanego(kind):
    assert uslugi.nastepny_status(RunStatus.aborted, kind) == RunStatus.aborted


# --- przerwanie dotyka tylko wlasciwego runu -------------------------------------

async def test_przerwanie_runu_na_runnerze_offline_nie_rusza_innego_runu_tego_samego_zespolu(
    swiat, polaczenia,
):
    _polacz(polaczenia, R_SWIEZY)
    offline = await _run(swiat.browarek, R_STARY)
    online = await _run(swiat.browarek, R_SWIEZY)

    assert await _sprawdz() == [offline]

    assert await _w_bazie(offline) == (RunStatus.aborted, POWOD)
    assert await _w_bazie(online) == (RunStatus.running, None)
    assert await _zdarzenia(online) == []


# --- wyscig ze zdarzeniem konca kroku --------------------------------------------

@pytest.mark.parametrize("wyprzedzony", ["run-a", "run-b"])
async def test_run_wyprzedzony_przez_koniec_kroku_nie_zostaje_przerwany_a_drugi_zostaje(
    swiat, tmp_path, monkeypatch, wyprzedzony,
):
    """Miedzy odczytem a zapisem przerwania inna sesja ustawia `to_accept`. Raz
    wyscig dotyczy runu o mniejszym id, raz o wiekszym - kolejnosc przetwarzania
    nie jest gwarantowana, wiec w jednym z wariantow wyscig trafia pierwszy."""
    runner_runu = {"run-a": R_STARY, "run-b": R_SWIEZY}
    await _run(swiat.browarek, R_STARY, run_id="run-a")
    await _run(swiat.browarek, R_SWIEZY, run_id="run-b")
    prawdziwe = uslugi.runner_online
    sciezka = tmp_path / "t.db"

    def z_wyscigiem(runner, polaczone, teraz=None):
        wynik = prawdziwe(runner, polaczone, teraz)
        if runner.id == runner_runu[wyprzedzony]:
            polaczenie = sqlite3.connect(sciezka, timeout=5)
            try:
                polaczenie.execute("UPDATE run SET status='to_accept' WHERE id=?", (wyprzedzony,))
                polaczenie.commit()
            finally:
                polaczenie.close()
        return wynik

    monkeypatch.setattr(uslugi, "runner_online", z_wyscigiem)
    drugi = "run-b" if wyprzedzony == "run-a" else "run-a"

    przerwane = await _sprawdz()

    assert przerwane == [drugi]
    assert await _w_bazie(wyprzedzony) == (RunStatus.to_accept, None)
    assert await _przerwania(wyprzedzony) == []
    assert await _w_bazie(drugi) == (RunStatus.aborted, POWOD)
    assert len(await _przerwania(drugi)) == 1


# --- kolizja numeru zdarzenia -----------------------------------------------------

def _zajety_numer_przy_pierwszym_wywolaniu(monkeypatch, zajety: int, zawsze: bool = False) -> None:
    prawdziwa = uslugi._nastepny_seq
    wywolania: list[int] = []

    async def atrapa(s, run_id, tenant_id):
        wywolania.append(1)
        if zawsze or len(wywolania) == 1:
            return zajety
        return await prawdziwa(s, run_id, tenant_id)

    monkeypatch.setattr(uslugi, "_nastepny_seq", atrapa)


async def _przerwij_wprost() -> list[str]:
    async with db_session.SessionLocal() as s:
        return await uslugi.przerwij_runy_offline(s, _teraz(), set())


async def test_kolizja_numeru_zdarzenia_jest_ponawiana_i_run_i_tak_zostaje_przerwany(
    swiat, monkeypatch,
):
    run_id = await _run(swiat.browarek, R_STARY)
    await _dopisz_zdarzenia(run_id, swiat.browarek, 1)
    _zajety_numer_przy_pierwszym_wywolaniu(monkeypatch, zajety=1)

    assert await _przerwij_wprost() == [run_id]

    assert await _w_bazie(run_id) == (RunStatus.aborted, POWOD)
    przerwania = await _przerwania(run_id)
    assert [e.seq for e in przerwania] == [2]


async def test_kolizja_numeru_w_jednym_runie_nie_odbiera_przerwania_kolejnemu_runowi(
    swiat, monkeypatch,
):
    pierwszy = await _run(swiat.browarek, R_STARY, run_id="run-a")
    drugi = await _run(swiat.browarek, R_SWIEZY, run_id="run-b")
    await _dopisz_zdarzenia(pierwszy, swiat.browarek, 1)
    await _dopisz_zdarzenia(drugi, swiat.browarek, 1)
    _zajety_numer_przy_pierwszym_wywolaniu(monkeypatch, zajety=1)

    assert set(await _przerwij_wprost()) == {pierwszy, drugi}

    for run_id in (pierwszy, drugi):
        assert await _w_bazie(run_id) == (RunStatus.aborted, POWOD)
        assert len(await _przerwania(run_id)) == 1


async def test_inny_blad_zapisu_niz_kolizja_numeru_nie_jest_ponawiany_ani_polykany(
    swiat, monkeypatch,
):
    """Gdy kolizji nie rozpoznano jako kolizji numeru, blad wychodzi, a status
    wraca do `running` (rollback)."""
    run_id = await _run(swiat.browarek, R_STARY)
    await _dopisz_zdarzenia(run_id, swiat.browarek, 1)
    _zajety_numer_przy_pierwszym_wywolaniu(monkeypatch, zajety=1)
    monkeypatch.setattr(uslugi, "_kolizja_numeru", lambda e: False)

    with pytest.raises(IntegrityError):
        await _przerwij_wprost()

    assert await _w_bazie(run_id) == (RunStatus.running, None)
    assert await _przerwania(run_id) == []


async def test_trwala_kolizja_numeru_konczy_sie_bledem_po_ograniczonej_liczbie_prob(
    swiat, monkeypatch,
):
    run_id = await _run(swiat.browarek, R_STARY)
    await _dopisz_zdarzenia(run_id, swiat.browarek, 1)
    _zajety_numer_przy_pierwszym_wywolaniu(monkeypatch, zajety=1, zawsze=True)

    with pytest.raises(StanNiemozliwy, match="probach"):
        await _przerwij_wprost()

    assert await _w_bazie(run_id) == (RunStatus.running, None)
    assert await _przerwania(run_id) == []


# --- zdarzenia_przerwania: pomijanie i zespol ------------------------------------

async def test_zdarzenia_przerwania_pomijaja_nieznany_run_i_run_bez_przerwania_ale_biora_kolejne(
    swiat,
):
    bez = await _run(swiat.browarek, R_STARY, RunStatus.running)
    await _dopisz_zdarzenia(bez, swiat.browarek, 2, kind="output")
    poprawny = await _run(swiat.browarek, R_STARY, RunStatus.aborted, POWOD)
    await _dopisz_zdarzenia(poprawny, swiat.browarek, 1, kind="run_aborted")

    async with db_session.SessionLocal() as s:
        po_nieznanym = await uslugi.zdarzenia_przerwania(s, ["nieznany-run", poprawny])
        po_runie_bez = await uslugi.zdarzenia_przerwania(s, [bez, poprawny])

    assert [r for r, _ in po_nieznanym] == [poprawny]
    assert [r for r, _ in po_runie_bez] == [poprawny]


async def test_zdarzenie_przerwania_innego_zespolu_o_tym_samym_runie_nie_jest_brane(swiat):
    run_id = await _run(swiat.browarek, R_STARY, RunStatus.aborted, POWOD)
    await _dopisz_zdarzenia(run_id, swiat.browarek, 1, kind="run_aborted")
    async with db_session.SessionLocal() as s:      # Event.tenant_id nie ma klucza obcego
        s.add(Event(tenant_id=swiat.piekarnia, run_id=run_id, seq=2, kind="run_aborted",
                    text="obce"))
        await s.commit()

    async with db_session.SessionLocal() as s:
        wynik = await uslugi.zdarzenia_przerwania(s, [run_id])

    assert [(r, e["seq"], e["text"]) for r, e in wynik] == [(run_id, 1, "l1")]
