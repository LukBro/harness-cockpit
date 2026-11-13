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
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, RolaWZespole, Run, RunStatus
from cockpit.services import runs as uslugi
from cockpit.services import sesje
from cockpit.services.dostep import Kontekst
from cockpit.settings import settings
from sesja_testowa import zaloguj_klienta, zaloz_runnera, zaloz_wlasciciela
from transport_asgi import TransportASGIStrumieniujacy

LOCALHOST = "http://127.0.0.1"
LIMIT_CZEKANIA_S = 5
KROTKI_LIMIT_KONTROLI_S = 0.02
DLUGI_LIMIT_KONTROLI_S = 3600
KONTROLI_DO_ZLICZENIA = 3
# Zle naglowki Last-Event-ID idace przez HTTP (kazdy ma dzialac jak brak naglowka).
NAGLOWKI_NIEPOPRAWNE_HTTP = ["abc", "", "-1", "99999999999"]
# Cyfra arabsko-indyjska: int() ja przyjmuje, a starlette nie przepusci jej przez HTTP (latin-1),
# wiec te przypadki ida wolaniem trasy wprost.
NAGLOWKI_NIEPOPRAWNE_WPROST = ["٣", "-1", "99999999999", ""]


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

    async with klient.stream("GET", f"/api/runs/{swiat.run_a}/events") as odp:
        assert odp.status_code == 200
        await _poczekaj_na_subskrypcje(swiat.run_a)
        # subskrypcja poprzedza odczyt historii (HAR-106): krotka sesja historii musi oddac
        # polaczenie, zanim strumien zacznie czekac na kolejke; trzymane na stale oblewa
        koniec = time.monotonic() + LIMIT_CZEKANIA_S
        while licznik.wypozyczone != 0 and time.monotonic() < koniec:
            await asyncio.sleep(0.01)

        assert licznik.wypozyczone == 0, (
            f"otwarty strumien trzyma {licznik.wypozyczone} polaczen z puli bazy")

    # licznik dziala: samo otwarcie strumienia (dostep do runu, sesja) siegalo do bazy
    assert licznik.maksimum > 0


async def test_zdarzenie_z_szyny_dociera_do_klienta_gdy_strumien_nie_trzyma_polaczenia(
    swiat, monkeypatch
):
    monkeypatch.setattr(settings, "sesja_kontrola_s", DLUGI_LIMIT_KONTROLI_S)
    klient = await _zalogowany(swiat)

    async with klient.stream("GET", f"/api/runs/{swiat.run_a}/events") as odp:
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

    async with klient.stream("GET", f"/api/runs/{swiat.run_a}/events") as odp:
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

    async with klient.stream("GET", f"/api/runs/{swiat.run_a}/events") as odp:
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

    assert await _status_odpowiedzi(klient, f"/api/runs/{nieznany}/events") == 404

    assert bus._run_subscribers.get(nieznany, []) == []
    assert licznik.maksimum > 0  # dostep sprawdzono w bazie, nie pominieto
    assert licznik.wypozyczone == 0


async def test_strumien_cudzego_runu_dostaje_404_a_odrzucenie_oddaje_polaczenie(swiat):
    licznik = LicznikPolaczen(swiat.engine.sync_engine.pool)
    klient = await _zalogowany(swiat)
    licznik.maksimum = licznik.wypozyczone = 0

    assert await _status_odpowiedzi(klient, f"/api/runs/{swiat.run_b}/events") == 404

    assert bus._run_subscribers.get(swiat.run_b, []) == []
    assert licznik.maksimum > 0
    assert licznik.wypozyczone == 0


async def test_strumien_cudzego_runu_nie_dostarcza_zdarzen_zespolu_z_sesji(swiat, monkeypatch):
    """Dwa zespoly w bazie: zdarzenie opublikowane dla runu B nie trafia do strumienia runu A."""
    monkeypatch.setattr(settings, "sesja_kontrola_s", DLUGI_LIMIT_KONTROLI_S)
    klient = await _zalogowany(swiat)

    async with klient.stream("GET", f"/api/runs/{swiat.run_a}/events") as odp:
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

    assert await _status_odpowiedzi(klient, f"/api/runs/{swiat.run_a}/events") == 401
    assert bus._run_subscribers.get(swiat.run_a, []) == []


