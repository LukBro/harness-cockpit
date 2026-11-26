"""HAR-204: niedokonczona linia wyjscia agenta ma ograniczony bufor.

Strumien jest wstrzykniety (`_emituj_strumien` to produkcyjny kod czytajacy wyjscie kroku),
wiec 10 MB bez znaku nowej linii mozna podac bez procesu.
"""
from __future__ import annotations

import asyncio
import tracemalloc

import pytest

from cockpit.services.invariants import Rola
from runner import krok as modul_kroku
from runner.agent import SpecKroku

LIMIT_ZNAKOW = 10
ROZMIAR_KAWALKA = 64 * 1024


def spec() -> SpecKroku:
    return SpecKroku(
        run_id="run-204", step_id="krok-204", agent_id="agent-1", skill_id="planowanie",
        rola=Rola.reviewer, wejscia=[], wyjscia=["plan.md"], komendy={},
    )


class StrumienZKawalkow:
    """Oddaje `liczba` razy ten sam kawalek, bez trzymania calosci w pamieci."""

    def __init__(self, kawalek: bytes, liczba: int) -> None:
        self._kawalek = kawalek
        self._zostalo = liczba

    async def read(self, n: int) -> bytes:
        if self._zostalo == 0:
            return b""
        self._zostalo -= 1
        return self._kawalek


class StrumienZListy:
    """Kazdy odczyt oddaje dokladnie jeden kawalek (`StreamReader` by je skleil)."""

    def __init__(self, kawalki: list[bytes]) -> None:
        self._kawalki = list(kawalki)

    async def read(self, n: int) -> bytes:
        return self._kawalki.pop(0) if self._kawalki else b""


async def _zdarzenia(strumien) -> list[dict]:
    zdarzenia: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    await asyncio.wait_for(modul_kroku._emituj_strumien(strumien, spec(), emit), 30)
    return zdarzenia


def _strumien(*kawalki: bytes) -> asyncio.StreamReader:
    s = asyncio.StreamReader()
    for k in kawalki:
        s.feed_data(k)
    s.feed_eof()
    return s


@pytest.fixture
def maly_limit(monkeypatch):
    monkeypatch.setattr(modul_kroku, "LIMIT_TEKSTU_ZDARZENIA", LIMIT_ZNAKOW)


async def test_linia_10_mb_bez_nowej_linii_nie_przekracza_limitu_bufora():
    kawalek = b"x" * ROZMIAR_KAWALKA
    liczba = 10 * 1024 * 1024 // ROZMIAR_KAWALKA
    tracemalloc.start()
    try:
        zdarzenia = await _zdarzenia(StrumienZKawalkow(kawalek, liczba))
        _, szczyt = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(zdarzenia) == 1
    assert len(zdarzenia[0]["text"]) == modul_kroku.LIMIT_TEKSTU_ZDARZENIA
    assert zdarzenia[0]["text_full_len"] == 10 * 1024 * 1024
    # bufor linii (limit * 4 bajty) + kawalek + tekst zdarzenia, a nie 10 MB
    assert szczyt < 1024 * 1024


async def test_po_przekroczeniu_limitu_reszta_linii_jest_odrzucona_a_nastepna_dociera(maly_limit):
    zdarzenia = await _zdarzenia(StrumienZListy([b"a" * 100, b"b" * 100 + b"\nnastepna\n"]))

    assert [z["text"] for z in zdarzenia] == ["a" * LIMIT_ZNAKOW, "nastepna"]
    assert zdarzenia[0]["text_full_len"] == 200
    assert "text_full_len" not in zdarzenia[1]


async def test_linia_mieszczaca_sie_w_limicie_nie_ma_znacznika_obciecia(maly_limit):
    zdarzenia = await _zdarzenia(_strumien(b"0123456789\n"))

    assert zdarzenia[0]["text"] == "0123456789"
    assert "text_full_len" not in zdarzenia[0]


