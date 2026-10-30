"""Run pamieta ticket; drugi run na tickecie z aktywnym runem wymaga potwierdzenia (HAR-58).

Wariant B: ostrzegamy (409 z id tamtego runu) i pozwalamy po potwierdzeniu. Swiat jak w
`test_dostep_http.py`: dwa zespoly, B zalozony pierwszy, kazdy z runnerem online. Skutki
sprawdzamy w bazie (liczba runow, `Run.ticket_id`), nie tylko w kodzie odpowiedzi.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Run, RunStatus
from cockpit.services import runs as uslugi_runow
from sesja_testowa import zaloguj_klienta, zaloz_runnera, zaloz_wlasciciela
from transport_asgi import TransportASGIStrumieniujacy

LOCALHOST = "http://127.0.0.1"
TICKET = "HAR-58"
INNY_TICKET = "HAR-99"


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rownolegle_runy.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    wlasciciel_b, tenant_b = await zaloz_wlasciciela(maker, "b@example.com", nazwa_zespolu="Piekarnia")
    wlasciciel_a, tenant_a = await zaloz_wlasciciela(maker, "a@example.com", nazwa_zespolu="Browarek")
    runner_a = await zaloz_runnera(maker, tenant_a, "runner-a", "token-a")
    runner_b = await zaloz_runnera(maker, tenant_b, "runner-b", "token-b")

    # aktywny run bez ticketu w kazdym zespole (ticket_id=None)
    async with maker() as s:
        bez_a = Run(tenant_id=tenant_a, runner_id=runner_a, status=RunStatus.running)
        bez_b = Run(tenant_id=tenant_b, runner_id=runner_b, status=RunStatus.running)
        s.add_all([bez_a, bez_b])
        await s.commit()
        runy_startowe = [bez_a.id, bez_b.id]

    polaczone = [runner_a, runner_b]
    for rid in polaczone:
        bus.mark_runner_connected(rid)
    klienci: list[AsyncClient] = []

    def nowy_klient() -> AsyncClient:
        k = AsyncClient(transport=TransportASGIStrumieniujacy(app), base_url=LOCALHOST)
        klienci.append(k)
        return k

    swiat = SimpleNamespace(
        maker=maker, nowy_klient=nowy_klient, tenant_a=tenant_a, tenant_b=tenant_b,
        runner_a=runner_a, runner_b=runner_b,
        wlasciciel_a=wlasciciel_a, wlasciciel_b=wlasciciel_b,
    )
    yield swiat
    for k in klienci:
        await k.aclose()
    for rid in polaczone:
        while rid in bus.connected_runner_ids():
            bus.mark_runner_disconnected(rid)
        getattr(bus, "_rozlaczone", {}).pop(rid, None)
    async with maker() as s:
        wszystkie = (await s.execute(select(Run.id))).scalars().all()
    for rid in set(wszystkie) | set(runy_startowe):
        bus._run_subscribers.pop(rid, None)
    await engine.dispose()


async def _klient_zespolu_a(swiat) -> AsyncClient:
    k = swiat.nowy_klient()
    await zaloguj_klienta(k, swiat.maker, swiat.wlasciciel_a)
    return k


async def _liczba_runow(swiat, tenant_id: str | None = None) -> int:
    async with swiat.maker() as s:
        q = select(func.count()).select_from(Run)
        if tenant_id is not None:
            q = q.where(Run.tenant_id == tenant_id)
        return (await s.execute(q)).scalar_one()


async def _wstaw_run(swiat, tenant_id, runner_id, status, ticket_id) -> str:
    async with swiat.maker() as s:
        run = Run(tenant_id=tenant_id, runner_id=runner_id, status=status, ticket_id=ticket_id)
        s.add(run)
        await s.commit()
        return run.id


async def _run_z_bazy(swiat, run_id: str) -> Run:
    async with swiat.maker() as s:
        return (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()


# --- run pamieta ticket --------------------------------------------------------------------


async def test_run_pamieta_ticket_na_ktorym_dziala(swiat):
    klient = await _klient_zespolu_a(swiat)

    odp = await klient.post("/api/runs", json={"ticket_id": TICKET})

    assert odp.status_code == 200
    run = await _run_z_bazy(swiat, odp.json()["run_id"])
    assert run.ticket_id == TICKET
    assert run.tenant_id == swiat.tenant_a


# --- ostrzezenie o aktywnym runie ------------------------------------------------------------


@pytest.mark.parametrize(
    "status", [RunStatus.pending, RunStatus.running, RunStatus.waiting],
    ids=["pending", "running", "waiting"],
)
async def test_drugi_run_na_tickecie_z_aktywnym_runem_wymaga_potwierdzenia_i_nie_powstaje_bez_niego(
    swiat, status
):
    istniejacy = await _wstaw_run(swiat, swiat.tenant_a, swiat.runner_a, status, TICKET)
    klient = await _klient_zespolu_a(swiat)
    przed = await _liczba_runow(swiat)

    odp = await klient.post("/api/runs", json={"ticket_id": TICKET})

    assert odp.status_code == 409
    assert odp.json()["aktywny_run_id"] == istniejacy
    assert odp.json()["detail"]
    assert await _liczba_runow(swiat) == przed


async def test_potwierdzony_drugi_run_na_tym_samym_tickecie_powstaje(swiat):
    istniejacy = await _wstaw_run(swiat, swiat.tenant_a, swiat.runner_a, RunStatus.running, TICKET)
    klient = await _klient_zespolu_a(swiat)
    przed = await _liczba_runow(swiat)

    odp = await klient.post("/api/runs", json={"ticket_id": TICKET, "potwierdz_rownolegly": True})

    assert odp.status_code == 200
    assert await _liczba_runow(swiat) == przed + 1
    nowy = await _run_z_bazy(swiat, odp.json()["run_id"])
    assert nowy.id != istniejacy
    assert nowy.ticket_id == TICKET
    assert (await _run_z_bazy(swiat, istniejacy)).status == RunStatus.running


@pytest.mark.parametrize(
    "status",
    [RunStatus.to_accept, RunStatus.failed, RunStatus.aborted, RunStatus.skipped],
    ids=["to_accept", "failed", "aborted", "skipped"],
)
async def test_zakonczony_run_na_tickecie_nie_wywoluje_ostrzezenia(swiat, status):
    await _wstaw_run(swiat, swiat.tenant_a, swiat.runner_a, status, TICKET)
    klient = await _klient_zespolu_a(swiat)
    przed = await _liczba_runow(swiat)

    odp = await klient.post("/api/runs", json={"ticket_id": TICKET})

    assert odp.status_code == 200
    assert await _liczba_runow(swiat) == przed + 1
    assert (await _run_z_bazy(swiat, odp.json()["run_id"])).ticket_id == TICKET


async def test_aktywny_run_innego_zespolu_na_tym_samym_tickecie_nie_wywoluje_ostrzezenia(swiat):
    await _wstaw_run(swiat, swiat.tenant_b, swiat.runner_b, RunStatus.running, TICKET)
    klient = await _klient_zespolu_a(swiat)
    przed_a = await _liczba_runow(swiat, swiat.tenant_a)

    odp = await klient.post("/api/runs", json={"ticket_id": TICKET})

    assert odp.status_code == 200
    assert await _liczba_runow(swiat, swiat.tenant_a) == przed_a + 1
    nowy = await _run_z_bazy(swiat, odp.json()["run_id"])
    assert nowy.tenant_id == swiat.tenant_a


async def test_aktywny_run_na_innym_tickecie_tego_samego_zespolu_nie_wywoluje_ostrzezenia(swiat):
    await _wstaw_run(swiat, swiat.tenant_a, swiat.runner_a, RunStatus.running, INNY_TICKET)
    klient = await _klient_zespolu_a(swiat)
    przed = await _liczba_runow(swiat)

    odp = await klient.post("/api/runs", json={"ticket_id": TICKET})

    assert odp.status_code == 200
    assert await _liczba_runow(swiat) == przed + 1


async def test_start_bez_ticketu_nie_wywoluje_ostrzezenia_mimo_aktywnego_runu_bez_ticketu(swiat):
    # swiat ma aktywny run zespolu A bez ticketu
    klient = await _klient_zespolu_a(swiat)
    przed = await _liczba_runow(swiat)

    odp = await klient.post("/api/runs", json={})

    assert odp.status_code == 200
    assert await _liczba_runow(swiat) == przed + 1
    assert (await _run_z_bazy(swiat, odp.json()["run_id"])).ticket_id is None


async def test_start_z_ticketem_nie_koliduje_z_aktywnym_runem_bez_ticketu(swiat):
    klient = await _klient_zespolu_a(swiat)
    przed = await _liczba_runow(swiat)

    odp = await klient.post("/api/runs", json={"ticket_id": TICKET})

    assert odp.status_code == 200
    assert await _liczba_runow(swiat) == przed + 1


# --- ostrzezenie idzie przed wyborem runnera -------------------------------------------------


async def test_ostrzezenie_o_aktywnym_runie_ma_pierwszenstwo_przed_brakiem_runnera_online(swiat):
    from cockpit.services.errors import WymagaPotwierdzenia

    istniejacy = await _wstaw_run(swiat, swiat.tenant_a, swiat.runner_a, RunStatus.running, TICKET)
    przed = await _liczba_runow(swiat)

    async with swiat.maker() as s:
        with pytest.raises(WymagaPotwierdzenia) as wyjatek:
            await uslugi_runow.utworz_run(
                s, swiat.tenant_a, "szkielet", frozenset(), ticket_id=TICKET,
            )

    assert wyjatek.value.aktywny_run_id == istniejacy
    assert istniejacy in str(wyjatek.value)
    assert await _liczba_runow(swiat) == przed


# --- walidacja ticket_id ---------------------------------------------------------------------


@pytest.mark.parametrize("ticket", ["", "x" * 201], ids=["pusty", "201_znakow"])
async def test_niepoprawny_ticket_id_daje_422_i_run_nie_powstaje(swiat, ticket):
    klient = await _klient_zespolu_a(swiat)
    przed = await _liczba_runow(swiat)

    odp = await klient.post("/api/runs", json={"ticket_id": ticket})

    assert odp.status_code == 422
    assert await _liczba_runow(swiat) == przed


async def test_ticket_id_o_dlugosci_200_znakow_jest_przyjmowany(swiat):
    klient = await _klient_zespolu_a(swiat)
    ticket = "x" * 200

    odp = await klient.post("/api/runs", json={"ticket_id": ticket})

    assert odp.status_code == 200
    assert (await _run_z_bazy(swiat, odp.json()["run_id"])).ticket_id == ticket


# --- wiele aktywnych runow na jednym tickecie (stan po potwierdzonym drugim runie) -----------


async def _wstaw_run_jawnie(swiat, run_id: str, utworzony, status=RunStatus.running) -> None:
    async with swiat.maker() as s:
        s.add(Run(
            id=run_id, tenant_id=swiat.tenant_a, runner_id=swiat.runner_a, status=status,
            ticket_id=TICKET, created_at=utworzony,
        ))
        await s.commit()


async def test_przy_dwoch_aktywnych_runach_na_tickecie_ostrzezenie_wskazuje_najstarszy(swiat):
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    # starszy run ma WIEKSZY identyfikator i jest wstawiony jako ostatni: ani kolejnosc
    # wstawienia, ani samo `id` nie daja poprawnej odpowiedzi
    await _wstaw_run_jawnie(swiat, "run-a-nowszy", start + timedelta(minutes=5))
    await _wstaw_run_jawnie(swiat, "run-z-starszy", start, status=RunStatus.waiting)
    klient = await _klient_zespolu_a(swiat)
    przed = await _liczba_runow(swiat)

    odp = await klient.post("/api/runs", json={"ticket_id": TICKET})

    assert odp.status_code == 409
    assert odp.json()["aktywny_run_id"] == "run-z-starszy"
    assert await _liczba_runow(swiat) == przed


async def test_przy_rownym_czasie_startu_ostrzezenie_wskazuje_run_o_mniejszym_identyfikatorze(swiat):
    from datetime import datetime, timezone

    start = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    # run o mniejszym id wstawiony jako drugi: bez rozstrzygniecia po id wygrywa kolejnosc wstawienia
    await _wstaw_run_jawnie(swiat, "run-b", start)
    await _wstaw_run_jawnie(swiat, "run-a", start)
    klient = await _klient_zespolu_a(swiat)
    przed = await _liczba_runow(swiat)

    odp = await klient.post("/api/runs", json={"ticket_id": TICKET})

    assert odp.status_code == 409
    assert odp.json()["aktywny_run_id"] == "run-a"
    assert await _liczba_runow(swiat) == przed
