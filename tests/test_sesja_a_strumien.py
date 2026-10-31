"""Strumien runu a sesja: wygasla albo uniewazniona sesja zamyka strumien komunikatem (HAR-70).

Run i runner dzialaja dalej: status runu zostaje `running`, a zdarzenia runnera nadal sie zapisuja.
Skutki sprawdzamy w bazie i w szynie, nie tylko w tym, co przyszlo klientowi.
Limit kontroli sesji obnizamy na obiekcie `cockpit.settings.settings` (czytany przy kazdym oczekiwaniu).
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import aclosing
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
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


async def test_zdarzenie_opublikowane_po_uniewaznieniu_sesji_nie_trafia_do_klienta(
    swiat, monkeypatch
):
    """Limit duzy, wiec zadziala wylacznie kontrola po zdarzeniu, i to przed jego wyslaniem."""
    monkeypatch.setattr(settings, "sesja_kontrola_s", 3600)
    klient = await _zalogowany(swiat, swiat.wlasciciel)

    async with klient.stream("GET", _sciezka(swiat)) as odp:
        async with aclosing(_zdarzenia_sse(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run)
            await _wyloguj_wszedzie(swiat, swiat.wlasciciel)
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

    async def awaria(s, identyfikator):
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

    assert "sesja_wygasla" not in odebrane
    assert bus._run_subscribers.get(swiat.run, []) == []


# --- ustawienie limitu kontroli ---------------------------------------------------------------


def test_domyslny_limit_kontroli_sesji_strumienia_to_pietnascie_sekund(monkeypatch):
    monkeypatch.delenv("COCKPIT_SESJA_KONTROLA_S", raising=False)
    assert Settings(_env_file=None).sesja_kontrola_s == 15


def test_limit_kontroli_sesji_strumienia_zero_jest_odrzucony():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sesja_kontrola_s=0)


def test_limit_kontroli_sesji_strumienia_jedna_sekunda_jest_przyjety():
    assert Settings(_env_file=None, sesja_kontrola_s=1).sesja_kontrola_s == 1
