"""HAR-17: status runu wylicza kokpit z rodzaju zdarzenia (maszyna przejsc),
a pole `status` w zadaniu runnera nie ma na niego wplywu."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, Runner, RunStatus

from sesja_testowa import zaloguj_klienta, zaloz_wlasciciela

NAGLOWEK = {"Authorization": "Bearer tajne"}


@pytest.fixture
async def klient(tmp_path, monkeypatch):
    """Zespol "Browarek" z runnerem (token `tajne`), ktory ma otwarty strumien (HAR-26)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    wlasciciel, tenant_id = await zaloz_wlasciciela(maker, nazwa_zespolu="Browarek")
    async with maker() as s:
        s.add(Runner(tenant_id=tenant_id, name="macbook",
                     token_hash=hashlib.sha256(b"tajne").hexdigest(),
                     last_seen_at=datetime.now(timezone.utc)))
        await s.commit()
    async with maker() as s:
        runner_id = (await s.execute(select(Runner.id))).scalars().one()
    # Sam swiezy last_seen_at nie czyni runnera dostepnym - potrzebny otwarty strumien.
    bus.mark_runner_connected(runner_id)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            await zaloguj_klienta(c, maker, wlasciciel)
            yield c
    finally:
        while runner_id in bus.connected_runner_ids():
            bus.mark_runner_disconnected(runner_id)
        getattr(bus, "_rozlaczone", {}).pop(runner_id, None)
        await engine.dispose()


async def _nowy_run(klient) -> str:
    r = await klient.post("/api/runs", json={})
    assert r.status_code == 200
    return r.json()["run_id"]


async def _zdarzenie(klient, run_id: str, kind: str, **reszta):
    return await klient.post("/api/events",
                             json={"run_id": run_id, "kind": kind, "step_id": "s0",
                                   "event_id": str(uuid.uuid4()), **reszta},
                             headers=NAGLOWEK)


async def _status_w_bazie(run_id: str) -> RunStatus:
    async with db_session.SessionLocal() as s:
        return (await s.execute(select(Run.status).where(Run.id == run_id))).scalar_one()


async def _liczba_zdarzen_w_bazie(run_id: str) -> int:
    async with db_session.SessionLocal() as s:
        return (await s.execute(
            select(func.count()).select_from(Event).where(Event.run_id == run_id)
        )).scalar_one()


async def _ustaw_status_w_bazie(run_id: str, status: RunStatus) -> None:
    # Stanow pending/waiting/skipped/aborted nie da sie osiagnac zdarzeniami runnera
    # (to wlasnie je sprawdzamy), wiec ustawiamy je bezposrednio w bazie.
    async with db_session.SessionLocal() as s:
        run = await s.get(Run, run_id)
        run.status = status
        await s.commit()


# --- przez prawdziwe API: skutek czytany z bazy ---

async def test_sukces_kroku_daje_run_do_odbioru(klient):
    run_id = await _nowy_run(klient)
    await _zdarzenie(klient, run_id, "step_finished")
    assert await _status_w_bazie(run_id) == RunStatus.to_accept


async def test_porazka_kroku_daje_run_nieudany(klient):
    run_id = await _nowy_run(klient)
    await _zdarzenie(klient, run_id, "step_failed")
    assert await _status_w_bazie(run_id) == RunStatus.failed


async def test_zwykle_zdarzenie_nie_zmienia_statusu_runu(klient):
    run_id = await _nowy_run(klient)
    await _zdarzenie(klient, run_id, "output", text="linia")
    assert await _status_w_bazie(run_id) == RunStatus.running


async def test_status_do_odbioru_podany_w_ciele_zwyklego_zdarzenia_nie_zmienia_runu(klient):
    run_id = await _nowy_run(klient)
    r = await _zdarzenie(klient, run_id, "output", status="to_accept")
    assert r.status_code == 200
    assert await _status_w_bazie(run_id) == RunStatus.running


async def test_status_nieudany_podany_w_ciele_sukcesu_kroku_nie_psuje_runu(klient):
    run_id = await _nowy_run(klient)
    r = await _zdarzenie(klient, run_id, "step_finished", status="failed")
    assert r.status_code == 200
    assert await _status_w_bazie(run_id) == RunStatus.to_accept


async def test_status_do_odbioru_podany_w_ciele_porazki_kroku_nie_ratuje_runu(klient):
    run_id = await _nowy_run(klient)
    r = await _zdarzenie(klient, run_id, "step_failed", status="to_accept")
    assert r.status_code == 200
    assert await _status_w_bazie(run_id) == RunStatus.failed


async def test_nieznany_status_w_ciele_zdarzenia_jest_pomijany_bez_bledu(klient):
    """Runner starszej wersji wysyla `status`; kokpit nie moze go odrzucac (200)."""
    run_id = await _nowy_run(klient)
    r = await _zdarzenie(klient, run_id, "output", status="cokolwiek")
    assert r.status_code == 200
    assert await _status_w_bazie(run_id) == RunStatus.running


async def test_run_nieudany_zostaje_nieudany_po_sukcesie_kroku(klient):
    run_id = await _nowy_run(klient)
    await _zdarzenie(klient, run_id, "step_failed")
    await _zdarzenie(klient, run_id, "step_finished")
    assert await _status_w_bazie(run_id) == RunStatus.failed


