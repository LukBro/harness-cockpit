"""HAR-45: bufor zdarzeń runnera na dysku i ponowne wysyłanie po powrocie kokpitu.

Zdarzenie jest dopisywane do bufora PRZED wysłaniem i usuwane dopiero po 2xx. Skutek dla
kokpitu czytamy z bazy (prawdziwa aplikacja, dwa zespoły), a skutek dla bufora z plików
w katalogu stanu (`COCKPIT_STATE_DIR` ustawia conftest na katalog testu). Odczekanie jest
wstrzykiwane: żaden test nie śpi realnie, a każdy test z `listen()` kończy się `stop()`
i ma twardy limit czasu.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, Runner, RunStatus, Tenant
from runner.bufor import (
    ODCZEKANIE_MAX_S,
    BladBufora,
    Bufor,
    katalog_stanu,
    odczekanie,
)
from runner.client import Runner as RunnerKlienta
from runner.client import TokenOdrzucony

KORZEN = Path(__file__).resolve().parent.parent
BAZA = "http://kokpit.test"
TOKEN = "token-wlasciciela-bufora-7731"
POLACZONO = b'data: {"type": "polaczono"}\n\n'
SSE = {"content-type": "text/event-stream"}


@contextmanager
def _umask(wartosc: int):
    stara = os.umask(wartosc)
    try:
        yield
    finally:
        os.umask(stara)


def _zd(run_id: str = "run-1", event_id: str | None = None, **pola) -> dict:
    return {"run_id": run_id, "event_id": event_id or str(uuid.uuid4()),
            "step_id": "s0", "kind": "output", "text": "x", **pola}


def _prawa(sciezka: Path) -> int:
    return stat.S_IMODE(sciezka.stat().st_mode)


# --- czas odczekania ------------------------------------------------------------

@pytest.mark.parametrize(
    ("proba", "oczekiwane"),
    [(0, 1.0), (1, 2.0), (2, 4.0), (3, 8.0), (4, 16.0), (5, 30.0), (6, 30.0), (50, 30.0)],
)
def test_odczekanie_przed_ponowieniem_podwaja_sie_i_nie_przekracza_trzydziestu_sekund(
    proba, oczekiwane,
):
    assert odczekanie(proba) == oczekiwane


def test_najdluzsze_odczekanie_przed_ponowieniem_to_trzydziesci_sekund():
    assert ODCZEKANIE_MAX_S == 30.0


# --- katalog stanu i prawa plików -----------------------------------------------

def test_katalog_stanu_to_ten_ze_zmiennej_srodowiskowej_a_bufor_lezy_w_jego_podkatalogu(
    monkeypatch, tmp_path,
):
    inny = tmp_path / "moj-stan"
    monkeypatch.setenv("COCKPIT_STATE_DIR", str(inny))

    assert katalog_stanu() == inny
    assert Bufor().katalog == inny / "bufor"


@pytest.mark.parametrize("zmienna", ["brak", "pusta"])
def test_bez_zmiennej_srodowiskowej_bufor_lezy_w_katalogu_stanu_uzytkownika_poza_repozytorium(
    monkeypatch, tmp_path, zmienna,
):
    dom = tmp_path / "dom"
    dom.mkdir()
    monkeypatch.setenv("HOME", str(dom))
    if zmienna == "brak":
        monkeypatch.delenv("COCKPIT_STATE_DIR")
    else:
        monkeypatch.setenv("COCKPIT_STATE_DIR", "")

    assert katalog_stanu() == dom / ".local" / "state" / "cockpit"
    bufor = Bufor()
    assert bufor.katalog == dom / ".local" / "state" / "cockpit" / "bufor"
    bufor.dopisz(_zd("run-1"))
    plik = bufor.katalog / "run-1.ndjson"
    assert plik.is_file()
    assert not plik.resolve().is_relative_to(KORZEN.resolve())


def test_plik_bufora_ma_prawa_tylko_dla_wlasciciela_a_jego_katalog_jest_prywatny_niezaleznie_od_umask(
    tmp_path,
):
    katalog = tmp_path / "stan" / "bufor"       # katalog nadrzedny tez jeszcze nie istnieje
    with _umask(0o000):
        bufor = Bufor(katalog)
        bufor.dopisz(_zd("run-1"))

    assert _prawa(katalog) == 0o700
    assert _prawa(katalog / "run-1.ndjson") == 0o600


def test_plik_bufora_przepisany_po_potwierdzeniu_nadal_ma_prawa_tylko_dla_wlasciciela(tmp_path):
    bufor = Bufor(tmp_path / "bufor")
    with _umask(0o000):
        bufor.dopisz(_zd("run-1", "e1"))
        bufor.dopisz(_zd("run-1", "e2"))
        bufor.potwierdz("run-1", "e1")

    assert _prawa(bufor.katalog / "run-1.ndjson") == 0o600
    assert sorted(p.name for p in bufor.katalog.iterdir()) == ["run-1.ndjson"]


# --- zawartość i kolejność ------------------------------------------------------

def test_zdarzenie_trafia_do_pliku_runu_jako_jedna_linia_json_z_polskimi_znakami(tmp_path):
    bufor = Bufor(tmp_path / "bufor")
    zdarzenie = _zd("run-1", "e1", text="zażółć gęślą jaźń")

    bufor.dopisz(zdarzenie)

    surowe = (bufor.katalog / "run-1.ndjson").read_bytes()
    assert surowe == (json.dumps(zdarzenie, ensure_ascii=False) + "\n").encode("utf-8")


def test_niepotwierdzone_zdarzenia_runu_wracaja_w_kolejnosci_dopisania(tmp_path):
    bufor = Bufor(tmp_path / "bufor")
    zdarzenia = [_zd("run-1", f"e{n}", text=f"linia {n}") for n in (3, 1, 2)]
    for zdarzenie in zdarzenia:
        bufor.dopisz(zdarzenie)

    assert bufor.niepotwierdzone() == zdarzenia


def test_niepotwierdzone_zdarzenia_roznych_runow_wracaja_runami_w_kolejnosci_nazw(tmp_path):
    bufor = Bufor(tmp_path / "bufor")
    b1, a1, b2, a2, c1 = (
        _zd("run-3", "b1"), _zd("run-1", "a1"), _zd("run-3", "b2"), _zd("run-1", "a2"),
        _zd("run-2", "c1"))
    for zdarzenie in (b1, a1, b2, a2, c1):
        bufor.dopisz(zdarzenie)

    assert bufor.niepotwierdzone() == [a1, a2, c1, b1, b2]


def test_bufor_czyta_tylko_pliki_ndjson_i_ignoruje_inne_pliki_w_katalogu(tmp_path):
    bufor = Bufor(tmp_path / "bufor")
    bufor.dopisz(_zd("run-1", "e1"))
    obce = json.dumps(_zd("run-2", "obcy"))
    (bufor.katalog / "notatki.txt").write_text(obce + "\n")
    (bufor.katalog / "run-3.ndjson.tmp").write_text(obce + "\n")

    assert [z["event_id"] for z in bufor.niepotwierdzone()] == ["e1"]


def test_bufor_bez_katalogu_nie_ma_niepotwierdzonych_zdarzen_i_katalogu_nie_tworzy(tmp_path):
    bufor = Bufor(tmp_path / "nie-ma-mnie")

    assert bufor.niepotwierdzone() == []
    assert not (tmp_path / "nie-ma-mnie").exists()


# --- potwierdzenie ---------------------------------------------------------------

def test_potwierdzenie_usuwa_tylko_wskazane_zdarzenie_i_zachowuje_kolejnosc_pozostalych(tmp_path):
    bufor = Bufor(tmp_path / "bufor")
    e1, e2, e3 = _zd("run-1", "e1"), _zd("run-1", "e2"), _zd("run-1", "e3")
    for zdarzenie in (e1, e2, e3):
        bufor.dopisz(zdarzenie)

    bufor.potwierdz("run-1", "e2")

    assert bufor.niepotwierdzone() == [e1, e3]


def test_potwierdzenie_w_jednym_runie_nie_usuwa_zdarzenia_o_tym_samym_identyfikatorze_z_innego_runu(
    tmp_path,
):
    bufor = Bufor(tmp_path / "bufor")
    w_pierwszym, w_drugim = _zd("run-1", "wspolny"), _zd("run-2", "wspolny")
    bufor.dopisz(w_pierwszym)
    bufor.dopisz(w_drugim)

    bufor.potwierdz("run-1", "wspolny")

    assert bufor.niepotwierdzone() == [w_drugim]


def test_potwierdzenie_ostatniego_zdarzenia_runu_usuwa_jego_plik(tmp_path):
    bufor = Bufor(tmp_path / "bufor")
    bufor.dopisz(_zd("run-1", "e1"))

    bufor.potwierdz("run-1", "e1")

    assert not (bufor.katalog / "run-1.ndjson").exists()
    assert bufor.niepotwierdzone() == []


def test_potwierdzenie_nieznanego_zdarzenia_albo_runu_nic_nie_zmienia_i_nie_rzuca(tmp_path):
    bufor = Bufor(tmp_path / "bufor")
    e1 = _zd("run-1", "e1")
    bufor.dopisz(e1)
    przed = (bufor.katalog / "run-1.ndjson").read_bytes()

    bufor.potwierdz("run-1", "nie-ma-takiego")
    bufor.potwierdz("run-9", "e1")

    assert (bufor.katalog / "run-1.ndjson").read_bytes() == przed
    assert bufor.niepotwierdzone() == [e1]


# --- uszkodzone wpisy ------------------------------------------------------------

def test_obcieta_ostatnia_linia_po_awarii_jest_pomijana_z_wpisem_na_stderr(tmp_path, capsys):
    bufor = Bufor(tmp_path / "bufor")
    dobre = _zd("run-1", "e1", text="tajna tresc wpisu")
    bufor.katalog.mkdir(parents=True)
    (bufor.katalog / "run-1.ndjson").write_text(
        json.dumps(dobre) + "\n" + '{"run_id": "run-1", "event_id": "e2", "text": "tajna tr')

    assert bufor.niepotwierdzone() == [dobre]
    err = capsys.readouterr().err
    assert err.splitlines() == ["bufor: pominiety uszkodzony wpis run-1.ndjson:2"]
    assert "tajna" not in err                      # treść wpisu nie trafia do logu


@pytest.mark.parametrize(
    "uszkodzona",
    [
        '{"run_id": "run-1", "event_id": "e2", "te',
        "[1, 2]",
        '"sam tekst"',
        "42",
        "null",
        '{"run_id": "run-1", "kind": "output"}',
        '{"event_id": "e2", "kind": "output"}',
    ],
    ids=["obcieta", "tablica", "napis", "liczba", "null", "bez_event_id", "bez_run_id"],
)
def test_wpis_ktory_nie_jest_obiektem_z_identyfikatorami_jest_pomijany_a_sasiednie_zostaja(
    tmp_path, capsys, uszkodzona,
):
    bufor = Bufor(tmp_path / "bufor")
    pierwsze, trzecie = _zd("run-1", "e1"), _zd("run-1", "e3")
    bufor.katalog.mkdir(parents=True)
    (bufor.katalog / "run-1.ndjson").write_text(
        json.dumps(pierwsze) + "\n" + uszkodzona + "\n" + json.dumps(trzecie) + "\n")

    assert bufor.niepotwierdzone() == [pierwsze, trzecie]
    assert capsys.readouterr().err.splitlines() == [
        "bufor: pominiety uszkodzony wpis run-1.ndjson:2"]


def test_nowe_zdarzenie_dopisane_po_obcietej_po_awarii_linii_nie_zlewa_sie_z_nia(tmp_path, capsys):
    bufor = Bufor(tmp_path / "bufor")
    stare = _zd("run-1", "e1")
    bufor.katalog.mkdir(parents=True)
    (bufor.katalog / "run-1.ndjson").write_text(
        json.dumps(stare) + "\n" + '{"run_id": "run-1", "event_id": "e2", "te')   # bez \n
    nowe = _zd("run-1", "e3", text="po awarii")

    bufor.dopisz(nowe)

    assert bufor.niepotwierdzone() == [stare, nowe]
    assert capsys.readouterr().err.splitlines() == [
        "bufor: pominiety uszkodzony wpis run-1.ndjson:2"]


# --- zły run_id ------------------------------------------------------------------

ZLE_RUN_ID = ["../x", "a/b", ".", "x.y", "", "a" * 65, "a b", "run\n1"]


@pytest.mark.parametrize("run_id", ZLE_RUN_ID, ids=repr)
def test_zdarzenie_z_run_id_wychodzacym_poza_dozwolone_znaki_jest_odrzucane_i_nic_nie_zapisuje(
    tmp_path, run_id,
):
    bufor = Bufor(tmp_path / "bufor")

    with pytest.raises(BladBufora):
        bufor.dopisz(_zd(run_id, "e1"))

    assert not (tmp_path / "x.ndjson").exists()
    assert [p for p in tmp_path.rglob("*.ndjson")] == []


@pytest.mark.parametrize("run_id", ZLE_RUN_ID, ids=repr)
def test_potwierdzenie_z_run_id_wychodzacym_poza_dozwolone_znaki_jest_odrzucane(
    tmp_path, run_id,
):
    bufor = Bufor(tmp_path / "bufor")
    bufor.dopisz(_zd("run-1", "e1"))

    with pytest.raises(BladBufora):
        bufor.potwierdz(run_id, "e1")

    assert len(bufor.niepotwierdzone()) == 1


@pytest.mark.parametrize(
    "run_id", ["a", "a" * 64, "ABC", "0123", "a-b", "a_b", "Run_1-A9"],
)
def test_run_id_z_liter_cyfr_myslnika_i_podkreslnika_do_64_znakow_jest_przyjmowany(
    tmp_path, run_id,
):
    bufor = Bufor(tmp_path / "bufor")
    zdarzenie = _zd(run_id, "e1")

    bufor.dopisz(zdarzenie)

    assert bufor.niepotwierdzone() == [zdarzenie]
    assert (bufor.katalog / f"{run_id}.ndjson").is_file()


@pytest.mark.parametrize("brakuje", ["run_id", "event_id"])
def test_zdarzenie_bez_run_id_albo_event_id_nie_trafia_do_bufora(tmp_path, brakuje):
    bufor = Bufor(tmp_path / "bufor")
    zdarzenie = _zd("run-1", "e1")
    del zdarzenie[brakuje]

    with pytest.raises(BladBufora):
        bufor.dopisz(zdarzenie)

    assert bufor.niepotwierdzone() == []


# --- błędy dysku tłumaczone na błąd dziedziny ------------------------------------

def test_katalog_zamiast_pliku_runu_to_blad_bufora_przy_odczycie(tmp_path):
    bufor = Bufor(tmp_path / "bufor")
    (bufor.katalog / "run-1.ndjson").mkdir(parents=True)

    with pytest.raises(BladBufora):
        bufor.niepotwierdzone()


@pytest.mark.skipif(os.geteuid() == 0, reason="root czyta pliki bez praw odczytu")
def test_plik_bufora_bez_praw_odczytu_to_blad_bufora(tmp_path):
    bufor = Bufor(tmp_path / "bufor")
    bufor.dopisz(_zd("run-1", "e1"))
    plik = bufor.katalog / "run-1.ndjson"
    plik.chmod(0o000)
    try:
        with pytest.raises(BladBufora):
            bufor.niepotwierdzone()
    finally:
        plik.chmod(0o600)


def test_plik_bufora_z_bajtami_niebedacymi_utf8_to_blad_bufora_a_nie_wyjatek_biblioteki(tmp_path):
    bufor = Bufor(tmp_path / "bufor")
    bufor.katalog.mkdir(parents=True)
    (bufor.katalog / "run-1.ndjson").write_bytes(b"\xff\xfe\x00 zepsute\n")

    with pytest.raises(BladBufora):
        bufor.niepotwierdzone()


def test_potwierdzenie_gdy_plik_runu_jest_katalogiem_to_blad_bufora(tmp_path):
    bufor = Bufor(tmp_path / "bufor")
    (bufor.katalog / "run-1.ndjson").mkdir(parents=True)

    with pytest.raises(BladBufora):
        bufor.potwierdz("run-1", "e1")


def test_zapis_gdy_katalog_stanu_jest_zwyklym_plikiem_to_blad_bufora(tmp_path):
    zajete = tmp_path / "stan"
    zajete.write_text("to nie katalog")

    with pytest.raises(BladBufora):
        Bufor(zajete / "bufor").dopisz(_zd("run-1", "e1"))


def test_zapis_gdy_plik_runu_jest_katalogiem_to_blad_bufora(tmp_path):
    bufor = Bufor(tmp_path / "bufor")
    (bufor.katalog / "run-1.ndjson").mkdir(parents=True)

    with pytest.raises(BladBufora):
        bufor.dopisz(_zd("run-1", "e1"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root zapisuje do katalogu bez praw zapisu")
def test_potwierdzenie_w_katalogu_bez_praw_zapisu_to_blad_bufora_a_plik_runu_zostaje_nietkniety(
    tmp_path,
):
    bufor = Bufor(tmp_path / "bufor")
    bufor.dopisz(_zd("run-1", "e1"))
    bufor.dopisz(_zd("run-1", "e2"))
    plik = bufor.katalog / "run-1.ndjson"
    przed = plik.read_bytes()
    bufor.katalog.chmod(0o500)                       # czytać wolno, tworzyć plików nie
    try:
        with pytest.raises(BladBufora):
            bufor.potwierdz("run-1", "e1")
    finally:
        bufor.katalog.chmod(0o700)

    assert plik.read_bytes() == przed
    assert sorted(p.name for p in bufor.katalog.iterdir()) == ["run-1.ndjson"]   # bez .tmp
    assert [z["event_id"] for z in bufor.niepotwierdzone()] == ["e1", "e2"]


def test_katalog_bufora_bedacy_zwyklym_plikiem_to_blad_bufora_przy_odczycie_zaleglych(tmp_path):
    zajete = tmp_path / "plik"
    zajete.write_text("to nie katalog")

    with pytest.raises(BladBufora):
        Bufor(zajete).niepotwierdzone()


# --- wysyłka: atrapy ---------------------------------------------------------------

class Odczekania:
    """Atrapa odczekania: nie śpi, zapisuje argumenty; po `zatrzymaj_po` wywołaniach
    zatrzymuje runnera (tak kończy się każdy test z pętlą ponawiania)."""

    def __init__(self, zatrzymaj_po: int | None = None) -> None:
        self.argumenty: list[float] = []
        self.zatrzymaj_po = zatrzymaj_po
        self.runner: RunnerKlienta | None = None

    async def __call__(self, sekundy: float) -> None:
        self.argumenty.append(sekundy)
        if self.zatrzymaj_po is not None and len(self.argumenty) >= self.zatrzymaj_po:
            await self.runner.stop()


def _runner(
    transport=None, zatrzymaj_po: int | None = None, maks_prob: int | None = None,
) -> tuple[RunnerKlienta, Odczekania]:
    odczekania = Odczekania(zatrzymaj_po)
    # parametr przekazujemy tylko, gdy test go ustawia: reszta testow sprawdza wartosc domyslna
    dodatkowe = {} if maks_prob is None else {"maks_prob_bledu_serwera": maks_prob}
    runner = RunnerKlienta(BAZA, TOKEN, transport=transport, odczekaj=odczekania, **dodatkowe)
    odczekania.runner = runner
    return runner, odczekania


def _kokpit_zdarzen(*odpowiedzi, domyslnie: int = 200):
    """Atrapa kokpitu: na kolejne POST /api/events odpowiada kolejnymi pozycjami skryptu
    (kod albo wyjątek do rzucenia), potem `domyslnie`. Zwraca (transport, zdarzenia, zadania);
    `zdarzenia` to ciała zdarzeń w kolejności wysłania."""
    skrypt = list(odpowiedzi)
    zdarzenia: list[dict] = []
    zadania: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        zadania.append(request)
        sciezka = request.url.path
        if sciezka == "/api/runners/register":
            return httpx.Response(
                200, json={"runner_id": "r1", "tenant_id": "t1", "heartbeat_co_s": 15})
        if sciezka == "/api/runners/stream":
            return httpx.Response(200, content=POLACZONO, headers=SSE)
        zdarzenia.append(json.loads(request.content))
        wynik = skrypt.pop(0) if skrypt else domyslnie
        if isinstance(wynik, Exception):
            raise wynik
        return httpx.Response(wynik)

    return httpx.MockTransport(handler), zdarzenia, zadania


async def _wyslij(runner: RunnerKlienta, transport, **zdarzenie) -> bool:
    async with httpx.AsyncClient(transport=transport) as klient:
        return await asyncio.wait_for(runner.send_event(klient, **zdarzenie), 10)


POLE = {"run_id": "run-1", "step_id": "s0", "kind": "output", "text": "x"}


async def test_zdarzenie_jest_w_buforze_juz_w_chwili_wysylania_i_znika_po_przyjeciu():
    widziane_w_buforze: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        widziane_w_buforze.append([z["event_id"] for z in Bufor().niepotwierdzone()])
        return httpx.Response(200)

    runner, _ = _runner()

    assert await _wyslij(runner, httpx.MockTransport(handler), event_id="e1", **POLE) is True

    assert widziane_w_buforze == [["e1"]]
    assert Bufor().niepotwierdzone() == []


async def test_niewyslane_zdarzenie_bez_identyfikatora_czeka_w_buforze_z_identyfikatorem_od_runnera():
    transport, _, _ = _kokpit_zdarzen(503)
    runner, _ = _runner(transport, zatrzymaj_po=1)

    await _wyslij(runner, transport, **POLE)

    (w_buforze,) = Bufor().niepotwierdzone()
    assert str(uuid.UUID(w_buforze["event_id"], version=4)) == w_buforze["event_id"]
    assert w_buforze["run_id"] == "run-1"


async def test_niewyslane_zdarzenie_z_identyfikatorem_od_wolajacego_czeka_w_buforze_pod_nim():
    transport, _, _ = _kokpit_zdarzen(503)
    runner, _ = _runner(transport, zatrzymaj_po=1)

    await _wyslij(runner, transport, event_id="moj-id", **POLE)

    assert [z["event_id"] for z in Bufor().niepotwierdzone()] == ["moj-id"]


# --- odczekanie przy awarii kokpitu -------------------------------------------------

async def test_kokpit_odpowiadajacy_bledem_serwera_dostaje_to_samo_zdarzenie_z_rosnacym_odczekaniem(
    capsys,
):
    transport, zdarzenia, _ = _kokpit_zdarzen(500, 503, httpx.ConnectError("nie ma kokpitu"))
    runner, odczekania = _runner(transport)

    assert await _wyslij(runner, transport, event_id="e1", **POLE) is True

    assert odczekania.argumenty == [1.0, 2.0, 4.0]
    assert [z["event_id"] for z in zdarzenia] == ["e1", "e1", "e1", "e1"]
    assert Bufor().niepotwierdzone() == []
    wpisy = capsys.readouterr().err.splitlines()
    wzorce = [
        r"kokpit niedostepny \(http=500\) - ponawiam za 1(\.0)? s",
        r"kokpit niedostepny \(http=503\) - ponawiam za 2(\.0)? s",
        r"kokpit niedostepny \(ConnectError\) - ponawiam za 4(\.0)? s",
    ]
    assert len(wpisy) == 3
    for wpis, wzorzec in zip(wpisy, wzorce):
        assert re.fullmatch(wzorzec, wpis), wpis


async def test_odczekanie_rosnie_az_do_sufitu_trzydziestu_sekund():
    transport, _, _ = _kokpit_zdarzen(*[500] * 8)
    runner, odczekania = _runner(transport)

    assert await _wyslij(runner, transport, event_id="e1", **POLE) is True

    assert odczekania.argumenty == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


async def test_kazde_zdarzenie_zaczyna_odczekiwanie_od_poczatku():
    transport, _, _ = _kokpit_zdarzen(500, 200, 500, 200)
    runner, odczekania = _runner(transport)

    await _wyslij(runner, transport, event_id="e1", **POLE)
    await _wyslij(runner, transport, event_id="e2", **POLE)

    assert odczekania.argumenty == [1.0, 1.0]


async def test_komunikat_o_niedostepnym_kokpicie_nie_ujawnia_tresci_wyjatku_ani_tokenu(capsys):
    transport, _, _ = _kokpit_zdarzen(
        httpx.ConnectError(f"polaczenie z Bearer {TOKEN} odrzucone"))
    runner, _ = _runner(transport)

    await _wyslij(runner, transport, event_id="e1", **POLE)

    err = capsys.readouterr().err
    assert "ConnectError" in err
    assert TOKEN not in err
    assert "odrzucone" not in err


async def test_zatrzymanie_runnera_w_trakcie_odczekiwania_konczy_wysylke_a_zdarzenie_zostaje_w_buforze():
    transport, zdarzenia, _ = _kokpit_zdarzen(503, 503, 503)
    runner, odczekania = _runner(transport, zatrzymaj_po=1)

    przyjete = await _wyslij(runner, transport, event_id="e1", **POLE)

    assert przyjete is False
    assert len(zdarzenia) == 1                   # po zatrzymaniu nie ma kolejnej próby
    assert odczekania.argumenty == [1.0]
    assert [z["event_id"] for z in Bufor().niepotwierdzone()] == ["e1"]


# --- HAR-146: zdarzenie, ktorego kokpit nigdy nie przyjmie, nie blokuje kolejki ----------

ZMIENNA_PROB = "COCKPIT_MAX_PROB_ZDARZENIA"
LOG_UTRATY_3 = "zdarzenie utracone: kind=output run_id=run-1 http=500 po 3 probach"


async def test_zdarzenie_ktore_kokpit_odrzuca_bledem_500_przy_kazdej_probie_jest_utracone_po_limicie_prob(
    capsys, monkeypatch,
):
    monkeypatch.delenv(ZMIENNA_PROB, raising=False)
    transport, zdarzenia, _ = _kokpit_zdarzen(500, 500, 500)
    runner, odczekania = _runner(transport, maks_prob=3)

    przyjete = await _wyslij(runner, transport, event_id="e1", **POLE)

    assert przyjete is False
    assert [z["event_id"] for z in zdarzenia] == ["e1", "e1", "e1"]
    assert Bufor().niepotwierdzone() == []
    wpisy = capsys.readouterr().err.splitlines()
    assert wpisy[-1] == LOG_UTRATY_3
    assert len([w for w in wpisy if w.startswith("kokpit niedostepny")]) == 2
    assert len(wpisy) == 3


async def test_po_ostatniej_dopuszczonej_probie_runner_nie_czeka_przed_utrata_zdarzenia(
    monkeypatch,
):
    monkeypatch.delenv(ZMIENNA_PROB, raising=False)
    transport, _, _ = _kokpit_zdarzen(500, 500, 500)
    runner, odczekania = _runner(transport, maks_prob=3)

    await _wyslij(runner, transport, event_id="e1", **POLE)

    assert odczekania.argumenty == [1.0, 2.0]            # miedzy probami, nie po ostatniej


async def test_wpis_o_utraconym_po_limicie_zdarzeniu_nie_ujawnia_tokenu_ani_tresci(
    capsys, monkeypatch,
):
    monkeypatch.delenv(ZMIENNA_PROB, raising=False)
    transport, _, _ = _kokpit_zdarzen(500, 500)
    runner, _ = _runner(transport, maks_prob=2)

    await _wyslij(runner, transport, event_id="e1", **{**POLE, "text": "tajna-tresc-zdarzenia"})

    err = capsys.readouterr().err
    assert "zdarzenie utracone" in err
    assert TOKEN not in err
    assert "tajna-tresc-zdarzenia" not in err


async def test_zdarzenie_przyjete_przy_ostatniej_dopuszczonej_probie_nie_jest_utracone(
    capsys, monkeypatch,
):
    monkeypatch.delenv(ZMIENNA_PROB, raising=False)
    transport, zdarzenia, _ = _kokpit_zdarzen(500, 500, 200)
    runner, odczekania = _runner(transport, maks_prob=3)

    przyjete = await _wyslij(runner, transport, event_id="e1", **POLE)

    assert przyjete is True
    assert len(zdarzenia) == 3
    assert odczekania.argumenty == [1.0, 2.0]
    assert Bufor().niepotwierdzone() == []
    assert "zdarzenie utracone" not in capsys.readouterr().err


async def test_zdarzenie_utracone_po_limicie_prob_nie_blokuje_nastepnego_wyslanego_osobno(
    monkeypatch,
):
    monkeypatch.delenv(ZMIENNA_PROB, raising=False)
    transport, zdarzenia, _ = _kokpit_zdarzen(500, 500, 500, 200)
    runner, _ = _runner(transport, maks_prob=3)

    pierwsze = await _wyslij(runner, transport, event_id="e1", **POLE)
    drugie = await _wyslij(runner, transport, event_id="e2", **POLE)

    assert (pierwsze, drugie) == (False, True)
    assert [z["event_id"] for z in zdarzenia] == ["e1", "e1", "e1", "e2"]
    assert Bufor().niepotwierdzone() == []


async def test_zdarzenie_utracone_z_zaleglosci_nie_blokuje_dosylania_nastepnych_w_buforze(
    monkeypatch,
):
    monkeypatch.delenv(ZMIENNA_PROB, raising=False)
    Bufor().dopisz(_zd("run-1", "stare-1"))
    transport, zdarzenia, _ = _kokpit_zdarzen(500, 500, 500, 200)
    runner, _ = _runner(transport, maks_prob=3)

    przyjete = await _wyslij(runner, transport, event_id="nowe", **POLE)

    assert przyjete is True
    assert [z["event_id"] for z in zdarzenia] == ["stare-1"] * 3 + ["nowe"]
    assert Bufor().niepotwierdzone() == []


async def test_licznik_prob_bledu_serwera_liczy_od_zera_dla_kazdego_zdarzenia(monkeypatch):
    monkeypatch.delenv(ZMIENNA_PROB, raising=False)
    Bufor().dopisz(_zd("run-1", "stare-1"))
    transport, zdarzenia, _ = _kokpit_zdarzen(500, 500, 500, 500, 500, 200)
    runner, odczekania = _runner(transport, maks_prob=3)

    przyjete = await _wyslij(runner, transport, event_id="nowe", **POLE)

    assert przyjete is True                       # "nowe": dwa 500 i 200, nie liczy 500 poprzednika
    assert [z["event_id"] for z in zdarzenia] == ["stare-1"] * 3 + ["nowe"] * 3
    assert odczekania.argumenty == [1.0, 2.0, 1.0, 2.0]
    assert Bufor().niepotwierdzone() == []


async def test_niedostepnosc_kokpitu_503_i_zerwane_polaczenia_nie_wyczerpuja_limitu_prob(
    capsys, monkeypatch,
):
    monkeypatch.delenv(ZMIENNA_PROB, raising=False)
    transport, zdarzenia, _ = _kokpit_zdarzen(
        500, 503, httpx.ConnectError("nie ma kokpitu"), 503, 200)
    runner, odczekania = _runner(transport, maks_prob=2)

    przyjete = await _wyslij(runner, transport, event_id="e1", **POLE)

    assert przyjete is True
    assert len(zdarzenia) == 5
    assert len(odczekania.argumenty) == 4
    assert Bufor().niepotwierdzone() == []
    assert "zdarzenie utracone" not in capsys.readouterr().err


@pytest.mark.parametrize("kod", [502, 503, 504])
async def test_brama_i_niedostepnosc_5xx_inne_niz_500_sa_ponawiane_bez_limitu(kod, monkeypatch):
    monkeypatch.delenv(ZMIENNA_PROB, raising=False)
    transport, zdarzenia, _ = _kokpit_zdarzen(*[kod] * 5, 200)
    runner, _ = _runner(transport, maks_prob=2)

    przyjete = await _wyslij(runner, transport, event_id="e1", **POLE)

    assert przyjete is True
    assert len(zdarzenia) == 6


async def test_limit_prob_zdarzenia_mozna_ustawic_zmienna_srodowiskowa(capsys, monkeypatch):
    monkeypatch.setenv(ZMIENNA_PROB, "2")
    transport, zdarzenia, _ = _kokpit_zdarzen(500, 500, 500)
    runner, _ = _runner(transport)

    przyjete = await _wyslij(runner, transport, event_id="e1", **POLE)

    assert przyjete is False
    assert len(zdarzenia) == 2
    assert capsys.readouterr().err.splitlines()[-1] == (
        "zdarzenie utracone: kind=output run_id=run-1 http=500 po 2 probach")


async def test_limit_prob_podany_runnerowi_ma_pierwszenstwo_przed_zmienna_srodowiskowa(monkeypatch):
    monkeypatch.setenv(ZMIENNA_PROB, "5")
    transport, zdarzenia, _ = _kokpit_zdarzen(500, 500, 500)
    runner, _ = _runner(transport, maks_prob=2)

    przyjete = await _wyslij(runner, transport, event_id="e1", **POLE)

    assert przyjete is False
    assert len(zdarzenia) == 2


async def test_domyslnie_zdarzenie_jest_ponawiane_dziewiec_razy_a_po_dziesiatym_bledzie_500_utracone(
    capsys, monkeypatch,
):
    monkeypatch.delenv(ZMIENNA_PROB, raising=False)
    transport, zdarzenia, _ = _kokpit_zdarzen(*[500] * 9, 200)
    runner, _ = _runner(transport)
    assert await _wyslij(runner, transport, event_id="e1", **POLE) is True
    assert len(zdarzenia) == 10
    capsys.readouterr()

    transport, zdarzenia, _ = _kokpit_zdarzen(*[500] * 10, 200)
    runner, odczekania = _runner(transport)
    assert await _wyslij(runner, transport, event_id="e2", **POLE) is False

    assert len(zdarzenia) == 10
    assert len(odczekania.argumenty) == 9
    assert capsys.readouterr().err.splitlines()[-1] == (
        "zdarzenie utracone: kind=output run_id=run-1 http=500 po 10 probach")
    assert Bufor().niepotwierdzone() == []


def test_limit_prob_zdarzenia_bez_zmiennej_srodowiskowej_to_dziesiec(monkeypatch):
    from runner.client import maks_prob_bledu_serwera

    monkeypatch.delenv(ZMIENNA_PROB, raising=False)

    assert maks_prob_bledu_serwera() == 10


def test_limit_prob_zdarzenia_ze_zmiennej_srodowiskowej_jest_czytany_przy_kazdym_wywolaniu(
    monkeypatch,
):
    from runner.client import maks_prob_bledu_serwera

    monkeypatch.setenv(ZMIENNA_PROB, "7")
    assert maks_prob_bledu_serwera() == 7
    monkeypatch.setenv(ZMIENNA_PROB, "1")
    assert maks_prob_bledu_serwera() == 1


@pytest.mark.parametrize("wartosc", ["0", "abc", "٣", "", "-1", "2.5", " 3"])
def test_limit_prob_zdarzenia_inny_niz_dodatnia_liczba_cyfr_ascii_to_blad_z_nazwa_zmiennej(
    monkeypatch, wartosc,
):
    from runner.client import maks_prob_bledu_serwera

    monkeypatch.setenv(ZMIENNA_PROB, wartosc)

    with pytest.raises(ValueError, match=ZMIENNA_PROB):
        maks_prob_bledu_serwera()


# --- odpowiedzi 4xx --------------------------------------------------------------

@pytest.mark.parametrize("kod", [302, 400, 403, 404, 422, 499])
async def test_zdarzenie_odrzucone_przez_kokpit_kodem_spoza_2xx_401_i_5xx_jest_zglaszane_jako_utracone_i_usuwane_z_bufora(
    kod, capsys,
):
    transport, zdarzenia, _ = _kokpit_zdarzen(kod)
    runner, odczekania = _runner(transport)

    przyjete = await _wyslij(runner, transport, event_id="e1", **POLE)

    assert przyjete is False
    assert capsys.readouterr().err.splitlines() == [
        f"zdarzenie utracone: kind=output run_id=run-1 http={kod}"]
    assert Bufor().niepotwierdzone() == []
    assert len(zdarzenia) == 1                   # utrata nie jest ponawiana
    assert odczekania.argumenty == []


async def test_po_utraconym_zdarzeniu_runner_wysyla_nastepne(capsys):
    transport, zdarzenia, _ = _kokpit_zdarzen(400, 200)
    runner, _ = _runner(transport)

    pierwsze = await _wyslij(runner, transport, event_id="e1", **POLE)
    drugie = await _wyslij(runner, transport, event_id="e2", **POLE)

    assert (pierwsze, drugie) == (False, True)
    assert [z["event_id"] for z in zdarzenia] == ["e1", "e2"]
    assert Bufor().niepotwierdzone() == []


async def test_wpis_o_utraconym_zdarzeniu_i_plik_bufora_nie_zawieraja_tokenu(capsys):
    transport, _, zadania = _kokpit_zdarzen(404, 500)
    runner, _ = _runner(transport, zatrzymaj_po=1)

    await _wyslij(runner, transport, event_id="e1", **POLE)
    await _wyslij(runner, transport, event_id="e2", **POLE)

    assert zadania[0].headers["Authorization"] == f"Bearer {TOKEN}"
    err = capsys.readouterr().err
    assert "zdarzenie utracone" in err and "kokpit niedostepny" in err
    assert TOKEN not in err
    zawartosc = "".join(p.read_text() for p in Bufor().katalog.glob("*.ndjson"))
    assert "e2" in zawartosc                      # plik istnieje, więc brak tokenu coś znaczy
    assert TOKEN not in zawartosc
    assert "Authorization" not in zawartosc


async def test_odrzucony_token_przerywa_wysylke_a_zdarzenie_zostaje_w_buforze(capsys):
    transport, zdarzenia, _ = _kokpit_zdarzen(401)
    runner, odczekania = _runner(transport)

    with pytest.raises(TokenOdrzucony):
        await _wyslij(runner, transport, event_id="e1", **POLE)

    assert [z["event_id"] for z in Bufor().niepotwierdzone()] == ["e1"]
    assert len(zdarzenia) == 1
    assert odczekania.argumenty == []
    assert "zdarzenie utracone" not in capsys.readouterr().err


# --- kolejność zaległych ------------------------------------------------------------

async def test_zalegle_zdarzenia_z_bufora_ida_przed_nowym_w_kolejnosci_dopisania():
    bufor = Bufor()
    bufor.dopisz(_zd("run-1", "stare-1"))
    bufor.dopisz(_zd("run-1", "stare-2"))
    transport, zdarzenia, _ = _kokpit_zdarzen()
    runner, _ = _runner(transport)

    assert await _wyslij(runner, transport, event_id="nowe", **POLE) is True

    assert [z["event_id"] for z in zdarzenia] == ["stare-1", "stare-2", "nowe"]
    assert Bufor().niepotwierdzone() == []


async def test_nieudane_zdarzenie_zaleglosci_blokuje_nastepne_do_swojego_przyjecia():
    bufor = Bufor()
    bufor.dopisz(_zd("run-1", "stare-1"))
    bufor.dopisz(_zd("run-1", "stare-2"))
    transport, zdarzenia, _ = _kokpit_zdarzen(500)
    runner, odczekania = _runner(transport)

    await _wyslij(runner, transport, event_id="nowe", **POLE)

    assert [z["event_id"] for z in zdarzenia] == ["stare-1", "stare-1", "stare-2", "nowe"]
    assert odczekania.argumenty == [1.0]


async def test_wynik_wysylki_mowi_o_nowym_zdarzeniu_a_nie_o_zaleglych():
    bufor = Bufor()
    bufor.dopisz(_zd("run-1", "stare-utracone"))
    transport, _, _ = _kokpit_zdarzen(400, 200)
    runner, _ = _runner(transport)
    assert await _wyslij(runner, transport, event_id="nowe-1", **POLE) is True

    bufor.dopisz(_zd("run-1", "stare-przyjete"))
    transport, _, _ = _kokpit_zdarzen(200, 400)
    runner, _ = _runner(transport)
    assert await _wyslij(runner, transport, event_id="nowe-2", **POLE) is False


# --- restart runnera: zaległe zdarzenia po połączeniu ---------------------------------

async def test_runner_po_restarcie_dosyla_zalegle_zdarzenia_po_zameldowaniu_a_przed_strumieniem():
    Bufor().dopisz(_zd("run-1", "przed-restartem-1"))
    Bufor().dopisz(_zd("run-1", "przed-restartem-2"))
    transport, zdarzenia, zadania = _kokpit_zdarzen()
    runner, odczekania = _runner(transport, zatrzymaj_po=1)

    await asyncio.wait_for(runner.listen(), 10)

    assert [z["event_id"] for z in zdarzenia] == ["przed-restartem-1", "przed-restartem-2"]
    assert [z.url.path for z in zadania] == [
        "/api/runners/register", "/api/events", "/api/events", "/api/runners/stream"]
    assert Bufor().niepotwierdzone() == []


async def test_zalegle_zdarzenia_czekaja_na_powrot_kokpitu_przed_otwarciem_strumienia():
    Bufor().dopisz(_zd("run-1", "przed-restartem"))
    transport, zdarzenia, zadania = _kokpit_zdarzen(503)
    runner, odczekania = _runner(transport, zatrzymaj_po=2)

    await asyncio.wait_for(runner.listen(), 10)

    assert [z.url.path for z in zadania] == [
        "/api/runners/register", "/api/events", "/api/events", "/api/runners/stream"]
    assert odczekania.argumenty == [1.0, 1.0]     # raz przy zaległym, raz po zamknięciu strumienia
    assert Bufor().niepotwierdzone() == []


async def test_odrzucony_token_przy_dosylaniu_zaleglych_konczy_nasluchiwanie_bez_ponawiania():
    Bufor().dopisz(_zd("run-1", "przed-restartem"))
    transport, zdarzenia, zadania = _kokpit_zdarzen(401)
    runner, odczekania = _runner(transport)

    with pytest.raises(TokenOdrzucony):
        await asyncio.wait_for(runner.listen(), 10)

    assert [z.url.path for z in zadania] == ["/api/runners/register", "/api/events"]
    assert odczekania.argumenty == []
    assert [z["event_id"] for z in Bufor().niepotwierdzone()] == ["przed-restartem"]


async def test_uszkodzony_bufor_przy_dosylaniu_zaleglych_to_blad_bufora_a_nie_wyjatek_biblioteki():
    (Bufor().katalog / "run-1.ndjson").mkdir(parents=True)
    transport, _, _ = _kokpit_zdarzen()
    runner, odczekania = _runner(transport)

    with pytest.raises(BladBufora):
        await asyncio.wait_for(runner.listen(), 10)

    assert odczekania.argumenty == []


# --- prawdziwy kokpit: skutek w bazie -------------------------------------------------

@dataclass
class Swiat:
    klient: AsyncClient
    run_id: str            # run zespołu Browarek, zlecony runnerowi z TOKEN
    obcy_run_id: str       # run zespołu Piekarnia


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
        await s.flush()
        piekarnia = Tenant(name="Piekarnia")
        s.add(piekarnia)
        await s.flush()
        wlasciciel = Runner(
            tenant_id=browarek.id, name="wlasciciel", last_seen_at=datetime.now(timezone.utc),
            token_hash=hashlib.sha256(TOKEN.encode()).hexdigest())
        obcy = Runner(
            tenant_id=piekarnia.id, name="obcy", last_seen_at=datetime.now(timezone.utc),
            token_hash=hashlib.sha256(b"token-obcego").hexdigest())
        s.add_all([wlasciciel, obcy])
        await s.flush()
        run = Run(tenant_id=browarek.id, runner_id=wlasciciel.id, status=RunStatus.running)
        obcy_run = Run(tenant_id=piekarnia.id, runner_id=obcy.id, status=RunStatus.running)
        s.add_all([run, obcy_run])
        await s.commit()
        ids = (run.id, obcy_run.id)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield Swiat(c, *ids)
    finally:
        await engine.dispose()


async def _zdarzenia_w_bazie(run_id: str | None = None) -> list[Event]:
    async with db_session.SessionLocal() as s:
        zapytanie = select(Event).order_by(Event.run_id, Event.seq)
        if run_id is not None:
            zapytanie = zapytanie.where(Event.run_id == run_id)
        return list((await s.execute(zapytanie)).scalars().all())


class ZawodnyKokpit(httpx.AsyncBaseTransport):
    """Prawdziwa aplikacja kokpitu za transportem, który bywa niedostępny albo gubi odpowiedź.
    Strumień poleceń jest atrapą (kończy się od razu): ASGI nie oddaje SSE po kawałku."""

    def __init__(self, niedostepny_przez: int = 0, gubi_odpowiedz_na: tuple[int, ...] = ()):
        self._prawdziwy = ASGITransport(app=app)
        self.pozostalo = niedostepny_przez
        self.gubi_odpowiedz_na = set(gubi_odpowiedz_na)
        self.sciezki: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.sciezki.append(request.url.path)
        numer = len(self.sciezki)
        if self.pozostalo > 0:
            self.pozostalo -= 1
            raise httpx.ConnectError("kokpit nie odpowiada", request=request)
        if request.url.path.endswith("/api/runners/stream"):
            return httpx.Response(200, content=POLACZONO, headers=SSE)
        odpowiedz = await self._prawdziwy.handle_async_request(request)
        if numer in self.gubi_odpowiedz_na:
            raise httpx.ReadTimeout("odpowiedz zgubiona", request=request)
        return odpowiedz


def _runner_z_baza(transport, zatrzymaj_po: int | None = None):
    odczekania = Odczekania(zatrzymaj_po)
    runner = RunnerKlienta("http://test", TOKEN, transport=transport, odczekaj=odczekania)
    odczekania.runner = runner
    return runner, odczekania


async def test_zdarzenia_wyslane_gdy_kokpit_byl_niedostepny_trafiaja_do_bazy_raz_i_w_kolejnosci(swiat):
    kokpit = ZawodnyKokpit(niedostepny_przez=5)
    runner, odczekania = _runner_z_baza(kokpit)

    async with httpx.AsyncClient(transport=kokpit) as klient:
        for tekst in ("pierwsze", "drugie", "trzecie"):
            przyjete = await asyncio.wait_for(runner.send_event(
                klient, run_id=swiat.run_id, step_id="s0", kind="output", text=tekst), 10)
            assert przyjete is True

    zapisane = await _zdarzenia_w_bazie()
    assert [(e.seq, e.text) for e in zapisane] == [(1, "pierwsze"), (2, "drugie"), (3, "trzecie")]
    assert {e.run_id for e in zapisane} == {swiat.run_id}          # nic nie trafiło do innego zespołu
    assert len({e.event_id for e in zapisane}) == 3
    assert odczekania.argumenty == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert Bufor().niepotwierdzone() == []


async def test_zdarzenie_ktorego_odpowiedz_zginela_po_zapisie_w_kokpicie_jest_w_bazie_dokladnie_raz(
    swiat,
):
    kokpit = ZawodnyKokpit(gubi_odpowiedz_na=(1,))
    runner, odczekania = _runner_z_baza(kokpit)

    async with httpx.AsyncClient(transport=kokpit) as klient:
        przyjete = await asyncio.wait_for(runner.send_event(
            klient, run_id=swiat.run_id, step_id="s0", kind="output", text="raz"), 10)

    assert przyjete is True
    assert odczekania.argumenty == [1.0]
    zapisane = await _zdarzenia_w_bazie()
    assert [(e.seq, e.text) for e in zapisane] == [(1, "raz")]
    assert Bufor().niepotwierdzone() == []


async def test_zdarzenia_z_bufora_po_restarcie_runnera_trafiaja_do_bazy_bez_duplikatow(swiat):
    w_bazie_juz = _zd(swiat.run_id, "drugie-id", text="dwa")
    odpowiedz = await swiat.klient.post(
        "/api/events", json=w_bazie_juz, headers={"Authorization": f"Bearer {TOKEN}"})
    assert odpowiedz.status_code == 200
    bufor = Bufor()                                # wpisy z poprzedniego procesu runnera
    bufor.dopisz(_zd(swiat.run_id, "pierwsze-id", text="jeden"))
    bufor.dopisz(w_bazie_juz)
    bufor.dopisz(_zd(swiat.run_id, "trzecie-id", text="trzy"))
    kokpit = ZawodnyKokpit()
    runner, _ = _runner_z_baza(kokpit, zatrzymaj_po=1)

    await asyncio.wait_for(runner.listen(), 10)

    zapisane = await _zdarzenia_w_bazie()
    assert [(e.seq, e.event_id, e.text) for e in zapisane] == [
        (1, "drugie-id", "dwa"), (2, "pierwsze-id", "jeden"), (3, "trzecie-id", "trzy")]
    assert {e.run_id for e in zapisane} == {swiat.run_id}
    assert await _zdarzenia_w_bazie(swiat.obcy_run_id) == []
    assert kokpit.sciezki[0] == "/api/runners/register"
    assert kokpit.sciezki[-1] == "/api/runners/stream"
    assert Bufor().niepotwierdzone() == []


def test_runner_zachowuje_token_podany_przy_tworzeniu_i_wysyla_go_w_naglowku():
    runner = RunnerKlienta("http://kokpit.test", "tok-abc-123", bufor=Bufor())

    assert runner.token == "tok-abc-123"
    assert runner._headers == {"Authorization": "Bearer tok-abc-123"}
