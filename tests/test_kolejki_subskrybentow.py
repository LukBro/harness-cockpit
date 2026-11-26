"""Wolny odbiorca strumienia runu nie rosnie w pamieci i nie wstrzymuje zapisu zdarzen (HAR-204).

Kolejka subskrybenta ma gorny limit. Po przepelnieniu subskrybent jest odlaczany, a jego
strumien konczy sie sygnalem konca; klient dociaga historie przez Last-Event-ID.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Run, RunStatus
from sesja_testowa import zaloguj_klienta, zaloz_runnera, zaloz_wlasciciela
from transport_asgi import TransportASGIStrumieniujacy

LIMIT_CZEKANIA_S = 5
RUN = "run-kolejek"


@pytest.fixture(autouse=True)
def _czysta_szyna():
    yield
    bus._run_subscribers.pop(RUN, None)


def _zdarzenie(seq: int) -> dict:
    return {"seq": seq, "type": "output"}


async def test_kolejka_subskrybenta_ma_gorny_limit_rozmiaru():
    q = bus.subscribe_run(RUN)
    try:
        assert q.maxsize == bus.MAKS_ZDARZEN_W_KOLEJCE_SUBSKRYBENTA > 0
    finally:
        bus.unsubscribe_run(RUN, q)


async def test_wydawca_przy_pelnej_kolejce_nie_czeka_na_odbiorce():
    q = bus.subscribe_run(RUN)
    try:
        for i in range(bus.MAKS_ZDARZEN_W_KOLEJCE_SUBSKRYBENTA):
            await asyncio.wait_for(bus.publish_event(RUN, _zdarzenie(i)), LIMIT_CZEKANIA_S)

        # nikt nie czyta kolejki; zdarzenie ponad limit nie moze zablokowac wydawcy
        await asyncio.wait_for(bus.publish_event(RUN, _zdarzenie(10**6)), LIMIT_CZEKANIA_S)
    finally:
        bus.unsubscribe_run(RUN, q)


async def test_przepelniona_kolejka_nie_rosnie_ponad_limit_i_konczy_sie_sygnalem_konca():
    q = bus.subscribe_run(RUN)
    for i in range(bus.MAKS_ZDARZEN_W_KOLEJCE_SUBSKRYBENTA + 10):
        await bus.publish_event(RUN, _zdarzenie(i))

    assert q.qsize() <= q.maxsize
    odebrane = []
    while not q.empty():
        odebrane.append(q.get_nowait())
    assert odebrane == [bus.KONIEC_STRUMIENIA]   # przepelniona kolejka oddaje tylko sygnal konca


async def test_przepelniony_subskrybent_jest_odlaczony_a_pozostali_dostaja_zdarzenia():
    wolny = bus.subscribe_run(RUN)
    szybki = bus.subscribe_run(RUN)
    for i in range(bus.MAKS_ZDARZEN_W_KOLEJCE_SUBSKRYBENTA):
        await bus.publish_event(RUN, _zdarzenie(i))
        szybki.get_nowait()
    await bus.publish_event(RUN, _zdarzenie(10**6))

    assert bus._run_subscribers[RUN] == [szybki]
    assert szybki.get_nowait() == _zdarzenie(10**6)
    assert wolny.get_nowait() is bus.KONIEC_STRUMIENIA
    bus.unsubscribe_run(RUN, szybki)


async def test_wypisanie_odlaczonego_po_przepelnieniu_nie_rzuca_i_zostawia_pozostalych():
    wolny = bus.subscribe_run(RUN)
    szybki = bus.subscribe_run(RUN)
    for i in range(bus.MAKS_ZDARZEN_W_KOLEJCE_SUBSKRYBENTA + 1):
        await bus.publish_event(RUN, _zdarzenie(i))
        szybki.get_nowait()

    bus.unsubscribe_run(RUN, wolny)   # tak jak `finally` generatora strumienia

    assert bus._run_subscribers[RUN] == [szybki]
    bus.unsubscribe_run(RUN, szybki)


async def test_kolejka_ponizej_limitu_zostaje_zasubskrybowana_i_dostaje_wszystko():
    q = bus.subscribe_run(RUN)
    for i in range(bus.MAKS_ZDARZEN_W_KOLEJCE_SUBSKRYBENTA):
        await bus.publish_event(RUN, _zdarzenie(i))

    assert bus._run_subscribers[RUN] == [q]
    assert [q.get_nowait()["seq"] for _ in range(q.qsize())] == list(
        range(bus.MAKS_ZDARZEN_W_KOLEJCE_SUBSKRYBENTA))
    bus.unsubscribe_run(RUN, q)


async def test_ostatni_odbiorca_wypisany_usuwa_klucz_runu_ze_szyny():
    q = bus.subscribe_run(RUN)
    bus.unsubscribe_run(RUN, q)

    assert RUN not in bus._run_subscribers


async def test_wypisanie_zostawia_klucz_dopoki_jest_inny_odbiorca():
    a = bus.subscribe_run(RUN)
    b = bus.subscribe_run(RUN)
    bus.unsubscribe_run(RUN, a)

    assert bus._run_subscribers[RUN] == [b]
    bus.unsubscribe_run(RUN, b)


async def test_wypisanie_dwa_razy_i_nieznanego_runu_nie_rzuca():
    q = bus.subscribe_run(RUN)
    bus.unsubscribe_run(RUN, q)
    bus.unsubscribe_run(RUN, q)
    bus.unsubscribe_run("run-nieznany", q)

    assert RUN not in bus._run_subscribers
    assert "run-nieznany" not in bus._run_subscribers


async def test_odczyt_szyny_przez_wydawce_nie_tworzy_wpisu_dla_runu_bez_odbiorcow():
    await bus.publish_event("run-bez-odbiorcow", _zdarzenie(1))

    assert "run-bez-odbiorcow" not in bus._run_subscribers


# --- strumien HTTP ------------------------------------------------------------------------


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kolejki.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    wlasciciel, tenant = await zaloz_wlasciciela(maker, "kolejki@example.com")
    runner = await zaloz_runnera(maker, tenant, "runner", "token-kolejek")
    async with maker() as s:
        run = Run(tenant_id=tenant, runner_id=runner, status=RunStatus.running)
        s.add(run)
        await s.commit()
        run_id = run.id
    klient = AsyncClient(transport=TransportASGIStrumieniujacy(app), base_url="http://127.0.0.1")
    await zaloguj_klienta(klient, maker, wlasciciel)
    yield SimpleNamespace(run=run_id, klient=klient)
    await klient.aclose()
    bus._run_subscribers.pop(run_id, None)
    await engine.dispose()


async def _czekaj_az(warunek, komunikat: str) -> None:
    koniec = time.monotonic() + LIMIT_CZEKANIA_S
    while not warunek():
        assert time.monotonic() < koniec, komunikat
        await asyncio.sleep(0.01)


async def test_klient_wolnego_strumienia_jest_rozlaczony_po_przepelnieniu_kolejki(swiat):
    async with swiat.klient.stream("GET", f"/api/runs/{swiat.run}/events") as odp:
        linie = odp.aiter_lines()
        await _czekaj_az(lambda: bus._run_subscribers.get(swiat.run), "brak subskrypcji")
        # Bez wait_for: publish_event nie oddaje petli, wiec generator nie zdazy odebrac zdarzen
        # i kolejka naprawde sie zapelnia (wait_for opakowuje wywolanie w zadanie).
        (q,) = bus._run_subscribers[swiat.run]
        for i in range(bus.MAKS_ZDARZEN_W_KOLEJCE_SUBSKRYBENTA + 1):
            await bus.publish_event(swiat.run, _zdarzenie(i + 1))

        assert swiat.run not in bus._run_subscribers
        assert q.qsize() <= q.maxsize

        async def do_konca():
            return [linia async for linia in linie]
        # strumien konczy sie sam (bez wyjatku); zawieszony oblewa limitem czasu
        await asyncio.wait_for(do_konca(), LIMIT_CZEKANIA_S)