async def test_linia_ponad_limit_znakow_ale_w_buforze_bajtowym_jest_obcieta_do_znakow(maly_limit):
    # 15 znakow po 2 bajty = 30 bajtow miesci sie w buforze (40 bajtow), ale ma wiecej znakow niz limit
    zdarzenia = await _zdarzenia(_strumien(("ą" * 15).encode() + b"\n"))

    assert zdarzenia[0]["text"] == "ą" * LIMIT_ZNAKOW
    assert zdarzenia[0]["text_full_len"] == 15


async def test_bufor_linii_miesci_limit_znakow_czterobajtowych(maly_limit):
    pelna = "😀" * LIMIT_ZNAKOW

    zdarzenia = await _zdarzenia(_strumien(pelna.encode() + b"\n"))

    assert zdarzenia[0]["text"] == pelna
    assert "text_full_len" not in zdarzenia[0]


@pytest.mark.parametrize("rozmiar_kawalka", [1000, 7])
async def test_pelna_dlugosc_linii_obcietej_przez_bufor_jest_liczona_w_znakach(
    maly_limit, rozmiar_kawalka
):
    # 100 znakow po 2 bajty przy buforze 40 bajtow; kawalek 7 bajtow tnie znaki na granicy
    dane = ("ą" * 100).encode() + b"\n"
    kawalki = [dane[i:i + rozmiar_kawalka] for i in range(0, len(dane), rozmiar_kawalka)]

    zdarzenia = await _zdarzenia(StrumienZListy(kawalki))

    assert [z["text"] for z in zdarzenia] == ["ą" * LIMIT_ZNAKOW]
    assert zdarzenia[0]["text_full_len"] == 100


@pytest.mark.parametrize("znak", ["Ā", "Ŀ"])    # drugi bajt: 0x80 i 0xBF, skraje kontynuacji
async def test_skrajne_bajty_kontynuacji_nie_licza_sie_jako_osobne_znaki(maly_limit, znak):
    zdarzenia = await _zdarzenia(_strumien((znak * 100).encode() + b"\n"))

    assert zdarzenia[0]["text_full_len"] == 100


async def test_dlugosc_drugiej_obcietej_linii_nie_dolicza_znakow_poprzedniej(maly_limit):
    zdarzenia = await _zdarzenia(_strumien(b"a" * 100 + b"\n" + b"b" * 70 + b"\n"))

    assert [z["text_full_len"] for z in zdarzenia] == [100, 70]


async def test_linia_z_samych_blednych_bajtow_ponad_bufor_ma_znacznik_obciecia(maly_limit):
    # same bajty kontynuacji: licznik znakow daje 0, o obcieciu mowi zdekodowany tekst bufora
    zdarzenia = await _zdarzenia(_strumien(b"\x80" * 100 + b"\n"))

    assert len(zdarzenia[0]["text"]) == LIMIT_ZNAKOW
    assert zdarzenia[0]["text_full_len"] > LIMIT_ZNAKOW


async def test_linia_ostatnia_bez_nowej_linii_jest_wyslana_na_koncu_strumienia(maly_limit):
    zdarzenia = await _zdarzenia(StrumienZListy([b"pierwsza\nostat", b"nia"]))

    assert [z["text"] for z in zdarzenia] == ["pierwsza", "ostatnia"]


async def test_zakonczenie_linii_crlf_traci_znak_powrotu_karetki_takze_na_granicy_kawalkow():
    zdarzenia = await _zdarzenia(StrumienZListy([b"a\r\nb\r", b"\nc\rd\n"]))

    assert [z["text"] for z in zdarzenia] == ["a", "b", "c\rd"]


async def test_pusta_linia_jest_wyslana_a_pusty_koniec_strumienia_nie():
    zdarzenia = await _zdarzenia(_strumien(b"a\n\nb\n"))

    assert [z["text"] for z in zdarzenia] == ["a", "", "b"]
