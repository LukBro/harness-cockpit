"""HAR-169: wpisy lekcji z zakresem (`cockpit.services.lekcje.wpisy_lekcji`) - przypadki brzegowe.

Uzupelnia testy w `test_manifest_kontekstu.py`: kazdy test ma wejscie, na ktorym tylko jeden
element funkcji decyduje o wyniku (separator sciezek, wartosc domyslna kroku, granica bloku kodu).
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

from cockpit.services.lekcje import WpisLekcji, waliduj_lekcje, wpisy_lekcji
from test_manifest_kontekstu import lekcja, plik_lekcji


def test_sciezki_lekcji_rozdziela_przecinek_a_spacja_wewnatrz_sciezki_zostaje():
    blok = lekcja(1, "budowa", "src/*.py, docs/moj plik.md,web/a.ts")

    (wpis,) = wpisy_lekcji(plik_lekcji(blok))

    assert wpis.sciezki == ("src/*.py", "docs/moj plik.md", "web/a.ts")


def test_sciezki_lekcji_traca_spacje_otaczajace_kazda_sciezke():
    blok = lekcja(1, "budowa", "a.md, src/a.py ,docs/b.md")

    (wpis,) = wpisy_lekcji(plik_lekcji(blok))

    assert wpis.sciezki == ("a.md", "src/a.py", "docs/b.md")


def test_zakres_sciezek_z_samych_przecinkow_i_spacji_daje_pusty_zbior_a_nie_pusta_sciezke():
    blok = lekcja(1, "budowa", ", ,")

    assert waliduj_lekcje(plik_lekcji(blok)) == []
    (wpis,) = wpisy_lekcji(plik_lekcji(blok))
    assert wpis.sciezki == ()


BEZ_KROKU = (
    "## L-004 — lekcja bez kroku\n"
    "zakres: ścieżki=a.md · od: 2026-01-01\n"
    "objaw: o\n"
)
BEZ_ZAKRESU = "## L-005 — lekcja bez zakresu\nobjaw: o\n"


@pytest.mark.parametrize(
    ("blok", "id_lekcji", "sciezki"),
    [
        pytest.param(BEZ_KROKU, "L-004", ("a.md",), id="zakres-bez-kroku"),
        pytest.param(BEZ_ZAKRESU, "L-005", (), id="brak-linii-zakresu"),
    ],
)
def test_lekcja_bez_kroku_ma_pusty_napis_jako_krok_a_nie_brak_wartosci(
    blok, id_lekcji, sciezki,
):
    (wpis,) = wpisy_lekcji(plik_lekcji(blok))

    assert wpis.id == id_lekcji
    assert wpis.krok == ""
    assert wpis.sciezki == sciezki


def test_tekst_pojedynczej_lekcji_to_dokladnie_jej_blok_z_pliku():
    blok = lekcja(1, "budowa", "a.md")

    (wpis,) = wpisy_lekcji(plik_lekcji(blok))

    assert wpis.tekst == blok


def test_naglowek_sekcji_z_samym_odstepem_po_hashach_konczy_tekst_poprzedniej_lekcji():
    blok = lekcja(1, "budowa", "a.md")

    (wpis,) = wpisy_lekcji(plik_lekcji(blok + "## \nnie ta lekcja\n"))

    assert wpis.tekst == blok


@pytest.mark.parametrize("plotek", ["```", "~~~"])
def test_linia_z_dopiskiem_po_plotku_nie_zamyka_bloku_kodu_z_przykladem_lekcji(plotek):
    przyklad = f"{plotek}markdown\n{plotek}X\n{lekcja(0, 'budowa', 'a.md')}{plotek}\n"

    wpisy = wpisy_lekcji(plik_lekcji(przyklad, lekcja(1, "budowa", "a.md")))

    assert [w.id for w in wpisy] == ["L-001"]


def test_wpis_lekcji_jest_niezmienny():
    (wpis,) = wpisy_lekcji(plik_lekcji(lekcja(1, "budowa", "a.md")))

    with pytest.raises(FrozenInstanceError):
        wpis.krok = "inny"  # type: ignore[misc]


def test_wpis_lekcji_ma_pola_w_kolejnosci_id_krok_sciezki_tekst():
    assert [f.name for f in fields(WpisLekcji)] == ["id", "krok", "sciezki", "tekst"]
    wpis = WpisLekcji("L-001", "budowa", ("a.md",), "tresc")
    assert (wpis.id, wpis.krok, wpis.sciezki, wpis.tekst) == (
        "L-001", "budowa", ("a.md",), "tresc")
