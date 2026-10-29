"""HAR-47: brak miejsca na dysku runnera konczy run statusem `aborted` z powodem
"brak miejsca", a nie cichym bledem zapisu.

Runner: prog wolnego miejsca przed krokiem i ENOSPC przy zapisie bufora. Komendy to
prawdziwe procesy, transport HTTP jest przechwycony (`httpx.MockTransport`), a jedyna
podmiana to pomiar dysku (`shutil.disk_usage`) i miejsce awarii zapisu (`os.fsync`,
`os.replace`, `os.open`) - kod `Runner.run_command` i `Bufor` jest produkcyjny.
Kokpit: `step_aborted` -> `aborted` z powodem; skutek czytamy z bazy przez prawdziwe API.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import signal
import uuid
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import runner.bufor as bufor_mod
from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, RunStatus, Tenant
from cockpit.db.models import Runner as RunnerWBazie
from cockpit.services.runs import nastepny_status
from runner.bufor import BladBufora, BrakMiejsca, Bufor, min_wolne_mb, wolne_mb
from runner.client import Runner, TokenOdrzucony

ZMIENNA = "COCKPIT_MIN_FREE_MB"
MIB = 1024 * 1024
RUN_ID = "run-1"
STEP_ID = "krok-1"
TOKEN = "tajny-token"
NAGLOWEK = {"Authorization": "Bearer tajne"}
KOMUNIKAT_ENOSPC = "brak miejsca: zapis zdarzen runnera nie powiodl sie (ENOSPC)"

Uzycie = namedtuple("Uzycie", "total used free")


@pytest.fixture(autouse=True)
def _prog_domyslny(monkeypatch):
    # prog z zewnatrz testu nie moze zmieniac wyniku: kazdy test ustawia go jawnie
    monkeypatch.delenv(ZMIENNA, raising=False)


# --- pomocnicy runnera ---------------------------------------------------------

def _podstaw_dysk(monkeypatch, wolne_mb: int | None = None, *, bajty: int | None = None,
                  blad: OSError | None = None) -> list[Path]:
    """Podstawia pomiar dysku; zwraca katalogi, ktore runner zmierzyl."""
    zmierzone: list[Path] = []

    def disk_usage(sciezka):
        zmierzone.append(Path(sciezka))
        if blad is not None:
            raise blad
        wolne = bajty if bajty is not None else wolne_mb * MIB
        return Uzycie(total=10**12, used=10**12 - wolne, free=wolne)

    monkeypatch.setattr(bufor_mod.shutil, "disk_usage", disk_usage)
    return zmierzone


def _blad_os(kod: int):
    def rzuc(*args, **kwargs):
        raise OSError(kod, os.strerror(kod))
    return rzuc


def _awaria_na_plikach_bufora(monkeypatch, nazwa: str, kod: int) -> None:
    """Awaria `os.<nazwa>` wylacznie dla plikow bufora (*.ndjson, *.tmp): reszta srodowiska
    testu (pytest, baza) nie moze jej zobaczyc."""
    prawdziwa = getattr(os, nazwa)

    def podstawka(*args, **kwargs):
        cel = str(args[0]) if args and isinstance(args[0], (str, os.PathLike)) else ""
        if nazwa == "fsync" or cel.endswith((".ndjson", ".tmp")):
            raise OSError(kod, os.strerror(kod))
        return prawdziwa(*args, **kwargs)

    monkeypatch.setattr(bufor_mod.os, nazwa, podstawka)


def _kokpit(status: int = 200):
    zadania: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        zadania.append(request)
        return httpx.Response(status)

    return httpx.MockTransport(handler), zadania


def _zdarzenia(zadania: list[httpx.Request]) -> list[dict]:
    return [json.loads(z.content) for z in zadania]


def _runner(**kw) -> Runner:
    async def bez_czekania(sekundy: float) -> None:
        return None

    return Runner("http://kokpit.test", TOKEN, odczekaj=bez_czekania, **kw)


def _komenda_ze_znacznikiem(znacznik: Path) -> list[str]:
    return ["sh", "-c", 'touch "$1"', "sh", str(znacznik)]


async def _krok(runner: Runner, transport, komenda: list[str]) -> None:
    async with httpx.AsyncClient(transport=transport) as client:
        await asyncio.wait_for(runner.run_command(client, RUN_ID, STEP_ID, komenda), 20)


async def _krok_ze_znacznikiem(tmp_path, monkeypatch=None):
    """Uruchamia krok tworzacy plik-znacznik. Zwraca (zdarzenia, znacznik)."""
    znacznik = tmp_path / "krok-wystartowal"
    transport, zadania = _kokpit()
    await _krok(_runner(), transport, _komenda_ze_znacznikiem(znacznik))
    return _zdarzenia(zadania), znacznik


def _bufor_jest_pusty() -> bool:
    return Bufor().niepotwierdzone() == []


# --- 1. za malo miejsca przed krokiem ---------------------------------------------

async def test_za_malo_miejsca_przed_krokiem_krok_nie_startuje_i_kokpit_dostaje_przerwanie_z_powodem(
    tmp_path, monkeypatch,
):
    zmierzone = _podstaw_dysk(monkeypatch, 50)

    zdarzenia, znacznik = await _krok_ze_znacznikiem(tmp_path)

    assert not znacznik.exists()
    assert [z["kind"] for z in zdarzenia] == ["step_aborted"]
    assert zdarzenia[0]["text"] == "brak miejsca: 50 MB wolne, wymagane 200 MB"
    assert zdarzenia[0]["run_id"] == RUN_ID
    assert zdarzenia[0]["step_id"] == STEP_ID
    assert zdarzenia[0]["event_id"]
    assert zmierzone and all(m == tmp_path for m in zmierzone)    # mierzymy katalog stanu


async def test_przerwanie_z_braku_miejsca_idzie_z_pominieciem_bufora_bo_bufor_tez_potrzebuje_dysku(
    tmp_path, monkeypatch,
):
    _podstaw_dysk(monkeypatch, 50)
    transport, zadania = _kokpit(status=500)       # kokpit pada: bufor ponowilby wysylke

    await _krok(_runner(), transport, ["sh", "-c", "exit 0"])

    assert len(zadania) == 1                       # jedna proba, zadnych ponowien
    assert _bufor_jest_pusty()
    assert not (tmp_path / "bufor").exists() or not list((tmp_path / "bufor").iterdir())


async def test_przerwanie_z_braku_miejsca_niesie_token_runnera():
    # bez tokenu kokpit odrzuci zdarzenie i run zostanie "running" na zawsze
    transport, zadania = _kokpit()
    await _wyslij_bezposrednio(transport, kind="step_aborted", text="x")

    assert zadania[0].headers["Authorization"] == f"Bearer {TOKEN}"


# --- 2. granice progu ----------------------------------------------------------------

async def test_dokladnie_wymagane_miejsce_wystarcza_krok_startuje(tmp_path, monkeypatch):
    _podstaw_dysk(monkeypatch, 200)

    zdarzenia, znacznik = await _krok_ze_znacznikiem(tmp_path)

    assert znacznik.exists()
    assert [z["kind"] for z in zdarzenia] == ["step_finished"]


async def test_o_jeden_megabajt_za_malo_miejsca_krok_nie_startuje(tmp_path, monkeypatch):
    _podstaw_dysk(monkeypatch, 199)

    zdarzenia, znacznik = await _krok_ze_znacznikiem(tmp_path)

    assert not znacznik.exists()
    assert [z["text"] for z in zdarzenia] == ["brak miejsca: 199 MB wolne, wymagane 200 MB"]


async def test_wolne_miejsce_liczone_w_megabajtach_zaokragla_w_dol_o_jeden_bajt_za_malo(
    tmp_path, monkeypatch,
):
    _podstaw_dysk(monkeypatch, bajty=200 * MIB - 1)

    zdarzenia, znacznik = await _krok_ze_znacznikiem(tmp_path)

    assert not znacznik.exists()
    assert [z["text"] for z in zdarzenia] == ["brak miejsca: 199 MB wolne, wymagane 200 MB"]


async def test_megabajt_to_1024_do_kwadratu_bajtow_a_nie_milion(tmp_path, monkeypatch):
    # 200 000 000 B = 190 MiB (za malo), ale 200 "dziesietnych" MB (wystarczylo)
    _podstaw_dysk(monkeypatch, bajty=200_000_000)

    zdarzenia, znacznik = await _krok_ze_znacznikiem(tmp_path)

    assert not znacznik.exists()
    assert [z["text"] for z in zdarzenia] == ["brak miejsca: 190 MB wolne, wymagane 200 MB"]


async def test_brak_jakiegokolwiek_miejsca_tez_przerywa_krok_z_zerem_w_komunikacie(
    tmp_path, monkeypatch,
):
    _podstaw_dysk(monkeypatch, 0)

    zdarzenia, znacznik = await _krok_ze_znacznikiem(tmp_path)

    assert not znacznik.exists()
    assert [z["text"] for z in zdarzenia] == ["brak miejsca: 0 MB wolne, wymagane 200 MB"]


# --- 3. prog z konfiguracji ----------------------------------------------------------

async def test_prog_ze_zmiennej_srodowiskowej_zmienia_wynik_dla_tego_samego_dysku(
    tmp_path, monkeypatch,
):
    _podstaw_dysk(monkeypatch, 100)
    monkeypatch.setenv(ZMIENNA, "50")

    zdarzenia, znacznik = await _krok_ze_znacznikiem(tmp_path)

    assert znacznik.exists()
    assert [z["kind"] for z in zdarzenia] == ["step_finished"]


async def test_prog_ze_zmiennej_wyzszy_niz_domyslny_przerywa_krok_z_tym_progiem_w_komunikacie(
    tmp_path, monkeypatch,
):
    _podstaw_dysk(monkeypatch, 300)
    monkeypatch.setenv(ZMIENNA, "500")

    zdarzenia, znacznik = await _krok_ze_znacznikiem(tmp_path)

    assert not znacznik.exists()
    assert [z["text"] for z in zdarzenia] == ["brak miejsca: 300 MB wolne, wymagane 500 MB"]


async def test_prog_zero_wylacza_sprawdzanie_nawet_gdy_dysk_jest_pelny(tmp_path, monkeypatch):
    _podstaw_dysk(monkeypatch, 0)
    monkeypatch.setenv(ZMIENNA, "0")

    zdarzenia, znacznik = await _krok_ze_znacznikiem(tmp_path)

    assert znacznik.exists()
    assert [z["kind"] for z in zdarzenia] == ["step_finished"]


def test_bez_zmiennej_prog_wynosi_200_megabajtow():
    assert min_wolne_mb() == 200


def test_pusta_zmienna_znaczy_prog_domyslny_a_nie_blad(monkeypatch):
    monkeypatch.setenv(ZMIENNA, "")

    assert min_wolne_mb() == 200


def test_prog_domyslny_to_200_a_nie_zero(monkeypatch):
    assert bufor_mod.PROG_WOLNEGO_MIEJSCA_MB == 200
    monkeypatch.setenv(ZMIENNA, "0")
    assert min_wolne_mb() == 0


@pytest.mark.parametrize("wartosc", ["50", "007", "1024"])
def test_prog_z_cyfr_ascii_jest_czytany_jako_liczba_calkowita(monkeypatch, wartosc):
    monkeypatch.setenv(ZMIENNA, wartosc)

    wynik = min_wolne_mb()

    assert type(wynik) is int
    assert wynik == int(wartosc)


def test_zmiana_progu_miedzy_wywolaniami_jest_widoczna_bo_zmienna_nie_jest_czytana_przy_imporcie(
    monkeypatch,
):
    monkeypatch.setenv(ZMIENNA, "50")
    assert min_wolne_mb() == 50
    monkeypatch.setenv(ZMIENNA, "70")
    assert min_wolne_mb() == 70
    monkeypatch.delenv(ZMIENNA)
    assert min_wolne_mb() == 200


@pytest.mark.parametrize(
    "wartosc",
    [" 5", "5 ", "5.0", "-1", "+5", "abc", "٣", "5\n", "0x10", "1_0"],
    ids=["spacja_z_przodu", "spacja_z_tylu", "ulamek", "ujemna", "plus", "tekst",
         "cyfra_arabska", "koncowy_newline", "szesnastkowa", "podkreslnik"],
)
def test_zly_prog_w_konfiguracji_jest_odrzucony_z_nazwa_zmiennej(monkeypatch, wartosc):
    monkeypatch.setenv(ZMIENNA, wartosc)

    with pytest.raises(ValueError, match=ZMIENNA):
        min_wolne_mb()


async def test_zly_prog_w_konfiguracji_nie_pozwala_wystartowac_krokowi(tmp_path, monkeypatch):
    _podstaw_dysk(monkeypatch, 10_000)          # miejsca jest duzo: decyduje tylko zla konfiguracja
    monkeypatch.setenv(ZMIENNA, "abc")

    zdarzenia, znacznik = await _krok_ze_znacznikiem(tmp_path)

    assert not znacznik.exists()
    assert [z["kind"] for z in zdarzenia] == ["step_aborted"]
    assert zdarzenia[0]["text"].startswith("zla konfiguracja: ")
    assert ZMIENNA in zdarzenia[0]["text"]
    assert _bufor_jest_pusty()


# --- 4. pomiar wolnego miejsca ------------------------------------------------------------

def test_wolne_miejsce_istniejacego_katalogu_jest_mierzone_na_nim_samym(tmp_path, monkeypatch):
    zmierzone = _podstaw_dysk(monkeypatch, bajty=5 * MIB + 123)

    assert wolne_mb(tmp_path) == 5
    assert zmierzone == [tmp_path]


def test_nieistniejacy_katalog_jest_mierzony_na_najblizszym_istniejacym_przodku(
    tmp_path, monkeypatch,
):
    zmierzone = _podstaw_dysk(monkeypatch, 7)

    assert wolne_mb(tmp_path / "a" / "b" / "c") == 7
    assert zmierzone == [tmp_path]


def test_pomiar_bierze_najblizszego_przodka_a_nie_korzen_gdy_czesc_sciezki_istnieje(
    tmp_path, monkeypatch,
):
    (tmp_path / "a").mkdir()
    zmierzone = _podstaw_dysk(monkeypatch, 7)

    wolne_mb(tmp_path / "a" / "b" / "c")

    assert zmierzone == [tmp_path / "a"]


@pytest.mark.parametrize("blad", [PermissionError, FileNotFoundError])
def test_blad_systemu_przy_pomiarze_wolnego_miejsca_nie_jest_ukrywany(tmp_path, monkeypatch, blad):
    _podstaw_dysk(monkeypatch, blad=blad(errno.EACCES, "odmowa"))

    with pytest.raises(blad):
        wolne_mb(tmp_path)


@pytest.mark.parametrize("blad", [PermissionError, FileNotFoundError])
async def test_nie_mozna_zmierzyc_wolnego_miejsca_krok_nie_startuje_i_kokpit_dostaje_przyczyne(
    tmp_path, monkeypatch, blad,
):
    _podstaw_dysk(monkeypatch, blad=blad(errno.EACCES, "odmowa"))

    zdarzenia, znacznik = await _krok_ze_znacznikiem(tmp_path)

    assert not znacznik.exists()
    assert [z["kind"] for z in zdarzenia] == ["step_aborted"]
    assert zdarzenia[0]["text"] == f"nie mozna sprawdzic wolnego miejsca: {blad.__name__}"
    assert _bufor_jest_pusty()


# --- 5. ENOSPC w buforze --------------------------------------------------------------------

def _zdarzenie_bufora(**reszta) -> dict:
    return {"run_id": RUN_ID, "event_id": str(uuid.uuid4()), "kind": "output", "text": "x",
            **reszta}


@pytest.mark.parametrize("miejsce", ["fsync", "open"])
def test_brak_miejsca_przy_dopisaniu_zdarzenia_do_bufora_to_osobny_blad_dziedziny(
    monkeypatch, miejsce,
):
    _awaria_na_plikach_bufora(monkeypatch, miejsce, errno.ENOSPC)

    with pytest.raises(BrakMiejsca) as wyjatek:
        Bufor().dopisz(_zdarzenie_bufora())

    assert isinstance(wyjatek.value, BladBufora)
    assert isinstance(wyjatek.value.__cause__, OSError)
    assert wyjatek.value.__cause__.errno == errno.ENOSPC
    assert str(wyjatek.value) == f"zapis {RUN_ID}.ndjson: OSError"


@pytest.mark.parametrize("miejsce", ["fsync", "open"])
def test_inny_blad_zapisu_bufora_niz_brak_miejsca_nie_jest_zglaszany_jako_brak_miejsca(
    monkeypatch, miejsce,
):
    _awaria_na_plikach_bufora(monkeypatch, miejsce, errno.EACCES)

    with pytest.raises(BladBufora) as wyjatek:
        Bufor().dopisz(_zdarzenie_bufora())

    assert type(wyjatek.value) is BladBufora
    assert not isinstance(wyjatek.value, BrakMiejsca)
    assert str(wyjatek.value) == f"zapis {RUN_ID}.ndjson: PermissionError"


def _bufor_z_dwoma_zdarzeniami() -> tuple[Bufor, dict]:
    """Potwierdzenie pierwszego zostawia drugie, wiec `potwierdz` musi przepisac plik."""
    bufor = Bufor()
    pierwsze, drugie = _zdarzenie_bufora(), _zdarzenie_bufora(text="y")
    bufor.dopisz(pierwsze)
    bufor.dopisz(drugie)
    return bufor, pierwsze


@pytest.mark.parametrize("miejsce", ["fsync", "open", "replace"])
def test_brak_miejsca_przy_potwierdzeniu_zdarzenia_to_osobny_blad_dziedziny(
    monkeypatch, miejsce,
):
    bufor, pierwsze = _bufor_z_dwoma_zdarzeniami()
    _awaria_na_plikach_bufora(monkeypatch, miejsce, errno.ENOSPC)

    with pytest.raises(BrakMiejsca) as wyjatek:
        bufor.potwierdz(RUN_ID, pierwsze["event_id"])

    assert wyjatek.value.__cause__.errno == errno.ENOSPC
    assert str(wyjatek.value) == f"zapis {RUN_ID}.ndjson: OSError"
    assert len(bufor.niepotwierdzone()) == 2           # nic nie zginelo przy nieudanym zapisie


@pytest.mark.parametrize("miejsce", ["fsync", "open", "replace"])
def test_inny_blad_przy_potwierdzeniu_zdarzenia_niz_brak_miejsca_zostaje_zwyklym_bledem_bufora(
    monkeypatch, miejsce,
):
    bufor, pierwsze = _bufor_z_dwoma_zdarzeniami()
    _awaria_na_plikach_bufora(monkeypatch, miejsce, errno.EACCES)

    with pytest.raises(BladBufora) as wyjatek:
        bufor.potwierdz(RUN_ID, pierwsze["event_id"])

    assert type(wyjatek.value) is BladBufora
    assert str(wyjatek.value) == f"zapis {RUN_ID}.ndjson: PermissionError"


def _awaria_na_deskryptorze_bufora(monkeypatch, nazwa: str, kod: int, *, po_polowie: bool = False):
    """Awaria `os.<nazwa>` (write albo fsync) tylko dla deskryptora pliku `*.ndjson` bufora.
    Z `po_polowie` pada `os.write`, ale dopiero po prawdziwym zapisie polowy danych (pelny dysk
    zostawia obciety wpis). Zwraca liste wywolan `os.ftruncate` (rozmiary), ktore przeszly dalej."""
    prawdziwy_open, prawdziwy_write = os.open, os.write
    deskryptory: set[int] = set()

    def otworz(sciezka, *args, **kwargs):
        fd = prawdziwy_open(sciezka, *args, **kwargs)
        if str(sciezka).endswith(".ndjson"):
            deskryptory.add(fd)
        return fd

    prawdziwa = getattr(os, nazwa)

    def podstawka(fd, *args, **kwargs):
        if fd in deskryptory:
            if po_polowie:
                dane = args[0]
                prawdziwy_write(fd, dane[: len(dane) // 2])
            raise OSError(kod, os.strerror(kod))
        return prawdziwa(fd, *args, **kwargs)

    monkeypatch.setattr(bufor_mod.os, "open", otworz)
    monkeypatch.setattr(bufor_mod.os, nazwa, podstawka)


def _ftruncate_z_awaria(monkeypatch, kod: int) -> list[int]:
    wywolania: list[int] = []

    def ftruncate(fd, rozmiar):
        wywolania.append(rozmiar)
        raise OSError(kod, os.strerror(kod))

    monkeypatch.setattr(bufor_mod.os, "ftruncate", ftruncate)
    return wywolania


def test_nieudane_sprzatanie_wpisu_po_braku_miejsca_nie_zaslania_braku_miejsca(monkeypatch):
    _awaria_na_deskryptorze_bufora(monkeypatch, "fsync", errno.ENOSPC)
    proby_obciecia = _ftruncate_z_awaria(monkeypatch, errno.EIO)

    with pytest.raises(BrakMiejsca) as wyjatek:
        Bufor().dopisz(_zdarzenie_bufora())

    assert proby_obciecia == [0]                       # sprzatanie bylo probowane i padlo
    assert type(wyjatek.value) is BrakMiejsca
    assert type(wyjatek.value.__cause__) is OSError
    assert wyjatek.value.__cause__.errno == errno.ENOSPC
    assert str(wyjatek.value) == f"zapis {RUN_ID}.ndjson: OSError"


async def test_nieudane_sprzatanie_wpisu_po_braku_miejsca_nadal_przerywa_run_z_powodem(
    tmp_path, monkeypatch,
):
    _podstaw_dysk(monkeypatch, 10_000)
    _awaria_na_deskryptorze_bufora(monkeypatch, "fsync", errno.ENOSPC)
    _ftruncate_z_awaria(monkeypatch, errno.EIO)
    transport, zadania = _kokpit()

    await _krok(_runner(), transport, ["sh", "-c", "echo wynik; exit 0"])   # bez wyjatku

    zdarzenia = _zdarzenia(zadania)
    assert [z["kind"] for z in zdarzenia] == ["step_aborted"]
    assert zdarzenia[0]["text"] == KOMUNIKAT_ENOSPC


@pytest.mark.parametrize(
    "miejsce, po_polowie",
    [("write", True), ("fsync", False)],
    ids=["awaria_zapisu_po_polowie_wpisu", "awaria_fsync_po_pelnym_zapisie"],
)
def test_wpis_nieudany_z_braku_miejsca_nie_zostaje_w_buforze(monkeypatch, miejsce, po_polowie):
    bufor = Bufor()
    wczesniejsze = _zdarzenie_bufora(text="wczesniejsze")
    bufor.dopisz(wczesniejsze)
    plik = bufor.katalog / f"{RUN_ID}.ndjson"
    rozmiar_przed = plik.stat().st_size
    zawartosc_przed = plik.read_bytes()
    _awaria_na_deskryptorze_bufora(monkeypatch, miejsce, errno.ENOSPC, po_polowie=po_polowie)

    with pytest.raises(BrakMiejsca):
        bufor.dopisz(_zdarzenie_bufora(text="nieudane"))

    assert plik.stat().st_size == rozmiar_przed
    assert plik.read_bytes() == zawartosc_przed
    assert bufor.niepotwierdzone() == [wczesniejsze]


def test_wpis_nieudany_z_innego_powodu_niz_brak_miejsca_tez_nie_zostaje_w_buforze(monkeypatch):
    bufor = Bufor()
    wczesniejsze = _zdarzenie_bufora(text="wczesniejsze")
    bufor.dopisz(wczesniejsze)
    plik = bufor.katalog / f"{RUN_ID}.ndjson"
    rozmiar_przed = plik.stat().st_size
    _awaria_na_deskryptorze_bufora(monkeypatch, "write", errno.EIO, po_polowie=True)

    with pytest.raises(BladBufora) as wyjatek:
        bufor.dopisz(_zdarzenie_bufora(text="nieudane"))

    assert type(wyjatek.value) is BladBufora
    assert wyjatek.value.__cause__.errno == errno.EIO
    assert plik.stat().st_size == rozmiar_przed
    assert bufor.niepotwierdzone() == [wczesniejsze]


def test_zapis_wpisu_przerwany_w_polowie_przez_system_jest_dokonczony_i_wpis_jest_kompletny(
    monkeypatch,
):
    prawdziwy_open, prawdziwy_write = os.open, os.write
    deskryptory: set[int] = set()
    zapisy: list[int] = []

    def otworz(sciezka, *args, **kwargs):
        fd = prawdziwy_open(sciezka, *args, **kwargs)
        if str(sciezka).endswith(".ndjson"):
            deskryptory.add(fd)
        return fd

    def krotki_zapis(fd, dane):
        if fd not in deskryptory:
            return prawdziwy_write(fd, dane)
        # pierwszy zapis przyjmuje tylko polowe bajtow (system moze zapisac mniej niz zadano)
        zapisano = prawdziwy_write(fd, dane[: max(1, len(dane) // 2)]) if not zapisy \
            else prawdziwy_write(fd, dane)
        zapisy.append(zapisano)
        return zapisano

    monkeypatch.setattr(bufor_mod.os, "open", otworz)
    monkeypatch.setattr(bufor_mod.os, "write", krotki_zapis)
    zdarzenie = _zdarzenie_bufora(text="dluga tresc " * 20)

    Bufor().dopisz(zdarzenie)

    assert len(zapisy) >= 2                            # petla doczytala reszte
    assert Bufor().niepotwierdzone() == [zdarzenie]    # caly JSON, nie obcieta polowa
    plik = Bufor().katalog / f"{RUN_ID}.ndjson"
    assert plik.read_bytes().endswith(b"\n")


# --- 5b. pomiar wolnego miejsca nie zapetla sie na nieistniejacej sciezce ---------------------

def test_pomiar_wolnego_miejsca_dla_sciezki_ktorej_nie_ma_nawet_w_korzeniu_konczy_sie_bledem(
    monkeypatch,
):
    sprawdzenia = []

    def nie_istnieje(self):
        sprawdzenia.append(self)
        assert len(sprawdzenia) < 1000, "pomiar sie zapetlil"
        return False

    monkeypatch.setattr(bufor_mod.Path, "exists", nie_istnieje)

    with pytest.raises(FileNotFoundError) as e:
        wolne_mb(Path("a/b"))

    assert e.value.errno == errno.ENOENT
    assert e.value.strerror == "brak istniejacego katalogu nad katalogiem stanu"


async def test_katalog_stanu_bez_istniejacego_przodka_przerywa_krok_z_przyczyna_a_nie_zapetla_runnera(
    tmp_path, monkeypatch,
):
    prawdziwe_istnieje = Path.exists
    sprawdzenia = []

    def istnieje(self, *args, **kwargs):
        # tylko katalog stanu i jego przodkowie "znikaja": reszta srodowiska testu dziala normalnie
        if self == tmp_path or tmp_path in self.parents or self in tmp_path.parents:
            sprawdzenia.append(self)
            assert len(sprawdzenia) < 1000, "pomiar sie zapetlil"
            return False
        return prawdziwe_istnieje(self, *args, **kwargs)

    monkeypatch.setattr(bufor_mod.Path, "exists", istnieje)
    znacznik = tmp_path / "krok-wystartowal"
    transport, zadania = _kokpit()

    await _krok(_runner(), transport, _komenda_ze_znacznikiem(znacznik))

    zdarzenia = _zdarzenia(zadania)
    assert [z["kind"] for z in zdarzenia] == ["step_aborted"]
    assert zdarzenia[0]["text"] == "nie mozna sprawdzic wolnego miejsca: FileNotFoundError"
    assert not os.path.exists(znacznik)                # os.path nie jest podstawione


# --- 6. ENOSPC przy zapisie bufora w trakcie kroku ---------------------------------------------

async def test_brak_miejsca_przy_zapisie_zdarzen_kroku_konczy_krok_przerwaniem_z_powodem(
    tmp_path, monkeypatch,
):
    _podstaw_dysk(monkeypatch, 10_000)                 # sprawdzenie przed krokiem przechodzi
    _awaria_na_plikach_bufora(monkeypatch, "fsync", errno.ENOSPC)
    transport, zadania = _kokpit()

    await _krok(_runner(), transport, ["sh", "-c", "echo wynik; exit 0"])

    zdarzenia = _zdarzenia(zadania)
    assert [z["kind"] for z in zdarzenia] == ["step_aborted"]
    assert zdarzenia[0]["text"] == KOMUNIKAT_ENOSPC
    assert zdarzenia[0]["run_id"] == RUN_ID
    assert zdarzenia[0]["step_id"] == STEP_ID
    assert _bufor_jest_pusty()                         # zdarzenie nie przeszlo przez bufor


async def test_brak_miejsca_przy_potwierdzaniu_wyslanego_zdarzenia_tez_przerywa_krok(
    tmp_path, monkeypatch,
):
    _podstaw_dysk(monkeypatch, 10_000)
    # zaleglosc w buforze: potwierdzenie jej pierwszego wpisu przepisuje plik (os.replace)
    Bufor().dopisz(_zdarzenie_bufora(event_id="zalegle-1"))
    Bufor().dopisz(_zdarzenie_bufora(event_id="zalegle-2"))
    _awaria_na_plikach_bufora(monkeypatch, "replace", errno.ENOSPC)
    transport, zadania = _kokpit()

    await _krok(_runner(), transport, ["sh", "-c", "exit 0"])

    zdarzenia = _zdarzenia(zadania)
    assert zdarzenia[-1]["kind"] == "step_aborted"
    assert zdarzenia[-1]["text"] == KOMUNIKAT_ENOSPC
    assert all(z["kind"] != "step_finished" for z in zdarzenia)
    assert sum(1 for z in zdarzenia if z["kind"] == "step_aborted") == 1


async def test_inny_blad_bufora_niz_brak_miejsca_przerywa_krok_wyjatkiem_a_nie_przerwaniem_runu(
    tmp_path, monkeypatch,
):
    _podstaw_dysk(monkeypatch, 10_000)
    _awaria_na_plikach_bufora(monkeypatch, "fsync", errno.EACCES)
    transport, zadania = _kokpit()

    with pytest.raises(BladBufora) as wyjatek:
        await _krok(_runner(), transport, ["sh", "-c", "exit 0"])

    assert type(wyjatek.value) is BladBufora
    assert _zdarzenia(zadania) == []                   # kokpit nie dostal falszywego "brak miejsca"


# --- 7. trwajacy proces kroku zatrzymany ---------------------------------------------------------

async def test_brak_miejsca_przy_zapisie_zdarzen_zatrzymuje_trwajacy_proces_kroku_ale_nie_nasluch(
    tmp_path, monkeypatch,
):
    _podstaw_dysk(monkeypatch, 10_000)
    runner = _runner()
    dlugi = await asyncio.create_subprocess_exec(
        "sleep", "30", start_new_session=True)
    runner._procesy.add(dlugi)
    try:
        _awaria_na_plikach_bufora(monkeypatch, "fsync", errno.ENOSPC)
        transport, zadania = _kokpit()

        await _krok(runner, transport, ["sh", "-c", "exit 0"])

        assert dlugi.returncode is not None            # zabity, nie czeka 30 s
        assert [z["kind"] for z in _zdarzenia(zadania)] == ["step_aborted"]
        assert not runner._zatrzymany.is_set()         # to nie jest stop(): nasluch i heartbeat zyja
    finally:
        if dlugi.returncode is None:
            os.killpg(dlugi.pid, signal.SIGKILL)
        await dlugi.wait()


async def test_przerwanie_z_braku_miejsca_przed_krokiem_nie_zatrzymuje_innych_procesow(
    tmp_path, monkeypatch,
):
    # zatrzymanie procesow dotyczy ENOSPC w trakcie zapisu, nie sprawdzenia progu przed krokiem
    _podstaw_dysk(monkeypatch, 50)
    runner = _runner()
    inny = await asyncio.create_subprocess_exec("sleep", "30", start_new_session=True)
    runner._procesy.add(inny)
    try:
        transport, _ = _kokpit()

        await _krok(runner, transport, ["sh", "-c", "exit 0"])

        assert inny.returncode is None
    finally:
        if inny.returncode is None:
            os.killpg(inny.pid, signal.SIGKILL)
        await inny.wait()


# --- 8. wysylka bezposrednia -----------------------------------------------------------------------

async def _wyslij_bezposrednio(transport, **kw) -> bool:
    runner = _runner()
    async with httpx.AsyncClient(transport=transport) as client:
        return await runner._wyslij_bezposrednio(client, run_id=RUN_ID, step_id=STEP_ID, **kw)


async def test_wysylka_bezposrednia_wysyla_jedno_zdarzenie_z_nowym_identyfikatorem_i_zwraca_sukces():
    transport, zadania = _kokpit(status=200)

    assert await _wyslij_bezposrednio(transport, kind="step_aborted", text="powod") is True

    assert len(zadania) == 1
    assert zadania[0].method == "POST"
    assert zadania[0].url.path == "/api/events"
    zdarzenie = json.loads(zadania[0].content)
    assert (zdarzenie["kind"], zdarzenie["text"]) == ("step_aborted", "powod")
    uuid.UUID(zdarzenie["event_id"])                   # ValueError, gdy to nie UUID
    assert _bufor_jest_pusty()


async def test_wysylka_bezposrednia_odrzucona_tokenem_konczy_sie_bledem_tokenu(capsys):
    transport, zadania = _kokpit(status=401)

    with pytest.raises(TokenOdrzucony):
        await _wyslij_bezposrednio(transport, kind="step_aborted", text="powod")

    assert TOKEN not in capsys.readouterr().err
    assert len(zadania) == 1


async def test_wysylka_bezposrednia_przy_awarii_kokpitu_nie_ponawia_i_zostawia_slad_na_stderr(
    capsys,
):
    transport, zadania = _kokpit(status=500)

    assert await _wyslij_bezposrednio(transport, kind="step_aborted", text="powod") is False

    assert len(zadania) == 1
    wpisy = capsys.readouterr().err.splitlines()
    assert len(wpisy) == 1
    assert re.fullmatch(
        rf"zdarzenie step_aborted niewyslane: run_id={RUN_ID} \(.*500.*\)", wpisy[0]), wpisy[0]
    assert TOKEN not in wpisy[0]
    assert _bufor_jest_pusty()


async def test_wysylka_bezposrednia_przy_bledzie_polaczenia_zwraca_porazke_i_nie_ujawnia_tokenu(
    capsys,
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("polaczenie odrzucone Bearer tajny-token")

    assert await _wyslij_bezposrednio(
        httpx.MockTransport(handler), kind="step_aborted", text="powod") is False

    wpisy = capsys.readouterr().err.splitlines()
    assert len(wpisy) == 1
    assert re.fullmatch(
        rf"zdarzenie step_aborted niewyslane: run_id={RUN_ID} \(.*ConnectError.*\)", wpisy[0]
    ), wpisy[0]
    assert TOKEN not in wpisy[0]


# --- 9. kokpit: przerwanie z powodem, skutek w bazie -----------------------------------------------------

@pytest.fixture
async def klient(tmp_path, monkeypatch):
    """Zespol "Browarek" z runnerem (token `tajne`), ktory ma otwarty strumien."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with maker() as s:
        t = Tenant(name="Browarek")
        s.add(t)
        await s.flush()
        s.add(RunnerWBazie(tenant_id=t.id, name="macbook",
                           token_hash=hashlib.sha256(b"tajne").hexdigest(),
                           last_seen_at=datetime.now(timezone.utc)))
        await s.commit()
    async with maker() as s:
        runner_id = (await s.execute(select(RunnerWBazie.id))).scalars().one()
    bus.mark_runner_connected(runner_id)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        while runner_id in bus.connected_runner_ids():
            bus.mark_runner_disconnected(runner_id)
        getattr(bus, "_rozlaczone", {}).pop(runner_id, None)
        await engine.dispose()