async def test_strumien_bez_sesji_dostaje_401_bez_subskrypcji(swiat):
    klient = swiat.nowy_klient()

    assert await _status_odpowiedzi(klient, f"/api/runs/{swiat.run_a}/events") == 401
    assert bus._run_subscribers.get(swiat.run_a, []) == []


# --- wznowienie od Last-Event-ID bez duplikatow (HAR-106) ---------------------------------------


async def _zdarzenia_sse_z_id(odp):
    """Zdarzenia SSE jako trojki (id, nazwa, dane); komentarze (ping) pomijamy."""
    id_, nazwa, dane = None, None, []
    async for linia in odp.aiter_lines():
        if linia == "":
            if nazwa is not None or dane:
                yield id_, nazwa, "\n".join(dane)
            id_, nazwa, dane = None, None, []
        elif linia.startswith("id:"):
            id_ = linia[len("id:"):].strip()
        elif linia.startswith("event:"):
            nazwa = linia[len("event:"):].strip()
        elif linia.startswith("data:"):
            dane.append(linia[len("data:"):].strip())


async def _tenant_runu(swiat, run_id: str) -> str:
    async with swiat.maker() as s:
        return (await s.execute(select(Run.tenant_id).where(Run.id == run_id))).scalar_one()


async def _zapisz(swiat, seqy, *, tenant_id=None, tekst=None):
    """Wstawia zdarzenia run_a w podanej kolejnosci seq (kazde w osobnej transakcji)."""
    tenant_id = tenant_id or await _tenant_runu(swiat, swiat.run_a)
    async with swiat.maker() as s:
        for seq in seqy:
            s.add(Event(tenant_id=tenant_id, run_id=swiat.run_a, seq=seq, kind="log",
                        text=tekst or f"hist-{seq}"))
            await s.commit()


async def _odbierz(swiat, klient, ile, naglowki=None, publikuj=None):
    """Otwiera strumien run_a, opcjonalnie publikuje na zywo po subskrypcji
    i zwraca `ile` pierwszych zdarzen jako (id, tresc)."""
    wynik = []
    async with klient.stream(
        "GET", f"/api/runs/{swiat.run_a}/events", headers=naglowki
    ) as odp:
        assert odp.status_code == 200
        async with aclosing(_zdarzenia_sse_z_id(odp)) as strumien:
            await _poczekaj_na_subskrypcje(swiat.run_a)
            if publikuj is not None:
                await publikuj()
            for _ in range(ile):
                id_, nazwa, dane = await asyncio.wait_for(anext(strumien), LIMIT_CZEKANIA_S)
                assert nazwa == "run"
                wynik.append((id_, json.loads(dane)))
    return wynik


async def _run_a(swiat, s) -> Run:
    return (await s.execute(select(Run).where(Run.id == swiat.run_a))).scalar_one()


async def test_wznowiony_strumien_runu_nie_powtarza_zdarzen_ktore_klient_juz_ma(
    swiat, monkeypatch
):
    monkeypatch.setattr(settings, "sesja_kontrola_s", DLUGI_LIMIT_KONTROLI_S)
    await _zapisz(swiat, [1, 2, 3, 4])
    klient = await _zalogowany(swiat)

    async def na_zywo():
        await bus.publish_event(swiat.run_a, {"seq": 5, "kind": "log", "text": "zywe-5"})

    odebrane = await _odbierz(swiat, klient, 3, {"Last-Event-ID": "2"}, na_zywo)

    # zdarzenie o seq rowno 2 klient juz ma (nie `>=`); 3, 4 z bazy, potem zywe 5
    assert [(i, z["seq"], z["text"]) for i, z in odebrane] == [
        ("3", 3, "hist-3"), ("4", 4, "hist-4"), ("5", 5, "zywe-5")]


