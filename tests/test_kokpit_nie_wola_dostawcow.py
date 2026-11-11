"""HAR-104: kokpit nie wola dostawcow ticketow - robi to wylacznie runner.

Kryterium: w `src/cockpit/` nie ma adresu API dostawcy (takze w komentarzu i docstringu)
ani importu `runner.tickety`. Skan musi umiec zawiesc, wiec kazdy ciag i kazdy ksztalt
importu ma przypadek z podlozonym plikiem, ktory skan oblewa, oraz przypadki, ktorych
skan nie moze oblac.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

KOKPIT = Path(__file__).resolve().parent.parent / "src" / "cockpit"

_ZAKAZANE_CIAGI = ("api.linear.app", "atlassian.net", "/rest/api/", "api.github.com")
_ZAKAZANY_MODUL = "runner.tickety"


def _blad_dysku(e: OSError) -> None:
    raise e


def _importy_zakazane(drzewo: ast.AST) -> list[int]:
    linie: list[int] = []
    for wezel in ast.walk(drzewo):
        if isinstance(wezel, ast.Import):
            for alias in wezel.names:
                if alias.name == _ZAKAZANY_MODUL or alias.name.startswith(_ZAKAZANY_MODUL + "."):
                    linie.append(wezel.lineno)
        elif isinstance(wezel, ast.ImportFrom) and wezel.level == 0 and wezel.module:
            if wezel.module == _ZAKAZANY_MODUL or wezel.module.startswith(_ZAKAZANY_MODUL + "."):
                linie.append(wezel.lineno)
            elif wezel.module == "runner" and any(a.name == "tickety" for a in wezel.names):
                linie.append(wezel.lineno)
    return linie


def skanuj(katalog: Path) -> list[str]:
    """Naruszenia w plikach `.py` pod `katalog`: `plik:linia: powod`, po jednym w linii.

    Plik nieczytelny albo z bledem skladni to tez naruszenie (brak dowodu to nie zielone).
    """
    wynik: list[str] = []
    for korzen, _katalogi, pliki in os.walk(katalog, onerror=_blad_dysku):
        for nazwa in sorted(pliki):
            if not nazwa.endswith(".py"):
                continue
            sciezka = Path(korzen) / nazwa
            wzgledna = sciezka.relative_to(katalog).as_posix()
            try:
                tekst = sciezka.read_text(encoding="utf-8")
                drzewo = ast.parse(tekst)
            except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
                wynik.append(f"{wzgledna}:0: plik nie do sprawdzenia")
                continue
            for numer, linia in enumerate(tekst.splitlines(), start=1):
                for ciag in _ZAKAZANE_CIAGI:
                    if ciag in linia:
                        wynik.append(f"{wzgledna}:{numer}: adres API dostawcy {ciag}")
            for numer in _importy_zakazane(drzewo):
                wynik.append(f"{wzgledna}:{numer}: import {_ZAKAZANY_MODUL}")
    return wynik


def _plik(tmp_path: Path, tresc: str, nazwa: str = "modul.py") -> Path:
    cel = tmp_path / nazwa
    cel.parent.mkdir(parents=True, exist_ok=True)
    cel.write_text(tresc, encoding="utf-8")
    return tmp_path


# =================================================================================
# Prawdziwy kod kokpitu
# =================================================================================

def test_kokpit_ma_kod_do_sprawdzenia_wiec_pusty_wynik_skanu_cos_znaczy():
    pliki = list(KOKPIT.rglob("*.py"))

    assert len(pliki) > 20


def test_w_kodzie_kokpitu_nie_ma_adresow_api_dostawcow_ani_importu_adapterow_runnera():
    assert skanuj(KOKPIT) == []


# =================================================================================
# Skan umie zawiesc: kazdy ciag i kazdy ksztalt importu
# =================================================================================

@pytest.mark.parametrize("ciag", [
    "api.linear.app", "atlassian.net", "/rest/api/", "api.github.com",
])
def test_skan_oblewa_adres_dostawcy_w_literale(tmp_path, ciag):
    katalog = _plik(tmp_path, f'ADRES = "https://x/{ciag}y"\n')

    wynik = skanuj(katalog)

    assert wynik == [f"modul.py:1: adres API dostawcy {ciag}"]


@pytest.mark.parametrize("ciag", [
    "api.linear.app", "atlassian.net", "/rest/api/", "api.github.com",
])
def test_skan_oblewa_adres_dostawcy_takze_w_komentarzu(tmp_path, ciag):
    katalog = _plik(tmp_path, f"x = 1\n# patrz {ciag}\n")

    assert skanuj(katalog) == [f"modul.py:2: adres API dostawcy {ciag}"]


@pytest.mark.parametrize("ciag", [
    "api.linear.app", "atlassian.net", "/rest/api/", "api.github.com",
])
def test_skan_oblewa_adres_dostawcy_takze_w_docstringu(tmp_path, ciag):
    katalog = _plik(tmp_path, f'"""Opis.\n\nPytamy {ciag} bezposrednio.\n"""\n')

    assert skanuj(katalog) == [f"modul.py:3: adres API dostawcy {ciag}"]


def test_skan_oblewa_adres_w_pliku_w_podkatalogu(tmp_path):
    katalog = _plik(tmp_path, 'A = "api.linear.app"\n', nazwa="glebiej/jeszcze/plik.py")

    assert skanuj(katalog) == ["glebiej/jeszcze/plik.py:1: adres API dostawcy api.linear.app"]


@pytest.mark.parametrize("zrodlo", [
    "import runner.tickety\n",
    "from runner.tickety import linear\n",
    "from runner.tickety.linear import pobierz\n",
    "from runner import tickety\n",
    "import runner.tickety.jira\n",
    "import os, runner.tickety\n",
    "from runner import odbior, tickety\n",
], ids=[
    "import_pakietu", "z_pakietu", "z_podmodulu", "z_runnera_pakiet", "import_podmodulu",
    "import_po_przecinku", "z_runnera_obok_innego",
])
def test_skan_oblewa_kazdy_ksztalt_importu_adapterow_runnera(tmp_path, zrodlo):
    katalog = _plik(tmp_path, zrodlo)

    assert skanuj(katalog) == ["modul.py:1: import runner.tickety"]


def test_skan_oblewa_import_wewnatrz_funkcji(tmp_path):
    katalog = _plik(tmp_path, "def f():\n    from runner.tickety import linear\n    return linear\n")

    assert skanuj(katalog) == ["modul.py:2: import runner.tickety"]


def test_skan_oblewa_plik_z_bledem_skladni_zamiast_uznac_go_za_czysty(tmp_path):
    katalog = _plik(tmp_path, "def (:\n")

    assert skanuj(katalog) == ["modul.py:0: plik nie do sprawdzenia"]


def test_skan_oblewa_plik_nie_w_utf8_zamiast_uznac_go_za_czysty(tmp_path):
    (tmp_path / "zly.py").write_bytes(b"# \xff\xfe\n")

    assert skanuj(tmp_path) == ["zly.py:0: plik nie do sprawdzenia"]


def test_skan_zglasza_kazde_wystapienie_osobno(tmp_path):
    katalog = _plik(
        tmp_path,
        'A = "api.github.com"\nB = 2\nimport runner.tickety\nC = "atlassian.net"\n',
    )

    assert skanuj(katalog) == [
        "modul.py:1: adres API dostawcy api.github.com",
        "modul.py:4: adres API dostawcy atlassian.net",
        "modul.py:3: import runner.tickety",
    ]


def test_skan_blad_dysku_przy_wchodzeniu_do_katalogu_nie_konczy_sie_pustym_wynikiem(tmp_path):
    brak = tmp_path / "nie-ma-takiego"

    with pytest.raises(FileNotFoundError):
        skanuj(brak)


# =================================================================================
# Skan nie oblewa tego, co jest dozwolone (lista przypadkow niezalezna od skanu)
# =================================================================================

@pytest.mark.parametrize("zrodlo", [
    'URL = "https://linear.app/harness/issue/HAR-1"\n',
    'URL = "https://github.com/LukBro/harness-cockpit/issues/1"\n',
    'URL = "https://atlassian.com/software/jira"\n',
    'SCIEZKA = "/rest/apix/3"\n',
    'SCIEZKA = "/rest/api"\n',
], ids=["adres_ticketu_linear", "adres_repo_github", "inna_domena_atlassian",
        "sciezka_bez_ukosnika_po_api", "sciezka_bez_koncowego_ukosnika"])
def test_skan_nie_oblewa_adresow_podobnych_do_zakazanych(tmp_path, zrodlo):
    katalog = _plik(tmp_path, zrodlo)

    assert skanuj(katalog) == []


@pytest.mark.parametrize("zrodlo", [
    "from runner.odbior import commituj\n",
    "from runner import odbior\n",
    "import runner.odbior\n",
    "from runner.tickety_stare import x\n",
    "import runner.tickety_stare\n",
    "from cockpit.tickety import x\n",
    "from . import tickety\n",
    "from .tickety import x\n",
], ids=["inny_modul_runnera", "inny_pakiet_z_runnera", "import_innego_modulu",
        "prefiks_nazwy_z_from", "prefiks_nazwy_z_import", "wlasny_modul_kokpitu",
        "wzgledny_pakiet", "wzgledny_modul"])
def test_skan_nie_oblewa_importow_innych_niz_adaptery_runnera(tmp_path, zrodlo):
    katalog = _plik(tmp_path, zrodlo)

    assert skanuj(katalog) == []


def test_skan_pomija_pliki_nie_python(tmp_path):
    (tmp_path / "notatka.md").write_text("api.linear.app\n", encoding="utf-8")
    (tmp_path / "front.js").write_text("fetch('https://api.github.com')\n", encoding="utf-8")

    assert skanuj(tmp_path) == []