async def _nowy_run(klient) -> str:
    r = await klient.post("/api/runs", json={})
    assert r.status_code == 200
    return r.json()["run_id"]


async def _zdarzenie(klient, run_id: str, kind: str, **reszta):
    cialo = {"run_id": run_id, "kind": kind, "step_id": "s0",
             "event_id": str(uuid.uuid4()), **reszta}
    r = await klient.post("/api/events", json=cialo, headers=NAGLOWEK)
    assert r.status_code == 200
    return r


async def _stan_w_bazie(run_id: str) -> tuple[RunStatus, str | None]:
    async with db_session.SessionLocal() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        return run.status, run.reason


async def _liczba_zdarzen_w_bazie(run_id: str) -> int:
    async with db_session.SessionLocal() as s:
        return (await s.execute(
            select(func.count()).select_from(Event).where(Event.run_id == run_id)
        )).scalar_one()


async def _ustaw_w_bazie(run_id: str, status: RunStatus, reason: str | None = None) -> None:
    async with db_session.SessionLocal() as s:
        run = await s.get(Run, run_id)
        run.status = status
        run.reason = reason
        await s.commit()


TEKST = "brak miejsca: 12 MB wolne, wymagane 200 MB"


async def test_run_w_toku_przerwany_z_braku_miejsca_ma_status_przerwany_i_powod_z_tekstu_zdarzenia(
    klient,
):
    run_id = await _nowy_run(klient)

    await _zdarzenie(klient, run_id, "step_aborted", text=TEKST)

    assert await _stan_w_bazie(run_id) == (RunStatus.aborted, TEKST)


