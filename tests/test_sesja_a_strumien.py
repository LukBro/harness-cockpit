"""Strumien runu a sesja: wygasla albo uniewazniona sesja zamyka strumien komunikatem (HAR-70).

Run i runner dzialaja dalej: status runu zostaje `running`, a zdarzenia runnera nadal sie zapisuja.
Skutki sprawdzamy w bazie i w szynie, nie tylko w tym, co przyszlo klientowi.
Limit kontroli sesji obnizamy na obiekcie `cockpit.settings.settings` (czytany przy kazdym oczekiwaniu).
"""
from __future__ import annotations

import asyncio
import importlib
import json
import time
import re
from contextlib import aclosing, contextmanager, suppress
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, RolaWZespole, Run, RunStatus
from cockpit.services import sesje
from cockpit.settings import Settings, settings
from sesja_testowa import zaloguj_klienta, zaloz_czlonka, zaloz_runnera, zaloz_wlasciciela
from transport_asgi import TransportASGIStrumieniujacy

LOCALHOST = "http://127.0.0.1"
TOKEN_RUNNERA = "token-runnera-strumienia"
LIMIT_CZEKANIA_S = 5
KROTKI_LIMIT_KONTROLI_S = 0.05


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sesja_a_strumien.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    wlasciciel, tenant = await zaloz_wlasciciela(maker, "wlasciciel@example.com")
    czlonek = await zaloz_czlonka(maker, tenant, RolaWZespole.member, "czlonek@example.com")
    runner = await zaloz_runnera(maker, tenant, "runner", TOKEN_RUNNERA)
    async with maker() as s:
        run = Run(tenant_id=tenant, runner_id=runner, status=RunStatus.running)
        s.add(run)
        await s.commit()
        run_id = run.id
    bus.mark_runner_connected(runner)

    klienci: list[AsyncClient] = []

    def nowy_klient() -> AsyncClient:
        k = AsyncClient(transport=TransportASGIStrumieniujacy(app), base_url=LOCALHOST)
        klienci.append(k)
        return k

    yield SimpleNamespace(
        maker=maker, nowy_klient=nowy_klient, tenant=tenant, run=run_id, runner=runner,
        wlasciciel=wlasciciel, czlonek=czlonek,
    )
    for k in klienci:
        await k.aclose()
    while runner in bus.connected_runner_ids():
        bus.mark_runner_disconnected(runner)
    getattr(bus, "_rozlaczone", {}).pop(runner, None)
    bus._run_subscribers.pop(run_id, None)
    await engine.dispose()


async def _zalogowany(swiat, user) -> AsyncClient:
    k = swiat.nowy_klient()
    await zaloguj_klienta(k, swiat.maker, user)
    return k


async def _zdarzenia_sse(odp):
    """Zdarzenia SSE jako pary (nazwa, dane); komentarze (ping) i `id:` pomijamy."""
    nazwa, dane = None, []
    async for linia in odp.aiter_lines():
        if linia == "":
            if nazwa is not None or dane:
                yield nazwa, "\n".join(dane)
            nazwa, dane = None, []
        elif linia.startswith("event:"):
            nazwa = linia[len("event:"):].strip()
        elif linia.startswith("data:"):
            dane.append(linia[len("data:"):].strip())


async def _nastepne(strumien):
    return await asyncio.wait_for(anext(strumien), LIMIT_CZEKANIA_S)


async def _do_konca(strumien) -> list[tuple[str | None, str]]:
    """Czyta strumien do wyczerpania iteratora; zawieszony strumien oblewa test limitem czasu."""
    async def zbierz():
        return [z async for z in strumien]
    return await asyncio.wait_for(zbierz(), LIMIT_CZEKANIA_S)


async def _poczekaj_na_subskrypcje(run_id: str) -> None:
    koniec = time.monotonic() + LIMIT_CZEKANIA_S
    while not bus._run_subscribers.get(run_id):
        assert time.monotonic() < koniec, "strumien nie zasubskrybowal szyny"
        await asyncio.sleep(0.01)


async def _poczekaj_na_brak_subskrypcji(run_id: str) -> None:
    koniec = time.monotonic() + LIMIT_CZEKANIA_S
    while bus._run_subscribers.get(run_id, []):
        assert time.monotonic() < koniec, "subskrypcja szyny nie zostala zwolniona"
        await asyncio.sleep(0.01)


async def _stan_runu(swiat) -> RunStatus:
    async with swiat.maker() as s:
        return (await s.execute(select(Run.status).where(Run.id == swiat.run))).scalar_one()