async def test_strumien_runu_bez_naglowka_oddaje_cala_historie_z_id_rownym_seq(swiat, monkeypatch):
    monkeypatch.setattr(settings, "sesja_kontrola_s", DLUGI_LIMIT_KONTROLI_S)
    await _zapisz(swiat, [1, 2, 3])
    klient = await _zalogowany(swiat)

    odebrane = await _odbierz(swiat, klient, 3)

    assert [(i, z["seq"], z["text"]) for i, z in odebrane] == [
        ("1", 1, "hist-1"), ("2", 2, "hist-2"), ("3", 3, "hist-3")]


async def test_naglowek_zero_oznacza_cala_historie_nie_brak_historii(swiat, monkeypatch):
    monkeypatch.setattr(settings, "sesja_kontrola_s", DLUGI_LIMIT_KONTROLI_S)
    await _zapisz(swiat, [1, 2])
    klient = await _zalogowany(swiat)

    odebrane = await _odbierz(swiat, klient, 2, {"Last-Event-ID": "0"})

    assert [z["seq"] for _, z in odebrane] == [1, 2]


async def test_odczyt_historii_po_numerze_zero_zwraca_wszystko_a_po_numerze_tylko_nowsze(swiat):
    await _zapisz(swiat, [1, 2, 3])
    async with swiat.maker() as s:
        run = await _run_a(swiat, s)
        po_zerze = await uslugi.zdarzenia_runu(s, run, 0)
        po_dwoch = await uslugi.zdarzenia_runu(s, run, 2)
        bez_numeru = await uslugi.zdarzenia_runu(s, run)

    assert [e.seq for e in po_zerze] == [1, 2, 3]
    assert [e.seq for e in po_dwoch] == [3]
    assert [e.seq for e in bez_numeru] == [1, 2, 3]


async def test_odczyt_historii_po_numerze_zero_pomija_zdarzenie_o_numerze_zero(swiat):
    # numer 0 to prawdziwy numer, nie brak numeru: zdarzenie 0 klient juz ma (wstawione wprost,
    # bo serwer numeruje od 1; tylko `is not None` odroznia 0 od braku)
    await _zapisz(swiat, [0, 1, 2])
    async with swiat.maker() as s:
        run = await _run_a(swiat, s)
        po_zerze = await uslugi.zdarzenia_runu(s, run, 0)
        bez_numeru = await uslugi.zdarzenia_runu(s, run)

    assert [e.seq for e in po_zerze] == [1, 2]
    assert [e.seq for e in bez_numeru] == [0, 1, 2]


@pytest.mark.parametrize("naglowek", NAGLOWKI_NIEPOPRAWNE_HTTP)
async def test_niepoprawny_naglowek_wznowienia_to_cala_historia_a_nie_blad(
    swiat, monkeypatch, naglowek
):
    monkeypatch.setattr(settings, "sesja_kontrola_s", DLUGI_LIMIT_KONTROLI_S)
    await _zapisz(swiat, [1, 2, 3, 4])
    klient = await _zalogowany(swiat)

    odebrane = await _odbierz(swiat, klient, 4, {"Last-Event-ID": naglowek})

    assert [i for i, _ in odebrane] == ["1", "2", "3", "4"]


