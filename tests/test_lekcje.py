"""Walidator `.ai/lessons.md` (HAR-87): lekcja bez zakresu nie przechodzi walidacji."""
import dataclasses
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest
from procesy import uruchom_proces_sync, zakoncz_proces_sync

from cockpit.api.bledy import KODY_BLEDOW
from cockpit.services.errors import BladDziedziny, BlednyPlikLekcji
from cockpit.services.lekcje import ProblemLekcji, waliduj_lekcje, wczytaj_lekcje

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from check_lessons import LESSONS, sprawdz  # noqa: E402

KORZEN = Path(__file__).resolve().parents[1]
NARZEDZIE = KORZEN / "tools" / "check_lessons.py"
LIMIT = 60

POPRAWNY_ZAKRES = "zakres: krok=mutacje · ścieżki=pyproject.toml · od: 2026-01-01"
WPIS_POPRAWNY = f"## L-001 — tytul\n{POPRAWNY_ZAKRES}\nobjaw: x\n"


def _problemy(tekst: str) -> list[tuple[str, int, str]]:
    return [(p.wpis, p.wiersz, p.kod) for p in waliduj_lekcje(tekst)]


def _bledny(wpis: str, wiersz: int) -> ProblemLekcji:
    return ProblemLekcji(
        wpis, wiersz, "bledny_naglowek", "nagłówek wpisu nie ma numeru w formacie L-NNN (trzy cyfry)"
    )


def _wpis_z_zakresem(zakres: str) -> str:
    return f"## L-001 — tytul\n{zakres}\nobjaw: x\n"


