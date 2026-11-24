"""HAR-200: nowy strumien runnera zamyka poprzednie, a polecenie trafia tylko do najnowszego.

Test wola prawdziwy punkt wejscia `runner_stream` (jak tests/test_szkielet.py) i czyta
skutki przez publiczne API szyny: `send_to_runner`, `runner_queue`, `connected_runner_ids`,
`otworz_strumien_runnera`, `nastepne_polecenie`.
Kazde czekanie na zdarzenie w tescie (odczyt, zamkniecie strumienia, sprzatanie zadan, brama
sprawdzenia aktualnosci, grupa zadan anyio) ma limit czasu LIMIT_S. Bez limitu zostaja tylko
krotkie oddania petli `asyncio.sleep(0)` i `asyncio.sleep(0.05)`, ktore nie czekaja na zdarzenie.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import anyio
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.api.app import runner_stream
from cockpit.db import session as db_session
from cockpit.services import runs

LIMIT_S = 2
KONIEC = "koniec-strumienia"


@pytest.fixture
def brama(monkeypatch):
    """Sprawdzenie aktualnosci polecenia (baza) zastapione bramka: polecenie z `blokuj`
    czeka, az test ja otworzy - tak test zatrzymuje strumien miedzy pobraniem a wyslaniem.
    Polecenie z `nieaktualne` po otwarciu bramki jest uznane za nieaktualne."""
    otwarta = asyncio.Event()

    async def aktualne(s, polecenie, runner_id, tenant_id) -> bool:
        if polecenie.get("blokuj"):
            await asyncio.wait_for(otwarta.wait(), timeout=LIMIT_S)
        return not polecenie.get("nieaktualne")

    # SessionLocal tylko tworzy sesje (bez polaczenia): wystarczy dowolny silnik
    silnik = create_async_engine("sqlite+aiosqlite://")
    monkeypatch.setattr(db_session, "SessionLocal", async_sessionmaker(silnik))
    monkeypatch.setattr(runs, "polecenie_aktualne", aktualne)
    return otwarta


@pytest.fixture
def runner_id():
    rid = f"runner-{uuid.uuid4().hex}"
    yield rid
    _sprzatnij(rid)


@pytest.fixture
def inny_runner_id():
    rid = f"runner-{uuid.uuid4().hex}"
    yield rid
    _sprzatnij(rid)


def _sprzatnij(rid: str) -> None:
    while rid in bus.connected_runner_ids():
        bus.mark_runner_disconnected(rid)
    bus._rozlaczone.pop(rid, None)
    bus._runner_queues.pop(rid, None)


def _cialo_strumienia(rid: str):
    """Prawdziwy `runner_stream`; zwraca cialo odpowiedzi (generator) jeszcze nieczytane."""
    async def nie_rozlaczony() -> bool:
        return False

    return asyncio.ensure_future(runner_stream(
        request=SimpleNamespace(is_disconnected=nie_rozlaczony),
        r=SimpleNamespace(id=rid, tenant_id="zespol-1"), s=None))


async def _otworz(rid: str):
    """Otwiera strumien i czyta sygnal `polaczono` (to on rejestruje strumien)."""
    odpowiedz = await asyncio.wait_for(_cialo_strumienia(rid), timeout=LIMIT_S)
    strumien = odpowiedz.body_iterator
    pierwsze = await asyncio.wait_for(strumien.__anext__(), timeout=LIMIT_S)
    assert json.loads(pierwsze["data"]) == {"type": "polaczono"}
    return strumien


async def _nastepne(strumien):
    try:
        zdarzenie = await strumien.__anext__()
    except StopAsyncIteration:
        return KONIEC
    return json.loads(zdarzenie["data"])


def _czeka_na_polecenie(strumien) -> asyncio.Task:
    return asyncio.ensure_future(_nastepne(strumien))


async def _wynik(zadanie: asyncio.Task):
    gotowe, _ = await asyncio.wait({zadanie}, timeout=LIMIT_S)
    assert gotowe, "strumien nie zareagowal w limicie czasu"
    return zadanie.result()


async def _aclose(strumien):
    await asyncio.wait_for(strumien.aclose(), timeout=LIMIT_S)


async def _zamknij(*strumienie, zadania=()):
    for z in zadania:
        z.cancel()
    if zadania:
        await asyncio.wait_for(
            asyncio.gather(*zadania, return_exceptions=True), timeout=LIMIT_S)
    for s in strumienie:
        await _aclose(s)


def _run_step(nr: int, **dodatkowe) -> dict:
    return {"type": "run_step", "run_id": f"run-{nr}", "step_id": "s0", **dodatkowe}


def _zawartosc_kolejki(rid: str) -> list[dict]:
    kolejka = bus.runner_queue(rid)
    wynik = []
    while not kolejka.empty():
        wynik.append(kolejka.get_nowait())
    return wynik


async def test_stary_strumien_wiszacy_na_oczekiwaniu_konczy_sie_gdy_runner_otworzy_nowy(
    brama, runner_id,
):
    stary = await _otworz(runner_id)
    czeka = _czeka_na_polecenie(stary)
    nowy = None
    try:
        await asyncio.sleep(0)  # stary dochodzi do oczekiwania na polecenie
        nowy = await _otworz(runner_id)
        assert await _wynik(czeka) == KONIEC
    finally:
        await _zamknij(stary, *([nowy] if nowy else []), zadania=[czeka])


async def test_polecenie_wyslane_po_otwarciu_nowego_strumienia_odbiera_tylko_nowy(
    brama, runner_id,
):
    stary = await _otworz(runner_id)
    czeka_stary = _czeka_na_polecenie(stary)
    await asyncio.sleep(0)
    nowy = await _otworz(runner_id)
    czeka_nowy = _czeka_na_polecenie(nowy)
    try:
        await asyncio.sleep(0)
        await bus.send_to_runner(runner_id, _run_step(1))
        await bus.send_to_runner(runner_id, _run_step(2))

        assert await _wynik(czeka_nowy) == _run_step(1)
        assert await _wynik(_czeka_na_polecenie(nowy)) == _run_step(2)
        assert await _wynik(czeka_stary) == KONIEC
    finally:
        await _zamknij(stary, nowy, zadania=[czeka_stary, czeka_nowy])


async def test_polecenie_pobrane_przez_zamkniety_strumien_wraca_na_poczatek_kolejki_nowego(
    brama, runner_id,
):
    stary = await _otworz(runner_id)
    await bus.send_to_runner(runner_id, _run_step(1, blokuj=True))
    await bus.send_to_runner(runner_id, _run_step(2))
    pobiera = _czeka_na_polecenie(stary)   # pobiera polecenie 1 i staje przed wyslaniem
    nowy = None
    try:
        await asyncio.sleep(0.05)
        assert not pobiera.done()
        assert bus.runner_queue(runner_id).qsize() == 1    # drugie polecenie czeka w kolejce
        nowy = await _otworz(runner_id)
        brama.set()                                         # stary konczy sprawdzanie aktualnosci
        assert await _wynik(pobiera) == KONIEC              # i nie wysyla polecenia, ktore wzial

        assert await _wynik(_czeka_na_polecenie(nowy)) == _run_step(1, blokuj=True)
        assert await _wynik(_czeka_na_polecenie(nowy)) == _run_step(2)
    finally:
        await _zamknij(stary, *([nowy] if nowy else []), zadania=[pobiera])


async def test_polecenie_wyslane_miedzy_strumieniami_trafia_do_nowego(brama, runner_id):
    stary = await _otworz(runner_id)
    await _aclose(stary)
    await bus.send_to_runner(runner_id, _run_step(1))
    nowy = await _otworz(runner_id)
    try:
        assert await _wynik(_czeka_na_polecenie(nowy)) == _run_step(1)
    finally:
        await _zamknij(nowy)


async def test_runner_zostaje_dostepny_gdy_stary_strumien_konczy_sie_po_otwarciu_nowego(
    brama, runner_id,
):
    stary = await _otworz(runner_id)
    czeka = _czeka_na_polecenie(stary)
    await asyncio.sleep(0)
    nowy = await _otworz(runner_id)
    try:
        assert await _wynik(czeka) == KONIEC           # stary skonczyl sie po rejestracji nowego
        assert runner_id in bus.connected_runner_ids()
        assert runner_id not in bus.disconnected_runners()
    finally:
        await _zamknij(stary, nowy, zadania=[czeka])
    assert runner_id not in bus.connected_runner_ids()


async def test_zamkniecie_starego_strumienia_po_nowym_nie_wylacza_runnera_ani_nowego(
    brama, runner_id,
):
    stary = await _otworz(runner_id)
    nowy = await _otworz(runner_id)
    try:
        await _aclose(stary)                           # klient zrywa stare polaczenie z opoznieniem
        assert runner_id in bus.connected_runner_ids()
        await bus.send_to_runner(runner_id, _run_step(1))
        assert await _wynik(_czeka_na_polecenie(nowy)) == _run_step(1)
    finally:
        await _zamknij(nowy)
    assert runner_id not in bus.connected_runner_ids()


async def test_runner_jest_niedostepny_dopiero_gdy_zamkniety_zostal_ostatni_strumien(
    brama, runner_id,
):
    stary = await _otworz(runner_id)
    nowy = await _otworz(runner_id)
    await _aclose(nowy)
    assert runner_id not in bus.connected_runner_ids()
    await _aclose(stary)
    assert runner_id not in bus.connected_runner_ids()
    assert runner_id in bus.disconnected_runners()


async def test_strumien_po_zamknieciu_wszystkich_poprzednich_odbiera_polecenia(brama, runner_id):
    pierwszy = await _otworz(runner_id)
    await _aclose(pierwszy)
    drugi = await _otworz(runner_id)
    await _aclose(drugi)
    trzeci = await _otworz(runner_id)
    try:
        await bus.send_to_runner(runner_id, _run_step(1))
        assert await _wynik(_czeka_na_polecenie(trzeci)) == _run_step(1)
        assert runner_id in bus.connected_runner_ids()
    finally:
        await _zamknij(trzeci)


async def test_nowy_strumien_jednego_runnera_nie_zamyka_strumienia_innego_runnera(
    brama, runner_id, inny_runner_id,
):
    cudzy = await _otworz(inny_runner_id)
    czeka_cudzy = _czeka_na_polecenie(cudzy)
    await asyncio.sleep(0)
    nowy = await _otworz(runner_id)
    try:
        await asyncio.sleep(0.05)
        assert not czeka_cudzy.done()
        assert inny_runner_id in bus.connected_runner_ids()
        await bus.send_to_runner(inny_runner_id, _run_step(7))
        assert await _wynik(czeka_cudzy) == _run_step(7)
    finally:
        await _zamknij(cudzy, nowy, zadania=[czeka_cudzy])


# --- zwrot polecenia: kolejnosc, rozlaczenie z zakresu anyio, szyna wprost ---

async def test_trzy_polecenia_po_zwrocie_pierwszego_przychodza_w_kolejnosci_wyslania(
    runner_id,
):
    stary = bus.otworz_strumien_runnera(runner_id)
    for nr in (2, 3):
        await bus.send_to_runner(runner_id, _run_step(nr))
    bus.zwroc_polecenie(runner_id, _run_step(1))        # pobrane, niewyslane - wraca na poczatek
    nowy = bus.otworz_strumien_runnera(runner_id)
    assert stary is not nowy

    odebrane = [
        await asyncio.wait_for(bus.nastepne_polecenie(nowy), timeout=LIMIT_S) for _ in range(3)
    ]

    assert odebrane == [_run_step(1), _run_step(2), _run_step(3)]
    bus.zamknij_strumien(nowy)


async def _klient_rozlaczony_zakresem(rid: str, po_polaczeniu) -> None:
    """Tak rozlacza klienta sse_starlette: zakres anyio ponawia anulowanie przy kazdym `await`.

    Strumien czyta zadanie w grupie; po sygnale `polaczono` i chwili oczekiwania na polecenie
    wykonuje `po_polaczeniu`, a zaraz potem anuluje zakres."""
    polaczono = asyncio.Event()
    odpowiedz = await asyncio.wait_for(_cialo_strumienia(rid), timeout=LIMIT_S)

    async def czytaj():
        async for _ in odpowiedz.body_iterator:
            polaczono.set()

    with anyio.fail_after(LIMIT_S):
        async with anyio.create_task_group() as zadania:
            zadania.start_soon(czytaj)
            await asyncio.wait_for(polaczono.wait(), timeout=LIMIT_S)
            await asyncio.sleep(0.05)                    # strumien czeka na polecenie
            await po_polaczeniu()
            zadania.cancel_scope.cancel()


async def test_rozlaczenie_klienta_tuz_po_wyslaniu_polecenia_nie_gubi_go_w_kolejce(
    brama, runner_id,
):
    async def wyslij():
        await bus.send_to_runner(runner_id, _run_step(1))

    await _klient_rozlaczony_zakresem(runner_id, wyslij)

    assert _zawartosc_kolejki(runner_id) == [_run_step(1)]
    assert runner_id not in bus.connected_runner_ids()


async def test_rozlaczenie_bezczynnego_klienta_nie_zostawia_odbiorcy_polykajacego_polecenia(
    brama, runner_id,
):
    async def nic():
        return None

    await _klient_rozlaczony_zakresem(runner_id, nic)
    await bus.send_to_runner(runner_id, _run_step(1))   # po rozlaczeniu, przy pustej kolejce
    await asyncio.sleep(0.05)

    assert _zawartosc_kolejki(runner_id) == [_run_step(1)]


async def test_polecenie_pobrane_przez_strumien_zamkniety_przed_pobraniem_stoi_na_poczatku_kolejki(
    runner_id,
):
    stary = bus.otworz_strumien_runnera(runner_id)
    await bus.send_to_runner(runner_id, _run_step(1))
    await bus.send_to_runner(runner_id, _run_step(2))
    bus.otworz_strumien_runnera(runner_id)               # zamyka `stary` przed pobraniem

    wynik = await asyncio.wait_for(bus.nastepne_polecenie(stary), timeout=LIMIT_S)

    assert wynik is None
    assert _zawartosc_kolejki(runner_id) == [_run_step(1), _run_step(2)]


async def test_anulowane_oczekiwanie_zwraca_pobrane_polecenie_na_poczatek_kolejki(runner_id):
    strumien = bus.otworz_strumien_runnera(runner_id)    # strumien nie jest zamkniety
    czeka = asyncio.ensure_future(bus.nastepne_polecenie(strumien))
    await asyncio.sleep(0)
    await bus.send_to_runner(runner_id, _run_step(1))
    await bus.send_to_runner(runner_id, _run_step(2))
    czeka.cancel()                                       # polecenie 1 pobrane w tej samej chwili

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(czeka, timeout=LIMIT_S)

    assert _zawartosc_kolejki(runner_id) == [_run_step(1), _run_step(2)]


async def test_polecenie_odebrane_przez_otwarty_strumien_nie_wraca_do_kolejki(runner_id):
    strumien = bus.otworz_strumien_runnera(runner_id)
    await bus.send_to_runner(runner_id, _run_step(1))

    wynik = await asyncio.wait_for(bus.nastepne_polecenie(strumien), timeout=LIMIT_S)

    assert wynik == _run_step(1)
    assert _zawartosc_kolejki(runner_id) == []


async def test_oczekiwanie_na_polecenie_nie_zostawia_po_sobie_dzialajacych_zadan(runner_id):
    strumien = bus.otworz_strumien_runnera(runner_id)    # otwarty, nie zamkniety
    await bus.send_to_runner(runner_id, _run_step(1))
    przed = asyncio.all_tasks()

    wynik = await asyncio.wait_for(bus.nastepne_polecenie(strumien), timeout=LIMIT_S)
    await asyncio.sleep(0.05)                            # anulowane zadania koncza sie

    assert wynik == _run_step(1)
    zostalo = {z for z in asyncio.all_tasks() - przed if not z.done()}
    assert zostalo == set()


async def test_polecenia_wyslane_zaraz_po_otwarciu_nowego_strumienia_ida_do_niego_po_kolei(
    runner_id,
):
    """Miedzy otwarciem nowego strumienia a wysylkami nie ma oddania petli (zadnego `await`
    na zdarzeniu), wiec stary strumien nie zdazy sam wyjsc z oczekiwania. Bez synchronicznego
    anulowania jego `get` polecenie 1 budzi stary strumien, nowy bierze 2 i dostaje 2 przed 1."""
    stary = bus.otworz_strumien_runnera(runner_id)
    czeka_stary = asyncio.ensure_future(bus.nastepne_polecenie(stary))
    await asyncio.sleep(0.05)                            # stary czeka w kolejce na polecenie
    nowy = bus.otworz_strumien_runnera(runner_id)
    czeka_nowy = asyncio.ensure_future(bus.nastepne_polecenie(nowy))   # bez oddania petli
    await bus.send_to_runner(runner_id, _run_step(1))    # `put` na kolejce bez limitu nie czeka
    await bus.send_to_runner(runner_id, _run_step(2))

    assert await asyncio.wait_for(czeka_nowy, timeout=LIMIT_S) == _run_step(1)
    assert await asyncio.wait_for(bus.nastepne_polecenie(nowy), timeout=LIMIT_S) == _run_step(2)
    assert await asyncio.wait_for(czeka_stary, timeout=LIMIT_S) is None
    assert _zawartosc_kolejki(runner_id) == []


# --- wyscig otwarcia nowego strumienia: kazde polecenie dokladnie raz ---

async def test_polecenie_wyslane_tuz_przed_otwarciem_nowego_strumienia_przychodzi_dokladnie_raz(
    brama, runner_id,
):
    stary = await _otworz(runner_id)
    czeka_stary = _czeka_na_polecenie(stary)
    await asyncio.sleep(0)
    await bus.send_to_runner(runner_id, _run_step(1))    # stary ma je pobrac, gdy nowy sie otwiera
    nowy = await _otworz(runner_id)
    zadania = [czeka_stary]
    try:
        assert await _wynik(czeka_stary) == KONIEC
        await bus.send_to_runner(runner_id, _run_step(2))

        assert await _wynik(_czeka_na_polecenie(nowy)) == _run_step(1)
        assert await _wynik(_czeka_na_polecenie(nowy)) == _run_step(2)
        trzecie = _czeka_na_polecenie(nowy)
        zadania.append(trzecie)
        await asyncio.sleep(0.05)
        assert not trzecie.done()                        # zadnego duplikatu
        assert bus.runner_queue(runner_id).qsize() == 0
    finally:
        await _zamknij(stary, nowy, zadania=zadania)


async def test_polecenie_wyslane_do_nowego_strumienia_i_zamknieciu_go_nie_wraca_do_kolejki(
    brama, runner_id,
):
    strumien = await _otworz(runner_id)
    await bus.send_to_runner(runner_id, _run_step(1))
    assert await _wynik(_czeka_na_polecenie(strumien)) == _run_step(1)

    await _aclose(strumien)

    assert _zawartosc_kolejki(runner_id) == []


async def test_polecenia_za_nieaktualnym_trafiaja_do_nowego_strumienia_po_kolei(
    brama, runner_id,
):
    stary = await _otworz(runner_id)
    await bus.send_to_runner(runner_id, _run_step(1, blokuj=True, nieaktualne=True))
    await bus.send_to_runner(runner_id, _run_step(2))
    await bus.send_to_runner(runner_id, _run_step(3))
    pobiera = _czeka_na_polecenie(stary)    # pobiera polecenie 1 i staje na sprawdzeniu
    nowy = None
    try:
        await asyncio.sleep(0.05)
        nowy = await _otworz(runner_id)
        brama.set()
        czeka_nowy = _czeka_na_polecenie(nowy)           # odczyt zaraz po otwarciu bramki

        assert await _wynik(czeka_nowy) == _run_step(2)
        assert await _wynik(_czeka_na_polecenie(nowy)) == _run_step(3)
        assert await _wynik(pobiera) == KONIEC
        assert _zawartosc_kolejki(runner_id) == []
    finally:
        await _zamknij(stary, *([nowy] if nowy else []), zadania=[pobiera])


async def test_polecenie_nieaktualne_sprawdzane_w_chwili_otwarcia_nowego_strumienia_ginie(
    brama, runner_id,
):
    stary = await _otworz(runner_id)
    await bus.send_to_runner(runner_id, _run_step(1, blokuj=True, nieaktualne=True))
    pobiera = _czeka_na_polecenie(stary)    # pobiera polecenie i staje na sprawdzeniu aktualnosci
    nowy = None
    try:
        await asyncio.sleep(0.05)
        assert not pobiera.done()
        nowy = await _otworz(runner_id)
        brama.set()                         # sprawdzenie: polecenie nieaktualne

        assert await _wynik(pobiera) == KONIEC
        assert _zawartosc_kolejki(runner_id) == []      # nieaktualne nie wraca do kolejki
    finally:
        await _zamknij(stary, *([nowy] if nowy else []), zadania=[pobiera])