async def _zdarzenia_w_bazie(swiat) -> list[Event]:
    async with swiat.maker() as s:
        return list((await s.execute(select(Event).where(Event.run_id == swiat.run))).scalars().all())


@contextmanager
def _zlicz_instrukcje_sesji():
    """Zlicza instrukcje SQL dotykajace tabeli sesji (UPDATE i SELECT osobno); listener zdejmowany w finally."""
    licznik = {"update": 0, "select": 0}
    silnik = db_session.engine.sync_engine

    def nasluch(conn, cursor, statement, parameters, context, executemany):
        sql = statement.lstrip().lower()
        if not re.search(r"\bsesja\b", sql):
            return
        if sql.startswith("update"):
            licznik["update"] += 1
        elif sql.startswith("select"):
            licznik["select"] += 1

    event.listen(silnik, "before_cursor_execute", nasluch)
    try:
        yield licznik
    finally:
        event.remove(silnik, "before_cursor_execute", nasluch)


async def _wyloguj_wszedzie(swiat, user) -> None:
    async with swiat.maker() as s:
        await sesje.wyloguj_wszedzie(s, user.id)


def _sciezka(swiat) -> str:
    return f"/api/runs/{swiat.run}/stream"


# --- sesja uniewazniona przy otwartym strumieniu ------------------------------------------------


