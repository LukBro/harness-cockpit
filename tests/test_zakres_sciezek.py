"""Twardy zakres sciezek runnera (HAR-29, BRO-93, BRO-98). Nazwy w jezyku dziedziny."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cockpit.services.errors import BladDziedziny
from cockpit.services.skills import ZRODLA
from runner.zakres import PozaZakresem, Zakres

LINIA_ODMOWY = re.compile(r"^(\S+) runner zakres: odmowa powod=(\S+) sciezka=(.*)$")


@pytest.fixture
def swiat(tmp_path, monkeypatch):
    """Projekt, obcy katalog i dom uzytkownika - wszystko rozwiazane (macOS: /tmp to dowiazanie)."""
    baza = tmp_path.resolve()
    projekt = baza / "repo"
    obcy = baza / "obcy"
    dom = baza / "dom"
    for k in (projekt, obcy, dom):
        k.mkdir()
    monkeypatch.setenv("HOME", str(dom))
    monkeypatch.delenv("COCKPIT_RUNNER_CONFIG", raising=False)
    return SimpleNamespace(baza=baza, projekt=projekt, obcy=obcy, dom=dom, zakres=Zakres([projekt]))


def _powod(zakres, sciezka, **kw):
    with pytest.raises(PozaZakresem) as e:
        zakres.rozwiaz(sciezka, **kw)
    return e.value.powod


def _konfig(plik: Path, tresc) -> Path:
    plik.parent.mkdir(parents=True, exist_ok=True)
    plik.write_text(tresc if isinstance(tresc, str) else json.dumps(tresc), encoding="utf-8")
    return plik


def _podkatalogi_rejestru_domowego():
    return [p for (baza, p, _) in ZRODLA if baza == "dom"]


def _podkatalogi_tylko_repo():
    dom = set(_podkatalogi_rejestru_domowego())
    return [p for (baza, p, _) in ZRODLA if baza == "repo" and p not in dom]


# --- podstawowy zakres ---------------------------------------------------------------

def test_plik_w_projekcie_jest_dozwolony(swiat):
    plik = swiat.projekt / "src" / "main.py"
    plik.parent.mkdir()
    plik.write_text("x")
    assert swiat.zakres.rozwiaz(plik) == plik


def test_nieistniejacy_plik_w_projekcie_jest_dozwolony_do_odczytu(swiat):
    assert swiat.zakres.rozwiaz(swiat.projekt / "jeszcze-nie-ma.txt") == swiat.projekt / "jeszcze-nie-ma.txt"


def test_zapis_nowego_pliku_w_projekcie_jest_dozwolony(swiat):
    cel = swiat.projekt / "nowy.txt"
    assert swiat.zakres.rozwiaz(cel, do_zapisu=True) == cel


def test_zapis_zwyklego_pliku_obok_katalogu_git_jest_dozwolony(swiat):
    (swiat.projekt / ".git").mkdir()
    cel = swiat.projekt / ".gitignore"
    assert swiat.zakres.rozwiaz(cel, do_zapisu=True) == cel


def test_sciezka_z_kropkami_wracajaca_do_projektu_jest_dozwolona(swiat):
    (swiat.projekt / "a").mkdir()
    sciezka = f"{swiat.projekt}/a/../plik.txt"
    assert swiat.zakres.rozwiaz(sciezka) == swiat.projekt / "plik.txt"


def test_katalog_projektu_podany_przez_dowiazanie_jest_zapamietany_jako_rzeczywisty(swiat):
    link = swiat.baza / "skrot"
    link.symlink_to(swiat.projekt)
    zakres = Zakres([link])
    assert zakres.korzenie == (swiat.projekt,)
    assert zakres.rozwiaz(swiat.projekt / "a.txt") == swiat.projekt / "a.txt"


def test_wyjscie_dwiema_kropkami_poza_projekt_jest_odmowione(swiat):
    (swiat.obcy / "plik.txt").write_text("x")
    sciezka = f"{swiat.projekt}/../obcy/plik.txt"
    assert _powod(swiat.zakres, sciezka) == "poza_zakresem"


def test_wyjscie_dwiema_kropkami_ze_srodka_podkatalogu_jest_odmowione(swiat):
    (swiat.projekt / "sub").mkdir()
    sciezka = f"{swiat.projekt}/sub/../../obcy/plik.txt"
    assert _powod(swiat.zakres, sciezka) == "poza_zakresem"


def test_zapis_z_wyjsciem_dwiema_kropkami_poza_projekt_jest_odmowiony(swiat):
    (swiat.projekt / "sub").mkdir()
    sciezka = f"{swiat.projekt}/sub/../../obcy/plik.txt"
    assert _powod(swiat.zakres, sciezka, do_zapisu=True) == "poza_zakresem"


def test_klucz_ssh_w_katalogu_domowym_jest_odmowiony(swiat):
    assert _powod(swiat.zakres, Path.home() / ".ssh" / "id_rsa") == "poza_zakresem"


def test_plik_systemowy_etc_passwd_jest_odmowiony(swiat):
    assert _powod(swiat.zakres, "/etc/passwd") == "poza_zakresem"


def test_katalog_o_wspolnym_prefiksie_nazwy_nie_nalezy_do_zakresu(swiat):
    zly = swiat.baza / "repo-zly"
    zly.mkdir()
    (zly / "plik").write_text("x")
    assert str(zly / "plik").startswith(str(swiat.projekt))
    assert _powod(swiat.zakres, zly / "plik") == "poza_zakresem"


def test_zapis_w_katalogu_o_wspolnym_prefiksie_nazwy_jest_odmowiony(swiat):
    zly = swiat.baza / "repo-zly"
    zly.mkdir()
    assert _powod(swiat.zakres, zly / "plik", do_zapisu=True) == "poza_zakresem"


def test_sam_korzen_projektu_jest_w_zakresie_do_odczytu(swiat):
    assert swiat.zakres.rozwiaz(swiat.projekt) == swiat.projekt


def test_nul_w_sciezce_jest_odmowiony_z_powodem_nul(swiat):
    assert _powod(swiat.zakres, f"{swiat.projekt}/plik\0.txt") == "nul"


def test_sciezka_wzgledna_jest_odmowiona_nawet_gdy_wskazuje_w_projekt(swiat, monkeypatch):
    (swiat.projekt / "plik.txt").write_text("x")
    monkeypatch.chdir(swiat.projekt)
    assert _powod(swiat.zakres, "plik.txt") == "wzgledna"


def test_odmowa_niesie_zadana_sciezke_bez_zmian_i_jest_bledem_dziedziny(swiat):
    zadana = f"{swiat.projekt}/../obcy/x"
    with pytest.raises(PozaZakresem) as e:
        swiat.zakres.rozwiaz(zadana)
    assert isinstance(e.value, BladDziedziny)
    assert e.value.sciezka == zadana
    assert e.value.powod == "poza_zakresem"


def test_zakres_z_wieloma_projektami_dopuszcza_pliki_z_kazdego(swiat):
    drugi = swiat.baza / "drugi"
    drugi.mkdir()
    zakres = Zakres([swiat.projekt, drugi])
    assert zakres.rozwiaz(drugi / "a.txt") == drugi / "a.txt"
    assert zakres.rozwiaz(swiat.projekt / "b.txt") == swiat.projekt / "b.txt"
    assert _powod(zakres, swiat.obcy / "c.txt") == "poza_zakresem"


# --- dowiazania ----------------------------------------------------------------------

def test_dowiazanie_w_projekcie_wskazujace_poza_zakres_jest_odmowione_do_odczytu(swiat):
    (swiat.obcy / "tajne.txt").write_text("x")
    link = swiat.projekt / "skrot.txt"
    link.symlink_to(swiat.obcy / "tajne.txt")
    assert _powod(swiat.zakres, link) == "poza_zakresem"


def test_dowiazanie_w_projekcie_wskazujace_poza_zakres_jest_odmowione_do_zapisu(swiat):
    (swiat.obcy / "tajne.txt").write_text("x")
    link = swiat.projekt / "skrot.txt"
    link.symlink_to(swiat.obcy / "tajne.txt")
    assert _powod(swiat.zakres, link, do_zapisu=True) == "dowiazanie"


def test_zapis_przez_dowiazanie_katalogu_nadrzednego_poza_zakres_jest_odmowiony(swiat):
    link = swiat.projekt / "wyjscie"
    link.symlink_to(swiat.obcy, target_is_directory=True)
    assert _powod(swiat.zakres, link / "nowy.txt", do_zapisu=True) == "poza_zakresem"
    assert not (swiat.obcy / "nowy.txt").exists()


def test_odczyt_przez_dowiazanie_katalogu_poza_zakres_jest_odmowiony(swiat):
    (swiat.obcy / "a.txt").write_text("x")
    link = swiat.projekt / "wyjscie"
    link.symlink_to(swiat.obcy, target_is_directory=True)
    assert _powod(swiat.zakres, link / "a.txt") == "poza_zakresem"


def test_zapis_przez_wiszace_dowiazanie_do_miejsca_w_zakresie_jest_odmowiony(swiat):
    # cel w zakresie, wiec tylko regula "dowiazanie" decyduje
    link = swiat.projekt / "wiszace.txt"
    link.symlink_to(swiat.projekt / "nie-ma-takiego.txt")
    assert not link.exists()
    assert _powod(swiat.zakres, link, do_zapisu=True) == "dowiazanie"


def test_zapis_przez_dowiazanie_wskazujace_w_obrebie_zakresu_jest_odmowiony(swiat):
    (swiat.projekt / "prawdziwy.txt").write_text("x")
    link = swiat.projekt / "alias.txt"
    link.symlink_to(swiat.projekt / "prawdziwy.txt")
    assert _powod(swiat.zakres, link, do_zapisu=True) == "dowiazanie"


def test_odczyt_przez_dowiazanie_wskazujace_w_obrebie_zakresu_jest_dozwolony(swiat):
    (swiat.projekt / "prawdziwy.txt").write_text("x")
    link = swiat.projekt / "alias.txt"
    link.symlink_to(swiat.projekt / "prawdziwy.txt")
    assert swiat.zakres.rozwiaz(link) == swiat.projekt / "prawdziwy.txt"


# --- zapis: nie plik -----------------------------------------------------------------

def test_zapis_do_sciezki_konczacej_sie_dwiema_kropkami_jest_odmowiony_jako_nie_plik(swiat):
    # projekt/sub/.. wskazuje projekt (w zakresie), wiec tylko regula "nie_plik" odmawia
    (swiat.projekt / "sub").mkdir()
    assert _powod(swiat.zakres, f"{swiat.projekt}/sub/..", do_zapisu=True) == "nie_plik"


def test_zapis_do_korzenia_systemu_jest_odmowiony_jako_nie_plik_nawet_gdy_korzen_jest_w_zakresie(swiat):
    # konstruktor nie odrzuca "/", wiec rodzic i cel sa w zakresie - odmawia wylacznie pusta nazwa
    assert _powod(Zakres([Path("/")]), "/", do_zapisu=True) == "nie_plik"


# --- zapis do .git (no_git_write) ----------------------------------------------------

def test_zapis_do_katalogu_git_jest_odmowiony_z_powodem_no_git_write(swiat):
    assert _powod(swiat.zakres, swiat.projekt / ".git" / "HEAD", do_zapisu=True) == "no_git_write"


def test_zapis_do_katalogu_git_wielkimi_literami_jest_odmowiony(swiat):
    assert _powod(swiat.zakres, swiat.projekt / ".GIT" / "HEAD", do_zapisu=True) == "no_git_write"


def test_zapis_gleboko_w_katalogu_git_jest_odmowiony(swiat):
    cel = swiat.projekt / ".git" / "hooks" / "pre-commit"
    assert _powod(swiat.zakres, cel, do_zapisu=True) == "no_git_write"


def test_zapis_do_katalogu_git_w_podkatalogu_projektu_jest_odmowiony(swiat):
    cel = swiat.projekt / "modul" / ".git" / "HEAD"
    assert _powod(swiat.zakres, cel, do_zapisu=True) == "no_git_write"


def test_zapis_do_katalogu_git_w_drugim_projekcie_tez_jest_odmowiony(swiat):
    drugi = swiat.baza / "drugi"
    drugi.mkdir()
    zakres = Zakres([swiat.projekt, drugi])
    assert _powod(zakres, drugi / ".git" / "HEAD", do_zapisu=True) == "no_git_write"


def test_zapis_do_katalogu_git_w_katalogu_rejestru_domowego_tez_jest_odmowiony(swiat, tmp_path):
    plik = _konfig(tmp_path / "cfg.json", {"projekty": [str(swiat.projekt)]})
    zakres = Zakres.z_konfiguracji(plik)
    podkatalog = _podkatalogi_rejestru_domowego()[0]
    cel = swiat.dom / podkatalog / ".git" / "HEAD"
    assert _powod(zakres, cel, do_zapisu=True) == "no_git_write"


def test_zapis_w_projekcie_lezacym_pod_katalogiem_o_nazwie_git_jest_dozwolony(swiat):
    # komponent .git NAD korzeniem projektu nie jest katalogiem repozytorium tego projektu
    korzen = swiat.baza / "baza" / ".git" / "repo"
    korzen.mkdir(parents=True)
    zakres = Zakres([korzen])
    cel = korzen / "plik.txt"
    assert zakres.rozwiaz(cel, do_zapisu=True) == cel


def test_zapis_do_git_w_projekcie_lezacym_pod_katalogiem_o_nazwie_git_jest_nadal_odmowiony(swiat):
    korzen = swiat.baza / "baza" / ".git" / "repo"
    korzen.mkdir(parents=True)
    assert _powod(Zakres([korzen]), korzen / ".git" / "HEAD", do_zapisu=True) == "no_git_write"


def test_odczyt_pliku_z_katalogu_git_jest_dozwolony(swiat):
    cel = swiat.projekt / ".git" / "config"
    assert swiat.zakres.rozwiaz(cel) == cel


def test_zapis_do_katalogu_git_ma_pierwszenstwo_przed_sekretem(swiat):
    assert _powod(swiat.zakres, swiat.projekt / ".git" / ".env", do_zapisu=True) == "no_git_write"


# --- sekrety (odczyt i zapis) --------------------------------------------------------

SEKRETY = [
    pytest.param(".env", id="env"),
    pytest.param(".env.local", id="env-kropka-gwiazdka"),
    pytest.param("klucz.pem", id="pem"),
    pytest.param("serwer.key", id="key"),
    pytest.param("id_rsa_backup", id="id_rsa"),
    pytest.param("id_ed25519_backup", id="id_ed25519"),
    pytest.param(".ENV", id="env-wielkie-litery"),
    pytest.param("KLUCZ.PEM", id="pem-wielkie-litery"),
    pytest.param("Id_RSA", id="id_rsa-wielkie-litery"),
]


@pytest.mark.parametrize("nazwa", SEKRETY)
def test_odczyt_pliku_sekretow_w_projekcie_jest_odmowiony(swiat, nazwa):
    assert _powod(swiat.zakres, swiat.projekt / nazwa) == "sekret"


@pytest.mark.parametrize("nazwa", SEKRETY)
def test_zapis_pliku_sekretow_w_projekcie_jest_odmowiony(swiat, nazwa):
    assert _powod(swiat.zakres, swiat.projekt / nazwa, do_zapisu=True) == "sekret"


@pytest.mark.parametrize("nazwa", ["id_rsa.pub", "id_ed25519.pub"])
def test_klucz_publiczny_o_nazwie_z_prefiksem_klucza_prywatnego_jest_odmowiony(swiat, nazwa):
    assert _powod(swiat.zakres, swiat.projekt / nazwa) == "sekret"


def test_sekret_w_podkatalogu_projektu_jest_odmowiony(swiat):
    assert _powod(swiat.zakres, swiat.projekt / "config" / "prod" / ".env") == "sekret"


def test_sekret_osiagniety_przez_dowiazanie_o_niewinnej_nazwie_jest_odmowiony(swiat):
    (swiat.projekt / ".env").write_text("HASLO=1")
    link = swiat.projekt / "zwykla.txt"
    link.symlink_to(swiat.projekt / ".env")
    assert _powod(swiat.zakres, link) == "sekret"


def test_sekret_podany_nazwa_zadana_jest_odmowiony_mimo_niewinnego_celu_dowiazania(swiat):
    # tylko nazwa ZADANA jest sekretem; cel (zwykly.txt) nim nie jest
    (swiat.projekt / "zwykly.txt").write_text("x")
    link = swiat.projekt / ".env"
    link.symlink_to(swiat.projekt / "zwykly.txt")
    assert _powod(swiat.zakres, link) == "sekret"


@pytest.mark.parametrize(
    "nazwa",
    ["envrc", ".envrc", ".environment", "klucz.txt", "keyboard.txt", "grid_rsa", "pem.txt", "env.py", "README.md"],
)
def test_zwykly_plik_o_nazwie_podobnej_do_sekretu_jest_dozwolony(swiat, nazwa):
    assert swiat.zakres.rozwiaz(swiat.projekt / nazwa) == swiat.projekt / nazwa
    assert swiat.zakres.rozwiaz(swiat.projekt / nazwa, do_zapisu=True) == swiat.projekt / nazwa


# --- konfiguracja --------------------------------------------------------------------

def test_brak_pliku_konfiguracji_odmawia_kazdej_sciezki(swiat, tmp_path):
    zakres = Zakres.z_konfiguracji(tmp_path / "nie-ma.json")
    assert zakres.korzenie == ()
    assert _powod(zakres, swiat.projekt / "a.txt") == "poza_zakresem"
    assert _powod(zakres, "/etc/passwd") == "poza_zakresem"


def test_brak_pliku_konfiguracji_odmawia_tez_rejestru_skilli_w_domu(swiat, tmp_path):
    zakres = Zakres.z_konfiguracji(tmp_path / "nie-ma.json")
    podkatalog = _podkatalogi_rejestru_domowego()[0]
    assert _powod(zakres, swiat.dom / podkatalog / "SKILL.md") == "poza_zakresem"


def test_konfiguracja_z_wieloma_projektami_dopuszcza_kazdy_z_nich(swiat, tmp_path):
    drugi = swiat.baza / "drugi"
    drugi.mkdir()
    plik = _konfig(tmp_path / "cfg.json", {"projekty": [str(swiat.projekt), str(drugi)]})
    zakres = Zakres.z_konfiguracji(plik)
    assert zakres.rozwiaz(swiat.projekt / "a") == swiat.projekt / "a"
    assert zakres.rozwiaz(drugi / "b") == drugi / "b"
    assert _powod(zakres, swiat.obcy / "c") == "poza_zakresem"


def test_konfiguracja_dolacza_katalogi_rejestru_skilli_z_domu(swiat, tmp_path):
    plik = _konfig(tmp_path / "cfg.json", {"projekty": [str(swiat.projekt)]})
    zakres = Zakres.z_konfiguracji(plik)
    for podkatalog in _podkatalogi_rejestru_domowego():
        cel = swiat.dom / podkatalog / "skill" / "SKILL.md"
        assert zakres.rozwiaz(cel) == cel


def test_katalogi_skilli_z_repozytorium_nie_tworza_korzeni_poza_projektami(swiat, tmp_path):
    plik = _konfig(tmp_path / "cfg.json", {"projekty": [str(swiat.projekt)]})
    zakres = Zakres.z_konfiguracji(plik)
    tylko_repo = _podkatalogi_tylko_repo()
    assert tylko_repo, "ZRODLA ma miec wpis repo, ktorego nie ma w domu"
    for podkatalog in tylko_repo:
        assert _powod(zakres, swiat.dom / podkatalog / "SKILL.md") == "poza_zakresem"


def test_rejestr_domowy_nie_otwiera_calego_katalogu_domowego(swiat, tmp_path):
    plik = _konfig(tmp_path / "cfg.json", {"projekty": [str(swiat.projekt)]})
    zakres = Zakres.z_konfiguracji(plik)
    assert _powod(zakres, swiat.dom / "dokumenty" / "notatka.txt") == "poza_zakresem"


def test_sciezka_konfiguracji_ze_zmiennej_srodowiska_jest_czytana_przy_wywolaniu(swiat, tmp_path, monkeypatch):
    plik = _konfig(tmp_path / "gdzies" / "inny.json", {"projekty": [str(swiat.projekt)]})
    monkeypatch.setenv("COCKPIT_RUNNER_CONFIG", str(plik))
    zakres = Zakres.z_konfiguracji()
    assert swiat.projekt in zakres.korzenie
    assert zakres.rozwiaz(swiat.projekt / "a") == swiat.projekt / "a"


def test_zmienna_srodowiska_wygrywa_z_domyslnym_plikiem_konfiguracji(swiat, tmp_path, monkeypatch):
    drugi = swiat.baza / "drugi"
    drugi.mkdir()
    _konfig(swiat.dom / ".config" / "cockpit" / "runner.json", {"projekty": [str(drugi)]})
    plik = _konfig(tmp_path / "inny.json", {"projekty": [str(swiat.projekt)]})
    monkeypatch.setenv("COCKPIT_RUNNER_CONFIG", str(plik))
    zakres = Zakres.z_konfiguracji()
    assert swiat.projekt in zakres.korzenie
    assert drugi not in zakres.korzenie


def test_domyslny_plik_konfiguracji_lezy_w_katalogu_domowym_uzytkownika(swiat):
    _konfig(swiat.dom / ".config" / "cockpit" / "runner.json", {"projekty": [str(swiat.projekt)]})
    zakres = Zakres.z_konfiguracji()
    assert swiat.projekt in zakres.korzenie
    assert zakres.rozwiaz(swiat.projekt / "a") == swiat.projekt / "a"


def test_domyslny_plik_konfiguracji_jest_liczony_przy_wywolaniu_z_aktualnego_domu(swiat, tmp_path, monkeypatch):
    drugi_dom = tmp_path.resolve() / "dom2"
    _konfig(drugi_dom / ".config" / "cockpit" / "runner.json", {"projekty": [str(swiat.obcy)]})
    monkeypatch.setenv("HOME", str(drugi_dom))
    zakres = Zakres.z_konfiguracji()
    assert swiat.obcy in zakres.korzenie
    assert swiat.projekt not in zakres.korzenie


def test_brak_domyslnego_pliku_konfiguracji_daje_pusty_zakres(swiat):
    assert Zakres.z_konfiguracji().korzenie == ()


# --- konfiguracja uszkodzona: fail-closed --------------------------------------------

def _uszkodzone(tmp_path):
    zly_utf8 = tmp_path / "utf8.json"
    zly_utf8.write_bytes(b"\xff\xfe\x00{")
    katalog = tmp_path / "katalog.json"
    katalog.mkdir()
    glebokie = tmp_path / "glebokie.json"
    glebokie.write_text("[" * 200000)
    return {
        "zly-json": _konfig(tmp_path / "zly.json", "{projekty: nie json"),
        "zly-utf8": zly_utf8,
        "katalog-zamiast-pliku": katalog,
        "zbyt-glebokie-zagniezdzenie": glebokie,
        "korzen-nie-jest-obiektem": _konfig(tmp_path / "lista.json", ["/tmp"]),
        "projekty-nie-sa-lista": _konfig(tmp_path / "napis.json", {"projekty": "/tmp"}),
        "projekty-sa-obiektem": _konfig(tmp_path / "obiekt.json", {"projekty": {"a": "/tmp"}}),
    }


@pytest.mark.parametrize(
    "rodzaj",
    ["zly-json", "zly-utf8", "katalog-zamiast-pliku", "zbyt-glebokie-zagniezdzenie",
     "korzen-nie-jest-obiektem", "projekty-nie-sa-lista", "projekty-sa-obiektem"],
)
def test_uszkodzona_konfiguracja_daje_pusty_zakres_i_jedna_linie_ostrzezenia(swiat, tmp_path, capsys, rodzaj):
    plik = _uszkodzone(tmp_path)[rodzaj]
    capsys.readouterr()
    zakres = Zakres.z_konfiguracji(plik)
    err = capsys.readouterr().err
    assert len([wiersz for wiersz in err.splitlines() if wiersz.strip()]) == 1
    assert zakres.korzenie == ()
    assert _powod(zakres, swiat.projekt / "a") == "poza_zakresem"
    podkatalog = _podkatalogi_rejestru_domowego()[0]
    assert _powod(zakres, swiat.dom / podkatalog / "SKILL.md") == "poza_zakresem"


def test_wpis_wzgledny_w_projektach_jest_pominiety_a_pozostale_dzialaja(swiat, tmp_path, capsys):
    plik = _konfig(tmp_path / "cfg.json", {"projekty": ["wzgledny/katalog", str(swiat.projekt)]})
    zakres = Zakres.z_konfiguracji(plik)
    err = capsys.readouterr().err
    assert zakres.rozwiaz(swiat.projekt / "a") == swiat.projekt / "a"
    assert Path("wzgledny/katalog").resolve() not in zakres.korzenie
    assert "wzgledny/katalog" in err


def test_wpis_korzenia_systemu_jest_pominiety_i_nie_otwiera_calego_dysku(swiat, tmp_path, capsys):
    plik = _konfig(tmp_path / "cfg.json", {"projekty": ["/", str(swiat.projekt)]})
    zakres = Zakres.z_konfiguracji(plik)
    assert Path("/") not in zakres.korzenie
    assert _powod(zakres, "/etc/passwd") == "poza_zakresem"
    assert zakres.rozwiaz(swiat.projekt / "a") == swiat.projekt / "a"
    assert capsys.readouterr().err.strip() != ""


def test_wpis_wskazujacy_dowiazaniem_na_korzen_systemu_jest_pominiety(swiat, tmp_path):
    link = swiat.baza / "do-korzenia"
    link.symlink_to("/", target_is_directory=True)
    plik = _konfig(tmp_path / "cfg.json", {"projekty": [str(link), str(swiat.projekt)]})
    zakres = Zakres.z_konfiguracji(plik)
    assert _powod(zakres, "/etc/passwd") == "poza_zakresem"


def test_wpis_ktory_nie_jest_napisem_jest_pominiety_a_pozostale_dzialaja(swiat, tmp_path, capsys):
    plik = _konfig(tmp_path / "cfg.json", {"projekty": [5, None, [str(swiat.obcy)], str(swiat.projekt)]})
    zakres = Zakres.z_konfiguracji(plik)
    assert zakres.rozwiaz(swiat.projekt / "a") == swiat.projekt / "a"
    assert _powod(zakres, swiat.obcy / "a") == "poza_zakresem"
    assert capsys.readouterr().err.strip() != ""


def test_wpis_z_nul_w_projektach_jest_pominiety_a_pozostale_dzialaja(swiat, tmp_path, capsys):
    plik = _konfig(tmp_path / "cfg.json", {"projekty": ["/tmp/a\u0000b", str(swiat.projekt)]})
    zakres = Zakres.z_konfiguracji(plik)
    assert zakres.rozwiaz(swiat.projekt / "a") == swiat.projekt / "a"
    assert capsys.readouterr().err.strip() != ""


def test_wpis_pominiety_nie_odbiera_zakresu_rejestru_skilli(swiat, tmp_path):
    plik = _konfig(tmp_path / "cfg.json", {"projekty": ["wzgledny"]})
    zakres = Zakres.z_konfiguracji(plik)
    podkatalog = _podkatalogi_rejestru_domowego()[0]
    cel = swiat.dom / podkatalog / "SKILL.md"
    assert zakres.rozwiaz(cel) == cel


# --- bledy resolve(): fail-closed ----------------------------------------------------

BLEDY_RESOLVE = [
    pytest.param(OSError, id="OSError"),
    pytest.param(RuntimeError, id="RuntimeError"),
    pytest.param(ValueError, id="ValueError"),
]


def _resolve_rzuca_dla(monkeypatch, cel: Path, wyjatek):
    """Path.resolve rzuca `wyjatek` tylko dla `cel`, pozostale sciezki rozwiazuje naprawde."""
    oryginal = Path.resolve

    def owijka(self, *a, **kw):
        if self == cel:
            raise wyjatek("sztuczny blad resolve")
        return oryginal(self, *a, **kw)

    monkeypatch.setattr(Path, "resolve", owijka)


@pytest.mark.parametrize("wyjatek", [OSError, RuntimeError])
def test_wpis_projektow_ktorego_nie_da_sie_rozwiazac_jest_pominiety_z_linia_na_stderr(
    swiat, tmp_path, capsys, monkeypatch, wyjatek
):
    zepsuty = swiat.baza / "zepsuty"
    plik = _konfig(tmp_path / "cfg.json", {"projekty": [str(zepsuty), str(swiat.projekt)]})
    _resolve_rzuca_dla(monkeypatch, zepsuty, wyjatek)
    zakres = Zakres.z_konfiguracji(plik)
    err = capsys.readouterr().err
    assert zakres.rozwiaz(swiat.projekt / "a") == swiat.projekt / "a"
    assert zepsuty not in zakres.korzenie
    assert "nie da sie rozwiazac" in err
    assert str(zepsuty) in err


@pytest.mark.parametrize("wyjatek", BLEDY_RESOLVE)
def test_odczyt_sciezki_ktorej_nie_da_sie_rozwiazac_jest_odmowiony_jako_poza_zakresem(
    swiat, capsys, monkeypatch, wyjatek
):
    cel = swiat.projekt / "petla.txt"  # w zakresie - gdyby resolve zadzialal, bylaby dozwolona
    _resolve_rzuca_dla(monkeypatch, cel, wyjatek)
    assert _powod(swiat.zakres, cel) == "poza_zakresem"
    linie = capsys.readouterr().err.splitlines()
    assert len(linie) == 1
    assert LINIA_ODMOWY.match(linie[0]).group(2) == "poza_zakresem"


@pytest.mark.parametrize("wyjatek", BLEDY_RESOLVE)
def test_zapis_gdy_nie_da_sie_rozwiazac_katalogu_nadrzednego_jest_odmowiony(swiat, capsys, monkeypatch, wyjatek):
    rodzic = swiat.projekt / "petla"
    _resolve_rzuca_dla(monkeypatch, rodzic, wyjatek)
    assert _powod(swiat.zakres, rodzic / "plik.txt", do_zapisu=True) == "poza_zakresem"
    linie = capsys.readouterr().err.splitlines()
    assert len(linie) == 1
    assert LINIA_ODMOWY.match(linie[0]).group(2) == "poza_zakresem"


@pytest.mark.parametrize("wyjatek", BLEDY_RESOLVE)
def test_zapis_gdy_nie_da_sie_rozwiazac_samego_celu_jest_odmowiony(swiat, capsys, monkeypatch, wyjatek):
    cel = swiat.projekt / "petla.txt"  # rodzic (projekt) rozwiazuje sie poprawnie
    _resolve_rzuca_dla(monkeypatch, cel, wyjatek)
    assert _powod(swiat.zakres, cel, do_zapisu=True) == "poza_zakresem"
    linie = capsys.readouterr().err.splitlines()
    assert len(linie) == 1
    assert LINIA_ODMOWY.match(linie[0]).group(2) == "poza_zakresem"


# --- log odmow -----------------------------------------------------------------------

def test_odmowa_zostawia_w_logu_runnera_jedna_linie_z_powodem_i_sciezka(swiat, capsys):
    zadana = swiat.obcy / "plik.txt"
    assert _powod(swiat.zakres, zadana) == "poza_zakresem"
    linie = capsys.readouterr().err.splitlines()
    assert len(linie) == 1
    dopasowanie = LINIA_ODMOWY.match(linie[0])
    assert dopasowanie, linie[0]
    assert dopasowanie.group(2) == "poza_zakresem"
    assert dopasowanie.group(3) == repr(str(zadana))


def test_odmowa_w_logu_ma_znacznik_czasu_iso_8601_w_utc(swiat, capsys):
    _powod(swiat.zakres, "/etc/passwd")
    linia = capsys.readouterr().err.splitlines()[0]
    znacznik = LINIA_ODMOWY.match(linia).group(1)
    czas = datetime.fromisoformat(znacznik)
    assert czas.tzinfo is not None
    assert czas.utcoffset() == timedelta(0)


def test_odmowa_w_logu_podaje_powod_sekret_dla_pliku_sekretow(swiat, capsys):
    _powod(swiat.zakres, swiat.projekt / ".env")
    linia = capsys.readouterr().err.splitlines()[0]
    assert LINIA_ODMOWY.match(linia).group(2) == "sekret"


def test_odmowa_w_logu_podaje_powod_nul_dla_sciezki_z_nul(swiat, capsys):
    _powod(swiat.zakres, f"{swiat.projekt}/a\0b")
    linia = capsys.readouterr().err.splitlines()[0]
    assert LINIA_ODMOWY.match(linia).group(2) == "nul"


def test_odmowa_w_logu_podaje_powod_no_git_write_dla_zapisu_do_katalogu_git(swiat, capsys):
    _powod(swiat.zakres, swiat.projekt / ".git" / "HEAD", do_zapisu=True)
    linia = capsys.readouterr().err.splitlines()[0]
    assert LINIA_ODMOWY.match(linia).group(2) == "no_git_write"


def test_odmowa_w_logu_podaje_zadana_sciezke_a_nie_rozwiazany_cel_dowiazania(swiat, capsys):
    (swiat.obcy / "x.txt").write_text("x")
    link = swiat.projekt / "skrot.txt"
    link.symlink_to(swiat.obcy / "x.txt")
    _powod(swiat.zakres, link)
    linia = capsys.readouterr().err.splitlines()[0]
    assert LINIA_ODMOWY.match(linia).group(3) == repr(str(link))


def test_sciezka_ze_znakiem_nowej_linii_daje_w_logu_dokladnie_jedna_linie(swiat, capsys):
    zadana = "/etc/a\nb\rc"
    _powod(swiat.zakres, zadana)
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "\r" not in err
    assert LINIA_ODMOWY.match(err.rstrip("\n")).group(3) == repr(zadana)


def test_odmowa_nie_wypisuje_tresci_pliku_ani_w_logu_ani_w_bledzie(swiat, capsys):
    tajne = swiat.obcy / "notatki.txt"
    tajne.write_text("TAJNA-TRESC-12345")
    with pytest.raises(PozaZakresem) as e:
        swiat.zakres.rozwiaz(tajne)
    assert "TAJNA-TRESC-12345" not in capsys.readouterr().err
    assert "TAJNA-TRESC-12345" not in str(e.value)


def test_odmowa_sekretu_w_projekcie_nie_wypisuje_jego_tresci(swiat, capsys):
    (swiat.projekt / ".env").write_text("HASLO=TAJNA-TRESC-12345")
    _powod(swiat.zakres, swiat.projekt / ".env")
    assert "TAJNA-TRESC-12345" not in capsys.readouterr().err


def test_dozwolona_sciezka_nie_zostawia_wpisu_w_logu(swiat, capsys):
    swiat.zakres.rozwiaz(swiat.projekt / "a.txt")
    swiat.zakres.rozwiaz(swiat.projekt / "b.txt", do_zapisu=True)
    assert capsys.readouterr().err == ""


def test_kazda_odmowa_zostawia_osobny_wpis_w_logu(swiat, capsys):
    _powod(swiat.zakres, "/etc/passwd")
    _powod(swiat.zakres, swiat.projekt / ".env")
    linie = capsys.readouterr().err.splitlines()
    assert [LINIA_ODMOWY.match(wiersz).group(2) for wiersz in linie] == ["poza_zakresem", "sekret"]
