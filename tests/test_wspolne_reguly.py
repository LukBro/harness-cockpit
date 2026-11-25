"""HAR-211: reguly wspolne dla kilku miejsc kodu maja jedna implementacje.

Ponawianie numeru zdarzenia (`_z_ponowieniem_seq`), wybor pierwszego runnera online
(`pierwszy_runner_online`), normalizacja czasu do UTC (`na_utc`) i izolacja zespolow
w odczycie zdarzen. Skutki czytamy z bazy SQLite przez prawdziwe funkcje serwisu.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.db.models import Base, Event, Run, Runner, RunStatus, Tenant
from cockpit.services import runs as uslugi
from cockpit.services.errors import StanNiemozliwy
from cockpit.services.sesje import na_utc


@pytest.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _zespol(maker, nazwa: str) -> str:
    async with maker() as s:
        t = Tenant(name=nazwa)
        s.add(t)
        await s.commit()
        return t.id


async def _runner(maker, tenant_id: str, rid: str, online: bool, polaczenia: list[str]) -> None:
    widziany = datetime.now(timezone.utc) if online else datetime.now(timezone.utc) - timedelta(
        days=1)
    async with maker() as s:
        s.add(Runner(id=rid, tenant_id=tenant_id, name=rid, token_hash=f"h-{rid}",
                     last_seen_at=widziany))
        await s.commit()
    if online:
        bus.mark_runner_connected(rid)
        polaczenia.append(rid)


@pytest.fixture
def polaczenia():
    zanotowane: list[str] = []
    yield zanotowane
    for rid in zanotowane:
        while rid in bus.connected_runner_ids():
            bus.mark_runner_disconnected(rid)
        bus._rozlaczone.pop(rid, None)


async def _run(maker, tenant_id: str, runner_id: str | None = None,
               status: RunStatus = RunStatus.running) -> str:
    async with maker() as s:
        run = Run(tenant_id=tenant_id, runner_id=runner_id, status=status)
        s.add(run)
        await s.commit()
        return run.id


# --- jedna implementacja ponawiania numeru zdarzenia ---

def _nazwy_w(funkcja) -> set[str]:
    drzewo = ast.parse(textwrap.dedent(inspect.getsource(funkcja)))
    return {n.id for n in ast.walk(drzewo) if isinstance(n, ast.Name)}


@pytest.mark.parametrize("funkcja", [uslugi.zapisz_zdarzenie, uslugi.przerwij_runy_offline])
def test_ponawianie_numeru_zdarzenia_ma_jedna_implementacje_we_wspolnym_helperze(funkcja):
    nazwy = _nazwy_w(funkcja)

    assert "_z_ponowieniem_seq" in nazwy
    assert "_PROBY_SEQ" not in nazwy   # limit prob zna tylko helper


async def test_zapis_zdarzenia_i_przerwanie_offline_koncza_po_tym_samym_limicie_prob_numeru(
    maker, monkeypatch,
):
    tenant = await _zespol(maker, "Browarek")
    await _runner(maker, tenant, "r-stary", False, [])
    run_przerwania = await _run(maker, tenant, "r-stary")
    run_zapisu = await _run(maker, tenant)
    async with maker() as s:
        s.add(Event(tenant_id=tenant, run_id=run_przerwania, kind="output", seq=1))
        s.add(Event(tenant_id=tenant, run_id=run_zapisu, kind="output", seq=1))
        await s.commit()
    wywolania: list[str] = []

    async def zajety(s, run_id, tenant_id):
        wywolania.append(run_id)
        return 1   # numer 1 jest juz zapisany w obu runach

    monkeypatch.setattr(uslugi, "_nastepny_seq", zajety)
    monkeypatch.setattr(uslugi, "_PROBY_SEQ", 3)

    async with maker() as s:
        with pytest.raises(StanNiemozliwy, match="po 3 probach"):
            await uslugi.przerwij_runy_offline(s, datetime.now(timezone.utc), set())
    async with maker() as s:
        run = await s.get(Run, run_zapisu)
        with pytest.raises(StanNiemozliwy, match="po 3 probach"):
            await uslugi.zapisz_zdarzenie(s, run, event_id="e-1", kind="output")

    assert wywolania == [run_przerwania] * 3 + [run_zapisu] * 3


# --- pierwszy runner online ---

async def test_pierwszy_runner_online_pomija_offline_i_wybiera_najmniejsze_id_swojego_zespolu(
    maker, polaczenia,
):
    moj, obcy = await _zespol(maker, "Browarek"), await _zespol(maker, "Piekarnia")
    await _runner(maker, obcy, "r-0-obcy", True, polaczenia)       # najmniejsze id, ale obcy
    await _runner(maker, moj, "r-1-offline", False, polaczenia)
    await _runner(maker, moj, "r-3-online", True, polaczenia)
    await _runner(maker, moj, "r-2-online", True, polaczenia)

    async with maker() as s:
        runner = await uslugi.pierwszy_runner_online(
            s, moj, frozenset(bus.connected_runner_ids()))

    assert runner.id == "r-2-online"


# --- czas do UTC ---

def test_czas_bez_strefy_dostaje_utc_a_czas_z_inna_strefa_zostaje_tym_samym_obiektem():
    plus_dwa = datetime(2024, 3, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    bez_strefy = datetime(2024, 3, 1, 12, 0)

    assert na_utc(plus_dwa) is plus_dwa
    assert na_utc(plus_dwa).utcoffset() == timedelta(hours=2)
    assert na_utc(bez_strefy) == datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc)
    assert na_utc(bez_strefy).utcoffset() == timedelta(0)


# --- izolacja zespolow w odczycie zdarzen ---

async def test_zdarzenia_runu_i_przerwania_nie_zwracaja_zdarzen_innego_zespolu(maker):
    moj, obcy = await _zespol(maker, "Browarek"), await _zespol(maker, "Piekarnia")
    run_id = await _run(maker, moj, status=RunStatus.aborted)
    async with maker() as s:
        s.add(Event(tenant_id=moj, run_id=run_id, kind="run_aborted", seq=1, text="moje"))
        # obce zdarzenia o tym samym run_id (kolizja identyfikatora), wyzszy numer
        s.add(Event(tenant_id=obcy, run_id=run_id, kind="run_aborted", seq=2, text="obce"))
        s.add(Event(tenant_id=obcy, run_id=run_id, kind="output", seq=3, text="obce"))
        await s.commit()

    async with maker() as s:
        run = await s.get(Run, run_id)
        zdarzenia = await uslugi.zdarzenia_runu(s, run)
        przerwania = await uslugi.zdarzenia_przerwania(s, [run_id])

    assert [(e.tenant_id, e.seq) for e in zdarzenia] == [(moj, 1)]
    assert [(rid, e["seq"], e["text"]) for rid, e in przerwania] == [(run_id, 1, "moje")]
