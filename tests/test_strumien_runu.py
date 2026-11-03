"""Strumien runu nie trzyma polaczenia z baza przez caly czas otwarcia (HAR-147).

Baza ma krotkie sprawdzenia (dostep do runu, wazna sesja), a oczekiwanie na zdarzenia idzie
bez polaczenia z puli. Liczymy polaczenia wypozyczone z puli silnika zdarzeniami `checkout`
i `checkin` (dziala dla kazdej puli), wiec test mierzy to, o co chodzi w tickecie.
Limit kontroli sesji ustawiamy na obiekcie `cockpit.settings.settings` (czytany przy kazdym oczekiwaniu).
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import aclosing
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Run, RunStatus
from cockpit.services import sesje
from cockpit.settings import settings
from sesja_testowa import zaloguj_klienta, zaloz_runnera, zaloz_wlasciciela
from transport_asgi import TransportASGIStrumieniujacy

LOCALHOST = "http://127.0.0.1"
LIMIT_CZEKANIA_S = 5
KROTKI_LIMIT_KONTROLI_S = 0.02
DLUGI_LIMIT_KONTROLI_S = 3600
KONTROLI_DO_ZLICZENIA = 3


class LicznikPolaczen:
    """Polaczenia wypozyczone z puli: aktualnie, najwiecej naraz i ile razy wrocily do zera."""

    def __init__(self, pula) -> None:
        self.wypozyczone = 0
        self.maksimum = 0
        self.wypozyczen = 0
        self.powrotow_do_zera = 0
        event.listen(pula, "checkout", self._wypozyczenie)
        event.listen(pula, "checkin", self._zwrot)

    def _wypozyczenie(self, *_args) -> None:
        self.wypozyczone += 1
        self.wypozyczen += 1
        self.maksimum = max(self.maksimum, self.wypozyczone)

    def _zwrot(self, *_args) -> None:
        self.wypozyczone -= 1
        if self.wypozyczone == 0:
            self.powrotow_do_zera += 1


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'strumien_runu.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # dwa zespoly: run zespolu B jest dla uzytkownika zespolu A cudzy
    wlasciciel_a, tenant_a = await zaloz_wlasciciela(maker, "a@example.com", nazwa_zespolu="A")
    wlasciciel_b, tenant_b = await zaloz_wlasciciela(maker, "b@example.com", nazwa_zespolu="B")
    runner_a = await zaloz_runnera(maker, tenant_a, "runner-a", "token-a-strumienia")
    runner_b = await zaloz_runnera(maker, tenant_b, "runner-b", "token-b-strumienia")
    async with maker() as s:
        run_a = Run(tenant_id=tenant_a, runner_id=runner_a, status=RunStatus.running)
        run_b = Run(tenant_id=tenant_b, runner_id=runner_b, status=RunStatus.running)
        s.add_all([run_a, run_b])
        await s.commit()
        id_a, id_b = run_a.id, run_b.id
    bus.mark_runner_connected(runner_a)
    bus.mark_runner_connected(runner_b)

    klienci: list[AsyncClient] = []

    def nowy_klient() -> AsyncClient:
        k = AsyncClient(transport=TransportASGIStrumieniujacy(app), base_url=LOCALHOST)
        klienci.append(k)
        return k

    yield SimpleNamespace(
        maker=maker, engine=engine, nowy_klient=nowy_klient,
        wlasciciel_a=wlasciciel_a, run_a=id_a, run_b=id_b,
    )
    for k in klienci:
        await k.aclose()
    for runner in (runner_a, runner_b):
        while runner in bus.connected_runner_ids():
            bus.mark_runner_disconnected(runner)
        getattr(bus, "_rozlaczone", {}).pop(runner, None)
    bus._run_subscribers.pop(id_a, None)
    bus._run_subscribers.pop(id_b, None)
    await engine.dispose()


async def _zalogowany(swiat) -> AsyncClient:
    k = swiat.nowy_klient()
    await zaloguj_klienta(k, swiat.maker, swiat.wlasciciel_a)
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


async def _poczekaj_na_subskrypcje(run_id: str) -> None:
    koniec = time.monotonic() + LIMIT_CZEKANIA_S
    while not bus._run_subscribers.get(run_id):
        assert time.monotonic() < koniec, "strumien nie zasubskrybowal szyny"
        await asyncio.sleep(0.01)


async def _status_odpowiedzi(klient: AsyncClient, sciezka: str) -> int:
    """Kod odpowiedzi strumienia; czytamy tylko naglowki i zamykamy (rozlaczenie klienta)."""
    async with klient.stream("GET", sciezka) as odp:
        return odp.status_code


# --- otwarty strumien nie zajmuje polaczenia z puli ---------------------------------------------


async def test_otwarty_strumien_runu_nie_trzyma_polaczenia_z_bazy(swiat, monkeypatch):
    monkeypatch.setattr(settings, "sesja_kontrola_s", DLUGI_LIMIT_KONTROLI_S)
    licznik = LicznikPolaczen(swiat.engine.sync_engine.pool)
    klient = await _zalogowany(swiat)
    licznik.maksimum = licznik.wypozyczone = 0  # pomiar tylko dla zadania strumienia

    async with klient.stream("GET", f"/api/runs/{swiat.run_a}/stream") as odp:
        assert odp.status_code == 200
        await _poczekaj_na_subskrypcje(swiat.run_a)

        assert licznik.wypozyczone == 0, (
            f"otwarty strumien trzyma {licznik.wypozyczone} polaczen z puli bazy")

    # licznik dziala: samo otwarcie strumienia (dostep do runu, sesja) siegalo do bazy
    assert licznik.maksimum > 0


async def test_zdarzenie_z_szyny_dociera_do_klienta_gdy_strumien_nie_trzyma_polaczenia(
    swiat, monkeypatch
):
    monkeypatch.setattr(settings, "sesja_kontrola_s", DLUGI_LIMIT_KONTROLI_S)
    klient = await _zalogowany(swiat)

    async with klient.stream("GET", f"/api/runs/{swiat.run_a}/stream") as odp:
        async with aclosing(_zdarzenia_sse(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run_a)
            await bus.publish_event(swiat.run_a, {"seq": 1, "kind": "log", "text": "z-szyny"})
            nazwa, dane = await asyncio.wait_for(anext(strumien), LIMIT_CZEKANIA_S)

    assert nazwa == "run"
    assert json.loads(dane)["text"] == "z-szyny"


async def test_kontrola_waznosci_sesji_w_otwartym_strumieniu_oddaje_polaczenie_po_kazdym_sprawdzeniu(
    swiat, monkeypatch
):
    monkeypatch.setattr(settings, "sesja_kontrola_s", KROTKI_LIMIT_KONTROLI_S)
    licznik = LicznikPolaczen(swiat.engine.sync_engine.pool)
    klient = await _zalogowany(swiat)

    async with klient.stream("GET", f"/api/runs/{swiat.run_a}/stream") as odp:
        async with aclosing(_zdarzenia_sse(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run_a)
            nastepne = asyncio.ensure_future(anext(strumien))
            try:
                przed = licznik.powrotow_do_zera
                koniec = time.monotonic() + LIMIT_CZEKANIA_S
                while licznik.powrotow_do_zera - przed < KONTROLI_DO_ZLICZENIA:
                    assert time.monotonic() < koniec, (
                        "miedzy kontrolami sesji polaczenie nie wraca do puli "
                        f"(powrotow do zera: {licznik.powrotow_do_zera - przed}, "
                        f"wypozyczone: {licznik.wypozyczone})")
                    await asyncio.sleep(0.01)

                assert not nastepne.done(), (
                    f"strumien waznej sesji sie zamknal: {nastepne.result()!r}")
            finally:
                # generator musi skonczyc anext, zanim aclosing() zawola aclose()
                nastepne.cancel()
                await asyncio.wait({nastepne}, timeout=LIMIT_CZEKANIA_S)

    assert licznik.wypozyczen > KONTROLI_DO_ZLICZENIA  # kontrole rzeczywiscie siegaly do bazy


async def test_strumien_po_uniewaznieniu_sesji_konczy_sie_komunikatem_i_oddaje_polaczenie(
    swiat, monkeypatch
):
    monkeypatch.setattr(settings, "sesja_kontrola_s", KROTKI_LIMIT_KONTROLI_S)
    licznik = LicznikPolaczen(swiat.engine.sync_engine.pool)
    klient = await _zalogowany(swiat)

    async with klient.stream("GET", f"/api/runs/{swiat.run_a}/stream") as odp:
        async with aclosing(_zdarzenia_sse(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run_a)
            async with swiat.maker() as s:
                await sesje.wyloguj_wszedzie(s, swiat.wlasciciel_a.id)
            nazwa, _ = await asyncio.wait_for(anext(strumien), LIMIT_CZEKANIA_S)

    assert nazwa == "sesja_wygasla"
    assert licznik.wypozyczone == 0


# --- dostep do runu zachowany -------------------------------------------------------------------


async def test_strumien_nieistniejacego_runu_dostaje_404_bez_subskrypcji_i_oddaje_polaczenie(swiat):
    licznik = LicznikPolaczen(swiat.engine.sync_engine.pool)
    klient = await _zalogowany(swiat)
    licznik.maksimum = licznik.wypozyczone = 0
    nieznany = "run-ktorego-nie-ma"

    assert await _status_odpowiedzi(klient, f"/api/runs/{nieznany}/stream") == 404

    assert bus._run_subscribers.get(nieznany, []) == []
    assert licznik.maksimum > 0  # dostep sprawdzono w bazie, nie pominieto
    assert licznik.wypozyczone == 0


async def test_strumien_cudzego_runu_dostaje_404_a_odrzucenie_oddaje_polaczenie(swiat):
    licznik = LicznikPolaczen(swiat.engine.sync_engine.pool)
    klient = await _zalogowany(swiat)
    licznik.maksimum = licznik.wypozyczone = 0

    assert await _status_odpowiedzi(klient, f"/api/runs/{swiat.run_b}/stream") == 404

    assert bus._run_subscribers.get(swiat.run_b, []) == []
    assert licznik.maksimum > 0
    assert licznik.wypozyczone == 0


async def test_strumien_cudzego_runu_nie_dostarcza_zdarzen_zespolu_z_sesji(swiat, monkeypatch):
    """Dwa zespoly w bazie: zdarzenie opublikowane dla runu B nie trafia do strumienia runu A."""
    monkeypatch.setattr(settings, "sesja_kontrola_s", DLUGI_LIMIT_KONTROLI_S)
    klient = await _zalogowany(swiat)

    async with klient.stream("GET", f"/api/runs/{swiat.run_a}/stream") as odp:
        async with aclosing(_zdarzenia_sse(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run_a)
            await bus.publish_event(swiat.run_b, {"seq": 1, "kind": "log", "text": "cudze"})
            await bus.publish_event(swiat.run_a, {"seq": 1, "kind": "log", "text": "wlasne"})
            nazwa, dane = await asyncio.wait_for(anext(strumien), LIMIT_CZEKANIA_S)

    assert nazwa == "run"
    assert json.loads(dane)["text"] == "wlasne"


async def test_strumien_uniewaznionej_sesji_dostaje_401_bez_subskrypcji(swiat):
    """Sesja istnieje w ciasteczku, ale zostala uniewazniona: strumien sie nie otwiera."""
    klient = await _zalogowany(swiat)
    async with swiat.maker() as s:
        await sesje.wyloguj_wszedzie(s, swiat.wlasciciel_a.id)

    assert await _status_odpowiedzi(klient, f"/api/runs/{swiat.run_a}/stream") == 401
    assert bus._run_subscribers.get(swiat.run_a, []) == []


async def test_strumien_bez_sesji_dostaje_401_bez_subskrypcji(swiat):
    klient = swiat.nowy_klient()

    assert await _status_odpowiedzi(klient, f"/api/runs/{swiat.run_a}/stream") == 401
    assert bus._run_subscribers.get(swiat.run_a, []) == []