def _uruchom_narzedzie(*argumenty: str) -> tuple[int, str, str]:
    proc = uruchom_proces_sync(
        [sys.executable, str(NARZEDZIE), *argumenty],
        cwd=KORZEN,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = proc.communicate(timeout=LIMIT)
    finally:
        zakoncz_proces_sync(proc, nazwa="check_lessons")
    return proc.returncode, out, err


# --- brak zakresu ---------------------------------------------------------------


def test_lekcja_bez_zakresu_nie_przechodzi_walidacji():
    tekst = "# Pułapki\n\nwstep\n\n## L-001 — tytul\nobjaw: x\nprzyczyna: y\n"
    assert _problemy(tekst) == [("L-001", 5, "brak_zakresu")]


def test_problem_wskazuje_wpis_i_wiersz_naglowka_wlasciwej_lekcji():
    tekst = f"{WPIS_POPRAWNY}\n## L-002 — drugi\nobjaw: x\n"
    assert _problemy(tekst) == [("L-002", 5, "brak_zakresu")]


def test_problem_niesie_komunikat_dla_czlowieka():
    (problem,) = waliduj_lekcje("## L-007 — x\nobjaw: x\n")
    assert isinstance(problem, ProblemLekcji)
    assert problem.komunikat.strip() != ""


def test_wpis_poprawny_nie_daje_problemow():
    assert waliduj_lekcje(WPIS_POPRAWNY) == []


def test_plik_z_samym_naglowkiem_nie_daje_problemow():
    assert waliduj_lekcje("# Pułapki\n") == []


def test_dwa_wpisy_bez_zakresu_daja_dwa_problemy_w_kolejnosci_pliku():
    tekst = "## L-001 — a\nobjaw: x\n## L-002 — b\nobjaw: y\n"
    assert _problemy(tekst) == [("L-001", 1, "brak_zakresu"), ("L-002", 3, "brak_zakresu")]


def test_wpis_bez_zakresu_nie_dostaje_zakresu_od_nastepnego_wpisu():
    tekst = f"## L-001 — a\nobjaw: x\n{WPIS_POPRAWNY.replace('L-001', 'L-002')}"
    assert _problemy(tekst) == [("L-001", 1, "brak_zakresu")]


# --- pola zakresu: kazde osobno -------------------------------------------------


def test_zakres_bez_kroku_daje_brak_kroku():
    tekst = _wpis_z_zakresem("zakres: ścieżki=pyproject.toml · od: 2026-01-01")
    assert _problemy(tekst) == [("L-001", 2, "brak_kroku")]


def test_zakres_z_pustym_krokiem_daje_brak_kroku():
    tekst = _wpis_z_zakresem("zakres: krok= · ścieżki=pyproject.toml · od: 2026-01-01")
    assert _problemy(tekst) == [("L-001", 2, "brak_kroku")]


def test_zakres_bez_sciezek_daje_brak_sciezek():
    tekst = _wpis_z_zakresem("zakres: krok=mutacje · od: 2026-01-01")
    assert _problemy(tekst) == [("L-001", 2, "brak_sciezek")]


def test_zakres_z_pustymi_sciezkami_daje_brak_sciezek():
    tekst = _wpis_z_zakresem("zakres: krok=mutacje · ścieżki= · od: 2026-01-01")
    assert _problemy(tekst) == [("L-001", 2, "brak_sciezek")]


def test_zakres_bez_daty_od_daje_brak_daty_od():
    tekst = _wpis_z_zakresem("zakres: krok=mutacje · ścieżki=pyproject.toml")
    assert _problemy(tekst) == [("L-001", 2, "brak_daty_od")]


def test_zakres_z_pusta_data_od_daje_brak_daty_od():
    tekst = _wpis_z_zakresem("zakres: krok=mutacje · ścieżki=pyproject.toml · od:")
    assert _problemy(tekst) == [("L-001", 2, "brak_daty_od")]


@pytest.mark.parametrize("data", ["2026-13-01", "1.01.2026", "jutro", "2026-1-01", "2026-02-30"])
def test_zakres_ze_zla_data_od_daje_bledna_data_od(data):
    tekst = _wpis_z_zakresem(f"zakres: krok=mutacje · ścieżki=pyproject.toml · od: {data}")
    assert _problemy(tekst) == [("L-001", 2, "bledna_data_od")]


def test_data_od_z_dopiskiem_po_dacie_jest_bledna():
    tekst = _wpis_z_zakresem("zakres: krok=mutacje · ścieżki=pyproject.toml · od: 2026-01-01x")
    assert _problemy(tekst) == [("L-001", 2, "bledna_data_od")]


def test_data_od_bez_spacji_po_dwukropku_jest_poprawna():
    tekst = _wpis_z_zakresem("zakres: krok=mutacje · ścieżki=pyproject.toml · od:2026-01-01")
    assert waliduj_lekcje(tekst) == []


def test_zakres_bez_spacji_wokol_kropki_srodkowej_jest_poprawny():
    tekst = _wpis_z_zakresem("zakres: krok=mutacje·ścieżki=pyproject.toml·od: 2026-01-01")
    assert waliduj_lekcje(tekst) == []


def test_zakres_bez_zadnego_pola_daje_trzy_problemy_w_ustalonej_kolejnosci():
    tekst = _wpis_z_zakresem("zakres:")
    assert _problemy(tekst) == [
        ("L-001", 2, "brak_kroku"),
        ("L-001", 2, "brak_sciezek"),
        ("L-001", 2, "brak_daty_od"),
    ]


def test_zakres_bez_kroku_i_z_zla_data_daje_oba_problemy_w_kolejnosci():
    tekst = _wpis_z_zakresem("zakres: ścieżki=a · od: jutro")
    assert _problemy(tekst) == [("L-001", 2, "brak_kroku"), ("L-001", 2, "bledna_data_od")]


def test_nazwa_sciezki_w_rozlozonym_unikodzie_jest_rozpoznana():
    nazwa = unicodedata.normalize("NFD", "ścieżki")
    assert nazwa != "ścieżki"
    tekst = _wpis_z_zakresem(f"zakres: krok=mutacje · {nazwa}=pyproject.toml · od: 2026-01-01")
    assert waliduj_lekcje(tekst) == []


def test_wiersz_problemu_zakresu_to_wiersz_linii_zakres_nie_naglowka():
    tekst = "## L-001 — a\n\nuwaga\nzakres: krok=x · od: 2026-01-01\n"
    assert _problemy(tekst) == [("L-001", 4, "brak_sciezek")]


def test_pierwsza_linia_zakresu_w_wpisie_decyduje_a_druga_jest_ignorowana():
    tekst = (
        "## L-001 — a\n"
        "zakres: krok=x · ścieżki=y · od: 2026-01-01\n"
        "zakres: krok=\n"
    )
    assert waliduj_lekcje(tekst) == []


# --- granice wpisow i bloki kodu --------------------------------------------------


@pytest.mark.parametrize("plot", ["```", "~~~"])
def test_naglowek_wpisu_w_bloku_kodu_nie_jest_wpisem(plot):
    tekst = f"# Pułapki\n\n{plot}markdown\n## L-000 — przyklad\nobjaw: x\n{plot}\n"
    assert waliduj_lekcje(tekst) == []


@pytest.mark.parametrize("plot", ["```", "~~~"])
def test_zakres_w_bloku_kodu_wewnatrz_wpisu_nie_liczy_sie_jako_zakres(plot):
    tekst = f"## L-001 — a\n{plot}\n{POPRAWNY_ZAKRES}\n{plot}\nobjaw: x\n"
    assert _problemy(tekst) == [("L-001", 1, "brak_zakresu")]


@pytest.mark.parametrize("plot", ["```", "~~~"])
def test_naglowek_w_bloku_kodu_nie_konczy_wpisu(plot):
    tekst = f"## L-001 — a\n{plot}\n## Cos\n{plot}\n{POPRAWNY_ZAKRES}\n"
    assert waliduj_lekcje(tekst) == []


def test_zakres_po_zamknieciu_bloku_kodu_liczy_sie():
    tekst = f"## L-001 — a\n```\nkod\n```\n{POPRAWNY_ZAKRES}\n"
    assert waliduj_lekcje(tekst) == []


def _brak_zakresu_l001(wiersz: int) -> ProblemLekcji:
    return ProblemLekcji("L-001", wiersz, "brak_zakresu", "wpis nie ma linii „zakres: …”")


def test_tylda_wewnatrz_bloku_z_backtickami_nie_zamyka_bloku():
    tekst = "```bash\n~~~\n## L-001 — a\nobjaw: x\n~~~\n```\n"
    assert waliduj_lekcje(tekst) == []


def test_backticki_wewnatrz_bloku_z_tyldami_nie_zamykaja_bloku():
    tekst = "~~~\n```\n## L-001 — a\nobjaw: x\n```\n~~~\n"
    assert waliduj_lekcje(tekst) == []


def test_tylda_wewnatrz_bloku_z_backtickami_nie_przeszkadza_w_zamknieciu_backtickami():
    tekst = "```bash\n~~~\n```\n## L-001 — a\nobjaw: x\n"
    assert waliduj_lekcje(tekst) == [_brak_zakresu_l001(4)]


def test_krotszy_plotek_wewnatrz_dluzszego_bloku_nie_zamyka_bloku():
    tekst = "````\n```\n## L-001 — a\nobjaw: x\n````\n"
    assert waliduj_lekcje(tekst) == []


def test_dluzszy_plotek_zamyka_krotszy_blok():
    tekst = "````\nkod\n`````\n## L-001 — a\nobjaw: x\n"
    assert waliduj_lekcje(tekst) == [_brak_zakresu_l001(4)]


def test_plotek_z_dopiskiem_nie_zamyka_bloku():
    tekst = "```\n```x\n## L-001 — a\nobjaw: x\n```\n"
    assert waliduj_lekcje(tekst) == []


def test_plotek_zamykajacy_z_koncowymi_spacjami_zamyka_blok():
    tekst = "```\nkod\n```   \n## L-001 — a\nobjaw: x\n"
    assert waliduj_lekcje(tekst) == [_brak_zakresu_l001(4)]


def test_dluzszy_plotek_z_tyld_zamyka_blok_z_trzech_tyld():
    tekst = "~~~\nkod\n~~~~\n## L-001 — a\nobjaw: x\n"
    assert waliduj_lekcje(tekst) == [_brak_zakresu_l001(4)]


def test_dwa_backticki_nie_otwieraja_bloku():
    tekst = "``\n## L-001 — a\nobjaw: x\n"
    assert waliduj_lekcje(tekst) == [_brak_zakresu_l001(2)]


def test_dwie_tyldy_nie_otwieraja_bloku():
    tekst = "~~\n## L-001 — a\nobjaw: x\n"
    assert waliduj_lekcje(tekst) == [_brak_zakresu_l001(2)]


def test_dwa_backticki_wewnatrz_bloku_nie_zamykaja_go():
    tekst = "```\n``\n## L-001 — a\nobjaw: x\n"
    assert waliduj_lekcje(tekst) == []


def test_niezamkniety_blok_kodu_na_koncu_pliku_ukrywa_naglowek_wpisu_za_nim():
    tekst = "```\nkod\n## L-001 — a\nobjaw: x\n"
    assert waliduj_lekcje(tekst) == []


def test_naglowek_innej_sekcji_konczy_wpis_i_zakres_po_nim_nie_nalezy_do_wpisu():
    tekst = f"## L-001 — a\nobjaw: x\n## Inna sekcja\n{POPRAWNY_ZAKRES}\n"
    assert _problemy(tekst) == [("L-001", 1, "brak_zakresu")]


def test_naglowek_lekcji_z_dwiema_cyframi_to_bledny_naglowek():
    assert waliduj_lekcje("# Pułapki\n\n## L-12 — t\nobjaw: x\n") == [_bledny("L-12", 3)]


def test_naglowek_lekcji_z_czterema_cyframi_to_bledny_naglowek():
    assert waliduj_lekcje("# Pułapki\n\n## L-0012 — t\nobjaw: x\n") == [_bledny("L-0012", 3)]


def test_naglowek_lekcji_z_jedna_cyfra_to_bledny_naglowek():
    assert waliduj_lekcje("## L-1 — t\nobjaw: x\n") == [_bledny("L-1", 1)]


def test_naglowek_lekcji_z_litera_tuz_po_numerze_to_bledny_naglowek_a_nie_wpis_l_001():
    assert waliduj_lekcje("## L-001x — t\nobjaw: x\n") == [_bledny("L-001x", 1)]


def test_bledny_naglowek_konczy_poprzedni_wpis_wiec_zakres_pod_nim_nie_nalezy_do_poprzedniego():
    tekst = f"## L-001 — a\nobjaw: x\n## L-12 — b\n{POPRAWNY_ZAKRES}\n"
    assert waliduj_lekcje(tekst) == [
        ProblemLekcji("L-001", 1, "brak_zakresu", "wpis nie ma linii „zakres: …”"),
        _bledny("L-12", 3),
    ]


def test_pod_blednym_naglowkiem_bez_zakresu_jest_tylko_bledny_naglowek():
    assert waliduj_lekcje("## L-12 — t\nobjaw: x\n") == [_bledny("L-12", 1)]


def test_pod_blednym_naglowkiem_zly_zakres_nie_daje_problemow_zakresu():
    assert waliduj_lekcje("## L-12 — t\nzakres: krok=\n") == [_bledny("L-12", 1)]


def test_bledne_naglowki_sa_zglaszane_w_kolejnosci_naglowkow_w_pliku():
    tekst = "## L-2 — a\n## L-001 — b\nobjaw: x\n## L-0003 — c\n"
    assert waliduj_lekcje(tekst) == [
        _bledny("L-2", 1),
        ProblemLekcji("L-001", 2, "brak_zakresu", "wpis nie ma linii „zakres: …”"),
        _bledny("L-0003", 4),
    ]


@pytest.mark.parametrize("plot", ["```", "~~~"])
def test_bledny_naglowek_w_bloku_kodu_nie_daje_problemu(plot):
    assert waliduj_lekcje(f"{plot}\n## L-12 — t\n{plot}\n") == []


def test_naglowek_trzeciego_poziomu_z_blednym_numerem_nie_daje_problemu():
    assert waliduj_lekcje("### L-12 — t\nobjaw: x\n") == []


@pytest.mark.parametrize("naglowek", ["## Lekcje", "## L", "## L — t"])
def test_naglowek_sekcji_zaczynajacy_sie_od_l_bez_myslnika_nie_jest_blednym_naglowkiem(naglowek):
    assert waliduj_lekcje(f"{naglowek}\nobjaw: x\n") == []


def test_naglowek_trzeciego_poziomu_nie_jest_wpisem():
    assert waliduj_lekcje("### L-001 — a\nobjaw: x\n") == []


def test_naglowek_trzeciego_poziomu_nie_konczy_wpisu():
    tekst = f"## L-001 — a\n### Szczegoly\n{POPRAWNY_ZAKRES}\n"
    assert waliduj_lekcje(tekst) == []


def test_konce_linii_windows_nie_psuja_walidacji_poprawnego_wpisu():
    assert waliduj_lekcje(WPIS_POPRAWNY.replace("\n", "\r\n")) == []


def test_konce_linii_windows_zachowuja_numer_wiersza_problemu():
    tekst = "# Pułapki\r\n\r\n## L-001 — a\r\nobjaw: x\r\n"
    assert _problemy(tekst) == [("L-001", 3, "brak_zakresu")]


def test_konce_linii_windows_nie_wchodza_do_wartosci_daty():
    tekst = _wpis_z_zakresem("zakres: krok=a · ścieżki=b · od: 2026-01-01").replace("\n", "\r\n")
    assert waliduj_lekcje(tekst) == []


# --- wczytywanie pliku ------------------------------------------------------------


def test_wczytanie_poprawnego_pliku_nie_daje_problemow(tmp_path):
    plik = tmp_path / "lessons.md"
    plik.write_bytes(WPIS_POPRAWNY.encode("utf-8"))
    assert wczytaj_lekcje(plik) == []


def test_wczytanie_pliku_zwraca_problemy_z_numerem_wpisu(tmp_path):
    plik = tmp_path / "lessons.md"
    plik.write_text("## L-004 — a\nobjaw: x\n", encoding="utf-8")
    assert [(p.wpis, p.wiersz, p.kod) for p in wczytaj_lekcje(plik)] == [
        ("L-004", 1, "brak_zakresu")
    ]


def test_brak_pliku_lekcji_to_blad_dziedziny_z_przyczyna_systemowa(tmp_path):
    plik = tmp_path / "nie-ma.md"
    with pytest.raises(BlednyPlikLekcji, match="nie-ma.md") as info:
        wczytaj_lekcje(plik)
    assert isinstance(info.value.__cause__, FileNotFoundError)


def test_katalog_zamiast_pliku_lekcji_to_blad_dziedziny_z_przyczyna_systemowa(tmp_path):
    with pytest.raises(BlednyPlikLekcji, match=tmp_path.name) as info:
        wczytaj_lekcje(tmp_path)
    assert isinstance(info.value.__cause__, OSError)
    assert not isinstance(info.value.__cause__, UnicodeDecodeError)


def test_plik_lekcji_nie_w_utf8_to_blad_dziedziny_z_nazwa_kodowania(tmp_path):
    plik = tmp_path / "zepsuty.md"
    plik.write_bytes(b"\xff\xfe## L-001")
    with pytest.raises(BlednyPlikLekcji, match="UTF-8") as info:
        wczytaj_lekcje(plik)
    assert isinstance(info.value.__cause__, UnicodeDecodeError)
    assert "zepsuty.md" in str(info.value)


# --- narzedzie tools/check_lessons.py ---------------------------------------------


def test_domyslny_plik_narzedzia_to_lessons_md_w_korzeniu_repozytorium():
    assert Path(LESSONS) == KORZEN / ".ai" / "lessons.md"


def test_prawdziwy_plik_lekcji_przechodzi_walidacje():
    assert sprawdz() == []


def test_raport_wskazuje_plik_wiersz_i_numer_wpisu(tmp_path):
    plik = tmp_path / "lessons.md"
    plik.write_text("# Pułapki\n\n## L-005 — a\nobjaw: x\n", encoding="utf-8")
    (linia,) = sprawdz(plik)
    assert linia.startswith(f"{plik}:3: L-005: ")


def test_raport_czystego_pliku_jest_pusty(tmp_path):
    plik = tmp_path / "lessons.md"
    plik.write_text(WPIS_POPRAWNY, encoding="utf-8")
    assert sprawdz(plik) == []


def test_sprawdzenie_nieistniejacego_pliku_przepuszcza_blad_dziedziny(tmp_path):
    with pytest.raises(BlednyPlikLekcji):
        sprawdz(tmp_path / "nie-ma.md")


def test_narzedzie_na_prawdziwym_pliku_lekcji_konczy_sie_kodem_zero_z_komunikatem():
    kod, out, err = _uruchom_narzedzie()
    assert kod == 0, err
    assert "w porządku" in out


def test_narzedzie_na_wpisie_bez_zakresu_konczy_sie_kodem_jeden_i_wskazuje_wpis(tmp_path):
    plik = tmp_path / "lessons.md"
    plik.write_text("# Pułapki\n\n## L-003 — a\nobjaw: x\n", encoding="utf-8")
    kod, out, _ = _uruchom_narzedzie(str(plik))
    assert kod == 1
    assert "L-003" in out
    assert ":3:" in out
    assert "w porządku" not in out


def test_narzedzie_na_blednym_naglowku_wpisu_konczy_sie_kodem_jeden_i_wskazuje_wpis(tmp_path):
    plik = tmp_path / "lessons.md"
    plik.write_text("## L-12 — t\nobjaw: x\n", encoding="utf-8")
    kod, out, _ = _uruchom_narzedzie(str(plik))
    assert kod == 1
    assert "L-12" in out
    assert "w porządku" not in out


def test_narzedzie_na_poprawnym_pliku_konczy_sie_kodem_zero(tmp_path):
    plik = tmp_path / "lessons.md"
    plik.write_text(WPIS_POPRAWNY, encoding="utf-8")
    kod, out, _ = _uruchom_narzedzie(str(plik))
    assert kod == 0
    assert "w porządku" in out


def test_narzedzie_na_pliku_nie_w_utf8_konczy_sie_kodem_dwa_i_pisze_na_stderr(tmp_path):
    plik = tmp_path / "zepsuty.md"
    plik.write_bytes(b"\xff\xfe## L-001")
    kod, out, err = _uruchom_narzedzie(str(plik))
    assert kod == 2
    assert "UTF-8" in err
    assert "w porządku" not in out


def test_narzedzie_na_brakujacym_pliku_konczy_sie_kodem_dwa(tmp_path):
    plik = tmp_path / "nie-ma.md"
    kod, out, err = _uruchom_narzedzie(str(plik))
    assert kod == 2
    assert "nie-ma.md" in err
    assert "w porządku" not in out


# --- dokladne komunikaty dla czlowieka ---------------------------------------------


def test_komunikat_braku_zakresu_wskazuje_brakujaca_linie():
    (problem,) = waliduj_lekcje("## L-001 — a\nobjaw: x\n")
    assert problem.kod == "brak_zakresu"
    assert problem.komunikat == "wpis nie ma linii „zakres: …”"


def test_komunikat_braku_kroku_podaje_jak_go_zapisac():
    (problem,) = waliduj_lekcje(_wpis_z_zakresem("zakres: ścieżki=a · od: 2026-01-01"))
    assert problem.kod == "brak_kroku"
    assert problem.komunikat == "zakres nie podaje kroku (krok=…)"


def test_komunikat_braku_sciezek_podaje_jak_je_zapisac():
    (problem,) = waliduj_lekcje(_wpis_z_zakresem("zakres: krok=a · od: 2026-01-01"))
    assert problem.kod == "brak_sciezek"
    assert problem.komunikat == "zakres nie podaje ścieżek (ścieżki=…)"


def test_komunikat_braku_daty_podaje_format_daty():
    (problem,) = waliduj_lekcje(_wpis_z_zakresem("zakres: krok=a · ścieżki=b"))
    assert problem.kod == "brak_daty_od"
    assert problem.komunikat == "zakres nie podaje daty (od: RRRR-MM-DD)"


def test_komunikat_blednej_daty_cytuje_wpisana_wartosc():
    (problem,) = waliduj_lekcje(_wpis_z_zakresem("zakres: krok=a · ścieżki=b · od: jutro"))
    assert problem.kod == "bledna_data_od"
    assert problem.komunikat == "data „jutro” nie jest datą RRRR-MM-DD"


# --- granice wpisow i pola ze znakiem rownosci -------------------------------------


def test_naglowek_innej_sekcji_przed_wpisem_nie_chowa_wpisu_bez_zakresu():
    tekst = "# Pułapki\n\n## Wpisy\n\n## L-001 — a\nobjaw: x\n"
    assert _problemy(tekst) == [("L-001", 5, "brak_zakresu")]


def test_wartosc_sciezki_ze_znakiem_rownosci_jest_poprawna():
    tekst = _wpis_z_zakresem("zakres: krok=mutacje · ścieżki=docs/a=b.md · od: 2026-01-01")
    assert waliduj_lekcje(tekst) == []


def test_wartosc_kroku_ze_znakiem_rownosci_jest_poprawna():
    tekst = _wpis_z_zakresem("zakres: krok=x=y · ścieżki=a · od: 2026-01-01")
    assert waliduj_lekcje(tekst) == []


# --- elementy poza zasiegiem mutmuta: stale, dataclass, kod bledu ------------------


def test_cyfry_spoza_ascii_w_dacie_od_to_bledna_data():
    tekst = _wpis_z_zakresem("zakres: krok=a · ścieżki=b · od: ٢٠٢٦-٠١-٠١")
    assert _problemy(tekst) == [("L-001", 2, "bledna_data_od")]


def test_problem_lekcji_jest_niezmienny():
    problem = ProblemLekcji("L-001", 1, "brak_zakresu", "x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        problem.kod = "inny"  # type: ignore[misc]


def test_kody_problemow_maja_dokladne_wartosci_tekstowe():
    assert [p.kod for p in waliduj_lekcje(_wpis_z_zakresem("zakres:"))] == [
        "brak_kroku",
        "brak_sciezek",
        "brak_daty_od",
    ]
    assert [p.kod for p in waliduj_lekcje("## L-001 — a\n")] == ["brak_zakresu"]
    assert [p.kod for p in waliduj_lekcje("## L-12 — a\n")] == ["bledny_naglowek"]
    assert [p.kod for p in waliduj_lekcje(_wpis_z_zakresem("zakres: krok=a · ścieżki=b · od: x"))] == [
        "bledna_data_od"
    ]


def test_zly_plik_lekcji_to_blad_dziedziny_z_kodem_http_422():
    assert issubclass(BlednyPlikLekcji, BladDziedziny)
    assert KODY_BLEDOW[BlednyPlikLekcji] == 422