async def test_run_do_odbioru_przerwany_z_braku_miejsca_w_kolejnym_kroku_ma_status_przerwany(
    klient,
):
    run_id = await _nowy_run(klient)
    await _zdarzenie(klient, run_id, "step_finished")
    assert (await _stan_w_bazie(run_id))[0] == RunStatus.to_accept

    await _zdarzenie(klient, run_id, "step_aborted", text=TEKST)

    assert await _stan_w_bazie(run_id) == (RunStatus.aborted, TEKST)


async def test_run_przerwany_wczesniej_przez_kokpit_zachowuje_pierwotny_powod(klient):
    run_id = await _nowy_run(klient)
    await _ustaw_w_bazie(run_id, RunStatus.aborted, "runner offline")

    await _zdarzenie(klient, run_id, "step_aborted", text=TEKST)

    assert await _stan_w_bazie(run_id) == (RunStatus.aborted, "runner offline")
    assert await _liczba_zdarzen_w_bazie(run_id) == 1        # zdarzenie zapisane, powod nietkniety


async def test_run_nieudany_nie_zamienia_sie_w_przerwany_i_zachowuje_swoj_powod(klient):
    run_id = await _nowy_run(klient)
    await _zdarzenie(klient, run_id, "step_failed", text="kod wyjscia 3")
    przed = await _stan_w_bazie(run_id)
    assert przed[0] == RunStatus.failed

    await _zdarzenie(klient, run_id, "step_aborted", text=TEKST)

    assert await _stan_w_bazie(run_id) == przed
    assert (await _stan_w_bazie(run_id))[1] != TEKST


