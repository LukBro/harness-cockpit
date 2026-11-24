"""HAR-49: zapytanie kokpitu do runnera (polecenie SSE z `req_id`) i odpowiedz runnera POST-em.

Odpowiedz jest skorelowana po `req_id` i przypisana do runnera, ktoremu zapytanie wyslano:
inny runner (nawet tego samego zespolu) nie rozwiazuje cudzego zapytania.
Warstwy: szyna (`cockpit.api.bus`), serwis (`zapytaj_runnera_zespolu`), trasa
`POST /api/runners/odpowiedzi`, operacje runnera i przelot end-to-end: prawdziwa aplikacja
kokpitu + prawdziwy `runner.client.Runner` na transporcie ASGI, ktory przepuszcza
nieskonczony strumien SSE (`httpx.ASGITransport` buforuje cale ciala, wiec by zawisl).
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import app as app_mod
from cockpit.api import bus
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, Runner, RunStatus, Tenant
from cockpit.services import zapytania_runnera as zapytania
from cockpit.services.errors import StanNiemozliwy
from cockpit.settings import Settings, settings
from runner import client as runner_client
from runner import operacje
from runner.client import Runner as RunnerKlient
from transport_asgi import TransportASGIStrumieniujacy

B_STARY = "b-0-stary"   # Browarek, strumien otwarty, ale heartbeat sprzed 10 progow; id sortuje sie pierwsze
B1 = "b-1"              # Browarek, swiezy
B2 = "b-2"              # Browarek, swiezy; w bazie wstawiony PRZED B1 (kolejnosc wstawiania != kolejnosc id)
OBCY = "0-obcy"         # Piekarnia, swiezy; id mniejsze niz kazdy runner Browarka
TOKENY = {B_STARY: "tok-stary", B1: "tok-b1", B2: "tok-b2", OBCY: "tok-obcy"}
SEKRET = "zxqv7-SEKRET-token-482913"


def _stary() -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=settings.runner_offline_after_s * 10)


def _naglowek(runner_id: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENY[runner_id]}"}


async def _poczekaj(warunek, limit: float = 5.0) -> None:
    """`warunek` to funkcja zwykla albo korutyna zwracajaca prawde, gdy mozna isc dalej."""
    koniec = time.monotonic() + limit
    while True:
        wynik = warunek()
        if asyncio.iscoroutine(wynik):
            wynik = await wynik
        if wynik:
            return
        assert time.monotonic() < koniec, "warunek nie spelnil sie w limicie czasu"
        await asyncio.sleep(0.01)


# --- stan globalny szyny sprzatany po kazdym tescie ------------------------------

@pytest.fixture(autouse=True)
def _czysta_szyna():
    """`_oczekujace`, kolejki i ewidencja polaczen to stan procesu: test nie zostawia po sobie nic."""
    slowniki = ("_oczekujace", "_runner_queues", "_polaczone", "_rozlaczone")
    zapisane = {n: dict(getattr(bus, n)) for n in slowniki if hasattr(bus, n)}
    yield
    for nazwa, przed in zapisane.items():
        biezacy = getattr(bus, nazwa)
        biezacy.clear()
        biezacy.update(przed)


def _polacz(*runner_ids: str) -> None:
    for rid in runner_ids:
        bus.mark_runner_connected(rid)


class _Zapytania:
    """Zapytania do runnera uruchamiane w tle; po tescie wszystkie sa dokonczone albo anulowane."""

    def __init__(self) -> None:
        self.zadania: list[asyncio.Task] = []

    async def start(self, runner_id: str, typ: str = "ping", dane: dict | None = None,
                    timeout_s: float = 5.0):
        """Zwraca (zadanie, polecenie): polecenie tak, jak zobaczy je runner."""
        zadanie = asyncio.create_task(bus.zapytaj(runner_id, typ, dane or {}, timeout_s))
        self.zadania.append(zadanie)
        polecenie = await asyncio.wait_for(bus.runner_queue(runner_id).get(), 5)
        return zadanie, polecenie


@pytest.fixture
async def zapytania_w_tle():
    z = _Zapytania()
    yield z
    for zadanie in z.zadania:
        if not zadanie.done():
            zadanie.cancel()
    if z.zadania:
        await asyncio.wait(z.zadania, timeout=5)


# --- swiat: dwa zespoly ----------------------------------------------------------

@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    def runner(rid: str, tenant_id: str, widziany: datetime) -> Runner:
        return Runner(id=rid, tenant_id=tenant_id, name=rid,
                      token_hash=hashlib.sha256(TOKENY[rid].encode()).hexdigest(),
                      last_seen_at=widziany)

    async with maker() as s:
        browarek, piekarnia = Tenant(name="Browarek"), Tenant(name="Piekarnia")
        s.add(browarek)
        await s.flush()
        s.add(piekarnia)
        await s.flush()
        teraz = datetime.now(timezone.utc)
        s.add(runner(B2, browarek.id, teraz))
        s.add(runner(B1, browarek.id, teraz))
        s.add(runner(B_STARY, browarek.id, _stary()))
        s.add(runner(OBCY, piekarnia.id, teraz))
        await s.commit()
        ids = SimpleNamespace(browarek=browarek.id, piekarnia=piekarnia.id)
    try:
        async with AsyncClient(transport=ASGITransport(app=app_mod.app),
                               base_url="http://test") as c:
            ids.klient = c
            yield ids
    finally:
        await engine.dispose()


# =================================================================================
# Szyna: zapytaj / odpowiedz
# =================================================================================

async def test_odpowiedz_runnera_trafia_do_zapytania_po_req_id(zapytania_w_tle):
    zadanie, polecenie = await zapytania_w_tle.start(B1)
    cialo = {"ok": True, "wynik": {"pong": True}}

    assert bus.odpowiedz(B1, polecenie["req_id"], cialo) is True

    assert await asyncio.wait_for(zadanie, 5) == cialo


async def test_polecenie_zapytania_niesie_typ_req_id_czas_nadania_limit_waznosci_pola_z_dane_i_nic_ponadto(
    zapytania_w_tle,
):
    przed = time.time()
    _, polecenie = await zapytania_w_tle.start(
        B1, "czytaj", {"sciezka": "a/b", "limit": 3}, timeout_s=7.5)
    po = time.time()

    assert isinstance(polecenie["req_id"], str) and polecenie["req_id"]
    assert polecenie == {"type": "czytaj", "req_id": polecenie["req_id"],
                         "sciezka": "a/b", "limit": 3,
                         "nadano_o": polecenie["nadano_o"], "wazne_s": 7.5}
    assert isinstance(polecenie["nadano_o"], float)
    assert przed <= polecenie["nadano_o"] <= po       # czas nadania z zegara kokpitu (epoch, s)


@pytest.mark.parametrize("timeout_s", [0.5, 15.0, 40.0])
async def test_polecenie_zapytania_jest_wazne_dokladnie_tyle_ile_kokpit_czeka_na_odpowiedz(
    zapytania_w_tle, timeout_s,
):
    _, polecenie = await zapytania_w_tle.start(B1, "ping", timeout_s=timeout_s)

    assert polecenie["wazne_s"] == timeout_s


async def test_dane_zapytania_nie_nadpisuja_czasu_nadania_ani_waznosci(zapytania_w_tle):
    przed = time.time()
    _, polecenie = await zapytania_w_tle.start(
        B1, "ping", {"nadano_o": 1.0, "wazne_s": 99999.0}, timeout_s=5.0)

    assert polecenie["nadano_o"] >= przed
    assert polecenie["wazne_s"] == 5.0


# --- HAR-201: kokpit wie, czy zapytanie jeszcze czeka na odpowiedz ----------------------

async def test_zapytanie_czekajace_na_odpowiedz_jest_oczekujace(zapytania_w_tle):
    _, polecenie = await zapytania_w_tle.start(B1)

    assert bus.oczekuje(polecenie["req_id"]) is True


async def test_zapytanie_po_odpowiedzi_runnera_nie_jest_juz_oczekujace(zapytania_w_tle):
    zadanie, polecenie = await zapytania_w_tle.start(B1)
    bus.odpowiedz(B1, polecenie["req_id"], {"ok": True})
    await asyncio.wait_for(zadanie, 5)

    assert bus.oczekuje(polecenie["req_id"]) is False


async def test_zapytanie_po_przekroczeniu_czasu_nie_jest_juz_oczekujace(zapytania_w_tle):
    zadanie, polecenie = await zapytania_w_tle.start(B1, "ping", timeout_s=0.05)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(zadanie, 5)

    assert bus.oczekuje(polecenie["req_id"]) is False


async def test_zapytanie_anulowane_nie_jest_juz_oczekujace(zapytania_w_tle):
    zadanie, polecenie = await zapytania_w_tle.start(B1)
    zadanie.cancel()
    await asyncio.wait({zadanie}, timeout=5)

    assert bus.oczekuje(polecenie["req_id"]) is False


async def test_zapytanie_ktorego_kokpit_nigdy_nie_wyslal_nie_jest_oczekujace():
    assert bus.oczekuje("nigdy-nie-wyslany") is False


async def test_dane_zapytania_nie_nadpisuja_typu_ani_req_id(zapytania_w_tle):
    _, polecenie = await zapytania_w_tle.start(
        B1, "ping", {"type": "run_step", "req_id": "podrobiony", "x": 1})

    assert polecenie["type"] == "ping"
    assert polecenie["req_id"] != "podrobiony"
    assert polecenie["x"] == 1
    assert polecenie["req_id"] in bus._oczekujace      # to prawdziwy req_id jest zarejestrowany
    assert "podrobiony" not in bus._oczekujace


async def test_kazde_zapytanie_ma_wlasny_req_id_i_odpowiedzi_nie_mieszaja_sie(zapytania_w_tle):
    pierwsze, polecenie1 = await zapytania_w_tle.start(B1, "ping")
    drugie, polecenie2 = await zapytania_w_tle.start(B1, "ping")
    assert polecenie1["req_id"] != polecenie2["req_id"]

    assert bus.odpowiedz(B1, polecenie2["req_id"], {"ok": True, "wynik": "drugie"}) is True
    assert bus.odpowiedz(B1, polecenie1["req_id"], {"ok": True, "wynik": "pierwsze"}) is True

    assert (await asyncio.wait_for(pierwsze, 5))["wynik"] == "pierwsze"
    assert (await asyncio.wait_for(drugie, 5))["wynik"] == "drugie"


async def test_polecenie_zapytania_trafia_do_kolejki_tylko_wskazanego_runnera(zapytania_w_tle):
    await zapytania_w_tle.start(B1)

    assert bus.runner_queue(B2).empty()
    assert bus.runner_queue(OBCY).empty()


async def test_zapytanie_jest_zarejestrowane_dla_wskazanego_runnera_przed_wyslaniem_polecenia(
    monkeypatch, zapytania_w_tle,
):
    """Szybki runner moze odpowiedziec, zanim `zapytaj` wroci z wysylki - wpis musi juz byc."""
    prawdziwa = bus.send_to_runner
    widziane: list[tuple[str, tuple[str, bool]]] = []

    async def podgladaj(runner_id, polecenie):
        wpis = bus._oczekujace.get(polecenie["req_id"])
        widziane.append((polecenie["req_id"], (None, None) if wpis is None
                         else (wpis[0], wpis[1].done())))
        await prawdziwa(runner_id, polecenie)

    monkeypatch.setattr(bus, "send_to_runner", podgladaj)

    _, polecenie = await zapytania_w_tle.start(B1)

    assert widziane == [(polecenie["req_id"], (B1, False))]


async def test_odpowiedz_innego_runnera_na_cudze_zapytanie_jest_odrzucona(zapytania_w_tle):
    zadanie, polecenie = await zapytania_w_tle.start(B1)
    req_id = polecenie["req_id"]

    assert bus.odpowiedz(B2, req_id, {"ok": True, "wynik": "od-b2"}) is False
    assert bus.odpowiedz(OBCY, req_id, {"ok": True, "wynik": "od-obcego"}) is False

    assert not bus._oczekujace[req_id][1].done()
    assert not zadanie.done()
    # odrzucona odpowiedz nie zuzywa zapytania: wlasciwy runner nadal moze odpowiedziec
    assert bus.odpowiedz(B1, req_id, {"ok": True, "wynik": "od-b1"}) is True
    assert (await asyncio.wait_for(zadanie, 5))["wynik"] == "od-b1"


async def test_odpowiedz_na_nieznany_req_id_jest_odrzucona():
    assert bus.odpowiedz(B1, "nigdy-nie-wyslany", {"ok": True}) is False
    assert "nigdy-nie-wyslany" not in bus._oczekujace


async def test_druga_odpowiedz_na_to_samo_zapytanie_jest_odrzucona_a_pierwsza_wygrywa(
    zapytania_w_tle,
):
    """Obie odpowiedzi bez oddania sterowania: wpis jeszcze jest w `_oczekujace`, wiec o False
    decyduje wylacznie to, ze future jest juz rozwiazany."""
    zadanie, polecenie = await zapytania_w_tle.start(B1)
    req_id = polecenie["req_id"]

    pierwsza = bus.odpowiedz(B1, req_id, {"ok": True, "wynik": "pierwsza"})
    druga = bus.odpowiedz(B1, req_id, {"ok": True, "wynik": "druga"})

    assert (pierwsza, druga) == (True, False)
    assert (await asyncio.wait_for(zadanie, 5))["wynik"] == "pierwsza"


async def test_zapytanie_bez_odpowiedzi_konczy_sie_bledem_po_podanym_czasie(zapytania_w_tle):
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(bus.zapytaj(B1, "ping", {}, 0.05), 5)
    czas = time.monotonic() - start

    assert 0.04 <= czas < 2       # czekal podany timeout, a nie zewnetrzny limit testu


async def test_wpis_zapytania_znika_po_odpowiedzi(zapytania_w_tle):
    zadanie, polecenie = await zapytania_w_tle.start(B1)
    assert polecenie["req_id"] in bus._oczekujace

    bus.odpowiedz(B1, polecenie["req_id"], {"ok": True})
    await asyncio.wait_for(zadanie, 5)

    assert bus._oczekujace == {}


async def test_wpis_zapytania_znika_po_przekroczeniu_czasu():
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(bus.zapytaj(B1, "ping", {}, 0.05), 5)

    assert bus._oczekujace == {}


async def test_wpis_zapytania_znika_gdy_zapytanie_zostanie_anulowane(zapytania_w_tle):
    zadanie, polecenie = await zapytania_w_tle.start(B1)
    assert polecenie["req_id"] in bus._oczekujace

    zadanie.cancel()
    await asyncio.wait({zadanie}, timeout=5)

    assert zadanie.cancelled()
    assert bus._oczekujace == {}


async def test_odpowiedz_spozniona_po_przekroczeniu_czasu_jest_odrzucona(zapytania_w_tle):
    zadanie, polecenie = await zapytania_w_tle.start(B1, "ping", timeout_s=0.05)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(zadanie, 5)

    assert bus.odpowiedz(B1, polecenie["req_id"], {"ok": True, "wynik": "za pozno"}) is False


# =================================================================================
# Konfiguracja
# =================================================================================

@pytest.fixture
def czyste_srodowisko(monkeypatch):
    monkeypatch.delenv("COCKPIT_RUNNER_ZAPYTANIE_TIMEOUT_S", raising=False)
    monkeypatch.delenv("COCKPIT_RUNNER_ODPOWIEDZ_MAX_KB", raising=False)


def test_zapytanie_do_runnera_domyslnie_czeka_15_sekund(czyste_srodowisko):
    assert Settings(_env_file=None).runner_zapytanie_timeout_s == 15


def test_limit_czasu_zapytania_do_runnera_moze_byc_ulamkiem_sekundy(czyste_srodowisko):
    assert Settings(_env_file=None, runner_zapytanie_timeout_s=0.5).runner_zapytanie_timeout_s == 0.5


def test_limit_czasu_zapytania_do_runnera_mozna_ustawic_zmienna_srodowiskowa(
    czyste_srodowisko, monkeypatch,
):
    monkeypatch.setenv("COCKPIT_RUNNER_ZAPYTANIE_TIMEOUT_S", "3")
    assert Settings(_env_file=None).runner_zapytanie_timeout_s == 3


@pytest.mark.parametrize("wartosc", [0, -1])
def test_limit_czasu_zapytania_do_runnera_zero_albo_ujemny_jest_odrzucony(
    czyste_srodowisko, wartosc,
):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, runner_zapytanie_timeout_s=wartosc)


def test_odpowiedz_runnera_domyslnie_moze_miec_4096_kb(czyste_srodowisko):
    # HAR-55: 6 x 512 KB (domyslny limit odczytu pliku x najgorsze rozdmuchanie przez JSON)
    # plus zapas na opakowanie odpowiedzi (req_id, ok, sciezka, rozmiar)
    assert Settings(_env_file=None).runner_odpowiedz_max_kb == 4096


def test_limit_rozmiaru_odpowiedzi_runnera_mozna_ustawic_zmienna_srodowiskowa(
    czyste_srodowisko, monkeypatch,
):
    monkeypatch.setenv("COCKPIT_RUNNER_ODPOWIEDZ_MAX_KB", "7")
    assert Settings(_env_file=None).runner_odpowiedz_max_kb == 7


def test_limit_rozmiaru_odpowiedzi_runnera_zero_jest_odrzucony_a_jeden_przyjety(czyste_srodowisko):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, runner_odpowiedz_max_kb=0)
    assert Settings(_env_file=None, runner_odpowiedz_max_kb=1).runner_odpowiedz_max_kb == 1


# =================================================================================
# Serwis: zapytaj_runnera_zespolu
# =================================================================================

class _Wyslij:
    """Atrapa `wyslij`: liczy wywolania i zwraca ustalone cialo albo rzuca ustalony wyjatek."""

    def __init__(self, cialo=None, blad: BaseException | None = None) -> None:
        self.cialo = {"ok": True, "wynik": {"pong": True}} if cialo is None else cialo
        self.blad = blad
        self.wywolania: list[tuple] = []

    async def __call__(self, runner_id, typ, dane, timeout_s):
        self.wywolania.append((runner_id, typ, dane, timeout_s))
        if self.blad is not None:
            raise self.blad
        return self.cialo


async def _serwis(tenant_id: str, wyslij, polaczone, typ: str = "ping", dane: dict | None = None):
    async with db_session.SessionLocal() as s:
        return await zapytania.zapytaj_runnera_zespolu(
            s, tenant_id, typ, dane if dane is not None else {}, frozenset(polaczone), wyslij)


async def test_zapytanie_do_zespolu_bez_runnera_online_konczy_sie_bledem_i_nic_nie_wysyla(swiat):
    wyslij = _Wyslij()

    start = time.monotonic()
    with pytest.raises(StanNiemozliwy, match="^runner offline"):
        await _serwis(swiat.browarek, wyslij, polaczone=set())

    assert wyslij.wywolania == []
    assert time.monotonic() - start < 1       # natychmiast, bez czekania na timeout


async def test_zapytanie_nie_trafia_do_runnera_innego_zespolu_nawet_gdy_jest_online(swiat):
    wyslij = _Wyslij()

    with pytest.raises(StanNiemozliwy, match="^runner offline"):
        await _serwis(swiat.browarek, wyslij, polaczone={OBCY})

    assert wyslij.wywolania == []


async def test_zapytanie_zespolu_idzie_do_jego_runnera_a_nie_do_runnera_innego_zespolu(swiat):
    wyslij = _Wyslij()

    await _serwis(swiat.browarek, wyslij, polaczone={OBCY, B1}, typ="ping", dane={"a": 1})

    assert [(rid, typ, dane) for rid, typ, dane, _ in wyslij.wywolania] == [(B1, "ping", {"a": 1})]


async def test_zapytanie_drugiego_zespolu_idzie_do_runnera_tego_zespolu(swiat):
    wyslij = _Wyslij()

    await _serwis(swiat.piekarnia, wyslij, polaczone={B1, OBCY})

    assert [rid for rid, *_ in wyslij.wywolania] == [OBCY]


async def test_runner_zespolu_bez_otwartego_strumienia_nie_jest_wybierany(swiat):
    wyslij = _Wyslij()

    with pytest.raises(StanNiemozliwy, match="^runner offline"):
        await _serwis(swiat.browarek, wyslij, polaczone=set())      # B1 ma swiezy heartbeat, ale strumienia brak

    assert wyslij.wywolania == []


async def test_runner_z_otwartym_strumieniem_ale_nieswiezym_heartbeatem_nie_jest_wybierany(swiat):
    wyslij = _Wyslij()

    await _serwis(swiat.browarek, wyslij, polaczone={B_STARY, B1})

    assert [rid for rid, *_ in wyslij.wywolania] == [B1]      # B_STARY ma mniejsze id, ale jest nieswiezy


async def test_gdy_tylko_runner_z_nieswiezym_heartbeatem_ma_strumien_zespol_jest_offline(swiat):
    wyslij = _Wyslij()

    with pytest.raises(StanNiemozliwy, match="^runner offline"):
        await _serwis(swiat.browarek, wyslij, polaczone={B_STARY})

    assert wyslij.wywolania == []


async def test_gdy_kilku_runnerow_zespolu_jest_online_zapytanie_idzie_do_pierwszego_wg_id(swiat):
    wyslij = _Wyslij()

    await _serwis(swiat.browarek, wyslij, polaczone={B2, B1})      # w bazie B2 wstawiono przed B1

    assert [rid for rid, *_ in wyslij.wywolania] == [B1]


async def test_zapytanie_dostaje_limit_czasu_z_konfiguracji_kokpitu(swiat, monkeypatch):
    wyslij = _Wyslij()

    monkeypatch.setattr(settings, "runner_zapytanie_timeout_s", 7.5)
    await _serwis(swiat.browarek, wyslij, polaczone={B1})
    monkeypatch.setattr(settings, "runner_zapytanie_timeout_s", 0.05)
    await _serwis(swiat.browarek, wyslij, polaczone={B1})

    assert [timeout for *_, timeout in wyslij.wywolania] == [7.5, 0.05]


async def test_runner_ktory_nie_odpowiada_w_limicie_czasu_daje_blad_dziedziny_a_nie_wyjatek_czasu(
    swiat, monkeypatch,
):
    monkeypatch.setattr(settings, "runner_zapytanie_timeout_s", 0.05)
    _polacz(B1)

    start = time.monotonic()
    with pytest.raises(StanNiemozliwy) as blad:
        await asyncio.wait_for(
            _serwis(swiat.browarek, bus.zapytaj, polaczone=bus.connected_runner_ids()), 5)

    assert str(blad.value) == "runner nie odpowiedzial w limicie czasu"
    assert time.monotonic() - start < 2
    assert bus._oczekujace == {}


async def test_przekroczenie_czasu_zglaszane_przez_wysylke_daje_blad_dziedziny(swiat):
    with pytest.raises(StanNiemozliwy, match="nie odpowiedzial") as blad:
        await _serwis(swiat.browarek, _Wyslij(blad=TimeoutError()), polaczone={B1})

    assert type(blad.value) is StanNiemozliwy
    assert str(blad.value) == "runner nie odpowiedzial w limicie czasu"


async def test_inny_wyjatek_wysylki_niz_przekroczenie_czasu_nie_jest_podawany_jako_brak_odpowiedzi(
    swiat,
):
    with pytest.raises(RuntimeError, match="awaria szyny"):
        await _serwis(swiat.browarek, _Wyslij(blad=RuntimeError("awaria szyny")), polaczone={B1})


async def test_blad_zgloszony_przez_runner_trafia_do_wolajacego_doslownie(swiat):
    wyslij = _Wyslij({"ok": False, "blad": "dysk jest pelny", "kod": "blad_operacji"})

    with pytest.raises(StanNiemozliwy) as blad:
        await _serwis(swiat.browarek, wyslij, polaczone={B1})

    assert str(blad.value) == "dysk jest pelny"


async def test_blad_runnera_bez_komunikatu_ma_komunikat_zastepczy(swiat):
    with pytest.raises(StanNiemozliwy) as blad:
        await _serwis(swiat.browarek, _Wyslij({"ok": False, "blad": None}), polaczone={B1})

    assert str(blad.value) == "runner zglosil blad"


@pytest.mark.parametrize("ok", ["tak", 1], ids=["napis", "liczba"])
async def test_odpowiedz_runnera_z_ok_innym_niz_prawda_logiczna_nie_jest_sukcesem(swiat, ok):
    with pytest.raises(StanNiemozliwy):
        await _serwis(swiat.browarek, _Wyslij({"ok": ok, "wynik": {"pong": True}}), polaczone={B1})


async def test_udana_odpowiedz_runnera_zwraca_wynik_operacji(swiat):
    wyslij = _Wyslij({"ok": True, "wynik": {"pong": True, "licznik": [1, 2]}, "blad": None, "kod": None})

    assert await _serwis(swiat.browarek, wyslij, polaczone={B1}) == {"pong": True, "licznik": [1, 2]}


async def test_udana_odpowiedz_bez_wyniku_zwraca_brak_a_nie_cala_odpowiedz(swiat):
    assert await _serwis(swiat.browarek, _Wyslij({"ok": True}), polaczone={B1}) is None


def _cialo_o_rozmiarze(bajty: int) -> dict:
    """Odpowiedz, ktorej JSON ma dokladnie `bajty` bajtow."""
    narzut = len(json.dumps({"ok": True, "wynik": ""}).encode())
    cialo = {"ok": True, "wynik": "a" * (bajty - narzut)}
    assert len(json.dumps(cialo).encode()) == bajty
    return cialo


async def test_odpowiedz_wieksza_niz_limit_z_konfiguracji_jest_odrzucona(swiat, monkeypatch):
    monkeypatch.setattr(settings, "runner_odpowiedz_max_kb", 1)

    with pytest.raises(StanNiemozliwy, match="za duza"):
        await _serwis(swiat.browarek, _Wyslij(_cialo_o_rozmiarze(1025)), polaczone={B1})


async def test_odpowiedz_rowna_limitowi_z_konfiguracji_jest_przyjeta(swiat, monkeypatch):
    monkeypatch.setattr(settings, "runner_odpowiedz_max_kb", 1)
    cialo = _cialo_o_rozmiarze(1024)

    assert await _serwis(swiat.browarek, _Wyslij(cialo), polaczone={B1}) == cialo["wynik"]


async def test_limit_rozmiaru_odpowiedzi_wynika_z_konfiguracji_a_nie_ze_stalej_w_kodzie(
    swiat, monkeypatch,
):
    cialo = _cialo_o_rozmiarze(1025)
    monkeypatch.setattr(settings, "runner_odpowiedz_max_kb", 1)
    with pytest.raises(StanNiemozliwy, match="za duza"):
        await _serwis(swiat.browarek, _Wyslij(cialo), polaczone={B1})

    monkeypatch.setattr(settings, "runner_odpowiedz_max_kb", 2)
    assert await _serwis(swiat.browarek, _Wyslij(cialo), polaczone={B1}) == cialo["wynik"]


# =================================================================================
# Trasa POST /api/runners/odpowiedzi
# =================================================================================

async def _odpowiedz_http(swiat, runner_id: str | None, tresc: dict):
    naglowki = {} if runner_id is None else _naglowek(runner_id)
    return await swiat.klient.post("/api/runners/odpowiedzi", json=tresc, headers=naglowki)


@pytest.mark.parametrize(
    "naglowki",
    [{}, {"Authorization": "tok-b1"}, {"Authorization": "Bearer nieznany-token"}],
    ids=["bez_tokenu", "token_bez_prefiksu_bearer", "token_nieznany"],
)
async def test_odpowiedz_bez_waznego_tokenu_runnera_dostaje_odmowe_a_zapytanie_czeka(
    swiat, zapytania_w_tle, naglowki,
):
    zadanie, polecenie = await zapytania_w_tle.start(B1)

    r = await swiat.klient.post(
        "/api/runners/odpowiedzi",
        json={"req_id": polecenie["req_id"], "ok": True, "wynik": "podszyty"}, headers=naglowki)

    assert r.status_code == 401
    assert not bus._oczekujace[polecenie["req_id"]][1].done()
    assert not zadanie.done()


async def test_poprawna_odpowiedz_runnera_dostaje_204_a_zapytanie_dostaje_cialo(
    swiat, zapytania_w_tle,
):
    zadanie, polecenie = await zapytania_w_tle.start(B1)

    r = await _odpowiedz_http(swiat, B1, {"req_id": polecenie["req_id"], "ok": True,
                                          "wynik": {"pong": True}})

    assert r.status_code == 204
    assert r.content == b""
    assert await asyncio.wait_for(zadanie, 5) == {
        "ok": True, "wynik": {"pong": True}, "blad": None, "kod": None}
    assert bus._oczekujace == {}


async def test_odpowiedz_z_bledem_runnera_przenosi_komunikat_i_kod_do_zapytania(
    swiat, zapytania_w_tle,
):
    zadanie, polecenie = await zapytania_w_tle.start(B1)

    r = await _odpowiedz_http(swiat, B1, {"req_id": polecenie["req_id"], "ok": False,
                                          "blad": "nieznana operacja: x", "kod": "nieznana_operacja"})

    assert r.status_code == 204
    assert await asyncio.wait_for(zadanie, 5) == {
        "ok": False, "wynik": None, "blad": "nieznana operacja: x", "kod": "nieznana_operacja"}


@pytest.mark.parametrize("odpowiadajacy", [B2, OBCY], ids=["inny_runner_zespolu", "runner_innego_zespolu"])
async def test_runner_odpowiadajacy_na_cudze_zapytanie_dostaje_404_a_zapytanie_czeka_dalej(
    swiat, zapytania_w_tle, odpowiadajacy,
):
    zadanie, polecenie = await zapytania_w_tle.start(B1)
    req_id = polecenie["req_id"]

    r = await _odpowiedz_http(swiat, odpowiadajacy, {"req_id": req_id, "ok": True, "wynik": "cudze"})

    assert r.status_code == 404
    assert r.json()["detail"] == "nieznane zapytanie"
    assert not bus._oczekujace[req_id][1].done()
    assert not zadanie.done()
    # prawdziwy adresat dalej moze odpowiedziec: odrzucona proba nie zuzyla zapytania
    r = await _odpowiedz_http(swiat, B1, {"req_id": req_id, "ok": True, "wynik": "wlasne"})
    assert r.status_code == 204
    assert (await asyncio.wait_for(zadanie, 5))["wynik"] == "wlasne"


async def test_odpowiedz_na_nieznany_req_id_dostaje_404(swiat):
    r = await _odpowiedz_http(swiat, B1, {"req_id": "nikt-nie-pytal", "ok": True})

    assert r.status_code == 404
    assert r.json()["detail"] == "nieznane zapytanie"


async def test_odpowiedz_po_zamknieciu_zapytania_dostaje_404(swiat, zapytania_w_tle):
    zadanie, polecenie = await zapytania_w_tle.start(B1, "ping", timeout_s=0.05)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(zadanie, 5)

    r = await _odpowiedz_http(swiat, B1, {"req_id": polecenie["req_id"], "ok": True})

    assert r.status_code == 404


@pytest.mark.parametrize(
    "tresc",
    [{"req_id": "a" * 65, "ok": True},
     {"req_id": "", "ok": True},
     {"ok": True},
     {"req_id": "r1"},
     {"req_id": "r1", "ok": False, "blad": "b" * 2001},
     {"req_id": "r1", "ok": False, "kod": "k" * 65}],
    ids=["req_id_65_znakow", "req_id_pusty", "bez_req_id", "bez_ok", "blad_2001_znakow", "kod_65_znakow"],
)
async def test_odpowiedz_z_niepoprawnym_ksztaltem_dostaje_422(swiat, tresc):
    assert (await _odpowiedz_http(swiat, B1, tresc)).status_code == 422


@pytest.mark.parametrize(
    "tresc",
    [{"req_id": "a" * 64, "ok": True},
     {"req_id": "r1", "ok": False, "blad": "b" * 2000},
     {"req_id": "r1", "ok": False, "kod": "k" * 64}],
    ids=["req_id_64_znaki", "blad_2000_znakow", "kod_64_znaki"],
)
async def test_odpowiedz_na_granicy_dlugosci_pol_przechodzi_walidacje_i_dostaje_404_nieznanego_zapytania(
    swiat, tresc,
):
    assert (await _odpowiedz_http(swiat, B1, tresc)).status_code == 404


# =================================================================================
# Operacje runnera
# =================================================================================

async def test_ping_runnera_odpowiada_pong_z_req_id_zapytania():
    assert await operacje.wykonaj({"type": "ping", "req_id": "r1"}) == {
        "req_id": "r1", "ok": True, "wynik": {"pong": True}}


async def test_nieznana_operacja_daje_czytelny_blad_z_kodem_i_req_id():
    assert await operacje.wykonaj({"type": "teleport", "req_id": "r2"}) == {
        "req_id": "r2", "ok": False, "blad": "nieznana operacja: teleport",
        "kod": "nieznana_operacja"}


@pytest.mark.parametrize("wyjatek", [RuntimeError, ValueError, OSError])
async def test_operacja_ktora_rzuca_wyjatek_daje_blad_bez_tresci_wyjatku_i_bez_sladu_stosu(
    monkeypatch, wyjatek,
):
    async def psuj(dane):
        raise wyjatek(f"w srodku jest {SEKRET}")

    monkeypatch.setitem(operacje.OPERACJE, "psuj", psuj)

    odp = await operacje.wykonaj({"type": "psuj", "req_id": "r3"})

    assert odp == {"req_id": "r3", "ok": False,
                   "blad": f"operacja psuj nie powiodla sie ({wyjatek.__name__})",
                   "kod": "blad_operacji"}
    assert SEKRET not in json.dumps(odp)
    assert "Traceback" not in json.dumps(odp)


async def test_operacja_dostaje_dane_zapytania_bez_typu_i_req_id(monkeypatch):
    dostala: list[dict] = []

    async def podsluchaj(dane):
        dostala.append(dict(dane))
        return {"ok": "w srodku"}

    monkeypatch.setitem(operacje.OPERACJE, "podsluchaj", podsluchaj)

    odp = await operacje.wykonaj({"type": "podsluchaj", "req_id": "r4", "a": 1, "b": [2]})

    assert dostala == [{"a": 1, "b": [2]}]
    assert odp == {"req_id": "r4", "ok": True, "wynik": {"ok": "w srodku"}}


async def test_wykonanie_nowej_operacji_z_tabeli_zwraca_jej_wynik(monkeypatch):
    async def lista(dane):
        return [1, 2, 3]

    monkeypatch.setitem(operacje.OPERACJE, "lista", lista)

    assert await operacje.wykonaj({"type": "lista", "req_id": "r5"}) == {
        "req_id": "r5", "ok": True, "wynik": [1, 2, 3]}


# =================================================================================
# Klient runnera: wysylka odpowiedzi
# =================================================================================

def _klient_z_atrapa(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _swieze(polecenie: dict, **nadpisz) -> dict:
    """Polecenie tak, jak nadaje je kokpit (HAR-201): z czasem nadania i limitem waznosci."""
    return {**polecenie, "nadano_o": time.time(), "wazne_s": 15.0, **nadpisz}


async def _odpowiedzi_runnera_na(polecenie: dict) -> list[dict]:
    """Runner obsluguje `polecenie`; zwraca ciala POST-ow na /api/runners/odpowiedzi."""
    odpowiedzi: list[dict] = []

    def handler(request):
        odpowiedzi.append(json.loads(request.content))
        return httpx.Response(204)

    runner = RunnerKlient("http://kokpit.test", SEKRET)
    async with _klient_z_atrapa(handler) as klient:
        await asyncio.wait_for(runner._odpowiedz_na_zapytanie(klient, polecenie), 5)
    return odpowiedzi


async def test_runner_odpowiada_na_zapytanie_postem_z_tokenem_i_wynikiem_operacji():
    zadania: list[httpx.Request] = []

    def handler(request):
        zadania.append(request)
        return httpx.Response(204)

    runner = RunnerKlient("http://kokpit.test/", SEKRET)
    async with _klient_z_atrapa(handler) as klient:
        await runner._odpowiedz_na_zapytanie(klient, _swieze({"type": "ping", "req_id": "r1"}))

    assert len(zadania) == 1
    assert zadania[0].method == "POST"
    assert str(zadania[0].url) == "http://kokpit.test/api/runners/odpowiedzi"
    assert zadania[0].headers["Authorization"] == f"Bearer {SEKRET}"
    assert json.loads(zadania[0].content) == {"req_id": "r1", "ok": True, "wynik": {"pong": True}}


def _cykl() -> dict:
    d: dict = {}
    d["d"] = d
    return d


@pytest.mark.parametrize(
    ("wynik", "typ_bledu"),
    [
        (object(), "TypeError"),
        ({"zbior": {1, 2}}, "TypeError"),
        (float("nan"), "ValueError"),
        ({"x": float("inf")}, "ValueError"),
        (_cykl(), "ValueError"),
    ],
    ids=["obiekt", "zbior", "nan", "inf", "cykl"],
)
async def test_wynik_operacji_nieserializowalny_do_json_wraca_do_kokpitu_jako_blad_operacji(
    monkeypatch, wynik, typ_bledu,
):
    # wykonaj() zwraca {"ok": True, "wynik": ...}; podmieniamy je, by wynik trafil prosto do klienta
    async def wykonaj(cmd):
        return {"req_id": cmd["req_id"], "ok": True, "wynik": wynik}

    monkeypatch.setattr(operacje, "wykonaj", wykonaj)
    zadania: list[httpx.Request] = []

    def handler(request):
        zadania.append(request)
        return httpx.Response(204)

    runner = RunnerKlient("http://kokpit.test", SEKRET)
    async with _klient_z_atrapa(handler) as klient:
        await asyncio.wait_for(
            runner._odpowiedz_na_zapytanie(
                klient, _swieze({"type": "x", "req_id": "r-nieser"})), 5)

    assert len(zadania) == 1
    assert zadania[0].method == "POST"
    assert str(zadania[0].url) == "http://kokpit.test/api/runners/odpowiedzi"
    assert zadania[0].headers["Authorization"] == f"Bearer {SEKRET}"
    odp = json.loads(zadania[0].content)
    assert set(odp) == {"req_id", "ok", "blad", "kod"}
    assert odp["req_id"] == "r-nieser"
    assert odp["ok"] is False
    assert odp["kod"] == "blad_operacji"
    assert typ_bledu in odp["blad"]
    assert "serializable" not in odp["blad"]
    assert "object at" not in odp["blad"]
    assert "Out of range" not in odp["blad"]
    assert "Circular" not in odp["blad"]


async def test_poprawny_wynik_operacji_idzie_do_kokpitu_bez_zmian_mimo_straznika_serializacji(
    monkeypatch,
):
    async def wykonaj(cmd):
        return {"req_id": cmd["req_id"], "ok": True, "wynik": {"a": [1, 2.5, None, "zażółć"]}}

    monkeypatch.setattr(operacje, "wykonaj", wykonaj)
    zadania: list[httpx.Request] = []

    def handler(request):
        zadania.append(request)
        return httpx.Response(204)

    runner = RunnerKlient("http://kokpit.test", SEKRET)
    async with _klient_z_atrapa(handler) as klient:
        await asyncio.wait_for(
            runner._odpowiedz_na_zapytanie(
                klient, _swieze({"type": "x", "req_id": "r-ok"})), 5)

    assert len(zadania) == 1
    assert json.loads(zadania[0].content) == {
        "req_id": "r-ok", "ok": True, "wynik": {"a": [1, 2.5, None, "zażółć"]}}


@pytest.mark.parametrize("status", [200, 204])
async def test_odpowiedz_przyjeta_przez_kokpit_niczego_nie_wypisuje(status, capsys):
    runner = RunnerKlient("http://kokpit.test", SEKRET)
    async with _klient_z_atrapa(lambda request: httpx.Response(status)) as klient:
        await runner._odpowiedz_na_zapytanie(klient, _swieze({"type": "ping", "req_id": "r1"}))

    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("status", [400, 401, 404, 500])
async def test_odpowiedz_odrzucona_przez_kokpit_to_jedna_linia_na_stderr_bez_wyjatku_i_bez_sekretow(
    status, capsys,
):
    runner = RunnerKlient("http://kokpit.test", SEKRET)
    async with _klient_z_atrapa(
        lambda request: httpx.Response(status, text=f"tresc z {SEKRET}")
    ) as klient:
        wynik = await runner._odpowiedz_na_zapytanie(
            klient, _swieze({"type": "ping", "req_id": "zapytanie-7f3"}))

    err = capsys.readouterr().err
    assert wynik is None
    assert len(err.splitlines()) == 1
    assert SEKRET not in err
    assert err == f"odpowiedz na zapytanie zapytanie-7f3 niewyslana (http={status})\n"


@pytest.mark.parametrize(
    ("blad", "nazwa"),
    [
        (httpx.ConnectError(f"brak polaczenia, token {SEKRET}"), "ConnectError"),
        (httpx.ReadTimeout(f"timeout {SEKRET}"), "ReadTimeout"),
    ],
    ids=["ConnectError", "ReadTimeout"],
)
async def test_awaria_sieci_przy_odpowiedzi_to_jedna_linia_na_stderr_bez_wyjatku_i_bez_sekretow(
    blad, nazwa, capsys,
):
    def handler(request):
        raise blad

    runner = RunnerKlient("http://kokpit.test", SEKRET)
    async with _klient_z_atrapa(handler) as klient:
        wynik = await runner._odpowiedz_na_zapytanie(
            klient, _swieze({"type": "ping", "req_id": "zapytanie-7f3"}))

    err = capsys.readouterr().err
    assert wynik is None
    assert len(err.splitlines()) == 1
    assert SEKRET not in err
    assert err == f"odpowiedz na zapytanie zapytanie-7f3 niewyslana ({nazwa})\n"


# --- HAR-201: runner nie wykonuje zapytania, na ktore kokpit juz nie czeka --------------------

async def test_swieze_zapytanie_jest_wykonane_a_pola_protokolu_nie_trafiaja_do_operacji(
    monkeypatch,
):
    dostala: list[dict] = []

    async def podsluchaj(dane):
        dostala.append(dict(dane))
        return "zrobione"

    monkeypatch.setitem(operacje.OPERACJE, "podsluchaj", podsluchaj)

    odpowiedzi = await _odpowiedzi_runnera_na(
        _swieze({"type": "podsluchaj", "req_id": "r-s", "a": 1}))

    assert dostala == [{"a": 1}]
    assert odpowiedzi == [{"req_id": "r-s", "ok": True, "wynik": "zrobione"}]


def _operacja_liczaca_wywolania(monkeypatch) -> list[dict]:
    wywolania: list[dict] = []

    async def licz(dane):
        wywolania.append(dict(dane))
        return "wykonano"

    monkeypatch.setitem(operacje.OPERACJE, "licz", licz)
    return wywolania


@pytest.mark.parametrize("wiek_s", [16.0, 100.0, 86400.0])
async def test_zapytanie_starsze_niz_limit_waznosci_jest_odrzucone_jako_przeterminowane_bez_wykonania(
    monkeypatch, wiek_s,
):
    wywolania = _operacja_liczaca_wywolania(monkeypatch)

    odpowiedzi = await _odpowiedzi_runnera_na(
        _swieze({"type": "licz", "req_id": "r-st"}, nadano_o=time.time() - wiek_s))

    assert wywolania == []
    assert odpowiedzi == [{"req_id": "r-st", "ok": False, "blad": _BLAD_PRZETERMINOWANE,
                           "kod": "przeterminowane"}]


async def test_zapytanie_w_polowie_limitu_waznosci_jest_jeszcze_wykonane(monkeypatch):
    wywolania = _operacja_liczaca_wywolania(monkeypatch)

    odpowiedzi = await _odpowiedzi_runnera_na(
        _swieze({"type": "licz", "req_id": "r-pol"}, nadano_o=time.time() - 5.0, wazne_s=15.0))

    assert len(wywolania) == 1
    assert odpowiedzi == [{"req_id": "r-pol", "ok": True, "wynik": "wykonano"}]


async def test_wiek_zapytania_jest_liczony_wzgledem_jego_wlasnego_limitu_a_nie_stalej_runnera(
    monkeypatch,
):
    """Ten sam wiek (10 s): przy limicie 5 s zapytanie jest przeterminowane, przy 30 s wazne."""
    wywolania = _operacja_liczaca_wywolania(monkeypatch)
    nadano = time.time() - 10.0

    krotki = await _odpowiedzi_runnera_na(
        {"type": "licz", "req_id": "r-5", "nadano_o": nadano, "wazne_s": 5.0})
    dlugi = await _odpowiedzi_runnera_na(
        {"type": "licz", "req_id": "r-30", "nadano_o": nadano, "wazne_s": 30.0})

    assert krotki[0]["kod"] == "przeterminowane"
    assert dlugi[0]["ok"] is True
    assert len(wywolania) == 1


_BRAK = object()


@pytest.mark.parametrize(
    ("nadano_o", "wazne_s"),
    [
        (_BRAK, 15.0), (None, 15.0), ("123.0", 15.0), (True, 15.0), ([1.0], 15.0),
        (float("nan"), 15.0), (float("inf"), 15.0),
        (None, None),
    ],
    ids=["brak_nadano_o", "nadano_o_null", "nadano_o_napis", "nadano_o_logiczne", "nadano_o_lista",
         "nadano_o_nan", "nadano_o_inf", "oba_null"],
)
async def test_zapytanie_z_nieprawidlowym_czasem_nadania_jest_odrzucone_bez_wykonania(
    monkeypatch, nadano_o, wazne_s,
):
    wywolania = _operacja_liczaca_wywolania(monkeypatch)
    polecenie = {"type": "licz", "req_id": "r-zle", "nadano_o": time.time(), "wazne_s": wazne_s}
    if nadano_o is _BRAK:
        del polecenie["nadano_o"]
    else:
        polecenie["nadano_o"] = nadano_o

    odpowiedzi = await _odpowiedzi_runnera_na(polecenie)

    assert wywolania == []
    assert odpowiedzi == [{"req_id": "r-zle", "ok": False, "blad": _BLAD_CZAS_NADANIA,
                           "kod": "zly_czas_nadania"}]


_BLAD_CZAS_NADANIA = "zapytanie bez poprawnego czasu nadania (nadano_o)"
_BLAD_LIMIT_WAZNOSCI = "zapytanie bez poprawnego limitu waznosci (wazne_s)"
_BLAD_PRZETERMINOWANE = "zapytanie przeterminowane: kokpit nie czeka juz na odpowiedz"


async def test_czas_nadania_ponad_zakres_liczby_zmiennoprzecinkowej_jest_odrzucony_bez_wykonania(
    monkeypatch,
):
    """`float(10**400)` rzuca OverflowError: to jest zly czas nadania, a nie awaria runnera."""
    wywolania = _operacja_liczaca_wywolania(monkeypatch)

    odpowiedzi = await _odpowiedzi_runnera_na(
        {"type": "licz", "req_id": "r-ov-n", "nadano_o": 10**400, "wazne_s": 15.0})

    assert wywolania == []
    assert odpowiedzi == [{"req_id": "r-ov-n", "ok": False, "blad": _BLAD_CZAS_NADANIA,
                           "kod": "zly_czas_nadania"}]


async def test_limit_waznosci_ponad_zakres_liczby_zmiennoprzecinkowej_jest_odrzucony_bez_wykonania(
    monkeypatch,
):
    wywolania = _operacja_liczaca_wywolania(monkeypatch)

    odpowiedzi = await _odpowiedzi_runnera_na(
        {"type": "licz", "req_id": "r-ov-w", "nadano_o": time.time(), "wazne_s": 10**400})

    assert wywolania == []
    assert odpowiedzi == [{"req_id": "r-ov-w", "ok": False, "blad": _BLAD_LIMIT_WAZNOSCI,
                           "kod": "zly_limit_waznosci"}]


@pytest.mark.parametrize(
    "wazne_s",
    [_BRAK, None, "15", True, [15], float("nan"), float("inf"), float("-inf"), -1.0, -1, 0, 0.0],
    ids=["brak", "null", "napis", "logiczne", "lista", "nan", "inf", "minus_inf", "ujemne",
         "ujemne_calkowite", "zero_calkowite", "zero"],
)
async def test_zapytanie_z_nieprawidlowym_limitem_waznosci_jest_odrzucone_bez_wykonania(
    monkeypatch, wazne_s,
):
    wywolania = _operacja_liczaca_wywolania(monkeypatch)
    polecenie = {"type": "licz", "req_id": "r-zle", "nadano_o": time.time()}
    if wazne_s is not _BRAK:
        polecenie["wazne_s"] = wazne_s

    odpowiedzi = await _odpowiedzi_runnera_na(polecenie)

    assert wywolania == []
    assert odpowiedzi == [{"req_id": "r-zle", "ok": False, "blad": _BLAD_LIMIT_WAZNOSCI,
                           "kod": "zly_limit_waznosci"}]


# Wstrzykniety `teraz` = 1000: wiek to dokladnie `1000 - nadano_o`, bez zegara i bez wyscigu.
_TERAZ = 1000.0


@pytest.mark.parametrize(
    ("polecenie", "oczekiwana"),
    [
        # zly czas nadania: odrzuca go tylko straznik czasu (limit jest poprawny)
        ({"nadano_o": None, "wazne_s": 15.0}, ("zly_czas_nadania", _BLAD_CZAS_NADANIA)),
        ({"wazne_s": 15.0}, ("zly_czas_nadania", _BLAD_CZAS_NADANIA)),
        ({"nadano_o": "990.0", "wazne_s": 15.0}, ("zly_czas_nadania", _BLAD_CZAS_NADANIA)),
        ({"nadano_o": [990.0], "wazne_s": 15.0}, ("zly_czas_nadania", _BLAD_CZAS_NADANIA)),
        # logiczne True to nie liczba 1.0: gdyby przeszlo, wiek 999 s dalby "przeterminowane"
        ({"nadano_o": True, "wazne_s": 15.0}, ("zly_czas_nadania", _BLAD_CZAS_NADANIA)),
        ({"nadano_o": False, "wazne_s": 15.0}, ("zly_czas_nadania", _BLAD_CZAS_NADANIA)),
        ({"nadano_o": float("nan"), "wazne_s": 15.0}, ("zly_czas_nadania", _BLAD_CZAS_NADANIA)),
        ({"nadano_o": float("inf"), "wazne_s": 15.0}, ("zly_czas_nadania", _BLAD_CZAS_NADANIA)),
        ({"nadano_o": float("-inf"), "wazne_s": 15.0}, ("zly_czas_nadania", _BLAD_CZAS_NADANIA)),
        ({"nadano_o": 10**400, "wazne_s": 15.0}, ("zly_czas_nadania", _BLAD_CZAS_NADANIA)),
        ({"nadano_o": -(10**400), "wazne_s": 15.0}, ("zly_czas_nadania", _BLAD_CZAS_NADANIA)),
        # oba pola zle: najpierw czas nadania
        ({"nadano_o": None, "wazne_s": None}, ("zly_czas_nadania", _BLAD_CZAS_NADANIA)),
        # zly limit waznosci: odrzuca go tylko straznik limitu (czas jest poprawny)
        ({"nadano_o": 999.0}, ("zly_limit_waznosci", _BLAD_LIMIT_WAZNOSCI)),
        ({"nadano_o": 999.0, "wazne_s": None}, ("zly_limit_waznosci", _BLAD_LIMIT_WAZNOSCI)),
        ({"nadano_o": 999.0, "wazne_s": "15"}, ("zly_limit_waznosci", _BLAD_LIMIT_WAZNOSCI)),
        ({"nadano_o": 999.0, "wazne_s": [15]}, ("zly_limit_waznosci", _BLAD_LIMIT_WAZNOSCI)),
        # logiczne True to nie limit 1.0: gdyby przeszlo, wiek 0 s zostawilby zapytanie waznym
        ({"nadano_o": _TERAZ, "wazne_s": True}, ("zly_limit_waznosci", _BLAD_LIMIT_WAZNOSCI)),
        ({"nadano_o": 999.0, "wazne_s": float("nan")}, ("zly_limit_waznosci", _BLAD_LIMIT_WAZNOSCI)),
        ({"nadano_o": 999.0, "wazne_s": float("inf")}, ("zly_limit_waznosci", _BLAD_LIMIT_WAZNOSCI)),
        ({"nadano_o": 999.0, "wazne_s": 10**400}, ("zly_limit_waznosci", _BLAD_LIMIT_WAZNOSCI)),
        ({"nadano_o": 999.0, "wazne_s": -(10**400)}, ("zly_limit_waznosci", _BLAD_LIMIT_WAZNOSCI)),
        # limit zerowy i ujemny przy wieku 0 s (gdyby przeszedl, wiek 0 nie przekracza 0)
        ({"nadano_o": _TERAZ, "wazne_s": 0}, ("zly_limit_waznosci", _BLAD_LIMIT_WAZNOSCI)),
        ({"nadano_o": _TERAZ, "wazne_s": 0.0}, ("zly_limit_waznosci", _BLAD_LIMIT_WAZNOSCI)),
        # limit ujemny przy czasie nadania w przyszlosci (wiek -1000 s): gdyby przeszedl,
        # -1000 > -1 jest falszem i zapytanie zostaloby wykonane
        ({"nadano_o": 2000.0, "wazne_s": -1.0}, ("zly_limit_waznosci", _BLAD_LIMIT_WAZNOSCI)),
        ({"nadano_o": 2000.0, "wazne_s": -1}, ("zly_limit_waznosci", _BLAD_LIMIT_WAZNOSCI)),
        # przeterminowane: oba pola poprawne, decyduje tylko wiek
        ({"nadano_o": 984.5, "wazne_s": 15.0}, ("przeterminowane", _BLAD_PRZETERMINOWANE)),
        ({"nadano_o": 0.0, "wazne_s": 15}, ("przeterminowane", _BLAD_PRZETERMINOWANE)),
    ],
    ids=["czas_null", "czas_brak", "czas_napis", "czas_lista", "czas_logiczne_prawda",
         "czas_logiczne_falsz", "czas_nan", "czas_inf", "czas_minus_inf", "czas_ponad_zakres",
         "czas_minus_ponad_zakres", "oba_null",
         "limit_brak", "limit_null", "limit_napis", "limit_lista", "limit_logiczne_prawda",
         "limit_nan", "limit_inf", "limit_ponad_zakres", "limit_minus_ponad_zakres",
         "limit_zero_calkowite", "limit_zero", "limit_ujemny_czas_w_przyszlosci",
         "limit_ujemny_calkowity_czas_w_przyszlosci",
         "wiek_o_pol_sekundy_ponad_limit", "wiek_od_poczatku_epoki"],
)
def test_odmowa_zapytania_ma_dokladny_kod_i_komunikat_zalezne_od_jedynej_wadliwej_rzeczy(
    polecenie, oczekiwana,
):
    assert runner_client._odmowa_zapytania(polecenie, _TERAZ) == oczekiwana


@pytest.mark.parametrize(
    "polecenie",
    [
        {"nadano_o": 985.0, "wazne_s": 15.0},        # wiek rowny limitowi: jeszcze wazne (porownanie `>`)
        {"nadano_o": 985, "wazne_s": 15},            # to samo na liczbach calkowitych
        {"nadano_o": 990.0, "wazne_s": 15.0},        # wiek 10 s
        {"nadano_o": _TERAZ, "wazne_s": 0.001},      # najmniejszy dodatni limit przechodzi
        {"nadano_o": 1005.0, "wazne_s": 15.0},       # czas nadania w przyszlosci (skos zegarow) nie jest wiekiem ponad limit
    ],
    ids=["wiek_rowny_limitowi", "wiek_rowny_limitowi_calkowite", "wiek_w_limicie",
         "limit_dodatni_ulamkowy", "czas_nadania_w_przyszlosci"],
)
def test_zapytanie_w_granicach_waznosci_nie_jest_odrzucane(polecenie):
    assert runner_client._odmowa_zapytania(polecenie, _TERAZ) is None


@pytest.mark.parametrize("nadmiar_s", [0.001, 0.5, 1.0])
def test_zapytanie_starsze_od_limitu_choc_o_ulamek_sekundy_jest_przeterminowane(nadmiar_s):
    polecenie = {"nadano_o": _TERAZ - 15.0 - nadmiar_s, "wazne_s": 15.0}

    assert runner_client._odmowa_zapytania(polecenie, _TERAZ) == (
        "przeterminowane", _BLAD_PRZETERMINOWANE)


async def test_zapytanie_o_wieku_rownym_limitowi_jest_wykonane_a_o_ulamek_starsze_odrzucone(
    monkeypatch,
):
    """Przez prawdziwy `_odpowiedz_na_zapytanie` z zamrozonym zegarem runnera."""
    wywolania = _operacja_liczaca_wywolania(monkeypatch)
    monkeypatch.setattr(runner_client, "time", SimpleNamespace(time=lambda: _TERAZ))

    na_granicy = await _odpowiedzi_runnera_na(
        {"type": "licz", "req_id": "r-gr", "nadano_o": _TERAZ - 15.0, "wazne_s": 15.0})
    ponad = await _odpowiedzi_runnera_na(
        {"type": "licz", "req_id": "r-po", "nadano_o": _TERAZ - 15.5, "wazne_s": 15.0})

    assert na_granicy == [{"req_id": "r-gr", "ok": True, "wynik": "wykonano"}]
    assert ponad == [{"req_id": "r-po", "ok": False, "blad": _BLAD_PRZETERMINOWANE,
                      "kod": "przeterminowane"}]
    assert len(wywolania) == 1


async def test_zapytanie_z_calkowitymi_liczbami_w_czasie_i_limicie_jest_wykonane(monkeypatch):
    """Liczba calkowita to poprawna liczba (JSON nie odroznia 15 od 15.0); odrzuca sie tylko
    wartosci, ktore nie sa liczbami."""
    wywolania = _operacja_liczaca_wywolania(monkeypatch)

    odpowiedzi = await _odpowiedzi_runnera_na(
        {"type": "licz", "req_id": "r-int", "nadano_o": int(time.time()), "wazne_s": 15})

    assert len(wywolania) == 1
    assert odpowiedzi[0]["ok"] is True


async def test_odpowiedz_o_przeterminowaniu_nie_zdradza_tresci_zapytania(monkeypatch):
    _operacja_liczaca_wywolania(monkeypatch)

    odpowiedzi = await _odpowiedzi_runnera_na(
        _swieze({"type": "licz", "req_id": "r-sek", "token": SEKRET}, nadano_o=time.time() - 100))

    assert SEKRET not in json.dumps(odpowiedzi)


# =================================================================================
# Przelot end-to-end: prawdziwa aplikacja kokpitu + prawdziwy runner
# =================================================================================

@pytest.fixture
async def runner_zywy(swiat, monkeypatch):
    """Prawdziwy runner Browarka (B1) polaczony z prawdziwa aplikacja; zatrzymany po tescie."""
    monkeypatch.setattr(settings, "runner_zapytanie_timeout_s", 20.0)
    runner = RunnerKlient("http://kokpit.test", TOKENY[B1],
                          transport=TransportASGIStrumieniujacy(app_mod.app))
    nasluch = asyncio.create_task(runner.listen())
    try:
        await _poczekaj(lambda: B1 in bus.connected_runner_ids(), limit=10)
        yield runner
    finally:
        await asyncio.wait_for(runner.stop(), 10)
        zakonczone, _ = await asyncio.wait({nasluch}, timeout=10)
        if nasluch not in zakonczone:
            nasluch.cancel()
            await asyncio.wait({nasluch}, timeout=5)


async def _zapytaj_zespol(tenant_id: str, typ: str, dane: dict | None = None):
    """Droga produkcyjna: serwis zespolu + `bus.zapytaj` jako wysylka."""
    async with db_session.SessionLocal() as s:
        return await asyncio.wait_for(
            zapytania.zapytaj_runnera_zespolu(
                s, tenant_id, typ, dane if dane is not None else {},
                bus.connected_runner_ids(), bus.zapytaj),
            15)


async def test_ping_przechodzi_w_obie_strony_miedzy_kokpitem_a_prawdziwym_runnerem(
    swiat, runner_zywy,
):
    assert await _zapytaj_zespol(swiat.browarek, "ping") == {"pong": True}
    assert bus._oczekujace == {}


async def test_nieznana_operacja_daje_czytelny_blad_a_runner_dalej_odpowiada_na_kolejne_zapytania(
    swiat, runner_zywy,
):
    with pytest.raises(StanNiemozliwy, match="nieznana operacja: teleport"):
        await _zapytaj_zespol(swiat.browarek, "teleport")

    assert B1 in bus.connected_runner_ids()
    assert await _zapytaj_zespol(swiat.browarek, "ping") == {"pong": True}


async def test_operacja_runnera_ktora_sie_wywraca_nie_ujawnia_kokpitowi_tresci_wyjatku(
    swiat, runner_zywy, monkeypatch, capsys,
):
    async def psuj(dane):
        raise RuntimeError(f"w srodku jest {SEKRET}")

    monkeypatch.setitem(operacje.OPERACJE, "psuj", psuj)

    with pytest.raises(StanNiemozliwy) as blad:
        await _zapytaj_zespol(swiat.browarek, "psuj")

    assert str(blad.value) == "operacja psuj nie powiodla sie (RuntimeError)"
    wyjscie = capsys.readouterr()
    assert SEKRET not in wyjscie.err
    assert SEKRET not in wyjscie.out
    assert await _zapytaj_zespol(swiat.browarek, "ping") == {"pong": True}      # runner dziala


async def test_dane_zapytania_docieraja_do_operacji_runnera_bez_pol_protokolu(
    swiat, runner_zywy, monkeypatch,
):
    async def echo(dane):
        return dane

    monkeypatch.setitem(operacje.OPERACJE, "echo", echo)

    wynik = await _zapytaj_zespol(swiat.browarek, "echo", {"sciezka": "a/b", "n": 2})

    assert wynik == {"sciezka": "a/b", "n": 2}


async def test_zapytanie_do_runnera_ktory_przestal_byc_polaczony_konczy_sie_natychmiast_bledem_offline(
    swiat, runner_zywy,
):
    await asyncio.wait_for(runner_zywy.stop(), 10)
    await _poczekaj(lambda: B1 not in bus.connected_runner_ids(), limit=10)

    start = time.monotonic()
    with pytest.raises(StanNiemozliwy, match="^runner offline"):
        await _zapytaj_zespol(swiat.browarek, "ping")

    assert time.monotonic() - start < 2


async def test_blad_zglaszany_przez_runner_przechodzi_przez_trase_odpowiedzi_do_wolajacego_serwisu(
    swiat, zapytania_w_tle, monkeypatch,
):
    """Serwis + szyna + trasa POST bez prawdziwego runnera: to klient HTTP odgrywa runnera."""
    monkeypatch.setattr(settings, "runner_zapytanie_timeout_s", 10.0)
    _polacz(B1)
    zadanie = asyncio.create_task(_zapytaj_zespol(swiat.browarek, "ping"))
    zapytania_w_tle.zadania.append(zadanie)
    polecenie = await asyncio.wait_for(bus.runner_queue(B1).get(), 5)

    r = await _odpowiedz_http(swiat, B1, {"req_id": polecenie["req_id"], "ok": False,
                                          "blad": "runner nie ma uprawnien", "kod": "blad_operacji"})

    assert r.status_code == 204
    with pytest.raises(StanNiemozliwy) as blad:
        await asyncio.wait_for(zadanie, 5)
    assert str(blad.value) == "runner nie ma uprawnien"


async def _utworz_run(tenant_id: str, runner_id: str) -> str:
    async with db_session.SessionLocal() as s:
        run = Run(tenant_id=tenant_id, runner_id=runner_id, status=RunStatus.running)
        s.add(run)
        await s.commit()
        return run.id


async def _rodzaje_zdarzen(run_id: str) -> list[tuple[str, str]]:
    async with db_session.SessionLocal() as s:
        wiersze = (await s.execute(
            select(Event.kind, Event.text).where(Event.run_id == run_id).order_by(Event.seq))).all()
    return [(k, t) for k, t in wiersze]


async def test_wolne_zapytanie_nie_blokuje_kroku_runu_wyslanego_tym_samym_strumieniem(
    swiat, runner_zywy, monkeypatch,
):
    ruszyla, zwolnij = asyncio.Event(), asyncio.Event()

    async def wolna(dane):
        ruszyla.set()
        await zwolnij.wait()
        return {"skonczona": True}

    monkeypatch.setitem(operacje.OPERACJE, "wolna", wolna)
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {
        "drukuj_x": (sys.executable, "-c", "print('x')")})
    run_id = await _utworz_run(swiat.browarek, B1)
    zapytanie = asyncio.create_task(_zapytaj_zespol(swiat.browarek, "wolna"))
    try:
        await asyncio.wait_for(ruszyla.wait(), 10)          # runner przyjal zapytanie i je wykonuje

        await bus.send_to_runner(B1, {"type": "run_step", "run_id": run_id, "step_id": "s0",
                                      "operacja": "drukuj_x"})

        async def krok_skonczony() -> bool:
            return ("step_finished", "kod wyjscia 0") in await _rodzaje_zdarzen(run_id)

        await _poczekaj(krok_skonczony, limit=10)
        assert not zapytanie.done()                         # krok sie skonczyl, zapytanie wciaz trwa
        assert ("output", "x") in await _rodzaje_zdarzen(run_id)

        zwolnij.set()
        assert await asyncio.wait_for(zapytanie, 10) == {"skonczona": True}
    finally:
        zwolnij.set()
        if not zapytanie.done():
            zapytanie.cancel()
            await asyncio.wait({zapytanie}, timeout=5)


async def test_zatrzymanie_runnera_anuluje_trwajace_zapytanie_i_nie_zostawia_zadan(
    swiat, runner_zywy, monkeypatch,
):
    ruszyla, anulowana = asyncio.Event(), asyncio.Event()

    async def wolna(dane):
        ruszyla.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            anulowana.set()
            raise

    monkeypatch.setitem(operacje.OPERACJE, "wolna", wolna)
    zapytanie = asyncio.create_task(_zapytaj_zespol(swiat.browarek, "wolna"))
    try:
        await asyncio.wait_for(ruszyla.wait(), 10)

        await asyncio.wait_for(runner_zywy.stop(), 10)

        await asyncio.wait_for(anulowana.wait(), 5)
        assert runner_zywy._zapytania == set()
    finally:
        if not zapytanie.done():
            zapytanie.cancel()
        await asyncio.wait({zapytanie}, timeout=5)


class _StrumienZZapytaniemKonczacySiePoStarcieOperacji(httpx.AsyncByteStream):
    """Kokpit wysyla `polaczono` i zapytanie z `req_id`, a gdy runner zacznie je wykonywac,
    zamyka strumien polecen (bez `stop()` po stronie runnera)."""

    def __init__(self, polecenie: dict, ruszyla: asyncio.Event) -> None:
        self._polecenie = polecenie
        self._ruszyla = ruszyla

    async def __aiter__(self):
        yield b'data: {"type": "polaczono"}\n\n'
        yield b"data: " + json.dumps(self._polecenie).encode() + b"\n\n"
        await asyncio.wait_for(self._ruszyla.wait(), 10)


async def test_zamkniecie_strumienia_przez_kokpit_anuluje_trwajace_zapytanie_przed_ponownym_polaczeniem(
    monkeypatch,
):
    """Sprzatanie zapytan ma dwie drogi: `stop()` i koniec strumienia polecen. Tu dziala tylko
    ta druga: stan jest odczytany w `odczekaj`, czyli po wyjsciu ze strumienia, a przed
    ponownym polaczeniem i przed jakimkolwiek `stop()`."""
    ruszyla, anulowana = asyncio.Event(), asyncio.Event()

    async def wolna(dane):
        ruszyla.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            anulowana.set()
            raise

    monkeypatch.setitem(operacje.OPERACJE, "wolna", wolna)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/runners/register":
            return httpx.Response(200, json={"runner_id": "r-1", "heartbeat_co_s": 30})
        if request.url.path == "/api/runners/stream":
            return httpx.Response(200, stream=_StrumienZZapytaniemKonczacySiePoStarcieOperacji(
                _swieze({"type": "wolna", "req_id": "zapytanie-1"}), ruszyla))
        return httpx.Response(204)

    stan: dict = {}

    async def odczekaj(sekundy: float) -> None:
        # wolane po wyjsciu z `_nasluchuj`; `stop()` jeszcze nie bylo
        stan["zapytania"] = len(runner._zapytania)
        stan["anulowana"] = anulowana.is_set()
        stan["zatrzymany"] = runner._zatrzymany.is_set()
        await asyncio.wait_for(runner.stop(), 10)

    runner = RunnerKlient("http://kokpit.test", SEKRET, odczekaj=odczekaj,
                          transport=httpx.MockTransport(handler))
    try:
        await asyncio.wait_for(runner.listen(), 10)
    finally:
        await asyncio.wait_for(runner.stop(), 10)

    assert stan == {"zapytania": 0, "anulowana": True, "zatrzymany": False}


# --- HAR-55: limit odpowiedzi kokpitu miesci kazdy plik przepuszczony przez runnera ---------

async def test_plik_o_rozmiarze_domyslnego_limitu_odczytu_z_samych_znakow_sterujacych_miesci_sie_w_odpowiedzi(
    swiat, runner_zywy, tmp_path, monkeypatch,
):
    """Najgorszy plik dla JSON: kazdy bajt to znak sterujacy (poprawny UTF-8, bez NUL), a
    `json.dumps` zamienia go w `\\u0001` (6 bajtow). Plik rowny domyslnemu limitowi odczytu
    runnera (512 KB) musi przejsc przez DOMYSLNY limit odpowiedzi kokpitu."""
    projekt = tmp_path / "projekt"
    projekt.mkdir()
    plik = projekt / "sterujace.txt"
    plik.write_bytes(b"\x01" * (512 * 1024))
    konfig = tmp_path / "runner.json"
    konfig.write_text(json.dumps({"projekty": [str(projekt)]}), encoding="utf-8")
    dom = tmp_path / "dom"
    dom.mkdir()
    monkeypatch.setenv("COCKPIT_RUNNER_CONFIG", str(konfig))
    monkeypatch.setenv("HOME", str(dom))
    monkeypatch.delenv("COCKPIT_MAX_READ_KB", raising=False)
    monkeypatch.delenv("COCKPIT_RUNNER_ODPOWIEDZ_MAX_KB", raising=False)
    monkeypatch.setattr(settings, "runner_odpowiedz_max_kb",
                        Settings(_env_file=None).runner_odpowiedz_max_kb)

    wynik = await _zapytaj_zespol(swiat.browarek, "read_file", {"sciezka": str(plik.resolve())})

    assert wynik["rozmiar"] == 512 * 1024
    assert wynik["tresc"] == "\x01" * (512 * 1024)


# --- HAR-201: strumien polecen nie wysyla runnerowi zapytania, na ktore kokpit juz nie czeka ---

async def _odbierz_z_prawdziwego_strumienia(runner_id: str, ile: int, limit_s: float = 3.0) -> list[dict]:
    """Otwiera prawdziwy `GET /api/runners/stream` prawdziwej aplikacji i zwraca polecenia, ktore
    runner by dostal (pierwsze `ile`; po limicie czasu te, ktore doszly)."""
    odebrane: list[dict] = []

    async def czytaj(klient: AsyncClient) -> None:
        async with klient.stream("GET", "/api/runners/stream", headers=_naglowek(runner_id)) as r:
            assert r.status_code == 200
            async for linia in r.aiter_lines():
                if linia.startswith("data:"):
                    odebrane.append(json.loads(linia.removeprefix("data:").strip()))
                    if len(odebrane) >= ile:
                        return

    async with AsyncClient(transport=TransportASGIStrumieniujacy(app_mod.app),
                           base_url="http://test") as klient:
        try:
            await asyncio.wait_for(czytaj(klient), limit_s)
        except TimeoutError:
            pass
    return odebrane


async def _zapytanie_po_ktorym_kokpit_juz_nie_czeka() -> None:
    """Polecenie zapytania trafia do kolejki runnera (brak otwartego strumienia), a kokpit
    konczy czekanie timeoutem: w kolejce zostaje zapytanie, ktorego nikt nie oczekuje."""
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(bus.zapytaj(B1, "ping", {}, 0.05), 5)
    assert bus._oczekujace == {}
    assert bus.runner_queue(B1).qsize() == 1


async def test_zapytanie_po_timeoucie_kokpitu_nie_trafia_do_strumienia_runnera(swiat):
    run_id = await _utworz_run(swiat.browarek, B1)
    await _zapytanie_po_ktorym_kokpit_juz_nie_czeka()
    # znacznik: polecenie bez req_id za zapytaniem; gdy dojdzie, zapytanie juz byloby wyslane
    await bus.send_to_runner(B1, {"type": "run_workflow", "run_id": run_id})

    odebrane = await _odbierz_z_prawdziwego_strumienia(B1, ile=2)

    assert [p["type"] for p in odebrane] == ["polaczono", "run_workflow"]
    assert not any("req_id" in p for p in odebrane)


async def test_zapytanie_oczekujace_na_odpowiedz_trafia_do_strumienia_runnera(
    swiat, zapytania_w_tle,
):
    zadanie = asyncio.create_task(bus.zapytaj(B1, "ping", {"x": 1}, 10.0))
    zapytania_w_tle.zadania.append(zadanie)
    await _poczekaj(lambda: len(bus._oczekujace) == 1)
    req_id = next(iter(bus._oczekujace))

    odebrane = await _odbierz_z_prawdziwego_strumienia(B1, ile=2)

    assert [p["type"] for p in odebrane] == ["polaczono", "ping"]
    assert odebrane[1]["req_id"] == req_id
    assert odebrane[1]["x"] == 1
    assert odebrane[1]["wazne_s"] == 10.0 and isinstance(odebrane[1]["nadano_o"], float)


async def test_przeterminowane_zapytanie_jest_pominiete_a_oczekujace_za_nim_dochodzi(
    swiat, zapytania_w_tle,
):
    await _zapytanie_po_ktorym_kokpit_juz_nie_czeka()
    zadanie = asyncio.create_task(bus.zapytaj(B1, "ping", {}, 10.0))
    zapytania_w_tle.zadania.append(zadanie)
    await _poczekaj(lambda: len(bus._oczekujace) == 1)
    req_id = next(iter(bus._oczekujace))

    odebrane = await _odbierz_z_prawdziwego_strumienia(B1, ile=2)

    assert [p.get("req_id") for p in odebrane] == [None, req_id]       # "polaczono", potem swieze


@pytest.mark.parametrize("typ", ["run_workflow", "cancel_run", "resume"])
async def test_polecenie_bez_req_id_dochodzi_do_runnera_mimo_filtra_przeterminowanych(swiat, typ):
    run_id = await _utworz_run(swiat.browarek, B1)
    await bus.send_to_runner(B1, {"type": typ, "run_id": run_id})

    odebrane = await _odbierz_z_prawdziwego_strumienia(B1, ile=2)

    assert [p["type"] for p in odebrane] == ["polaczono", typ]


async def test_run_step_w_strumieniu_dochodzi_do_runnera_gdy_run_trwa(swiat):
    run_id = await _utworz_run(swiat.browarek, B1)
    await bus.send_to_runner(B1, {"type": "run_step", "run_id": run_id, "step_id": "s0",
                                  "operacja": "x"})

    odebrane = await _odbierz_z_prawdziwego_strumienia(B1, ile=2)

    assert [p["type"] for p in odebrane] == ["polaczono", "run_step"]
