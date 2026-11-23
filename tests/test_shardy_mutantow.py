"""Pelny przebieg mutmuta w shardach po plikach zrodel (`tools/shardy_mutantow.py`).

Jeden job `mutation-main` przekroczyl 240 min przy 14799/15011 mutantow (run 45061686542). Shard dostaje
czesc plikow zrodel, mutuje je, a job zapadki skleja wyniki. Sprawdzamy: podzial jest kompletny, rozlaczny
i wyrownany; wyniki shardu to tylko jego mutanty i tylko sprawdzone; sklejenie odrzuca brak i powtorzenie.
"""
import sys
from pathlib import Path

import pytest

from procesy import uruchom_i_zbierz

KORZEN = Path(__file__).resolve().parents[1]
SKRYPT = KORZEN / "tools" / "shardy_mutantow.py"
sys.path.insert(0, str(KORZEN / "tools"))


@pytest.fixture
def sh():
    import shardy_mutantow

    return shardy_mutantow


def _uruchom(*argumenty: str):
    return uruchom_i_zbierz([sys.executable, str(SKRYPT), *argumenty], cwd=KORZEN, timeout=120, capture_output=True, text=True)


# --- podzial plikow ------------------------------------------------------------------------------


def test_najwiekszy_plik_idzie_do_shardu_z_najmniejsza_suma_linii(sh):
    shardy = sh.przydziel({"a.py": 100, "b.py": 60, "c.py": 50, "d.py": 10}, 2)

    # a -> 0 (100); b -> 1 (60); c -> 1 (110), bo 60 < 100; d -> 0 (110).
    assert shardy == [["a.py", "d.py"], ["b.py", "c.py"]]


def test_przy_rownej_sumie_plik_idzie_do_shardu_o_nizszym_indeksie_a_rowne_pliki_ida_alfabetycznie(sh):
    assert sh.przydziel({"b.py": 5, "a.py": 5}, 2) == [["a.py"], ["b.py"]]


def test_przydzial_nie_zalezy_od_kolejnosci_plikow_na_wejsciu(sh):
    pliki = {f"p{i}.py": (i * 37) % 11 + 1 for i in range(20)}

    assert sh.przydziel(pliki, 3) == sh.przydziel(dict(reversed(list(pliki.items()))), 3)


@pytest.mark.parametrize("liczba", [0, -1])
def test_liczba_shardow_ponizej_jednego_to_blad(sh, liczba):
    with pytest.raises(sh.BladNarzedzia, match="liczba shardow musi byc co najmniej 1"):
        sh.przydziel({"a.py": 1}, liczba)


def test_wzorce_obejmuja_funkcje_i_metody_modulu_takze_pakietu(sh):
    assert sh.wzorce(["src/cockpit/services/runs.py", "src/runner/__init__.py"]) == [
        "cockpit.services.runs.x_*",
        "cockpit.services.runs.xǁ*",
        "runner.x_*",
        "runner.xǁ*",
    ]


# --- wyniki shardu -------------------------------------------------------------------------------

RAPORT = """    cockpit.services.runs.x_a__mutmut_1: killed
    cockpit.services.runs.xǁKlasaǁm__mutmut_2: survived
    cockpit.services.x_pakiet__mutmut_1: killed
    cockpit.services.inne.x_b__mutmut_1: not checked
"""


def test_wyniki_shardu_to_tylko_mutanty_jego_modulow_nie_podmodulow_pakietu(sh):
    # `cockpit.services` (pakiet) nie wciaga mutantow `cockpit.services.runs` ani `.inne`.
    assert sh.wyniki_shardu(RAPORT, ["src/cockpit/services/__init__.py"]) == [
        "cockpit.services.x_pakiet__mutmut_1: killed"
    ]
    assert sh.wyniki_shardu(RAPORT, ["src/cockpit/services/runs.py"]) == [
        "cockpit.services.runs.x_a__mutmut_1: killed",
        "cockpit.services.runs.xǁKlasaǁm__mutmut_2: survived",
    ]


def test_niesprawdzony_mutant_shardu_to_brak_dowodu(sh):
    with pytest.raises(sh.BladDowodu, match="mutant shardu cockpit.services.inne.x_b__mutmut_1 nie zostal sprawdzony"):
        sh.wyniki_shardu(RAPORT, ["src/cockpit/services/inne.py"])


def test_niesprawdzony_mutant_innego_shardu_nie_oblewa_tego_shardu(sh):
    assert sh.wyniki_shardu(RAPORT, ["src/cockpit/services/__init__.py"])


def test_shard_bez_mutantow_w_raporcie_to_brak_dowodu(sh):
    with pytest.raises(sh.BladDowodu, match="shard nie ma zadnego mutanta"):
        sh.wyniki_shardu(RAPORT, ["src/runner/main.py"])