@pytest.mark.parametrize("stan", [RunStatus.pending, RunStatus.waiting, RunStatus.skipped])
async def test_run_oczekujacy_albo_pominiety_nie_dostaje_powodu_z_przerwania_kroku(klient, stan):
    run_id = await _nowy_run(klient)
    await _ustaw_w_bazie(run_id, stan)

    await _zdarzenie(klient, run_id, "step_aborted", text=TEKST)

    assert await _stan_w_bazie(run_id) == (stan, None)


async def test_ponowione_zdarzenie_o_tym_samym_identyfikatorze_nie_zmienia_powodu(klient):
    run_id = await _nowy_run(klient)
    id_zdarzenia = str(uuid.uuid4())
    await _zdarzenie(klient, run_id, "step_aborted", text=TEKST, event_id=id_zdarzenia)

    await _zdarzenie(klient, run_id, "step_aborted", text="inny tekst", event_id=id_zdarzenia)

    assert await _stan_w_bazie(run_id) == (RunStatus.aborted, TEKST)
    assert await _liczba_zdarzen_w_bazie(run_id) == 1


async def test_zwykle_zdarzenie_z_tekstem_nie_ustawia_powodu_runu(klient):
    run_id = await _nowy_run(klient)

    await _zdarzenie(klient, run_id, "output", text=TEKST)

    assert await _stan_w_bazie(run_id) == (RunStatus.running, None)


