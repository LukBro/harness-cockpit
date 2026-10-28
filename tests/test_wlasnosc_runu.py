"""HAR-19: zdarzenia do runu przyjmuje wylacznie runner, ktoremu run zlecono.
Skutek czytamy z bazy (liczba zdarzen, status runu), nie z kodu odpowiedzi."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, Runner, RunStatus, Tenant


def _naglowek(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class Swiat:
    klient: AsyncClient
    run_id: str            # zlecony runnerowi-wlascicielowi
    run_bez_runnera_id: str  # zespol Browarek, bez przypisanego runnera
    run_obcego_zespolu_id: str


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    """Zespol "Browarek": runner-wlasciciel (`wlasciciel`) i drugi runner
    (`sasiad`), oba online z poprawnymi tokenami. Osobny zespol "Piekarnia"
    ma runnera (`obcy`) i wlasny run."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    teraz = datetime.now(timezone.utc)

    def runner(tenant_id: str, nazwa: str, token: str) -> Runner:
        return Runner(tenant_id=tenant_id, name=nazwa,
                      token_hash=hashlib.sha256(token.encode()).hexdigest(),
                      last_seen_at=teraz)

    async with maker() as s:
        browarek = Tenant(name="Browarek")
        piekarnia = Tenant(name="Piekarnia")
        s.add_all([browarek, piekarnia])
        await s.flush()
        wlasciciel = runner(browarek.id, "wlasciciel", "token-wlasciciela")
        sasiad = runner(browarek.id, "sasiad", "token-sasiada")
        obcy = runner(piekarnia.id, "obcy", "token-obcego")
        s.add_all([wlasciciel, sasiad, obcy])
        await s.flush()
        run = Run(tenant_id=browarek.id, runner_id=wlasciciel.id,
                  status=RunStatus.running)
        bez_runnera = Run(tenant_id=browarek.id, runner_id=None,
                          status=RunStatus.running)
        run_obcego = Run(tenant_id=piekarnia.id, runner_id=obcy.id,
                         status=RunStatus.running)
        s.add_all([run, bez_runnera, run_obcego])
        await s.commit()
        ids = (run.id, bez_runnera.id, run_obcego.id)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            yield Swiat(c, *ids)
    finally:
        await engine.dispose()


async def _zdarzenie(klient: AsyncClient, run_id: str, token: str,
                     kind: str = "step_failed"):
    # `step_failed` zmienilby status runu na `failed`, gdyby zdarzenie przeszlo.
    return await klient.post("/api/events",
                             json={"run_id": run_id, "kind": kind, "step_id": "s0",
                                   "event_id": str(uuid.uuid4())},
                             headers=_naglowek(token))


async def _status_w_bazie(run_id: str) -> RunStatus:
    async with db_session.SessionLocal() as s:
        return (await s.execute(select(Run.status).where(Run.id == run_id))).scalar_one()


async def _liczba_zdarzen_w_bazie(run_id: str) -> int:
    async with db_session.SessionLocal() as s:
        return (await s.execute(
            select(func.count()).select_from(Event).where(Event.run_id == run_id)
        )).scalar_one()


# --- pozytywna kontrola ---

async def test_runner_ktoremu_zlecono_run_dopisuje_zdarzenia_i_zmienia_status(swiat):
    r = await _zdarzenie(swiat.klient, swiat.run_id, "token-wlasciciela")
    assert r.status_code == 200
    assert await _liczba_zdarzen_w_bazie(swiat.run_id) == 1
    assert await _status_w_bazie(swiat.run_id) == RunStatus.failed


# --- odrzucenie ---

async def test_runner_z_tego_samego_zespolu_nie_dopisuje_zdarzen_do_cudzego_runu(swiat):
    r = await _zdarzenie(swiat.klient, swiat.run_id, "token-sasiada")
    assert r.status_code == 404
    assert await _liczba_zdarzen_w_bazie(swiat.run_id) == 0


async def test_runner_z_tego_samego_zespolu_nie_zmienia_statusu_cudzego_runu(swiat):
    await _zdarzenie(swiat.klient, swiat.run_id, "token-sasiada", "step_failed")
    await _zdarzenie(swiat.klient, swiat.run_id, "token-sasiada", "step_finished")
    assert await _status_w_bazie(swiat.run_id) == RunStatus.running


async def test_runner_innego_zespolu_nie_dopisuje_zdarzen_do_runu(swiat):
    r = await _zdarzenie(swiat.klient, swiat.run_id, "token-obcego")
    assert r.status_code == 404
    assert await _liczba_zdarzen_w_bazie(swiat.run_id) == 0
    assert await _status_w_bazie(swiat.run_id) == RunStatus.running


async def test_runner_nie_dopisuje_zdarzen_do_runu_innego_zespolu_o_cudzym_runnerze(swiat):
    """Odwrotny kierunek: wlasciciel Browarka nie dotyka runu Piekarni."""
    r = await _zdarzenie(swiat.klient, swiat.run_obcego_zespolu_id, "token-wlasciciela")
    assert r.status_code == 404
    assert await _liczba_zdarzen_w_bazie(swiat.run_obcego_zespolu_id) == 0
    assert await _status_w_bazie(swiat.run_obcego_zespolu_id) == RunStatus.running


async def test_run_bez_przypisanego_runnera_nie_przyjmuje_zdarzen_od_zadnego_runnera(swiat):
    for token in ("token-wlasciciela", "token-sasiada"):
        r = await _zdarzenie(swiat.klient, swiat.run_bez_runnera_id, token)
        assert r.status_code == 404
    assert await _liczba_zdarzen_w_bazie(swiat.run_bez_runnera_id) == 0
    assert await _status_w_bazie(swiat.run_bez_runnera_id) == RunStatus.running


async def test_odmowa_dla_cudzego_runu_nie_zdradza_ze_run_istnieje(swiat):
    cudzy = await _zdarzenie(swiat.klient, swiat.run_id, "token-sasiada")
    nieistniejacy = await _zdarzenie(swiat.klient, "00000000-0000-0000-0000-000000000000",
                                     "token-sasiada")
    assert cudzy.status_code == nieistniejacy.status_code == 404
    assert cudzy.json() == nieistniejacy.json()