async def test_wylogowanie_wszedzie_konczy_otwarty_strumien_komunikatem_sesja_wygasla(
    swiat, monkeypatch
):
    monkeypatch.setattr(settings, "sesja_kontrola_s", KROTKI_LIMIT_KONTROLI_S)
    klient = await _zalogowany(swiat, swiat.wlasciciel)
    inne_urzadzenie = await _zalogowany(swiat, swiat.wlasciciel)

    async with klient.stream("GET", _sciezka(swiat)) as odp:
        assert odp.status_code == 200
        async with aclosing(_zdarzenia_sse(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run)
            wylogowanie = await inne_urzadzenie.post("/api/konto/wyloguj-wszedzie")
            assert wylogowanie.status_code == 204
            zdarzenia = await _do_konca(strumien)

    assert [nazwa for nazwa, _ in zdarzenia] == ["sesja_wygasla"]


async def test_zdarzenie_sesja_wygasla_niesie_komunikat_o_wygasnieciu_sesji(swiat, monkeypatch):
    monkeypatch.setattr(settings, "sesja_kontrola_s", KROTKI_LIMIT_KONTROLI_S)
    klient = await _zalogowany(swiat, swiat.wlasciciel)

    async with klient.stream("GET", _sciezka(swiat)) as odp:
        async with aclosing(_zdarzenia_sse(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run)
            await _wyloguj_wszedzie(swiat, swiat.wlasciciel)
            nazwa, dane = await _nastepne(strumien)

    assert nazwa == "sesja_wygasla"
    assert json.loads(dane) == {"komunikat": sesje.KOMUNIKAT_SESJI}


async def test_run_i_runner_dzialaja_dalej_po_zamknieciu_strumienia_wygasla_sesja(
    swiat, monkeypatch
):
    monkeypatch.setattr(settings, "sesja_kontrola_s", KROTKI_LIMIT_KONTROLI_S)
    klient = await _zalogowany(swiat, swiat.wlasciciel)

    async with klient.stream("GET", _sciezka(swiat)) as odp:
        async with aclosing(_zdarzenia_sse(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run)
            await _wyloguj_wszedzie(swiat, swiat.wlasciciel)
            await _do_konca(strumien)

    assert await _stan_runu(swiat) == RunStatus.running
    assert swiat.runner in bus.connected_runner_ids()

    runner = swiat.nowy_klient()
    odpowiedz = await runner.post(
        "/api/events",
        headers={"Authorization": f"Bearer {TOKEN_RUNNERA}"},
        json={"run_id": swiat.run, "event_id": "e1", "step_id": "s0", "kind": "log", "text": "x"},
    )
    assert odpowiedz.status_code == 200
    zapisane = await _zdarzenia_w_bazie(swiat)
    assert [(e.event_id, e.kind, e.tenant_id) for e in zapisane] == [("e1", "log", swiat.tenant)]
    assert await _stan_runu(swiat) == RunStatus.running


async def test_zamkniety_przez_wygasla_sesje_strumien_zwalnia_subskrypcje_w_szynie(
    swiat, monkeypatch
):
    monkeypatch.setattr(settings, "sesja_kontrola_s", KROTKI_LIMIT_KONTROLI_S)
    klient = await _zalogowany(swiat, swiat.wlasciciel)

    async with klient.stream("GET", _sciezka(swiat)) as odp:
        async with aclosing(_zdarzenia_sse(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run)
            assert len(bus._run_subscribers[swiat.run]) == 1
            await _wyloguj_wszedzie(swiat, swiat.wlasciciel)
            await _do_konca(strumien)
            await _poczekaj_na_brak_subskrypcji(swiat.run)

    assert bus._run_subscribers.get(swiat.run, []) == []


# --- kiedy kontrola idzie ---------------------------------------------------------------------


async def test_kontrola_sesji_idzie_takze_gdy_w_szynie_nie_ma_zadnego_zdarzenia(swiat, monkeypatch):
    """Tylko kontrola po uplywie limitu moze tu zadzialac: nic nie budzi czekania na kolejke."""
    monkeypatch.setattr(settings, "sesja_kontrola_s", KROTKI_LIMIT_KONTROLI_S)
    klient = await _zalogowany(swiat, swiat.wlasciciel)

    async with klient.stream("GET", _sciezka(swiat)) as odp:
        async with aclosing(_zdarzenia_sse(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run)
            await _wyloguj_wszedzie(swiat, swiat.wlasciciel)
            nazwa, _ = await _nastepne(strumien)

    assert nazwa == "sesja_wygasla"


async def test_zdarzenie_opublikowane_po_uplywie_okna_kontroli_od_uniewaznienia_sesji_nie_trafia_do_klienta(
    swiat, monkeypatch
):
    """Okno kontroli minelo, zanim zdarzenie dotarlo do strumienia: kontrola idzie przed jego wyslaniem."""
    monkeypatch.setattr(settings, "sesja_kontrola_s", KROTKI_LIMIT_KONTROLI_S)
    klient = await _zalogowany(swiat, swiat.wlasciciel)

    async with klient.stream("GET", _sciezka(swiat)) as odp:
        async with aclosing(_zdarzenia_sse(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run)
            await _wyloguj_wszedzie(swiat, swiat.wlasciciel)
            # Blokada petli zdarzen: okno mija, a strumien nie zdazyl go zauwazyc;
            # zdarzenie trafia do kolejki razem z naleznym terminem kontroli.
            time.sleep(3 * KROTKI_LIMIT_KONTROLI_S)
            await bus.publish_event(
                swiat.run, {"seq": 1, "kind": "log", "text": "tresc-po-wylogowaniu"})
            zdarzenia = await _do_konca(strumien)

    assert [nazwa for nazwa, _ in zdarzenia] == ["sesja_wygasla"]
    assert all("tresc-po-wylogowaniu" not in dane for _, dane in zdarzenia)


# --- wazna sesja: strumien zyje -----------------------------------------------------------------


async def test_wazna_sesja_nie_zamyka_strumienia_mimo_kilku_cykli_kontroli_i_dostarcza_zdarzenie(
    swiat, monkeypatch
):
    monkeypatch.setattr(settings, "sesja_kontrola_s", KROTKI_LIMIT_KONTROLI_S)
    klient = await _zalogowany(swiat, swiat.wlasciciel)

    async with klient.stream("GET", _sciezka(swiat)) as odp:
        async with aclosing(_zdarzenia_sse(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run)
            nastepne = asyncio.ensure_future(anext(strumien))
            # ok. dziesieciu cykli kontroli; asyncio.wait nie anuluje czytania, gdy minie czas
            gotowe, _ = await asyncio.wait({nastepne}, timeout=10 * KROTKI_LIMIT_KONTROLI_S)
            assert not gotowe, f"strumien wazniej sesji sie zamknal: {nastepne.result()!r}"

            await bus.publish_event(swiat.run, {"seq": 1, "kind": "log", "text": "po-cyklach"})
            nazwa, dane = await asyncio.wait_for(nastepne, LIMIT_CZEKANIA_S)

    assert nazwa == "run"
    assert json.loads(dane)["text"] == "po-cyklach"


@pytest.mark.parametrize("przypadek", ["inny_uzytkownik", "druga_sesja_tego_samego_uzytkownika"])
async def test_uniewaznienie_cudzej_sesji_nie_zamyka_strumienia_tej_sesji(
    swiat, monkeypatch, przypadek
):
    monkeypatch.setattr(settings, "sesja_kontrola_s", KROTKI_LIMIT_KONTROLI_S)
    klient = await _zalogowany(swiat, swiat.wlasciciel)

    async with klient.stream("GET", _sciezka(swiat)) as odp:
        async with aclosing(_zdarzenia_sse(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run)
            nastepne = asyncio.ensure_future(anext(strumien))
            if przypadek == "inny_uzytkownik":
                await _wyloguj_wszedzie(swiat, swiat.czlonek)
            else:
                async with swiat.maker() as s:
                    druga, _ = await sesje.utworz(s, swiat.wlasciciel)
                async with swiat.maker() as s:
                    await sesje.wyloguj(s, druga)
            gotowe, _ = await asyncio.wait({nastepne}, timeout=10 * KROTKI_LIMIT_KONTROLI_S)
            assert not gotowe, f"strumien zamkniety cudzym wylogowaniem: {nastepne.result()!r}"

            await bus.publish_event(swiat.run, {"seq": 1, "kind": "log", "text": "nadal-zyje"})
            nazwa, dane = await asyncio.wait_for(nastepne, LIMIT_CZEKANIA_S)

    assert nazwa == "run"
    assert json.loads(dane)["text"] == "nadal-zyje"


async def test_awaria_kontroli_sesji_nie_jest_zglaszana_jako_wygasniecie_sesji_a_subskrypcja_jest_zwolniona(
    swiat, monkeypatch
):
    """Inny wyjątek niż brak dostępu (np. awaria bazy) kończy strumień bez `sesja_wygasla`."""
    monkeypatch.setattr(settings, "sesja_kontrola_s", KROTKI_LIMIT_KONTROLI_S)
    klient = await _zalogowany(swiat, swiat.wlasciciel)
    odebrane: list[str | None] = []

    wywolania: list[str] = []

    async def awaria(s, identyfikator):
        wywolania.append(identyfikator)
        raise RuntimeError("baza niedostepna")

    async def czytaj(strumien):
        try:
            async for nazwa, _ in strumien:
                odebrane.append(nazwa)
        except Exception:  # noqa: BLE001 - zerwanie strumienia moze wyjsc jako wyjatek transportu
            pass

    try:
        async with klient.stream("GET", _sciezka(swiat)) as odp:
            async with aclosing(_zdarzenia_sse(odp)) as strumien:
                await _poczekaj_na_subskrypcje(swiat.run)
                monkeypatch.setattr(sesje, "rozstrzygnij", awaria)
                await asyncio.wait_for(czytaj(strumien), LIMIT_CZEKANIA_S)
                await _poczekaj_na_brak_subskrypcji(swiat.run)
    except Exception:  # noqa: BLE001 - j.w., przy zamykaniu odpowiedzi
        pass

    assert wywolania, "kontrola sesji nie zostala wywolana"
    assert "sesja_wygasla" not in odebrane
    assert bus._run_subscribers.get(swiat.run, []) == []


# --- kontrola co okno, nie przy kazdym zdarzeniu (HAR-199) ------------------------------------


async def test_sto_zdarzen_w_oknie_kontroli_sesji_nie_powoduje_wiecej_niz_jednego_zapisu_sesji(
    swiat, monkeypatch
):
    monkeypatch.setattr(settings, "sesja_kontrola_s", 3600)
    klient = await _zalogowany(swiat, swiat.wlasciciel)

    async with klient.stream("GET", _sciezka(swiat)) as odp:
        async with aclosing(_zdarzenia_sse(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run)
            with _zlicz_instrukcje_sesji() as licznik:
                for seq in range(1, 101):
                    await bus.publish_event(swiat.run, {"seq": seq, "kind": "log", "text": f"z{seq}"})
                odebrane = [(await _nastepne(strumien))[0] for _ in range(100)]

    assert odebrane == ["run"] * 100
    assert licznik["update"] <= 1
    assert licznik["select"] <= 1


async def test_uniewazniona_sesja_zamyka_strumien_w_oknie_kontroli_takze_przy_ciaglym_ruchu(
    swiat, monkeypatch
):
    """Szyna dostaje zdarzenia czesciej niz co okno: cisza nie nadejdzie, kontrola idzie po zegarze."""
    monkeypatch.setattr(settings, "sesja_kontrola_s", 0.2)
    klient = await _zalogowany(swiat, swiat.wlasciciel)

    async def publikuj():
        seq = 0
        while True:
            seq += 1
            await bus.publish_event(swiat.run, {"seq": seq, "kind": "log", "text": "ruch"})
            await asyncio.sleep(0.01)

    async def czytaj_do_wygasniecia(strumien) -> list[str | None]:
        nazwy = []
        async for nazwa, _ in strumien:
            nazwy.append(nazwa)
            if nazwa == "sesja_wygasla":
                break
        return nazwy

    zadanie = None
    try:
        async with klient.stream("GET", _sciezka(swiat)) as odp:
            async with aclosing(_zdarzenia_sse(odp)) as strumien:
                await _poczekaj_na_subskrypcje(swiat.run)
                zadanie = asyncio.ensure_future(publikuj())
                await _wyloguj_wszedzie(swiat, swiat.wlasciciel)
                nazwy = await asyncio.wait_for(czytaj_do_wygasniecia(strumien), LIMIT_CZEKANIA_S)
                zadanie.cancel()
                reszta = await _do_konca(strumien)
    finally:
        if zadanie is not None:
            zadanie.cancel()
            with suppress(asyncio.CancelledError):
                await zadanie

    assert nazwy[-1] == "sesja_wygasla"
    assert "sesja_wygasla" not in reszta


async def test_zdarzenie_tuz_przed_koncem_okna_nie_odracza_kontroli_o_kolejne_pelne_okno(
    swiat, monkeypatch
):
    """Okno liczone od ostatniej kontroli: zdarzenie w 0,6 s okna 1 s nie przesuwa kontroli na ok. 1,6 s."""
    okno_s = 1.0
    monkeypatch.setattr(settings, "sesja_kontrola_s", okno_s)
    klient = await _zalogowany(swiat, swiat.wlasciciel)

    async with klient.stream("GET", _sciezka(swiat)) as odp:
        poczatek = time.monotonic()
        async with aclosing(_zdarzenia_sse(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run)
            await _wyloguj_wszedzie(swiat, swiat.wlasciciel)
            await asyncio.sleep(max(0.0, poczatek + 0.6 - time.monotonic()))
            await bus.publish_event(swiat.run, {"seq": 1, "kind": "log", "text": "w-oknie"})

            async def do_wygasniecia():
                async for nazwa, _ in strumien:
                    if nazwa == "sesja_wygasla":
                        return time.monotonic()
                raise AssertionError("strumien skonczyl sie bez sesja_wygasla")

            moment = await asyncio.wait_for(do_wygasniecia(), LIMIT_CZEKANIA_S)

    assert moment - poczatek < okno_s + 0.35


def _licz_rozstrzygniecia_sesji(monkeypatch) -> list[str]:
    """Podmienia `sesje.rozstrzygnij` na wersje liczaca wywolania i delegujaca do prawdziwej."""
    wywolania: list[str] = []
    oryginal = sesje.rozstrzygnij

    async def liczaca(s, identyfikator):
        wywolania.append(identyfikator)
        return await oryginal(s, identyfikator)

    monkeypatch.setattr(sesje, "rozstrzygnij", liczaca)
    return wywolania


async def test_strumien_nie_kontroluje_sesji_zaraz_po_otwarciu_bo_otwarcie_ja_juz_rozstrzygnelo(
    swiat, monkeypatch
):
    """Jedyne rozstrzygniecie w oknie 3600 s to to z otwarcia strumienia; pierwsza kontrola po oknie."""
    monkeypatch.setattr(settings, "sesja_kontrola_s", 3600)
    wywolania = _licz_rozstrzygniecia_sesji(monkeypatch)
    klient = await _zalogowany(swiat, swiat.wlasciciel)

    async with klient.stream("GET", _sciezka(swiat)) as odp:
        async with aclosing(_zdarzenia_sse(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run)
            await bus.publish_event(swiat.run, {"seq": 1, "kind": "log", "text": "pierwsze"})
            nazwa, _ = await _nastepne(strumien)

    assert nazwa == "run"
    assert len(wywolania) == 1


async def test_ciagly_ruch_w_szynie_powoduje_kontrole_sesji_raz_na_okno_nie_przy_kazdym_zdarzeniu(
    swiat, monkeypatch
):
    okno_s = 0.3
    monkeypatch.setattr(settings, "sesja_kontrola_s", okno_s)
    wywolania = _licz_rozstrzygniecia_sesji(monkeypatch)
    klient = await _zalogowany(swiat, swiat.wlasciciel)
    odebrane: list[str | None] = []

    async def publikuj():
        seq = 0
        while True:
            seq += 1
            await bus.publish_event(swiat.run, {"seq": seq, "kind": "log", "text": "ruch"})
            await asyncio.sleep(0.01)

    async def czytaj(strumien):
        async for nazwa, _ in strumien:
            odebrane.append(nazwa)

    zadania: list[asyncio.Future] = []
    kontrole = 0
    czas_s = 0.0
    try:
        async with klient.stream("GET", _sciezka(swiat)) as odp:
            async with aclosing(_zdarzenia_sse(odp)) as strumien:
                await _poczekaj_na_subskrypcje(swiat.run)
                poczatek = time.monotonic()
                zadania.append(asyncio.ensure_future(publikuj()))
                zadania.append(asyncio.ensure_future(czytaj(strumien)))
                await asyncio.sleep(1.0)
                czas_s = time.monotonic() - poczatek
                kontrole = len(wywolania)
                for z in zadania:
                    z.cancel()
                # Dokoncz anulowane zadania przed aclosing: inaczej aclose() trafia w generator,
                # ktory czytaj() wciaz iteruje.
                await asyncio.wait(zadania, timeout=LIMIT_CZEKANIA_S)
                for z in zadania:
                    with suppress(asyncio.CancelledError, Exception):
                        z.result()
    finally:
        for z in zadania:
            z.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(z, LIMIT_CZEKANIA_S)

    assert len(odebrane) >= 20
    # 1 rozstrzygniecie z otwarcia + co najwyzej jedna kontrola na okno (z zapasem na jitter)
    assert kontrole <= 1 + int(czas_s / okno_s) + 2


async def test_strumien_z_minionym_oknem_przed_pierwszym_obrotem_kontroluje_sesje_i_dostarcza_zdarzenie(
    swiat, monkeypatch
):
    """Zegar skacze o 100 s zaraz po otwarciu: pierwszy obrot nie czeka na szyne, tylko kontroluje."""
    monkeypatch.setattr(settings, "sesja_kontrola_s", 10)
    odczyty: list[int] = []

    def zegar() -> float:
        odczyty.append(1)
        return 0.0 if len(odczyty) == 1 else 100.0

    modul = importlib.import_module("cockpit.api.app")
    monkeypatch.setattr(modul, "time", SimpleNamespace(monotonic=zegar))
    wywolania = _licz_rozstrzygniecia_sesji(monkeypatch)
    klient = await _zalogowany(swiat, swiat.wlasciciel)

    async with klient.stream("GET", _sciezka(swiat)) as odp:
        async with aclosing(_zdarzenia_sse(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run)
            await bus.publish_event(swiat.run, {"seq": 1, "kind": "log", "text": "po-skoku"})
            nazwa, dane = await _nastepne(strumien)

    assert nazwa == "run"
    assert json.loads(dane)["text"] == "po-skoku"
    assert len(wywolania) == 2


# --- reszta okna do nastepnej kontroli -----------------------------------------------------------


def _pozostalo(ostatnia: float, teraz: float, okno_s: float) -> float:
    return sesje.pozostalo_do_kontroli(ostatnia, teraz, okno_s)


def test_przed_uplywem_okna_do_kontroli_zostaje_dokladnie_reszta_okna():
    assert _pozostalo(10.0, 12.5, 5.0) == 2.5


def test_tuz_po_kontroli_do_nastepnej_zostaje_cale_okno():
    assert _pozostalo(10.0, 10.0, 5.0) == 5.0


def test_dokladnie_na_granicy_okna_kontrola_jest_nalezna_od_zaraz():
    assert _pozostalo(10.0, 15.0, 5.0) == 0.0


def test_po_uplywie_okna_kontrola_jest_nalezna_od_zaraz_a_czas_oczekiwania_nie_jest_ujemny():
    assert _pozostalo(10.0, 20.0, 5.0) == 0.0


# --- ustawienie limitu kontroli ---------------------------------------------------------------


def test_domyslny_limit_kontroli_sesji_strumienia_to_pietnascie_sekund(monkeypatch):
    monkeypatch.delenv("COCKPIT_SESJA_KONTROLA_S", raising=False)
    assert Settings(_env_file=None).sesja_kontrola_s == 15


def test_limit_kontroli_sesji_strumienia_zero_jest_odrzucony():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sesja_kontrola_s=0)


def test_limit_kontroli_sesji_strumienia_jedna_sekunda_jest_przyjety():
    assert Settings(_env_file=None, sesja_kontrola_s=1).sesja_kontrola_s == 1