async def test_porazka_po_sukcesie_daje_run_nieudany(klient):
    run_id = await _nowy_run(klient)
    await _zdarzenie(klient, run_id, "step_finished")
    assert await _status_w_bazie(run_id) == RunStatus.to_accept
    await _zdarzenie(klient, run_id, "step_failed")
    assert await _status_w_bazie(run_id) == RunStatus.failed


async def test_kolejny_sukces_kroku_zostawia_run_do_odbioru(klient):
    run_id = await _nowy_run(klient)
    await _zdarzenie(klient, run_id, "step_finished")
    await _zdarzenie(klient, run_id, "step_finished")
    assert await _status_w_bazie(run_id) == RunStatus.to_accept


async def test_zwykle_zdarzenie_nie_zmienia_runu_do_odbioru(klient):
    run_id = await _nowy_run(klient)
    await _zdarzenie(klient, run_id, "step_finished")
    await _zdarzenie(klient, run_id, "output", status="running")
    assert await _status_w_bazie(run_id) == RunStatus.to_accept


async def test_zwykle_zdarzenie_nie_zmienia_runu_nieudanego(klient):
    run_id = await _nowy_run(klient)
    await _zdarzenie(klient, run_id, "step_failed")
    await _zdarzenie(klient, run_id, "output", status="to_accept")
    assert await _status_w_bazie(run_id) == RunStatus.failed


async def test_status_w_ciele_nie_przestawia_runu_nieudanego_na_do_odbioru(klient):
    run_id = await _nowy_run(klient)
    await _zdarzenie(klient, run_id, "step_failed")
    r = await _zdarzenie(klient, run_id, "step_finished", status="to_accept")
    assert r.status_code == 200
    assert await _status_w_bazie(run_id) == RunStatus.failed


async def test_zdarzenie_na_runie_nieudanym_zapisuje_sie_mimo_braku_zmiany_statusu(klient):
    run_id = await _nowy_run(klient)
    await _zdarzenie(klient, run_id, "step_failed")
    assert await _liczba_zdarzen_w_bazie(run_id) == 1
    r = await _zdarzenie(klient, run_id, "step_finished")
    assert r.status_code == 200
    assert r.json()["seq"] == 2
    assert await _liczba_zdarzen_w_bazie(run_id) == 2
    assert await _status_w_bazie(run_id) == RunStatus.failed


@pytest.mark.parametrize("stan", [RunStatus.pending, RunStatus.waiting,
                                  RunStatus.skipped, RunStatus.aborted])
@pytest.mark.parametrize("kind", ["step_finished", "step_failed"])
async def test_zdarzenia_runnera_nie_ruszaja_runu_czekajacego_pominietego_ani_przerwanego(
    klient, stan, kind,
):
    run_id = await _nowy_run(klient)
    await _ustaw_status_w_bazie(run_id, stan)
    r = await _zdarzenie(klient, run_id, kind)
    assert r.status_code == 200
    assert await _status_w_bazie(run_id) == stan
    assert await _liczba_zdarzen_w_bazie(run_id) == 1


# --- czysta maszyna przejsc ---

_KINDY = ("step_finished", "step_failed", "output")

_PRZEJSCIA = [
    # (obecny, kind, oczekiwany)
    (RunStatus.running, "step_finished", RunStatus.to_accept),
    (RunStatus.running, "step_failed", RunStatus.failed),
    (RunStatus.running, "output", RunStatus.running),
    (RunStatus.to_accept, "step_finished", RunStatus.to_accept),
    (RunStatus.to_accept, "step_failed", RunStatus.failed),
    (RunStatus.to_accept, "output", RunStatus.to_accept),
    (RunStatus.failed, "step_finished", RunStatus.failed),
    (RunStatus.failed, "step_failed", RunStatus.failed),
    (RunStatus.failed, "output", RunStatus.failed),
]
_PRZEJSCIA += [(st, k, st)
               for st in (RunStatus.pending, RunStatus.waiting,
                          RunStatus.skipped, RunStatus.aborted)
               for k in _KINDY]


@pytest.mark.parametrize("obecny,kind,oczekiwany", _PRZEJSCIA)
def test_maszyna_przejsc_statusu_runu(obecny, kind, oczekiwany):
    from cockpit.services.runs import nastepny_status
    assert nastepny_status(obecny, kind) == oczekiwany


def test_tabela_przejsc_obejmuje_kazdy_status_i_kazdy_rodzaj_zdarzenia():
    """Dodanie statusu bez decyzji o jego przejsciach ma oblac ten test."""
    pokryte = {(o, k) for o, k, _ in _PRZEJSCIA}
    assert pokryte == {(st, k) for st in RunStatus for k in _KINDY}


@pytest.mark.parametrize("kind", ["", "cost", "log", "STEP_FINISHED", "step_finished "])
@pytest.mark.parametrize("obecny", [RunStatus.running, RunStatus.to_accept])
def test_nieznany_rodzaj_zdarzenia_nie_zmienia_statusu(obecny, kind):
    from cockpit.services.runs import nastepny_status
    assert nastepny_status(obecny, kind) == obecny