# --- sklejenie -----------------------------------------------------------------------------------


def _zapisz(katalog: Path, tresci: dict[int, str]) -> Path:
    katalog.mkdir()
    for indeks, tresc in tresci.items():
        (katalog / f"shard-{indeks}.txt").write_text(tresc, encoding="utf-8")
    return katalog


def test_sklejenie_daje_mutanty_wszystkich_shardow_po_kolei(sh, tmp_path):
    katalog = _zapisz(tmp_path / "w", {0: "a.x_f__mutmut_1: killed\n", 1: "b.x_g__mutmut_1: survived\n"})

    assert sh.scal(katalog, 2) == ["a.x_f__mutmut_1: killed", "b.x_g__mutmut_1: survived"]


def test_brak_pliku_shardu_to_brak_dowodu(sh, tmp_path):
    katalog = _zapisz(tmp_path / "w", {0: "a.x_f__mutmut_1: killed\n"})

    with pytest.raises(sh.BladDowodu, match="brak wynikow shardu 1"):
        sh.scal(katalog, 2)


def test_pusty_plik_shardu_to_brak_dowodu(sh, tmp_path):
    katalog = _zapisz(tmp_path / "w", {0: "a.x_f__mutmut_1: killed\n", 1: "\n"})

    with pytest.raises(sh.BladDowodu, match="pusty wynik shardu 1"):
        sh.scal(katalog, 2)


def test_mutant_w_dwoch_shardach_to_brak_dowodu(sh, tmp_path):
    katalog = _zapisz(tmp_path / "w", {0: "a.x_f__mutmut_1: killed\n", 1: "a.x_f__mutmut_1: survived\n"})

    with pytest.raises(sh.BladDowodu, match="mutant a.x_f__mutmut_1 w shardach 0 i 1"):
        sh.scal(katalog, 2)


# --- CLI na prawdziwej konfiguracji mutmuta ------------------------------------------------------


def test_shardy_razem_obejmuja_kazdy_plik_mutmuta_dokladnie_raz(sh):
    pliki = sh._pliki_mutmuta(KORZEN)
    shardy = sh.przydziel(pliki, 8)
    wszystkie = [p for shard in shardy for p in shard]

    assert sorted(wszystkie) == sorted(pliki)
    assert len(wszystkie) == len(set(wszystkie))
    assert "src/cockpit/services/runs.py" in pliki


def test_cli_wzorce_wypisuje_wzorce_plikow_shardu(sh):
    wynik = _uruchom("wzorce", "--indeks", "0", "--liczba", "8")

    assert wynik.returncode == 0, wynik.stderr
    pliki = sh.przydziel(sh._pliki_mutmuta(KORZEN), 8)[0]
    assert wynik.stdout.splitlines() == sh.wzorce(pliki)


def test_cli_shard_bez_plikow_to_kod_2(sh):
    liczba = len(sh._pliki_mutmuta(KORZEN)) + 1
    wynik = _uruchom("wzorce", "--indeks", str(liczba - 1), "--liczba", str(liczba))

    assert wynik.returncode == 2
    assert f"blad: brak dowodu: shard {liczba - 1} z {liczba} nie dostal zadnego pliku zrodel" in wynik.stderr


def test_cli_indeks_poza_zakresem_to_kod_2():
    wynik = _uruchom("wzorce", "--indeks", "8", "--liczba", "8")

    assert wynik.returncode == 2
    assert "blad: indeks shardu 8 poza zakresem 0..7" in wynik.stderr


def test_cli_wyniki_z_nieczytelnym_raportem_to_kod_2(tmp_path):
    wynik = _uruchom("wyniki", "--indeks", "0", "--liczba", "8", "--raport", str(tmp_path / "brak.txt"))

    assert wynik.returncode == 2
    assert "nie da sie odczytac raportu" in wynik.stderr


def test_cli_scal_wypisuje_sklejone_wyniki_a_brak_shardu_to_kod_2(tmp_path):
    katalog = _zapisz(tmp_path / "w", {0: "a.x_f__mutmut_1: killed\n", 1: "b.x_g__mutmut_1: killed\n"})

    dobry = _uruchom("scal", "--liczba", "2", "--katalog", str(katalog))
    zly = _uruchom("scal", "--liczba", "3", "--katalog", str(katalog))

    assert dobry.returncode == 0, dobry.stderr
    assert dobry.stdout == "a.x_f__mutmut_1: killed\nb.x_g__mutmut_1: killed\n"
    assert zly.returncode == 2
    assert "brak wynikow shardu 2" in zly.stderr