@pytest.mark.parametrize("naglowek", NAGLOWKI_NIEPOPRAWNE_WPROST)
async def test_niepoprawny_naglowek_wznowienia_wolany_wprost_to_cala_historia(
    swiat, monkeypatch, naglowek
):
    monkeypatch.setattr(settings, "sesja_kontrola_s", DLUGI_LIMIT_KONTROLI_S)
    from cockpit.api.app import run_stream

    await _zapisz(swiat, [1, 2, 3, 4])
    tenant = await _tenant_runu(swiat, swiat.run_a)
    async with swiat.maker() as s:
        identyfikator, _ = await sesje.utworz(s, swiat.wlasciciel_a)

    async def nie_rozlaczony() -> bool:
        return False

    odpowiedz = await run_stream(
        swiat.run_a, SimpleNamespace(is_disconnected=nie_rozlaczony),
        k=Kontekst(user_id=swiat.wlasciciel_a.id, tenant_id=tenant, rola=RolaWZespole.owner),
        identyfikator=identyfikator, last_event_id=naglowek)
    strumien = odpowiedz.body_iterator
    try:
        ids = [
            (await asyncio.wait_for(strumien.__anext__(), LIMIT_CZEKANIA_S))["id"]
            for _ in range(4)
        ]
    finally:
        await strumien.aclose()

    assert ids == ["1", "2", "3", "4"]


async def test_zdarzenie_z_okna_miedzy_odczytem_historii_a_szyna_dociera_raz_i_bez_duplikatow(
    swiat, monkeypatch
):
    """Okno z kryterium 2: zdarzenie zapisane po odczycie historii trafia na szyne.

    Subskrypcja przed odczytem je zlapie (inaczej ginie); duplikat zdarzenia juz wyslanego
    z historii nie moze dotrzec drugi raz."""
    monkeypatch.setattr(settings, "sesja_kontrola_s", DLUGI_LIMIT_KONTROLI_S)
    await _zapisz(swiat, [1, 2, 3])
    tenant = await _tenant_runu(swiat, swiat.run_a)
    prawdziwa = uslugi.zdarzenia_runu

    async def z_oknem(s, run, po_seq=None):
        historia = await prawdziwa(s, run, po_seq)
        # historia odczytana; teraz zdarzenie 4 zapisane i opublikowane, wraz z duplikatem 3
        async with swiat.maker() as s2:
            nowe = Event(tenant_id=tenant, run_id=run.id, seq=4, kind="log", text="z-okna")
            s2.add(nowe)
            await s2.commit()
            await bus.publish_event(run.id, uslugi.zdarzenie_na_szyne(historia[-1]))
            await bus.publish_event(run.id, uslugi.zdarzenie_na_szyne(nowe))
            # zdarzenie na zywo po oknie, w tej samej kolejnosci co zapis: seq rosnace na szynie
            await bus.publish_event(run.id, {"seq": 5, "kind": "log", "text": "zywe-5"})
        return historia

    monkeypatch.setattr(uslugi, "zdarzenia_runu", z_oknem)
    klient = await _zalogowany(swiat)

    odebrane = await _odbierz(swiat, klient, 5)

    assert [(i, z["seq"]) for i, z in odebrane] == [
        ("1", 1), ("2", 2), ("3", 3), ("4", 4), ("5", 5)]
    assert odebrane[3][1]["text"] == "z-okna"


async def test_strumien_runu_nie_oddaje_zdarzen_innego_zespolu_o_tym_samym_run_id(
    swiat, monkeypatch
):
    monkeypatch.setattr(settings, "sesja_kontrola_s", DLUGI_LIMIT_KONTROLI_S)
    tenant_b = await _tenant_runu(swiat, swiat.run_b)
    await _zapisz(swiat, [1, 3])
    await _zapisz(swiat, [2], tenant_id=tenant_b, tekst="cudze")
    klient = await _zalogowany(swiat)

    odebrane = await _odbierz(swiat, klient, 2)

    assert [(z["seq"], z["text"]) for _, z in odebrane] == [(1, "hist-1"), (3, "hist-3")]
    async with swiat.maker() as s:
        zwrocone = await uslugi.zdarzenia_runu(s, await _run_a(swiat, s))
    assert [(e.seq, e.text) for e in zwrocone] == [(1, "hist-1"), (3, "hist-3")]


