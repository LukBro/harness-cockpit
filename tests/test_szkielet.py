"""Nazwy testow w jezyku dziedziny, nie technologii (BRO-95)."""
from __future__ import annotations

import hashlib

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Runner, Tenant


@pytest.fixture
async def klient(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with maker() as s:
        t = Tenant(name="Browarek")
        s.add(t)
        await s.flush()
        s.add(Runner(tenant_id=t.id, name="macbook",
                     token_hash=hashlib.sha256(b"tajne").hexdigest()))
        await s.commit()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


async def test_kokpit_odpowiada_ze_zyje(klient):
    r = await klient.get("/api/health")
    assert r.json() == {"status": "ok"}


async def test_runner_bez_tokenu_dostaje_odmowe(klient):
    r = await klient.post("/api/runners/register", json={"name": "x"})
    assert r.status_code == 401


async def test_runner_z_niewaznym_tokenem_dostaje_odmowe_z_podpowiedzia(klient):
    r = await klient.post("/api/runners/register", json={"name": "x"},
                          headers={"Authorization": "Bearer nieznany"})
    assert r.status_code == 401
    assert "Ustawieniach" in r.json()["detail"]


async def test_runner_melduje_sie_tokenem_i_dostaje_identyfikator(klient):
    r = await klient.post("/api/runners/register", json={"name": "macbook"},
                          headers={"Authorization": "Bearer tajne"})
    assert r.status_code == 200
    assert r.json()["runner_id"]


async def test_uruchomienie_tworzy_run_w_stanie_w_toku(klient):
    r = await klient.post("/api/runs", json={"workflow": "szkielet"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    szczegoly = (await klient.get(f"/api/runs/{run_id}")).json()
    assert szczegoly["status"] == "running"
    assert szczegoly["events"] == []


async def test_zdarzenia_dostaja_kolejne_numery(klient):
    run_id = (await klient.post("/api/runs", json={})).json()["run_id"]
    h = {"Authorization": "Bearer tajne"}
    a = await klient.post("/api/events", json={"run_id": run_id, "kind": "output",
                                               "text": "pierwsze"}, headers=h)
    b = await klient.post("/api/events", json={"run_id": run_id, "kind": "output",
                                               "text": "drugie"}, headers=h)
    assert (a.json()["seq"], b.json()["seq"]) == (1, 2)


async def test_brak_pomiaru_kosztu_zapisuje_sie_jako_brak_nie_jako_zero(klient):
    """BRO-87: zero znaczy zmierzone zero. Brak pomiaru to None."""
    run_id = (await klient.post("/api/runs", json={})).json()["run_id"]
    await klient.post("/api/events",
                      json={"run_id": run_id, "kind": "step_finished"},
                      headers={"Authorization": "Bearer tajne"})
    async with db_session.SessionLocal() as s:
        from sqlalchemy import select

        from cockpit.db.models import Event
        ev = (await s.execute(select(Event))).scalars().one()
    assert ev.cost_usd is None
    assert ev.cost_source == "unavailable"


async def test_run_zakonczony_sukcesem_ma_stan_do_odbioru_nie_gotowe(klient):
    """BRO-108: odbior jest czynnoscia czlowieka."""
    run_id = (await klient.post("/api/runs", json={})).json()["run_id"]
    await klient.post("/api/events",
                      json={"run_id": run_id, "kind": "step_finished",
                            "status": "to_accept"},
                      headers={"Authorization": "Bearer tajne"})
    assert (await klient.get(f"/api/runs/{run_id}")).json()["status"] == "to_accept"


async def test_zapis_zera_przy_braku_pomiaru_jest_odrzucany(klient):
    """BRO-87, niezmiennik measured_or_null: zero znaczy ZMIERZONE zero."""
    run_id = (await klient.post("/api/runs", json={})).json()["run_id"]
    r = await klient.post(
        "/api/events",
        json={"run_id": run_id, "kind": "step_finished", "cost_usd": 0.0,
              "cost_source": "unavailable"},
        headers={"Authorization": "Bearer tajne"},
    )
    assert r.status_code == 422
    assert "measured_or_null" in r.json()["detail"]
