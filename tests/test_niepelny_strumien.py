"""HAR-24: run, ktoremu brakuje zdarzen `output`, dostaje oznaczenie niepelnego
strumienia zamiast cichego "kod wyjscia 0". Skutek czytamy z bazy (Run.reason)
i z GET /api/runs/{id}, przez prawdziwa aplikacje."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, Runner, RunStatus, Tenant

TOKEN = "token-runnera"


@dataclass
class Swiat:
    klient: AsyncClient
    run_id: str
    inny_run_id: str  # drugi run tego samego runnera
    piekarnia_id: str  # tenant_id drugiego zespolu (bez runow)


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
        piekarnia = Tenant(name="Piekarnia")
        s.add(piekarnia)
        await s.flush()
        runner = Runner(tenant_id=browarek.id, name="r",
                        token_hash=hashlib.sha256(TOKEN.encode()).hexdigest(),
                        last_seen_at=datetime.now(timezone.utc))
        s.add(runner)
        await s.flush()
        run = Run(tenant_id=browarek.id, runner_id=runner.id, status=RunStatus.running)
        inny = Run(tenant_id=browarek.id, runner_id=runner.id, status=RunStatus.running)
        s.add_all([run, inny])
        await s.commit()
        ids = (run.id, inny.id, piekarnia.id)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            yield Swiat(c, *ids)
    finally:
        await engine.dispose()


async def _wyslij(swiat: Swiat, kind: str, run_id: str | None = None,
                  step_id: str = "krok-1", **extra):
    r = await swiat.klient.post(
        "/api/events",
        json={"run_id": run_id or swiat.run_id, "step_id": step_id,
              "kind": kind, "event_id": str(uuid.uuid4()), **extra},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert r.status_code == 200, r.text


async def _outputy(swiat: Swiat, ile: int, run_id: str | None = None,
                   step_id: str = "krok-1"):
    for i in range(ile):
        await _wyslij(swiat, "output", run_id, step_id, text=f"linia {i}")


async def _reason_w_bazie(run_id: str) -> str | None:
    async with db_session.SessionLocal() as s:
        return (await s.execute(select(Run.reason).where(Run.id == run_id))).scalar_one()


async def _reason_w_api(swiat: Swiat, run_id: str | None = None) -> str | None:
    r = await swiat.klient.get(f"/api/runs/{run_id or swiat.run_id}")
    assert r.status_code == 200
    assert "reason" in r.json()
    return r.json()["reason"]


async def test_run_z_brakujacymi_zdarzeniami_output_ma_oznaczenie_niepelnego_strumienia(swiat):
    await _outputy(swiat, 2)
    await _wyslij(swiat, "step_finished", wyslane_output=3)

    oczekiwane = "niepelny strumien: zapisano 2 z 3 zdarzen output"
    assert await _reason_w_bazie(swiat.run_id) == oczekiwane
    assert await _reason_w_api(swiat) == oczekiwane


async def test_run_ze_zgodna_liczba_zdarzen_output_nie_ma_oznaczenia(swiat):
    await _outputy(swiat, 2)
    await _wyslij(swiat, "step_finished", wyslane_output=2)

    assert await _reason_w_bazie(swiat.run_id) is None
    assert await _reason_w_api(swiat) is None


async def test_nieudany_krok_z_brakujacymi_zdarzeniami_output_tez_ma_oznaczenie(swiat):
    await _outputy(swiat, 1)
    await _wyslij(swiat, "step_failed", wyslane_output=4)

    oczekiwane = "niepelny strumien: zapisano 1 z 4 zdarzen output"
    assert await _reason_w_bazie(swiat.run_id) == oczekiwane
    assert await _reason_w_api(swiat) == oczekiwane


async def test_krok_bez_zadnego_zapisanego_output_zglasza_zapisano_zero(swiat):
    await _wyslij(swiat, "step_finished", wyslane_output=2)

    assert await _reason_w_bazie(swiat.run_id) == \
        "niepelny strumien: zapisano 0 z 2 zdarzen output"


async def test_output_innego_kroku_nie_wlicza_sie_do_biezacego_kroku(swiat):
    await _outputy(swiat, 3, step_id="krok-0")
    await _outputy(swiat, 1, step_id="krok-1")
    await _wyslij(swiat, "step_finished", step_id="krok-1", wyslane_output=3)

    assert await _reason_w_bazie(swiat.run_id) == \
        "niepelny strumien: zapisano 1 z 3 zdarzen output"


async def test_output_innego_kroku_nie_udaje_kompletnego_strumienia_biezacego_kroku(swiat):
    # Razem w runie 3 output == wyslane_output, ale krok-1 ma ich tylko 1.
    await _outputy(swiat, 2, step_id="krok-0")
    await _outputy(swiat, 1, step_id="krok-1")
    await _wyslij(swiat, "step_finished", step_id="krok-1", wyslane_output=3)

    assert await _reason_w_bazie(swiat.run_id) is not None


async def test_output_innego_runu_nie_udaje_kompletnego_strumienia(swiat):
    # Ten sam step_id w drugim runie; razem 3 == wyslane_output, w tym runie 1.
    await _outputy(swiat, 2, run_id=swiat.inny_run_id)
    await _outputy(swiat, 1)
    await _wyslij(swiat, "step_finished", wyslane_output=3)

    assert await _reason_w_bazie(swiat.run_id) == \
        "niepelny strumien: zapisano 1 z 3 zdarzen output"
    assert await _reason_w_bazie(swiat.inny_run_id) is None


async def test_zdarzenie_koncowe_bez_liczby_wyslanych_output_nie_oznacza_runu(swiat):
    await _wyslij(swiat, "step_finished")  # 0 zapisanych, brak deklaracji

    assert await _reason_w_bazie(swiat.run_id) is None
    assert await _reason_w_api(swiat) is None


async def test_deklaracja_zero_wyslanych_output_przy_zerze_zapisanych_nie_oznacza_runu(swiat):
    await _wyslij(swiat, "step_finished", wyslane_output=0)

    assert await _reason_w_bazie(swiat.run_id) is None


async def test_zdarzenie_output_z_liczba_wyslanych_nie_oznacza_runu(swiat):
    # Tylko zdarzenia konczace krok porownuja liczby.
    await _wyslij(swiat, "output", text="x", wyslane_output=5)

    assert await _reason_w_bazie(swiat.run_id) is None


async def test_zgodna_liczba_nie_czysci_wczesniejszego_oznaczenia(swiat):
    await _outputy(swiat, 1, step_id="krok-1")
    await _wyslij(swiat, "step_failed", step_id="krok-1", wyslane_output=2)
    await _wyslij(swiat, "step_failed", step_id="krok-2", wyslane_output=0)

    assert await _reason_w_bazie(swiat.run_id) == \
        "niepelny strumien: zapisano 1 z 2 zdarzen output"


async def test_oznaczenie_niepelnego_strumienia_nie_zmienia_statusu_runu(swiat):
    await _wyslij(swiat, "step_finished", wyslane_output=2)

    async with db_session.SessionLocal() as s:
        status = (await s.execute(
            select(Run.status).where(Run.id == swiat.run_id))).scalar_one()
    assert status == RunStatus.to_accept


async def test_zdarzenie_innego_rodzaju_w_tym_samym_kroku_nie_udaje_brakujacego_output(swiat):
    # Zapisane: 1 output + 1 step_started w tym samym runie i kroku. Wyslano 2 output.
    # Tylko filtr rodzaju odroznia je: bez niego zapisano == 2 i oznaczenia by nie bylo.
    await _wyslij(swiat, "step_started")
    await _outputy(swiat, 1)
    await _wyslij(swiat, "step_finished", wyslane_output=2)

    oczekiwane = "niepelny strumien: zapisano 1 z 2 zdarzen output"
    assert await _reason_w_bazie(swiat.run_id) == oczekiwane
    assert await _reason_w_api(swiat) == oczekiwane


async def test_zdarzenie_innego_rodzaju_w_tym_samym_kroku_nie_psuje_kompletnego_strumienia(swiat):
    # Output zgodne (2 z 2) + obce zdarzenia w kroku: bez filtra rodzaju liczba 4 != 2
    # i run dostalby falszywe oznaczenie.
    await _wyslij(swiat, "step_started")
    await _outputy(swiat, 2)
    await _wyslij(swiat, "tool_call", text="narzedzie")
    await _wyslij(swiat, "step_finished", wyslane_output=2)

    assert await _reason_w_bazie(swiat.run_id) is None
    assert await _reason_w_api(swiat) is None


async def test_deklaracja_zero_wyslanych_output_przy_zapisanym_output_oznacza_run(swiat):
    # Deklaracja 0 to nadal deklaracja: porownanie, nie sprawdzenie "czy podano liczbe".
    await _outputy(swiat, 1)
    await _wyslij(swiat, "step_finished", wyslane_output=0)

    assert await _reason_w_bazie(swiat.run_id) == \
        "niepelny strumien: zapisano 1 z 0 zdarzen output"


async def test_output_innego_zespolu_w_tym_samym_kroku_nie_udaje_kompletnego_strumienia(swiat):
    # Obcy output (tenant Piekarni, ten sam run_id i step_id, seq=5 bez kolizji):
    # bez filtra tenant_id w liczniku zapisano by 2 == wyslane i oznaczenia by nie bylo.
    await _outputy(swiat, 1)
    async with db_session.SessionLocal() as s:
        s.add(Event(tenant_id=swiat.piekarnia_id, run_id=swiat.run_id, seq=5,
                    step_id="krok-1", kind="output"))
        await s.commit()

    await _wyslij(swiat, "step_finished", wyslane_output=2)

    assert await _reason_w_bazie(swiat.run_id) == \
        "niepelny strumien: zapisano 1 z 2 zdarzen output"