async def test_sukces_kroku_z_tekstem_nie_ustawia_powodu_runu(klient):
    run_id = await _nowy_run(klient)

    await _zdarzenie(klient, run_id, "step_finished", text=TEKST)

    assert await _stan_w_bazie(run_id) == (RunStatus.to_accept, None)


async def test_runner_z_za_malo_miejsca_konczy_prawdziwy_run_w_kokpicie_jako_przerwany_z_powodem(
    klient, tmp_path, monkeypatch,
):
    """Caly tor: prawdziwy `Runner.run_command` -> prawdziwa aplikacja -> baza."""
    run_id = await _nowy_run(klient)
    _podstaw_dysk(monkeypatch, 12)
    runner = Runner("http://test", "tajne")
    znacznik = tmp_path / "krok-wystartowal"

    await asyncio.wait_for(
        runner.run_command(klient, run_id, "s0", _komenda_ze_znacznikiem(znacznik)), 20)

    assert not znacznik.exists()
    assert await _stan_w_bazie(run_id) == (RunStatus.aborted, TEKST)


@pytest.fixture
def zapytania_o_runy_dla_postgresa():
    """Zapytania SELECT z tabeli runow wykonane przez sesje ORM, skompilowane dla PostgreSQL.
    SQLite nie renderuje FOR UPDATE, wiec blokade widac dopiero w zapytaniu dla PostgreSQL."""
    from sqlalchemy import event
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.orm import Session

    zebrane: list[str] = []

    def podsluch(stan):
        if not stan.is_select:
            return
        sql = str(stan.statement.compile(dialect=postgresql.dialect()))
        if re.search(rf"FROM {Run.__tablename__}\b", sql):
            zebrane.append(sql)

    event.listen(Session, "do_orm_execute", podsluch)
    try:
        yield zebrane
    finally:
        event.remove(Session, "do_orm_execute", podsluch)