async def test_historia_runu_nie_zawiera_zdarzen_innego_runu_tego_samego_zespolu(swiat):
    tenant_a = await _tenant_runu(swiat, swiat.run_a)
    await _zapisz(swiat, [1, 3])
    async with swiat.maker() as s:  # zdarzenie zespolu A, ale z innego runu (run_b)
        s.add(Event(tenant_id=tenant_a, run_id=swiat.run_b, seq=2, kind="log", text="inny-run"))
        await s.commit()

    async with swiat.maker() as s:
        zwrocone = await uslugi.zdarzenia_runu(s, await _run_a(swiat, s))

    assert [(e.seq, e.text) for e in zwrocone] == [(1, "hist-1"), (3, "hist-3")]


async def test_historia_strumienia_runu_idzie_rosnaco_po_seq_niezaleznie_od_kolejnosci_zapisu(
    swiat, monkeypatch
):
    monkeypatch.setattr(settings, "sesja_kontrola_s", DLUGI_LIMIT_KONTROLI_S)
    await _zapisz(swiat, [3, 1, 2])
    klient = await _zalogowany(swiat)

    odebrane = await _odbierz(swiat, klient, 3)

    assert [i for i, _ in odebrane] == ["1", "2", "3"]
    async with swiat.maker() as s:
        zwrocone = await uslugi.zdarzenia_runu(s, await _run_a(swiat, s))
    assert [e.seq for e in zwrocone] == [1, 2, 3]


def test_naglowek_wznowienia_rowny_najwiekszej_liczbie_int32_jest_przyjety_a_wiekszy_odrzucony():
    from cockpit.api.app import _po_seq

    # 2**31 - 1 to ostatni numer, ktory miesci sie w kolumnie seq; 2**31 juz nie (jak brak naglowka)
    assert _po_seq("2147483647") == 2147483647
    assert _po_seq("2147483648") is None


async def test_naglowek_wiekszy_od_int32_o_jeden_oddaje_cala_historie(swiat, monkeypatch):
    monkeypatch.setattr(settings, "sesja_kontrola_s", DLUGI_LIMIT_KONTROLI_S)
    await _zapisz(swiat, [1, 2, 3])
    klient = await _zalogowany(swiat)

    odebrane = await _odbierz(swiat, klient, 3, {"Last-Event-ID": "2147483648"})

    assert [i for i, _ in odebrane] == ["1", "2", "3"]


async def test_wznowiony_strumien_bez_nowych_zdarzen_w_bazie_nie_oddaje_duplikatu_numeru_klienta(
    swiat, monkeypatch
):
    monkeypatch.setattr(settings, "sesja_kontrola_s", DLUGI_LIMIT_KONTROLI_S)
    klient = await _zalogowany(swiat)

    async def na_zywo():
        # 3 i 5 klient juz ma (Last-Event-ID: 5); 6 jest nowe
        await bus.publish_event(swiat.run_a, {"seq": 3, "kind": "log", "text": "duplikat-3"})
        await bus.publish_event(swiat.run_a, {"seq": 5, "kind": "log", "text": "duplikat-5"})
        await bus.publish_event(swiat.run_a, {"seq": 6, "kind": "log", "text": "zywe-6"})

    odebrane = await _odbierz(swiat, klient, 1, {"Last-Event-ID": "5"}, na_zywo)

    assert [(i, z["text"]) for i, z in odebrane] == [("6", "zywe-6")]


async def test_zdarzenie_na_zywo_o_numerze_z_historii_lub_powtorzone_dociera_tylko_raz(
    swiat, monkeypatch
):
    monkeypatch.setattr(settings, "sesja_kontrola_s", DLUGI_LIMIT_KONTROLI_S)
    await _zapisz(swiat, [1, 2, 3])
    klient = await _zalogowany(swiat)

    async def na_zywo():
        for seq in (2, 3, 4, 4, 5):
            await bus.publish_event(swiat.run_a, {"seq": seq, "kind": "log", "text": f"zywe-{seq}"})

    odebrane = await _odbierz(swiat, klient, 5, None, na_zywo)

    assert [(i, z["text"]) for i, z in odebrane] == [
        ("1", "hist-1"), ("2", "hist-2"), ("3", "hist-3"), ("4", "zywe-4"), ("5", "zywe-5")]