async def test_zapis_przerwania_blokuje_wiersz_runu_przed_odczytem_statusu(
    klient, zapytania_o_runy_dla_postgresa,
):
    run_id = await _nowy_run(klient)
    zapytania_o_runy_dla_postgresa.clear()           # liczy sie tylko zapis zdarzenia

    await _zdarzenie(klient, run_id, "step_aborted", text=TEKST)

    zablokowane = [q for q in zapytania_o_runy_dla_postgresa if "FOR UPDATE" in q]
    assert zablokowane, zapytania_o_runy_dla_postgresa
    assert await _stan_w_bazie(run_id) == (RunStatus.aborted, TEKST)


# --- maszyna przejsc (czysta funkcja) -------------------------------------------------------------------

_PO_PRZERWANIU = {
    RunStatus.running: RunStatus.aborted,
    RunStatus.to_accept: RunStatus.aborted,
}


@pytest.mark.parametrize("obecny", list(RunStatus))
def test_przerwanie_kroku_przestawia_na_przerwany_tylko_run_w_toku_albo_do_odbioru(obecny):
    assert nastepny_status(obecny, "step_aborted") == _PO_PRZERWANIU.get(obecny, obecny)


def test_sukces_kroku_nie_cofa_runu_do_odbioru_po_dodaniu_przerwania():
    assert nastepny_status(RunStatus.to_accept, "step_finished") == RunStatus.to_accept
