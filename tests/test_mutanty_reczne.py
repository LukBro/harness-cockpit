"""Wykonywalna tabela recznych mutantow (HAR-150): `tools/mutanty_reczne.py`.

Skrypt czyta `tests/mutanty_reczne/<ticket>.toml`, nakłada każdą mutację w NOWEJ kopii
repozytorium (katalog tymczasowy) i sprawdza, że wskazany test oblewa. Kody: 0 wszystkie
mutanty zabite, 1 przeżyły (lista `plik: szukaj → zamien`), 2 błąd (brak dowodu).

Testy używają prawdziwego kodu skryptu. Prawdziwy pytest i git działają wyłącznie w
mini-repozytoriach w `tmp_path`; zaślepka `uruchom_test` jest użyta tylko tam, gdzie badamy
parametry wiersza poleceń (limit czasu), nie wynik testu.
"""
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import types
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from procesy import (
    LimitCzasuTestu,
    czy_zyje,
    uruchom_proces_sync,
    zabij_proces,
    zakoncz_proces_sync,
)

KORZEN = Path(__file__).resolve().parents[1]
SKRYPT = KORZEN / "tools" / "mutanty_reczne.py"
sys.path.insert(0, str(KORZEN / "tools"))
import mutanty_reczne  # noqa: E402

BladNarzedzia = mutanty_reczne.BladNarzedzia
Mutant = mutanty_reczne.Mutant

# --- zegar limitu od zdarzenia, nie od startu procesu (HAR-226) ---------------
# Limit w `tools/mutanty_reczne.py` liczy `communicate(timeout=...)` od chwili `Popen`. Pod
# obciazeniem rownoleglych sesji start procesu potomka (fork/exec, import Pythona) zjada ten
# limit, zanim potomek powstanie. Testy limitu podmieniaja wiec `Popen` na podklase, ktora
# przed PIERWSZYM `communicate` czeka na zdarzenie "potomek zapisal PID"; dalej idzie prawdziwe
# `Popen.communicate(timeout)` i prawdziwe `os.killpg` z `_uruchom_proces`.
# Zabezpieczenie przed zawieszeniem na zdarzeniu (nie czas, ktorego test oczekuje):
LIMIT_ZDARZENIA = 120.0
# Czas na zniknieciu potomka po zabiciu grupy: ZAWSZE krotszy niz 60 s snu potomka, inaczej
# mutant bez `os.killpg` przezylby, bo potomek skonczylby sie sam.
LIMIT_ZNIKNIECIA_POTOMKA = 30.0


def _czekaj_synchronicznie(warunek: Callable[[], object], limit: float, nazwa: str) -> None:
    koniec = time.monotonic() + limit
    while not warunek():
        if time.monotonic() >= koniec:
            raise LimitCzasuTestu(f"{nazwa}: przekroczono limit {limit} s")
        time.sleep(0.05)


def _pid_zapisany(plik: Path) -> bool:
    try:
        return plik.read_text(encoding="utf-8").strip() != ""
    except OSError:
        return False


def _podmien_popen_na_zegar_od_potomka(monkeypatch, plik_pid: Path) -> dict[str, float]:
    """`Popen` widziany przez `tools/mutanty_reczne.py` czeka przed pierwszym `communicate`
    na niepusty `plik_pid`. Zwraca slownik, w ktorym po tym zdarzeniu jest klucz `zdarzenie`
    (`time.monotonic()` chwili, gdy PID potomka byl juz zapisany)."""
    zapis: dict[str, float] = {}

    class PopenZegarOdPotomka(subprocess.Popen):
        czekano = False

        def communicate(self, input=None, timeout=None):
            if not self.czekano:
                self.czekano = True
                _czekaj_synchronicznie(
                    lambda: _pid_zapisany(plik_pid) or self.poll() is not None,
                    LIMIT_ZDARZENIA,
                    f"potomek nie zapisal PID {plik_pid.name}",
                )
                zapis["zdarzenie"] = time.monotonic()
            return super().communicate(input, timeout)

    # Podmiana tylko w module narzedzia (kopia przestrzeni nazw), nie globalnego `subprocess`.
    podmieniony = types.SimpleNamespace(**vars(subprocess))
    podmieniony.Popen = PopenZegarOdPotomka
    monkeypatch.setattr(mutanty_reczne, "subprocess", podmieniony)
    return zapis

# --- dane mini-repozytorium -------------------------------------------------

PYPROJECT_MINI = """\
[tool.pytest.ini_options]
addopts = "-m 'not wolny'"
markers = ["wolny: pominiety domyslnie"]
"""
MODUL = """\
def dodaj(a, b):
    return a + b


def odejmij(a, b):
    return a - b
"""
TEST_MODUL = """\
from modul import dodaj, odejmij


def test_dodawanie():
    assert dodaj(2, 3) == 5


def test_odejmowanie():
    assert odejmij(5, 2) == 3


def test_czerwony_bez_mutacji():
    assert False
"""
TEST_DODAWANIE = "tests/test_modul.py::test_dodawanie"
TEST_ODEJMOWANIE = "tests/test_modul.py::test_odejmowanie"
TEST_CZERWONY = "tests/test_modul.py::test_czerwony_bez_mutacji"
PLIK_MODULU = "src/modul.py"
TABELA_W_REPO = "tests/mutanty_reczne/HAR-0.toml"

# Linia, ktorej zmiana zabija test_dodawanie, i dwie zmiany, ktorych test nie widzi.
SZUKAJ = "return a + b"
ZABIJA = "return a - b"
PRZEZYWA_A = "return b + a"
PRZEZYWA_B = "return (a + b)"


# --- pomocnicy tabel --------------------------------------------------------


def _toml(wartosc) -> str:
    if isinstance(wartosc, bool):
        return "true" if wartosc else "false"
    if isinstance(wartosc, int):
        return str(wartosc)
    if isinstance(wartosc, list):
        return "[" + ", ".join(_toml(x) for x in wartosc) + "]"
    return json.dumps(wartosc, ensure_ascii=False)


def _wpis(**pola) -> str:
    return "[[mutant]]\n" + "".join(f"{k} = {_toml(v)}\n" for k, v in pola.items()) + "\n"


def _pola(**zmiany) -> dict:
    pola = {
        "plik": "src/a.py",
        "szukaj": "x = 1",
        "zamien": "x = 2",
        "test": "tests/test_a.py::test_x",
    }
    pola.update(zmiany)
    return pola


def _mutant(szukaj: str, zamien: str, test: str = TEST_DODAWANIE, plik: str = PLIK_MODULU) -> str:
    return _wpis(plik=plik, szukaj=szukaj, zamien=zamien, test=test)


def _zapisz_tabele(tmp_path: Path, tresc: str, nazwa: str = "tabela.toml") -> Path:
    sciezka = tmp_path / nazwa
    sciezka.write_text(tresc, encoding="utf-8")
    return sciezka


# --- pomocnicy mini-repozytorium --------------------------------------------


def _git(katalog: Path, *argv: str) -> str:
    polecenie = [
        "git",
        "-c", "core.hooksPath=/dev/null",
        "-c", "commit.gpgsign=false",
        "-c", "user.name=LukBro",
        "-c", "user.email=lukaszbrowar@gmail.com",
        *argv,
    ]  # fmt: skip
    proces = uruchom_proces_sync(
        polecenie, cwd=katalog, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        wyjscie, blad = proces.communicate(timeout=30)
    finally:
        zakoncz_proces_sync(proces, nazwa="git w mini-repozytorium")
    assert proces.returncode == 0, blad
    return wyjscie


def _repo(tmp_path: Path, tabela: str | None = None, nazwa: str = "repo") -> Path:
    """Mini-repozytorium z jednym commitem: modul, jego testy i (opcjonalnie) tabela."""
    repo = tmp_path / nazwa
    (repo / "src").mkdir(parents=True)
    (repo / "tests" / "mutanty_reczne").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(PYPROJECT_MINI, encoding="utf-8")
    (repo / ".gitignore").write_text("ignorowany.txt\n", encoding="utf-8")
    (repo / PLIK_MODULU).write_text(MODUL, encoding="utf-8")
    (repo / "tests" / "test_modul.py").write_text(TEST_MODUL, encoding="utf-8")
    if tabela is not None:
        (repo / TABELA_W_REPO).write_text(tabela, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "start")
    return repo


def _migawka(repo: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(repo)): p.read_bytes()
        for p in sorted(repo.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(repo).parts
    }


def _kopia_testowa(katalog: Path, testy: dict[str, str], modul: str = "WARTOSC = 1\n") -> Path:
    """Katalog roboczy dla `uruchom_test` (bez gita): pyproject, src/modul.py, tests/*.py."""
    (katalog / "src").mkdir(parents=True)
    (katalog / "tests").mkdir()
    (katalog / "pyproject.toml").write_text(PYPROJECT_MINI, encoding="utf-8")
    (katalog / "src" / "modul.py").write_text(modul, encoding="utf-8")
    for nazwa, tresc in testy.items():
        (katalog / "tests" / nazwa).write_text(tresc, encoding="utf-8")
    return katalog


def _main(argv: list[str]) -> int:
    """`main`, ktory przy bledzie argparse (SystemExit) zwraca jego kod."""
    try:
        return mutanty_reczne.main(argv)
    except SystemExit as wyjscie:
        return int(wyjscie.code)


def _uruchom(repo: Path, *tabele: str, limit: str = "60") -> int:
    return mutanty_reczne.main([*tabele, "--korzen", str(repo), "--limit", limit])


def _podstawka_testu(monkeypatch) -> list[tuple[Path, str, float]]:
    """Zaslepka `uruchom_test`: 1 (oblany) gdy w kopii jest mutacja `a * b`, inaczej 0.
    Uzywana tylko do badania parametrow wiersza polecen, nie wyniku prawdziwego testu."""
    wywolania: list[tuple[Path, str, float]] = []

    def uruchom_test(kopia, test, limit):
        wywolania.append((kopia, test, limit))
        tresc = (kopia / PLIK_MODULU).read_text(encoding="utf-8")
        return 1 if "a * b" in tresc else 0

    monkeypatch.setattr(mutanty_reczne, "uruchom_test", uruchom_test)
    return wywolania


TABELA_ZABITY = _mutant(SZUKAJ, ZABIJA)
TABELA_PRZEZYWA = _mutant(SZUKAJ, PRZEZYWA_A)
TABELA_DO_PODSTAWKI = _mutant(SZUKAJ, "return a * b")


# =============================================================================
# wczytaj_tabele: walidacja tabeli
# =============================================================================


def test_poprawna_tabela_daje_mutanty_w_kolejnosci_wpisow(tmp_path):
    tabela = _zapisz_tabele(
        tmp_path,
        _wpis(plik="src/a.py", szukaj="x → 1", zamien="x = 2", test="tests/t.py::test_a")
        + _wpis(plik="web/b.ts", szukaj="a", zamien="b", test="tests/t.py::test_b"),
    )

    assert mutanty_reczne.wczytaj_tabele(tabela) == [
        Mutant("src/a.py", "x → 1", "x = 2", "tests/t.py::test_a"),
        Mutant("web/b.ts", "a", "b", "tests/t.py::test_b"),
    ]


def test_mutant_jest_niezmienny_po_wczytaniu():
    mutant = Mutant("a.py", "x", "y", "tests/t.py::test_a")

    with pytest.raises(FrozenInstanceError):
        mutant.szukaj = "z"  # type: ignore[misc]


def test_plik_z_kropkami_w_nazwie_katalogu_nie_jest_wyjsciem_poza_korzen(tmp_path):
    # Tylko pelny skladnik `..` wychodzi z repozytorium; `..dane` to zwykla nazwa.
    tabela = _zapisz_tabele(tmp_path, _wpis(**_pola(plik="src/..dane/x.py")))

    assert [m.plik for m in mutanty_reczne.wczytaj_tabele(tabela)] == ["src/..dane/x.py"]


POLA_TABELI = ("plik", "szukaj", "zamien", "test")
ZLE_TABELE = {
    "pusta_lista": "mutant = []\n",
    "brak_klucza_mutant": "",
    "mutant_jest_napisem": 'mutant = "tekst"\n',
    "mutant_jest_tabela_a_nie_lista": "[mutant]\n"
    + "".join(f"{k} = {_toml(v)}\n" for k, v in _pola().items()),
    "wpis_jest_liczba": "mutant = [1, 2]\n",
    "drugi_wpis_nie_jest_tabela": "mutant = [{ "
    + ", ".join(f"{k} = {_toml(v)}" for k, v in _pola().items())
    + " }, 5]\n",
    "nieznany_klucz_we_wpisie": _wpis(**_pola(opis="po co")),
    "nieznany_klucz_najwyzszego_poziomu": 'tytul = "x"\n\n' + _wpis(**_pola()),
    "plik_absolutny": _wpis(**_pola(plik="/etc/hosts")),
    "plik_wychodzi_poza_korzen": _wpis(**_pola(plik="../x.py")),
    "plik_wychodzi_poza_korzen_w_srodku_sciezki": _wpis(**_pola(plik="src/../../x.py")),
    "plik_to_sama_kropka_kropka": _wpis(**_pola(plik="..")),
    "mutant_tozsamosciowy": _wpis(**_pola(szukaj="x = 1", zamien="x = 1")),
    "zly_toml": "mutant = [\n",
}
for _pole in POLA_TABELI:
    ZLE_TABELE[f"brak_pola_{_pole}"] = _wpis(**{k: v for k, v in _pola().items() if k != _pole})
    ZLE_TABELE[f"pole_{_pole}_jest_liczba"] = _wpis(**_pola(**{_pole: 5}))
    ZLE_TABELE[f"pole_{_pole}_jest_bool"] = _wpis(**_pola(**{_pole: True}))
    ZLE_TABELE[f"pole_{_pole}_jest_lista"] = _wpis(**_pola(**{_pole: ["x"]}))
    if _pole != "zamien":  # HAR-184: pusty `zamien` to usuniecie `szukaj`, nie zla tabela
        ZLE_TABELE[f"pole_{_pole}_jest_pustym_napisem"] = _wpis(**_pola(**{_pole: ""}))


@pytest.mark.parametrize(
    "tresc", [pytest.param(t, id=i) for i, t in ZLE_TABELE.items()]
)
def test_niepoprawna_tabela_konczy_sie_bledem_narzedzia(tmp_path, tresc):
    tabela = _zapisz_tabele(tmp_path, tresc)

    with pytest.raises(BladNarzedzia):
        mutanty_reczne.wczytaj_tabele(tabela)


def test_wiersz_z_pustym_zamien_jest_poprawnym_mutantem_usuwajacym_fragment(tmp_path):
    tabela = _zapisz_tabele(tmp_path, _wpis(**_pola(zamien="")))

    assert mutanty_reczne.wczytaj_tabele(tabela) == [
        Mutant("src/a.py", "x = 1", "", "tests/test_a.py::test_x")
    ]


@pytest.mark.parametrize("pole", ["plik", "szukaj", "test"])
def test_puste_pole_inne_niz_zamien_jest_bledem_wskazujacym_to_pole(tmp_path, pole):
    # Pozostale trzy pola sa niepuste: tylko puste `pole` moze wywolac blad.
    tabela = _zapisz_tabele(tmp_path, _wpis(**_pola(**{pole: ""})))

    with pytest.raises(BladNarzedzia, match=f"pole '{pole}'"):
        mutanty_reczne.wczytaj_tabele(tabela)


@pytest.mark.parametrize(
    "wartosc",
    [
        pytest.param(5, id="liczba"),
        pytest.param(0, id="zero"),
        pytest.param(True, id="bool_prawda"),
        pytest.param(False, id="bool_falsz"),
        pytest.param(["x"], id="lista"),
        pytest.param([], id="pusta_lista"),
    ],
)
def test_zamien_inne_niz_napis_jest_bledem_wskazujacym_to_pole(tmp_path, wartosc):
    # `0`, `False` i `[]` sa "puste" w sensie Pythona: odrozniaja sprawdzanie typu od falsy.
    tabela = _zapisz_tabele(tmp_path, _wpis(**_pola(zamien=wartosc)))

    with pytest.raises(BladNarzedzia, match="pole 'zamien'"):
        mutanty_reczne.wczytaj_tabele(tabela)


def test_zamien_rowne_szukaj_jest_bledem_mutanta_tozsamosciowego(tmp_path):
    tabela = _zapisz_tabele(tmp_path, _wpis(**_pola(szukaj="x = 1", zamien="x = 1")))

    with pytest.raises(BladNarzedzia, match="tozsamosciowy"):
        mutanty_reczne.wczytaj_tabele(tabela)


def test_tabela_w_zlym_utf8_konczy_sie_bledem_narzedzia(tmp_path):
    tabela = tmp_path / "tabela.toml"
    tabela.write_bytes(b"[[mutant]]\nplik = \"a\xff\xfe\"\n")

    with pytest.raises(BladNarzedzia):
        mutanty_reczne.wczytaj_tabele(tabela)


def test_brak_pliku_tabeli_konczy_sie_bledem_narzedzia(tmp_path):
    with pytest.raises(BladNarzedzia):
        mutanty_reczne.wczytaj_tabele(tmp_path / "nie-ma.toml")


def test_katalog_zamiast_pliku_tabeli_konczy_sie_bledem_narzedzia(tmp_path):
    katalog = tmp_path / "tabela.toml"
    katalog.mkdir()

    with pytest.raises(BladNarzedzia):
        mutanty_reczne.wczytaj_tabele(katalog)


# =============================================================================
# zastosuj_mutacje: zmiana wylacznie w kopii, szukaj dokladnie raz
# =============================================================================


def _para_katalogow(tmp_path: Path, tresc: bytes) -> tuple[Path, Path]:
    oryginal, kopia = tmp_path / "oryginal", tmp_path / "kopia"
    for katalog in (oryginal, kopia):
        (katalog / "src").mkdir(parents=True)
        (katalog / "src" / "a.py").write_bytes(tresc)
    return oryginal, kopia


def test_mutacja_z_jednym_wystapieniem_zmienia_tylko_kopie_a_oryginal_zostaje_bajt_w_bajt(tmp_path):
    tresc = "a = 1\nb = 2\nc = 3\n".encode()
    oryginal, kopia = _para_katalogow(tmp_path, tresc)

    mutanty_reczne.zastosuj_mutacje(kopia, Mutant("src/a.py", "b = 2", "b = 20", "t"))

    assert (kopia / "src" / "a.py").read_bytes() == "a = 1\nb = 20\nc = 3\n".encode()
    assert (oryginal / "src" / "a.py").read_bytes() == tresc


def test_mutacja_zachowuje_konce_linii_i_znaki_spoza_ascii_w_reszcie_pliku(tmp_path):
    _, kopia = _para_katalogow(tmp_path, "a = 'ą'\r\nb = 2\r\n".encode())

    mutanty_reczne.zastosuj_mutacje(kopia, Mutant("src/a.py", "b = 2", "b = 3", "t"))

    assert (kopia / "src" / "a.py").read_bytes() == "a = 'ą'\r\nb = 3\r\n".encode()


def test_mutacja_z_pustym_zamien_usuwa_dokladnie_szukaj_z_kopii_a_oryginal_zostaje_bajt_w_bajt(
    tmp_path,
):
    tresc = "a = 1\nb = 2\nc = 3\n".encode()
    oryginal, kopia = _para_katalogow(tmp_path, tresc)

    mutanty_reczne.zastosuj_mutacje(kopia, Mutant("src/a.py", "b = 2\n", "", "t"))

    assert (kopia / "src" / "a.py").read_bytes() == "a = 1\nc = 3\n".encode()
    assert (oryginal / "src" / "a.py").read_bytes() == tresc


def test_mutacja_bez_wystapienia_szukaj_konczy_sie_bledem_i_nie_zmienia_pliku(tmp_path):
    tresc = b"a = 1\n"
    _, kopia = _para_katalogow(tmp_path, tresc)

    with pytest.raises(BladNarzedzia, match="0"):
        mutanty_reczne.zastosuj_mutacje(kopia, Mutant("src/a.py", "z = 9", "z = 8", "t"))

    assert (kopia / "src" / "a.py").read_bytes() == tresc


def test_mutacja_z_wieloma_wystapieniami_szukaj_konczy_sie_bledem_z_liczba_i_nie_zmienia_pliku(
    tmp_path,
):
    tresc = b"x = 1\ny = 1\nz = 1\n"
    _, kopia = _para_katalogow(tmp_path, tresc)

    with pytest.raises(BladNarzedzia, match="3"):
        mutanty_reczne.zastosuj_mutacje(kopia, Mutant("src/a.py", "= 1", "= 2", "t"))

    assert (kopia / "src" / "a.py").read_bytes() == tresc


def test_mutacja_dokladnie_dwoch_wystapien_tez_jest_bledem(tmp_path):
    # Granica "raz": dwa wystapienia to nadal za duzo (odrzuca `> 2` zamiast `> 1`).
    tresc = b"x = 1\ny = 1\n"
    _, kopia = _para_katalogow(tmp_path, tresc)

    with pytest.raises(BladNarzedzia, match="2"):
        mutanty_reczne.zastosuj_mutacje(kopia, Mutant("src/a.py", "= 1", "= 7", "t"))

    assert (kopia / "src" / "a.py").read_bytes() == tresc


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignoruje prawa plikow, wiec chmod nie wywola bledu zapisu"
)
def test_mutacja_pliku_bez_prawa_zapisu_konczy_sie_bledem_narzedzia_a_plik_zostaje_bez_zmian(
    tmp_path,
):
    # Odczyt sie udaje (0o444), zapis nie: decyduje wylacznie gałąź tłumacząca blad zapisu.
    tresc = b"a = 1\n"
    _, kopia = _para_katalogow(tmp_path, tresc)
    plik = kopia / "src" / "a.py"
    plik.chmod(0o444)
    try:
        with pytest.raises(BladNarzedzia, match="zapisac"):
            mutanty_reczne.zastosuj_mutacje(kopia, Mutant("src/a.py", "a = 1", "a = 2", "t"))

        assert plik.read_bytes() == tresc
    finally:
        plik.chmod(0o644)


def test_mutacja_pliku_nieistniejacego_w_kopii_konczy_sie_bledem_narzedzia(tmp_path):
    _, kopia = _para_katalogow(tmp_path, b"a = 1\n")

    with pytest.raises(BladNarzedzia):
        mutanty_reczne.zastosuj_mutacje(kopia, Mutant("src/brak.py", "a", "b", "t"))


def test_mutacja_pliku_w_zlym_utf8_konczy_sie_bledem_narzedzia(tmp_path):
    _, kopia = _para_katalogow(tmp_path, b"a = '\xff\xfe'\n")

    with pytest.raises(BladNarzedzia):
        mutanty_reczne.zastosuj_mutacje(kopia, Mutant("src/a.py", "a", "b", "t"))


# =============================================================================
# ocena kodu testu: kod 1 zabity, kod 0 przezyl, reszta to brak sygnalu
# =============================================================================


def test_test_ktory_oblal_po_mutacji_oznacza_zabitego_mutanta():
    assert mutanty_reczne.ocen_kod_testu(1) is True


def test_test_ktory_przeszedl_po_mutacji_oznacza_przezywajacego_mutanta():
    assert mutanty_reczne.ocen_kod_testu(0) is False


@pytest.mark.parametrize(
    "kod",
    [
        pytest.param(2, id="przerwany_albo_blad_zbierania"),
        pytest.param(3, id="blad_wewnetrzny_pytesta"),
        pytest.param(4, id="zly_node_id_albo_uzycie"),
        pytest.param(5, id="nic_nie_zebrano"),
        pytest.param(-9, id="zabity_sygnalem"),
    ],
)
def test_kod_pytesta_inny_niz_zero_i_jeden_to_brak_dowodu_a_nie_zabity_mutant(kod):
    with pytest.raises(BladNarzedzia):
        mutanty_reczne.ocen_kod_testu(kod)


# =============================================================================
# uruchom i skopiuj_repo
# =============================================================================


def test_uruchom_nieistniejacej_binarki_konczy_sie_bledem_narzedzia(tmp_path):
    with pytest.raises(BladNarzedzia):
        mutanty_reczne.uruchom(["binarka-ktorej-nie-ma-xyz"], tmp_path)


def test_uruchom_zwraca_niezerowy_kod_polecenia_bez_wyjatku(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    bez_repo = tmp_path / "bez_repo"
    bez_repo.mkdir()

    wynik = mutanty_reczne.uruchom(["git", "rev-parse", "--git-dir"], bez_repo)

    assert wynik.returncode != 0


def test_uruchom_zwraca_tekst_ze_standardowego_wyjscia_polecenia(tmp_path):
    wynik = mutanty_reczne.uruchom([sys.executable, "-c", "print('witaj')"], tmp_path)

    assert wynik.returncode == 0
    assert wynik.stdout == "witaj\n"


def test_uruchom_zachowuje_bajty_spoza_utf8_w_wyjsciu_polecenia_zamiast_rzucac_wyjatku(tmp_path):
    # Bez `errors="surrogateescape"` dekodowanie rzuca UnicodeDecodeError.
    wynik = mutanty_reczne.uruchom(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'a\\xff')"], tmp_path
    )

    assert wynik.stdout == "a\udcff"


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignoruje prawa plikow, wiec chmod nie wywola bledu odczytu"
)
def test_kopiowanie_nieczytelnego_pliku_nieslezonego_konczy_sie_bledem_narzedzia(tmp_path):
    repo = _repo(tmp_path)
    nieczytelny = repo / "nieczytelny.txt"
    nieczytelny.write_text("tajne", encoding="utf-8")
    nieczytelny.chmod(0o000)
    cel = tmp_path / "cel"
    cel.mkdir()
    try:
        with pytest.raises(BladNarzedzia, match="nieczytelny.txt"):
            mutanty_reczne.skopiuj_repo(repo, cel)
    finally:
        nieczytelny.chmod(0o644)


def test_kopiowanie_katalogu_bez_repozytorium_konczy_sie_bledem_narzedzia(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    bez_repo = tmp_path / "bez_repo"
    bez_repo.mkdir()
    (bez_repo / "a.txt").write_text("x", encoding="utf-8")
    cel = tmp_path / "cel"
    cel.mkdir()

    with pytest.raises(BladNarzedzia):
        mutanty_reczne.skopiuj_repo(bez_repo, cel)

    assert list(cel.iterdir()) == []


def test_kopia_repo_zawiera_sledzone_i_nieslezone_nieignorowane_a_nie_ignorowane_ani_git(tmp_path):
    repo = _repo(tmp_path)
    (repo / "nowy.txt").write_text("nieslezony", encoding="utf-8")
    (repo / "ignorowany.txt").write_text("ignorowany", encoding="utf-8")
    (repo / "src" / "podkatalog").mkdir()
    (repo / "src" / "podkatalog" / "gleboki.txt").write_text("gleboko", encoding="utf-8")
    cel = tmp_path / "cel"
    cel.mkdir()

    mutanty_reczne.skopiuj_repo(repo, cel)

    assert (cel / "nowy.txt").read_text(encoding="utf-8") == "nieslezony"
    assert not (cel / "ignorowany.txt").exists()
    assert not (cel / ".git").exists()
    assert (cel / PLIK_MODULU).read_text(encoding="utf-8") == MODUL
    assert (cel / "src" / "podkatalog" / "gleboki.txt").read_text(encoding="utf-8") == "gleboko"
    assert (cel / "tests" / "test_modul.py").read_text(encoding="utf-8") == TEST_MODUL


def test_kopia_repo_zachowuje_pliki_ze_spacja_i_polskimi_znakami_w_nazwie(tmp_path):
    repo = _repo(tmp_path)
    nazwa = "plik ze spacją ą.txt"
    (repo / nazwa).write_text("treść", encoding="utf-8")
    cel = tmp_path / "cel"
    cel.mkdir()

    mutanty_reczne.skopiuj_repo(repo, cel)

    assert (cel / nazwa).read_text(encoding="utf-8") == "treść"


def test_kopia_repo_pomija_plik_sledzony_usuniety_z_dysku_bez_wyjatku(tmp_path):
    repo = _repo(tmp_path)
    # Wpis w indeksie bez pliku na dysku: stan "sledzony, ale usuniety z dysku".
    skrot = _git_wejscie(repo, "zniknie")
    _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{skrot},zniknie.txt")
    cel = tmp_path / "cel"
    cel.mkdir()

    mutanty_reczne.skopiuj_repo(repo, cel)

    assert not (cel / "zniknie.txt").exists()
    assert (cel / PLIK_MODULU).exists()


def _git_wejscie(repo: Path, tresc: str) -> str:
    sciezka = repo.parent / "obiekt-tymczasowy.txt"
    sciezka.write_text(tresc, encoding="utf-8")
    return _git(repo, "hash-object", "-w", str(sciezka)).strip()


# =============================================================================
# uruchom_test na prawdziwym pytescie w kopii
# =============================================================================


def test_test_zielony_w_kopii_daje_kod_0(tmp_path):
    kopia = _kopia_testowa(tmp_path / "kopia", {"test_a.py": "def test_ok():\n    assert True\n"})

    assert mutanty_reczne.uruchom_test(kopia, "tests/test_a.py::test_ok", 60) == 0


def test_test_czerwony_w_kopii_daje_kod_1(tmp_path):
    kopia = _kopia_testowa(tmp_path / "kopia", {"test_a.py": "def test_zle():\n    assert False\n"})

    assert mutanty_reczne.uruchom_test(kopia, "tests/test_a.py::test_zle", 60) == 1


def test_nieistniejacy_node_id_daje_kod_pytesta_4_a_nie_wyjatek(tmp_path):
    kopia = _kopia_testowa(tmp_path / "kopia", {"test_a.py": "def test_ok():\n    assert True\n"})

    assert mutanty_reczne.uruchom_test(kopia, "tests/test_a.py::test_nie_ma", 60) == 4


def test_test_oznaczony_markerem_wykluczonym_w_addopts_projektu_nadal_sie_uruchamia(tmp_path):
    # `addopts = "-m 'not wolny'"` odrzucilby ten test (kod 5); `-o addopts=` go uruchamia.
    kopia = _kopia_testowa(
        tmp_path / "kopia",
        {
            "test_wolny.py": (
                "import pytest\n\n\n@pytest.mark.wolny\ndef test_oznaczony():\n    assert True\n"
            )
        },
    )

    assert mutanty_reczne.uruchom_test(kopia, "tests/test_wolny.py::test_oznaczony", 60) == 0


def test_test_importuje_kod_z_kopii_a_nie_z_oryginalu_nawet_gdy_oryginal_jest_w_pythonpath(
    tmp_path, monkeypatch
):
    oryginal = _kopia_testowa(tmp_path / "oryginal", {}, modul="WARTOSC = 1\n")
    kopia = _kopia_testowa(
        tmp_path / "kopia",
        {
            "test_wartosc.py": (
                "from modul import WARTOSC\n\n\n"
                "def test_wartosc_z_kopii():\n    assert WARTOSC == 2\n"
            )
        },
        modul="WARTOSC = 2\n",
    )
    monkeypatch.setenv("PYTHONPATH", str(oryginal / "src"))

    kod = mutanty_reczne.uruchom_test(kopia, "tests/test_wartosc.py::test_wartosc_z_kopii", 60)

    assert kod == 0


def test_uruchomienie_testu_nie_zmienia_srodowiska_procesu_wolajacego(tmp_path, monkeypatch):
    kopia = _kopia_testowa(tmp_path / "kopia", {"test_a.py": "def test_ok():\n    assert True\n"})
    monkeypatch.setenv("PYTHONPATH", "/stara/sciezka")

    mutanty_reczne.uruchom_test(kopia, "tests/test_a.py::test_ok", 60)

    assert os.environ["PYTHONPATH"] == "/stara/sciezka"


def test_uruchomienie_testu_nie_zostawia_cache_pytesta_w_kopii(tmp_path):
    kopia = _kopia_testowa(tmp_path / "kopia", {"test_a.py": "def test_ok():\n    assert True\n"})

    mutanty_reczne.uruchom_test(kopia, "tests/test_a.py::test_ok", 60)

    assert not (kopia / ".pytest_cache").exists()


def test_test_przekraczajacy_limit_czasu_konczy_sie_bledem_narzedzia_a_nie_zawiesza_przebiegu(
    tmp_path,
):
    kopia = _kopia_testowa(
        tmp_path / "kopia",
        {"test_spi.py": "import time\n\n\ndef test_spi():\n    time.sleep(60)\n"},
    )
    start = time.monotonic()

    with pytest.raises(BladNarzedzia, match="czas"):
        mutanty_reczne.uruchom_test(kopia, "tests/test_spi.py::test_spi", 1)

    assert time.monotonic() - start < 30


def test_po_przekroczeniu_limitu_ginie_takze_proces_potomny_testu(tmp_path, monkeypatch):
    kopia = _kopia_testowa(
        tmp_path / "kopia",
        {
            "test_wnuk.py": (
                "import os\nimport pathlib\nimport subprocess\nimport sys\nimport time\n\n\n"
                "def test_spi_z_wnukiem():\n"
                "    wnuk = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "    pathlib.Path('wnuk.pid.tmp').write_text(str(wnuk.pid))\n"
                "    os.replace('wnuk.pid.tmp', 'wnuk.pid')\n"
                "    time.sleep(60)\n"
            )
        },
    )
    plik_pid = kopia / "wnuk.pid"
    # Limit 4 s liczy sie dopiero od chwili, gdy wnuk zapisal PID (start pytesta w kopii i wnuka
    # pod obciazeniem nie zjada limitu); dalej dziala prawdziwy `communicate(timeout)` i `killpg`.
    _podmien_popen_na_zegar_od_potomka(monkeypatch, plik_pid)
    pid = None
    try:
        with pytest.raises(BladNarzedzia):
            mutanty_reczne.uruchom_test(kopia, "tests/test_wnuk.py::test_spi_z_wnukiem", 4)

        pid = int(plik_pid.read_text(encoding="utf-8"))
        _czekaj_synchronicznie(
            lambda: not czy_zyje(pid), LIMIT_ZNIKNIECIA_POTOMKA, f"zniknięcie wnuka {pid}"
        )
    finally:
        if pid is not None:
            zabij_proces(pid)


def test_nieistniejacy_katalog_kopii_konczy_uruchomienie_testu_bledem_narzedzia(tmp_path):
    with pytest.raises(BladNarzedzia):
        mutanty_reczne.uruchom_test(tmp_path / "brak", "tests/test_a.py::test_ok", 5)


# =============================================================================
# main: kody wyjscia, wypisywane wiersze, brak zmian w repozytorium
# =============================================================================


def test_mutant_zabity_przez_test_konczy_sie_kodem_0_i_niczego_nie_wypisuje(tmp_path, capsys):
    repo = _repo(tmp_path, TABELA_ZABITY)

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 0
    assert capsys.readouterr().out == ""


def test_mutant_ktorego_test_nie_oblewa_konczy_sie_kodem_1_i_wypisuje_wiersz(tmp_path, capsys):
    repo = _repo(tmp_path, TABELA_PRZEZYWA)

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 1
    assert capsys.readouterr().out == f"{PLIK_MODULU}: {SZUKAJ} → {PRZEZYWA_A}\n"


def test_mutant_usuwajacy_fragment_ktory_test_wykrywa_konczy_sie_kodem_0_i_niczego_nie_wypisuje(
    tmp_path, capsys
):
    # Usuniecie " + b" zostawia poprawne `return a`, a test_dodawanie oblewa.
    repo = _repo(tmp_path, _mutant(" + b", ""))

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 0
    assert capsys.readouterr().out == ""


def test_mutant_usuwajacy_fragment_ktorego_test_nie_wykrywa_konczy_sie_kodem_1_i_wypisuje_wiersz(
    tmp_path, capsys
):
    # Import `odejmij` jest potrzebny tylko test_odejmowanie; test_dodawanie przechodzi dalej.
    repo = _repo(
        tmp_path, _mutant(", odejmij", "", test=TEST_DODAWANIE, plik="tests/test_modul.py")
    )

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 1
    assert capsys.readouterr().out == "tests/test_modul.py: , odejmij → \n"


def test_przy_mieszanej_tabeli_wypisani_sa_tylko_przezywajacy_w_kolejnosci_wpisow(
    tmp_path, capsys
):
    tabela = (
        _mutant(SZUKAJ, PRZEZYWA_A)
        + _mutant(SZUKAJ, ZABIJA)
        + _mutant(SZUKAJ, PRZEZYWA_B)
    )
    repo = _repo(tmp_path, tabela)

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 1
    assert capsys.readouterr().out == (
        f"{PLIK_MODULU}: {SZUKAJ} → {PRZEZYWA_A}\n{PLIK_MODULU}: {SZUKAJ} → {PRZEZYWA_B}\n"
    )


def test_wiersz_przezywajacego_mutanta_z_innego_pliku_niesie_jego_sciezke(tmp_path, capsys):
    repo = _repo(tmp_path, _mutant("assert dodaj(2, 3) == 5", "assert dodaj(2, 3) >= 5",
                                   test=TEST_DODAWANIE, plik="tests/test_modul.py"))

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 1
    assert capsys.readouterr().out == (
        "tests/test_modul.py: assert dodaj(2, 3) == 5 → assert dodaj(2, 3) >= 5\n"
    )


def test_szukaj_nieobecne_w_pliku_konczy_sie_kodem_2_z_komunikatem_na_stderr(tmp_path, capsys):
    repo = _repo(tmp_path, _mutant("return a * b", "return a / b"))

    kod = _uruchom(repo, TABELA_W_REPO)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.out == ""
    assert zrzut.err.startswith("blad: ")


def test_szukaj_wystepujace_dwa_razy_konczy_sie_kodem_2_a_nie_zabitym_mutantem(tmp_path, capsys):
    # `return` jest w obu funkcjach; zamiana obu rozwaliłaby modul, wiec bez kontroli byłby "zabity".
    repo = _repo(tmp_path, _mutant("return", "pass"))

    kod = _uruchom(repo, TABELA_W_REPO)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.err.startswith("blad: ")


def test_test_czerwony_juz_bez_mutacji_konczy_sie_kodem_2_zamiast_falszywie_zabitym_mutantem(
    tmp_path, capsys
):
    # Gdyby przebieg bazowy nie istnial, czerwony test "zabilby" kazdy mutant (kod 0).
    repo = _repo(tmp_path, _mutant(SZUKAJ, ZABIJA, test=TEST_CZERWONY))

    kod = _uruchom(repo, TABELA_W_REPO)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.err.startswith("blad: ")


def test_nieistniejacy_node_id_testu_konczy_sie_kodem_2_a_nie_zabitym_mutantem(tmp_path, capsys):
    repo = _repo(tmp_path, _mutant(SZUKAJ, ZABIJA, test="tests/test_modul.py::test_nie_ma"))

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 2
    assert capsys.readouterr().err.startswith("blad: ")


def test_nieistniejacy_node_id_w_przebiegu_bazowym_zglasza_zly_node_id_z_kodem_pytesta_4_a_nie_test_czerwony(
    tmp_path, capsys
):
    node_id = "tests/test_modul.py::test_nie_ma"
    repo = _repo(tmp_path, _mutant(SZUKAJ, ZABIJA, test=node_id))

    kod = _uruchom(repo, TABELA_W_REPO)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.err.startswith("blad: ")
    assert node_id in zrzut.err
    assert "node id" in zrzut.err
    assert "kodem 4" in zrzut.err
    assert "nie przechodzi bez mutacji" not in zrzut.err


def test_test_czerwony_w_przebiegu_bazowym_zglasza_nie_przechodzi_bez_mutacji_a_nie_zly_node_id(
    tmp_path, capsys
):
    repo = _repo(tmp_path, _mutant(SZUKAJ, ZABIJA, test=TEST_CZERWONY))

    kod = _uruchom(repo, TABELA_W_REPO)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.err.startswith("blad: ")
    assert TEST_CZERWONY in zrzut.err
    assert "test nie przechodzi bez mutacji" in zrzut.err
    assert "node id" not in zrzut.err


@pytest.mark.parametrize("kod_bazowy", [2, 3, 5, -9])
def test_kazdy_kod_pytesta_bazowego_inny_niz_0_i_1_zglasza_zly_node_id_z_tym_kodem(
    tmp_path, capsys, monkeypatch, kod_bazowy
):
    repo = _repo(tmp_path, TABELA_DO_PODSTAWKI)
    monkeypatch.setattr(mutanty_reczne, "uruchom_test", lambda kopia, test, limit: kod_bazowy)

    kod = _uruchom(repo, TABELA_W_REPO)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.err.startswith("blad: ")
    assert TEST_DODAWANIE in zrzut.err
    assert "node id" in zrzut.err
    assert f"kodem {kod_bazowy}" in zrzut.err
    assert "nie przechodzi bez mutacji" not in zrzut.err


def test_mutacja_psujaca_skladnie_modulu_to_blad_a_nie_zabity_mutant(tmp_path, capsys):
    # Bazowy test przechodzi; po mutacji pytest konczy sie bledem zbierania (kod 2), nie oblaniem.
    repo = _repo(tmp_path, _mutant(SZUKAJ, "return a +"))

    kod = _uruchom(repo, TABELA_W_REPO)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.out == ""
    assert zrzut.err.startswith("blad: ")


def test_mutacja_zmieniajaca_nazwe_testu_to_blad_a_nie_zabity_mutant(tmp_path, capsys):
    # Bazowy test przechodzi; po mutacji testu o tym node id nie ma (kod pytesta 4).
    repo = _repo(
        tmp_path,
        _mutant("def test_dodawanie", "def dodawanie", plik="tests/test_modul.py"),
    )

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 2
    assert capsys.readouterr().err.startswith("blad: ")


def test_kazda_mutacja_idzie_w_nowej_kopii_wiec_druga_nie_widzi_zmiany_pierwszej(tmp_path):
    # Drugi `szukaj` ("a + b") istnieje tylko w oryginale: pierwsza mutacja zamienia cala linie.
    # We wspolnej kopii drugi wpis nie znalazlby wzorca (kod 2); w nowych kopiach oba mutanty gina.
    tabela = _mutant(SZUKAJ, "return 0") + _mutant("a + b", "a * b")
    repo = _repo(tmp_path, tabela)

    assert _uruchom(repo, TABELA_W_REPO) == 0


def test_przebieg_nie_zmienia_plikow_repozytorium_a_git_status_jest_czysty(tmp_path):
    tabela = _mutant(SZUKAJ, ZABIJA) + _mutant(SZUKAJ, PRZEZYWA_A)
    repo = _repo(tmp_path, tabela)
    przed = _migawka(repo)

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 1
    assert _git(repo, "status", "--porcelain") == ""
    assert _migawka(repo) == przed


def test_blad_w_trakcie_przebiegu_tez_nie_zmienia_repozytorium(tmp_path):
    repo = _repo(tmp_path, _mutant(SZUKAJ, ZABIJA) + _mutant("return a * b", "x"))
    przed = _migawka(repo)

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 2
    assert _git(repo, "status", "--porcelain") == ""
    assert _migawka(repo) == przed


def test_kopie_robocze_leza_w_katalogu_tymczasowym_i_znikaja_po_przebiegu(tmp_path, monkeypatch):
    katalog_tmp = tmp_path / "tmp"
    katalog_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(katalog_tmp))
    repo = _repo(tmp_path, TABELA_ZABITY)
    rzeczywista = mutanty_reczne.uruchom_test
    kopie: list[Path] = []

    def szpieg(kopia, test, limit):
        kopie.append(kopia)
        return rzeczywista(kopia, test, limit)

    monkeypatch.setattr(mutanty_reczne, "uruchom_test", szpieg)

    assert _uruchom(repo, TABELA_W_REPO) == 0

    assert len(kopie) >= 2  # przebieg bazowy i mutacja
    for kopia in kopie:
        assert kopia.parent == katalog_tmp
        assert kopia.name.startswith("mutanty-reczne-")
        assert not kopia.exists()
    assert list(katalog_tmp.iterdir()) == []


def test_brak_mozliwosci_utworzenia_katalogu_tymczasowego_konczy_sie_kodem_2_z_komunikatem(
    tmp_path, monkeypatch, capsys
):
    repo = _repo(tmp_path, TABELA_ZABITY)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "nie-ma-takiego-katalogu"))

    kod = _uruchom(repo, TABELA_W_REPO)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.out == ""
    assert zrzut.err.startswith("blad: ")


def test_blad_sprzatania_katalogu_tymczasowego_zmienia_wynik_z_zabitego_mutanta_na_kod_2(
    tmp_path, monkeypatch, capsys
):
    katalog_tmp = tmp_path / "tmp"
    katalog_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(katalog_tmp))
    repo = _repo(tmp_path, TABELA_ZABITY)

    def rmtree_z_bledem(sciezka, *args, **kwargs):
        raise OSError("zablokowany katalog")

    monkeypatch.setattr(mutanty_reczne.shutil, "rmtree", rmtree_z_bledem)

    kod = _uruchom(repo, TABELA_W_REPO)

    zrzut = capsys.readouterr()
    assert kod == 2  # bez tlumaczenia OSError wynik bylby wyjatkiem, a bez bledu - kodem 0
    assert zrzut.err.startswith("blad: ")
    assert "usunac" in zrzut.err


def test_przezywajacy_mutant_o_wielowierszowym_szukaj_jest_wypisany_w_jednej_linii(
    tmp_path, capsys
):
    szukaj = "def dodaj(a, b):\n    return a + b"
    zamien = "def dodaj(a, b):\n    return b + a"
    repo = _repo(tmp_path, _mutant(szukaj, zamien))

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 1
    assert capsys.readouterr().out == (
        f"{PLIK_MODULU}: def dodaj(a, b):\\n    return a + b"
        " → def dodaj(a, b):\\n    return b + a\n"
    )


def test_tabela_podana_jako_sciezka_wzgledna_jest_liczona_od_korzenia_z_opcji_korzen(tmp_path):
    repo = _repo(tmp_path, TABELA_ZABITY)

    assert mutanty_reczne.main([TABELA_W_REPO, "--korzen", str(repo)]) == 0


def test_mutacja_zapisana_w_tej_samej_sekundzie_co_oryginal_nie_uruchamia_starego_bajtkodu(
    tmp_path, monkeypatch
):
    # Mutacja `+` -> `-` nie zmienia rozmiaru pliku; gdy zapis wypada w tej samej sekundzie co
    # mtime oryginalu (copy2 go zachowuje), `.pyc` z przebiegu bazowego wyglada na aktualny
    # i test uruchamia kod sprzed mutacji (czerwone CI HAR-150). Zapis z zachowanym mtime
    # odtwarza ten przypadek deterministycznie.
    # Zmienna odziedziczona po wolajacym (np. sam `mutanty_reczne` w CI) ukrylaby brak poprawki.
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    repo = _repo(tmp_path, TABELA_ZABITY)
    zastosuj = mutanty_reczne.zastosuj_mutacje

    def zastosuj_z_tym_samym_mtime(kopia, mutant):
        stat = (kopia / mutant.plik).stat()
        zastosuj(kopia, mutant)
        os.utime(kopia / mutant.plik, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    monkeypatch.setattr(mutanty_reczne, "zastosuj_mutacje", zastosuj_z_tym_samym_mtime)

    assert _uruchom(repo, TABELA_W_REPO) == 0


def test_bez_opcji_korzen_korzeniem_jest_biezacy_katalog(tmp_path, monkeypatch):
    repo = _repo(tmp_path, TABELA_DO_PODSTAWKI)
    _podstawka_testu(monkeypatch)
    monkeypatch.chdir(repo)

    assert _main([TABELA_W_REPO]) == 0


def test_dwie_tabele_sa_przetwarzane_a_przezywajacy_z_drugiej_daje_kod_1(tmp_path, capsys):
    repo = _repo(tmp_path, TABELA_ZABITY)
    (repo / "tests" / "mutanty_reczne" / "HAR-1.toml").write_text(
        TABELA_PRZEZYWA, encoding="utf-8"
    )

    kod = _uruchom(repo, TABELA_W_REPO, "tests/mutanty_reczne/HAR-1.toml")

    assert kod == 1
    assert capsys.readouterr().out == f"{PLIK_MODULU}: {SZUKAJ} → {PRZEZYWA_A}\n"


@pytest.mark.parametrize(
    "pierwsza_przezywa",
    [pytest.param(True, id="przezywajacy_przed_bledem"), pytest.param(False, id="blad_przed_przezywajacym")],
)
def test_blad_w_jednej_tabeli_wygrywa_z_przezywajacym_w_drugiej_i_daje_kod_2(
    tmp_path, pierwsza_przezywa
):
    repo = _repo(tmp_path, TABELA_PRZEZYWA)
    zla = _mutant("return a * b", "return a / b")
    (repo / "tests" / "mutanty_reczne" / "HAR-1.toml").write_text(zla, encoding="utf-8")
    tabele = [TABELA_W_REPO, "tests/mutanty_reczne/HAR-1.toml"]
    if not pierwsza_przezywa:
        tabele.reverse()

    assert _uruchom(repo, *tabele) == 2


def test_niepoprawna_tabela_w_wierszu_polecen_konczy_sie_kodem_2_z_komunikatem(tmp_path, capsys):
    repo = _repo(tmp_path, "mutant = []\n")

    kod = _uruchom(repo, TABELA_W_REPO)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.err.startswith("blad: ")


def test_brak_pliku_tabeli_w_wierszu_polecen_konczy_sie_kodem_2(tmp_path, capsys):
    repo = _repo(tmp_path)

    assert _uruchom(repo, "tests/mutanty_reczne/nie-ma.toml") == 2
    assert capsys.readouterr().err.startswith("blad: ")


def test_wywolanie_bez_zadnej_tabeli_to_blad_wiersza_polecen(tmp_path, capsys):
    # Brak tabeli to brak dowodu, nie "wszystkie mutanty zabite".
    assert _main(["--korzen", str(tmp_path)]) == 2


# --- limit czasu (zaslepka testu: badamy tylko przekazanie parametru) ------


def test_domyslny_limit_czasu_testu_to_300_sekund(tmp_path, monkeypatch):
    repo = _repo(tmp_path, TABELA_DO_PODSTAWKI)
    wywolania = _podstawka_testu(monkeypatch)

    assert mutanty_reczne.main([TABELA_W_REPO, "--korzen", str(repo)]) == 0

    assert wywolania
    assert {limit for _, _, limit in wywolania} == {300}


def test_limit_czasu_z_wiersza_polecen_trafia_do_uruchomienia_testu(tmp_path, monkeypatch):
    repo = _repo(tmp_path, TABELA_DO_PODSTAWKI)
    wywolania = _podstawka_testu(monkeypatch)

    assert _uruchom(repo, TABELA_W_REPO, limit="7.5") == 0

    assert {limit for _, _, limit in wywolania} == {7.5}


@pytest.mark.parametrize(
    "limit",
    [
        pytest.param("0", id="zero"),
        pytest.param("-1", id="ujemny"),
        pytest.param("abc", id="nie_liczba"),
        # Tylko warunek skonczonosci je odrzuca: `inf <= 0` i `nan <= 0` sa falszem.
        pytest.param("inf", id="nieskonczony"),
        pytest.param("nan", id="nie_liczba_ieee"),
    ],
)
def test_limit_czasu_niedodatni_albo_nie_liczba_jest_bledem_wiersza_polecen(
    tmp_path, monkeypatch, limit
):
    repo = _repo(tmp_path, TABELA_DO_PODSTAWKI)
    wywolania = _podstawka_testu(monkeypatch)

    kod = _main([TABELA_W_REPO, "--korzen", str(repo), "--limit", limit])

    assert kod == 2
    assert wywolania == []


# --- prawdziwy skrypt jako program -----------------------------------------


def _uruchom_skrypt(*argv: str) -> tuple[int, str, str]:
    proces = uruchom_proces_sync(
        [sys.executable, str(SKRYPT), *argv],
        cwd=KORZEN,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wyjscie, blad = proces.communicate(timeout=120)
    finally:
        zakoncz_proces_sync(proces, nazwa="mutanty_reczne.py")
    return proces.returncode, wyjscie, blad


def test_skrypt_uruchomiony_jako_program_konczy_sie_kodem_1_i_wypisuje_wiersz(tmp_path):
    repo = _repo(tmp_path, TABELA_PRZEZYWA)

    kod, wyjscie, _ = _uruchom_skrypt(TABELA_W_REPO, "--korzen", str(repo), "--limit", "60")

    assert kod == 1
    assert wyjscie == f"{PLIK_MODULU}: {SZUKAJ} → {PRZEZYWA_A}\n"


def test_skrypt_uruchomiony_jako_program_konczy_sie_kodem_2_i_blad_idzie_na_stderr(tmp_path):
    repo = _repo(tmp_path)

    kod, wyjscie, blad = _uruchom_skrypt("tabela-ktorej-nie-ma.toml", "--korzen", str(repo))

    assert kod == 2
    assert wyjscie == ""
    assert blad.startswith("blad: ")


def test_skrypt_uruchomiony_jako_program_konczy_sie_kodem_0_gdy_mutanty_zabite(tmp_path):
    repo = _repo(tmp_path, TABELA_ZABITY)

    kod, wyjscie, _ = _uruchom_skrypt(TABELA_W_REPO, "--korzen", str(repo), "--limit", "60")

    assert kod == 0
    assert wyjscie == ""


# =============================================================================
# HAR-156: front i Postgres dla mutantow sprawdzanych testami e2e;
# test pominiety albo bez raportu nigdy nie jest "przeszedl" ani "zabil mutanta"
# =============================================================================

TEST_POMIJANY = "tests/test_pomijany.py::test_pomijany"
TEST_SKIP_PO_MUTACJI = "tests/test_skip_po_mutacji.py::test_warunkowy"
TEST_STATIC = "tests/test_static.py::test_static"
TEST_E2E = "tests/e2e/test_dodawanie_e2e.py::test_dodawanie_e2e"
PLIK_WEB = "web/src/x.txt"

TEST_POMIJANY_TRESC = """\
import pytest


def test_pomijany():
    pytest.skip("brak bazy")
"""
# Skip tylko gdy mutacja zepsula dodawanie; z COCKPIT_WYMAGAJ_BAZY=1 skip jest porazka
# (tak jak w fixture `czysta_baza` projektu), wiec to zmienna robi z pominiecia "zabitego".
TEST_SKIP_PO_MUTACJI_TRESC = """\
import os

import pytest
from modul import dodaj


def test_warunkowy():
    if dodaj(2, 3) != 5:
        if os.environ.get("COCKPIT_WYMAGAJ_BAZY") == "1":
            pytest.fail("wymagana baza")
        pytest.skip("brak bazy")
"""
# Czyta to, co atrapa `npm run build` skopiowala z web/src/x.txt do src/cockpit/static/x.txt.
TEST_STATIC_TRESC = """\
from pathlib import Path


def test_static():
    assert Path("src/cockpit/static/x.txt").read_text(encoding="utf-8").strip() == "A"
"""
TEST_E2E_TRESC = """\
from pathlib import Path

from modul import dodaj


def test_dodawanie_e2e():
    assert Path("src/cockpit/static/index.html").is_file()
    assert dodaj(2, 3) == 5
"""

POMIJANY_I_MODUL = {PLIK_MODULU: MODUL, "tests/test_pomijany.py": TEST_POMIJANY_TRESC}
SKIP_PO_MUTACJI_I_MODUL = {
    PLIK_MODULU: MODUL,
    "tests/test_skip_po_mutacji.py": TEST_SKIP_PO_MUTACJI_TRESC,
}
FRONT_I_MODUL = {
    PLIK_MODULU: MODUL,
    PLIK_WEB: "A\n",
    "tests/test_static.py": TEST_STATIC_TRESC,
    "tests/e2e/test_dodawanie_e2e.py": TEST_E2E_TRESC,
}


def _repo_z_plikami(tmp_path: Path, pliki: dict[str, str], tabela: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests" / "mutanty_reczne").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(PYPROJECT_MINI, encoding="utf-8")
    for sciezka, tresc in pliki.items():
        plik = repo / sciezka
        plik.parent.mkdir(parents=True, exist_ok=True)
        plik.write_text(tresc, encoding="utf-8")
    (repo / TABELA_W_REPO).write_text(tabela, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "start")
    return repo


# --- atrapa npm: skrypt sh na poczatku PATH ----------------------------------

ATRAPA_NPM = """#!/bin/sh
echo "$(pwd -P)|$*" >> "$ATRAPA_LOG_NPM"
case "$1" in
  ci)
    mkdir -p node_modules
    exit "${ATRAPA_KOD_CI:-0}"
    ;;
  run)
    if [ -n "$ATRAPA_USPIJ" ]; then
      sleep "$ATRAPA_USPIJ" &
      echo "$!" > "$ATRAPA_PID_NPM.tmp" && mv "$ATRAPA_PID_NPM.tmp" "$ATRAPA_PID_NPM"
      wait
    fi
    [ -n "$ATRAPA_WYJSCIE_BUILD" ] && echo "$ATRAPA_WYJSCIE_BUILD"
    [ -n "$ATRAPA_BLAD_BUILD" ] && echo "$ATRAPA_BLAD_BUILD" >&2
    [ -n "$ATRAPA_BAJTY_BUILD" ] && printf 'a\\377b\\n'
    [ -n "$ATRAPA_ZABIJ_BUILD" ] && kill -KILL $$
    if [ -n "$ATRAPA_PADNIJ_NA" ] && grep -q "$ATRAPA_PADNIJ_NA" src/x.txt 2>/dev/null; then
      exit 7
    fi
    if [ "${ATRAPA_KOD_BUILD:-0}" != "0" ]; then
      exit "$ATRAPA_KOD_BUILD"
    fi
    if [ "${ATRAPA_INDEKS:-1}" = "1" ]; then
      mkdir -p ../src/cockpit/static
      [ -f src/x.txt ] && cp src/x.txt ../src/cockpit/static/x.txt
      echo ok > ../src/cockpit/static/index.html
    fi
    exit 0
    ;;
esac
exit 0
"""


class AtrapaNpm:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.log = tmp_path / "log-npm.txt"
        self.pid = tmp_path / "pid-npm.txt"
        katalog = tmp_path / "atrapa-npm"
        katalog.mkdir()
        plik = katalog / "npm"
        plik.write_text(ATRAPA_NPM, encoding="utf-8")
        plik.chmod(0o755)
        monkeypatch.setenv("PATH", f"{katalog}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("ATRAPA_LOG_NPM", str(self.log))
        monkeypatch.setenv("ATRAPA_PID_NPM", str(self.pid))
        self._monkeypatch = monkeypatch

    def ustaw(self, **zmienne: str) -> None:
        for nazwa, wartosc in zmienne.items():
            self._monkeypatch.setenv(nazwa, wartosc)

    @property
    def wywolania(self) -> list[tuple[Path, str]]:
        if not self.log.exists():
            return []
        wynik = []
        for linia in self.log.read_text(encoding="utf-8").splitlines():
            katalog, _, argumenty = linia.partition("|")
            wynik.append((Path(katalog), argumenty))
        return wynik

    @property
    def polecenia(self) -> list[str]:
        return [argumenty for _, argumenty in self.wywolania]


@pytest.fixture
def npm(tmp_path, monkeypatch):
    return AtrapaNpm(tmp_path, monkeypatch)


def _kopia_z_webem(tmp_path: Path) -> Path:
    kopia = tmp_path / "kopia-frontu"
    (kopia / "web" / "src").mkdir(parents=True)
    return kopia


# --- skip, zero testow i brak raportu to kod 2 (main na prawdziwym pytescie) ---


def test_test_pominiety_juz_w_przebiegu_bez_mutacji_to_kod_2_z_brakiem_dowodu_a_nie_przezywajacy_mutant(
    tmp_path, capsys
):
    repo = _repo_z_plikami(tmp_path, POMIJANY_I_MODUL, _mutant(SZUKAJ, ZABIJA, test=TEST_POMIJANY))

    kod = _uruchom(repo, TABELA_W_REPO)

    zrzut = capsys.readouterr()
    assert kod == 2  # bez kontroli raportu: pominiecie to kod pytesta 0, czyli "przezyl" (kod 1)
    assert zrzut.out == ""
    assert "pominięty" in zrzut.err
    assert "brak dowodu" in zrzut.err
    assert TEST_POMIJANY in zrzut.err


def test_test_pominiety_dopiero_po_mutacji_to_kod_2_a_nie_zabity_ani_przezywajacy_mutant(
    tmp_path, capsys
):
    repo = _repo_z_plikami(
        tmp_path,
        SKIP_PO_MUTACJI_I_MODUL,
        _mutant(SZUKAJ, ZABIJA, test=TEST_SKIP_PO_MUTACJI),
    )

    kod = _uruchom(repo, TABELA_W_REPO)

    zrzut = capsys.readouterr()
    assert kod == 2  # przebieg bazowy jest czysty, wiec tylko raport drugiego przebiegu to wykrywa
    assert zrzut.out == ""
    assert "pominięty" in zrzut.err


def test_zmienna_wymagaj_bazy_z_procesu_wolajacego_nie_zamienia_skipa_w_zabitego_mutanta(
    tmp_path, monkeypatch, capsys
):
    # Z COCKPIT_WYMAGAJ_BAZY=1 skip testu staje sie porazka (kod 1 = "zabity", kod wyjscia 0).
    monkeypatch.setenv("COCKPIT_WYMAGAJ_BAZY", "1")
    repo = _repo_z_plikami(
        tmp_path,
        SKIP_PO_MUTACJI_I_MODUL,
        _mutant(SZUKAJ, ZABIJA, test=TEST_SKIP_PO_MUTACJI),
    )

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 2
    assert "pominięty" in capsys.readouterr().err


def test_uruchomienie_testu_nie_usuwa_zmiennej_wymagaj_bazy_ze_srodowiska_wolajacego(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COCKPIT_WYMAGAJ_BAZY", "1")
    kopia = _kopia_testowa(tmp_path / "kopia", {"test_a.py": "def test_ok():\n    assert True\n"})

    mutanty_reczne.uruchom_test(kopia, "tests/test_a.py::test_ok", 60)

    assert os.environ["COCKPIT_WYMAGAJ_BAZY"] == "1"


def test_zmienna_wymagaj_bazy_nie_trafia_do_procesu_pytesta(tmp_path, monkeypatch):
    # Test sam sprawdza swoje srodowisko: ze zmienna w procesie pytesta bylby czerwony (kod 1).
    monkeypatch.setenv("COCKPIT_WYMAGAJ_BAZY", "1")
    kopia = _kopia_testowa(
        tmp_path / "kopia",
        {
            "test_a.py": (
                "import os\n\n\ndef test_bez_zmiennej():\n"
                "    assert 'COCKPIT_WYMAGAJ_BAZY' not in os.environ\n"
            )
        },
    )

    assert mutanty_reczne.uruchom_test(kopia, "tests/test_a.py::test_bez_zmiennej", 60) == 0


# --- uruchom_test: raport JUnit ---------------------------------------------


def _podstawka_raportu(monkeypatch) -> list[tuple[Path, str]]:
    wywolania: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        mutanty_reczne, "sprawdz_raport_junit", lambda sciezka, test: wywolania.append((sciezka, test))
    )
    return wywolania


def test_raport_junit_ma_stala_nazwe_i_jest_zapisywany_w_kopii_przez_uruchom_test(tmp_path):
    kopia = _kopia_testowa(tmp_path / "kopia", {"test_a.py": "def test_ok():\n    assert True\n"})

    mutanty_reczne.uruchom_test(kopia, "tests/test_a.py::test_ok", 60)

    assert mutanty_reczne.RAPORT_JUNIT == ".mutanty-reczne-junit.xml"
    assert (kopia / ".mutanty-reczne-junit.xml").is_file()


def test_test_zielony_jest_sprawdzany_raportem_junit_z_kopii(tmp_path, monkeypatch):
    wywolania = _podstawka_raportu(monkeypatch)
    kopia = _kopia_testowa(tmp_path / "kopia", {"test_a.py": "def test_ok():\n    assert True\n"})

    kod = mutanty_reczne.uruchom_test(kopia, "tests/test_a.py::test_ok", 60)

    assert kod == 0
    assert wywolania == [(kopia / mutanty_reczne.RAPORT_JUNIT, "tests/test_a.py::test_ok")]


def test_test_czerwony_tez_jest_sprawdzany_raportem_junit(tmp_path, monkeypatch):
    wywolania = _podstawka_raportu(monkeypatch)
    kopia = _kopia_testowa(tmp_path / "kopia", {"test_a.py": "def test_zle():\n    assert False\n"})

    kod = mutanty_reczne.uruchom_test(kopia, "tests/test_a.py::test_zle", 60)

    assert kod == 1
    assert len(wywolania) == 1


def test_zly_node_id_nie_jest_sprawdzany_raportem_bo_kod_pytesta_4_ocenia_wolajacy(
    tmp_path, monkeypatch
):
    wywolania = _podstawka_raportu(monkeypatch)
    kopia = _kopia_testowa(tmp_path / "kopia", {"test_a.py": "def test_ok():\n    assert True\n"})

    kod = mutanty_reczne.uruchom_test(kopia, "tests/test_a.py::test_nie_ma", 60)

    assert kod == 4
    assert wywolania == []


def test_test_pominiety_w_kopii_konczy_uruchomienie_bledem_narzedzia_z_node_id(tmp_path):
    kopia = _kopia_testowa(tmp_path / "kopia", {"test_a.py": TEST_POMIJANY_TRESC})

    with pytest.raises(BladNarzedzia) as wyjatek:
        mutanty_reczne.uruchom_test(kopia, "tests/test_a.py::test_pomijany", 60)

    assert "pominięty" in str(wyjatek.value)
    assert "tests/test_a.py::test_pomijany" in str(wyjatek.value)


def test_raport_z_pierwszego_przebiegu_nie_jest_czytany_w_drugim_przebiegu_w_tej_samej_kopii(
    tmp_path,
):
    # Drugi test konczy proces pytesta kodem 0 bez zapisu raportu. Gdyby stary raport zostawal,
    # ten przebieg uchodzilby za udany na podstawie raportu pierwszego.
    kopia = _kopia_testowa(
        tmp_path / "kopia",
        {
            "test_a.py": (
                "import os\n\n\ndef test_ok():\n    assert True\n\n\n"
                "def test_umiera_bez_raportu():\n    os._exit(0)\n"
            )
        },
    )
    assert mutanty_reczne.uruchom_test(kopia, "tests/test_a.py::test_ok", 60) == 0
    assert (kopia / mutanty_reczne.RAPORT_JUNIT).is_file()

    with pytest.raises(BladNarzedzia, match="brak dowodu"):
        mutanty_reczne.uruchom_test(kopia, "tests/test_a.py::test_umiera_bez_raportu", 60)


def test_stary_raport_ktorego_nie_da_sie_usunac_konczy_uruchomienie_testu_bledem_narzedzia(tmp_path):
    # Katalog pod nazwa raportu: `unlink` rzuca OSError (EISDIR albo EPERM zaleznie od systemu).
    kopia = _kopia_testowa(tmp_path / "kopia", {"test_a.py": "def test_ok():\n    assert True\n"})
    raport = kopia / mutanty_reczne.RAPORT_JUNIT
    raport.mkdir()
    (raport / "cos.txt").write_text("x", encoding="utf-8")

    with pytest.raises(BladNarzedzia, match="usunac starego raportu"):
        mutanty_reczne.uruchom_test(kopia, "tests/test_a.py::test_ok", 60)


# --- sprawdz_raport_junit: jednostkowo ----------------------------------------


def _raport(tmp_path: Path, tresc: str) -> Path:
    sciezka = tmp_path / "raport.xml"
    sciezka.write_text(tresc, encoding="utf-8")
    return sciezka


RAPORT_POPRAWNY = '<testsuites><testsuite tests="1" skipped="0" failures="0"/></testsuites>'
NODE_ID = "tests/test_a.py::test_x"


def test_raport_z_jednym_uruchomionym_testem_bez_pominiec_jest_dowodem(tmp_path):
    mutanty_reczne.sprawdz_raport_junit(_raport(tmp_path, RAPORT_POPRAWNY), NODE_ID)


def test_brak_pliku_raportu_to_blad_narzedzia_z_brakiem_dowodu(tmp_path):
    with pytest.raises(BladNarzedzia, match="brak dowodu"):
        mutanty_reczne.sprawdz_raport_junit(tmp_path / "nie-ma.xml", NODE_ID)


def test_raport_bedacy_katalogiem_to_blad_narzedzia_z_brakiem_dowodu(tmp_path):
    katalog = tmp_path / "raport.xml"
    katalog.mkdir()

    with pytest.raises(BladNarzedzia, match="brak dowodu"):
        mutanty_reczne.sprawdz_raport_junit(katalog, NODE_ID)


def test_raport_xml_ktorego_nie_da_sie_sparsowac_to_blad_narzedzia_z_brakiem_dowodu(tmp_path):
    with pytest.raises(BladNarzedzia, match="brak dowodu"):
        mutanty_reczne.sprawdz_raport_junit(_raport(tmp_path, "<testsuite tests="), NODE_ID)


@pytest.mark.parametrize(
    "tresc",
    [
        pytest.param('<testsuite tests="abc" skipped="0"/>', id="tests_nie_jest_liczba"),
        pytest.param('<testsuite tests="1" skipped="x"/>', id="skipped_nie_jest_liczba"),
    ],
)
def test_raport_z_atrybutem_nie_bedacym_liczba_to_blad_narzedzia_z_brakiem_dowodu(tmp_path, tresc):
    with pytest.raises(BladNarzedzia, match="brak dowodu"):
        mutanty_reczne.sprawdz_raport_junit(_raport(tmp_path, tresc), NODE_ID)


@pytest.mark.parametrize(
    "tresc",
    [
        pytest.param('<testsuites><testsuite tests="0" skipped="0"/></testsuites>', id="testsuites"),
        pytest.param('<testsuite tests="0" skipped="0"/>', id="sam_testsuite"),
        pytest.param(
            '<testsuites><testsuite tests="0" skipped="0"/><testsuite tests="0" skipped="0"/></testsuites>',
            id="dwa_puste_suite",
        ),
    ],
)
def test_raport_bez_ani_jednego_uruchomionego_testu_to_blad_narzedzia_z_brakiem_dowodu(
    tmp_path, tresc
):
    with pytest.raises(BladNarzedzia, match="brak dowodu"):
        mutanty_reczne.sprawdz_raport_junit(_raport(tmp_path, tresc), NODE_ID)


def test_pominiecie_w_raporcie_to_blad_narzedzia_z_brakiem_dowodu_i_node_id_testu(tmp_path):
    with pytest.raises(BladNarzedzia) as wyjatek:
        mutanty_reczne.sprawdz_raport_junit(
            _raport(tmp_path, '<testsuites><testsuite tests="1" skipped="1"/></testsuites>'),
            NODE_ID,
        )

    assert "pominięty" in str(wyjatek.value)
    assert "brak dowodu" in str(wyjatek.value)
    assert NODE_ID in str(wyjatek.value)


def test_pominiecie_w_sam_testsuite_bez_testsuites_to_blad_narzedzia(tmp_path):
    with pytest.raises(BladNarzedzia, match="pominięty"):
        mutanty_reczne.sprawdz_raport_junit(
            _raport(tmp_path, '<testsuite tests="1" skipped="1"/>'), NODE_ID
        )


def test_sam_testsuite_bez_pominiec_jest_dowodem(tmp_path):
    mutanty_reczne.sprawdz_raport_junit(
        _raport(tmp_path, '<testsuite tests="1" skipped="0"/>'), NODE_ID
    )


def test_pominiecie_w_drugim_testsuite_tez_jest_bledem_bo_skipped_sumuje_sie_po_wszystkich(tmp_path):
    with pytest.raises(BladNarzedzia, match="pominięty"):
        mutanty_reczne.sprawdz_raport_junit(
            _raport(
                tmp_path,
                '<testsuites><testsuite tests="1" skipped="0"/>'
                '<testsuite tests="1" skipped="1"/></testsuites>',
            ),
            NODE_ID,
        )


def test_testy_uruchomione_w_drugim_testsuite_licza_sie_jako_dowod_bo_tests_sumuje_sie_po_wszystkich(
    tmp_path,
):
    mutanty_reczne.sprawdz_raport_junit(
        _raport(
            tmp_path,
            '<testsuites><testsuite tests="0" skipped="0"/>'
            '<testsuite tests="1" skipped="0"/></testsuites>',
        ),
        NODE_ID,
    )


def test_raport_bez_licznika_tests_to_zero_uruchomionych_testow_a_wiec_brak_dowodu(tmp_path):
    with pytest.raises(BladNarzedzia, match="brak dowodu"):
        mutanty_reczne.sprawdz_raport_junit(_raport(tmp_path, '<testsuite skipped="0"/>'), NODE_ID)


def test_raport_bez_licznika_skipped_to_zero_pominiec_gdy_test_sie_uruchomil(tmp_path):
    mutanty_reczne.sprawdz_raport_junit(_raport(tmp_path, '<testsuite tests="1"/>'), NODE_ID)


# --- wymaga_frontu i pod_webem ---------------------------------------------------


@pytest.mark.parametrize(
    ("plik", "test", "oczekiwane"),
    [
        pytest.param("web/src/App.tsx", "tests/test_x.py::t", True, id="plik_pod_web"),
        pytest.param("tests/e2e/x.py", "tests/test_x.py::t", True, id="plik_pod_tests_e2e"),
        pytest.param("tools/x.py", "tests/e2e/test_a.py::t", True, id="test_pod_tests_e2e"),
        pytest.param("webx/a.ts", "tests/test_x.py::t", False, id="katalog_webx_to_nie_web"),
        pytest.param("src/web/a.ts", "tests/test_x.py::t", False, id="web_nie_na_poczatku_sciezki"),
        pytest.param("tests/e2e_x.py", "tests/test_x.py::t", False, id="plik_e2e_x_to_nie_katalog_e2e"),
        pytest.param("tests/e2ex/t.py", "tests/test_x.py::t", False, id="katalog_e2ex_to_nie_e2e"),
        pytest.param("tools/x.py", "tests/e2e_x/test_a.py::t", False, id="test_w_katalogu_e2e_x"),
        pytest.param("tools/x.py", "tests/e2ex/test_a.py::t", False, id="test_w_katalogu_e2ex"),
        pytest.param("tools/x.py", "tests/test_x.py::t", False, id="zwykly_plik_i_zwykly_test"),
        pytest.param("tools/x.py", "x/tests/e2e/test_a.py::t", False, id="e2e_nie_na_poczatku_testu"),
        pytest.param(
            "tools/x.py", "tests/test_x.py::test_a[tests/e2e/x]", False, id="e2e_tylko_w_parametrze"
        ),
    ],
)
def test_mutant_wymaga_frontu_tylko_gdy_plik_lub_test_leza_pod_web_albo_tests_e2e(
    plik, test, oczekiwane
):
    mutant = Mutant(plik, "a", "b", test)

    assert mutanty_reczne.wymaga_frontu(mutant) is oczekiwane


def test_node_id_z_separatorem_tuz_po_katalogu_tests_e2e_tez_wymaga_frontu():
    # Tylko odciecie czesci po `::` zostawia sciezke `tests/e2e`; bez niego drugim elementem
    # jest `e2e::test_a`.
    mutant = Mutant("tools/x.py", "a", "b", "tests/e2e::test_a")

    assert mutanty_reczne.wymaga_frontu(mutant) is True


@pytest.mark.parametrize(
    ("plik", "oczekiwane"),
    [
        pytest.param("web/a", True, id="plik_w_web"),
        pytest.param("web/src/x/y.ts", True, id="gleboko_w_web"),
        pytest.param("tests/e2e/x.py", False, id="e2e_nie_przebudowuje_frontu"),
        pytest.param("webx/a", False, id="webx"),
        pytest.param("src/web/a", False, id="web_nie_na_poczatku"),
    ],
)
def test_front_jest_przebudowywany_po_mutacji_tylko_dla_pliku_pod_web(plik, oczekiwane):
    assert mutanty_reczne.pod_webem(plik) is oczekiwane


# --- zbuduj_front z atrapa npm ----------------------------------------------------


def test_front_bez_zainstalowanych_zaleznosci_najpierw_instaluje_je_przez_npm_ci_potem_buduje(
    tmp_path, npm
):
    kopia = _kopia_z_webem(tmp_path)

    mutanty_reczne.zbuduj_front(kopia, 30)

    assert npm.polecenia == ["ci", "run build"]


def test_front_z_istniejacym_katalogiem_node_modules_jest_tylko_budowany_bez_npm_ci(tmp_path, npm):
    kopia = _kopia_z_webem(tmp_path)
    (kopia / "web" / "node_modules").mkdir()

    mutanty_reczne.zbuduj_front(kopia, 30)

    assert npm.polecenia == ["run build"]


def test_node_modules_bedacy_zwyklym_plikiem_nie_zwalnia_z_npm_ci(tmp_path, npm):
    kopia = _kopia_z_webem(tmp_path)
    (kopia / "web" / "node_modules").write_text("to nie katalog", encoding="utf-8")

    mutanty_reczne.zbuduj_front(kopia, 30)

    assert npm.polecenia[0] == "ci"


def test_drugie_budowanie_w_tej_samej_kopii_nie_instaluje_zaleznosci_ponownie(tmp_path, npm):
    kopia = _kopia_z_webem(tmp_path)

    mutanty_reczne.zbuduj_front(kopia, 30)
    mutanty_reczne.zbuduj_front(kopia, 30)

    assert npm.polecenia == ["ci", "run build", "run build"]


def test_npm_ci_i_npm_run_build_biegna_w_katalogu_web_kopii(tmp_path, npm):
    kopia = _kopia_z_webem(tmp_path)

    mutanty_reczne.zbuduj_front(kopia, 30)

    assert [katalog for katalog, _ in npm.wywolania] == [(kopia / "web").resolve()] * 2


def test_blad_npm_ci_konczy_sie_bledem_narzedzia_z_poleceniem_i_kodem_bez_budowania(tmp_path, npm):
    npm.ustaw(ATRAPA_KOD_CI="47")
    kopia = _kopia_z_webem(tmp_path)

    with pytest.raises(BladNarzedzia) as wyjatek:
        mutanty_reczne.zbuduj_front(kopia, 30)

    assert "npm ci" in str(wyjatek.value)
    assert "47" in str(wyjatek.value)
    assert npm.polecenia == ["ci"]


def test_blad_budowania_to_blad_narzedzia_z_kodem_poleceniem_i_koncowka_wyjscia(tmp_path, npm):
    # Koncowka: dlugie wyjscie ma w komunikacie swoj koniec, a nie poczatek.
    npm.ustaw(
        ATRAPA_KOD_BUILD="41",
        ATRAPA_WYJSCIE_BUILD="POCZATEK" + "x" * 6000 + "KONIEC-STDOUT",
        ATRAPA_BLAD_BUILD="KONIEC-STDERR",
    )
    kopia = _kopia_z_webem(tmp_path)

    with pytest.raises(BladNarzedzia) as wyjatek:
        mutanty_reczne.zbuduj_front(kopia, 30)

    komunikat = str(wyjatek.value)
    assert "npm run build" in komunikat
    assert "41" in komunikat
    assert "KONIEC-STDOUT" in komunikat
    assert "KONIEC-STDERR" in komunikat
    assert "POCZATEK" not in komunikat


def test_budowanie_z_kodem_0_bez_index_html_to_blad_narzedzia_z_brakiem_dowodu(tmp_path, npm):
    npm.ustaw(ATRAPA_INDEKS="0")
    kopia = _kopia_z_webem(tmp_path)

    with pytest.raises(BladNarzedzia, match="brak dowodu"):
        mutanty_reczne.zbuduj_front(kopia, 30)

    assert not (kopia / "src" / "cockpit" / "static" / "index.html").exists()


def test_udane_budowanie_zostawia_index_html_w_static_kopii(tmp_path, npm):
    kopia = _kopia_z_webem(tmp_path)

    mutanty_reczne.zbuduj_front(kopia, 30)

    assert (kopia / "src" / "cockpit" / "static" / "index.html").is_file()


def test_brak_npm_w_path_to_blad_narzedzia_a_nie_oserror(tmp_path, monkeypatch):
    pusty = tmp_path / "pusty-path"
    pusty.mkdir()
    monkeypatch.setenv("PATH", str(pusty))
    kopia = _kopia_z_webem(tmp_path)

    with pytest.raises(BladNarzedzia, match="npm"):
        mutanty_reczne.zbuduj_front(kopia, 30)


def test_przekroczony_limit_budowania_to_blad_narzedzia_a_proces_npm_ginie(
    tmp_path, npm, monkeypatch
):
    npm.ustaw(ATRAPA_USPIJ="60")
    kopia = _kopia_z_webem(tmp_path)
    (kopia / "web" / "node_modules").mkdir()
    # Limit 5 s liczy sie dopiero od chwili, gdy potomek atrapy zapisal PID; atrapa spi 60 s,
    # wiec bez limitu (`communicate()`) albo bez zabicia grupy test oblewa.
    zapis = _podmien_popen_na_zegar_od_potomka(monkeypatch, npm.pid)
    pid = None
    try:
        with pytest.raises(BladNarzedzia, match="przekroczony"):
            mutanty_reczne.zbuduj_front(kopia, 5)

        assert time.monotonic() - zapis["zdarzenie"] < 50
        pid = int(npm.pid.read_text(encoding="utf-8"))
        _czekaj_synchronicznie(
            lambda: not czy_zyje(pid), LIMIT_ZNIKNIECIA_POTOMKA, f"zniknięcie potomka npm {pid}"
        )
    finally:
        if pid is not None:
            zabij_proces(pid)


def test_npm_zabity_sygnalem_to_blad_narzedzia_z_kodem_sygnalu_a_nie_udane_budowanie(tmp_path, npm):
    # Kod procesu zabitego sygnalem jest ujemny; `kod > 0` uznaloby go za sukces.
    npm.ustaw(ATRAPA_ZABIJ_BUILD="1")
    kopia = _kopia_z_webem(tmp_path)
    (kopia / "web" / "node_modules").mkdir()

    with pytest.raises(BladNarzedzia) as wyjatek:
        mutanty_reczne.zbuduj_front(kopia, 30)

    assert "npm run build" in str(wyjatek.value)
    assert "-9" in str(wyjatek.value)


def test_wyjscie_npm_w_zlym_utf8_nie_psuje_udanego_budowania(tmp_path, npm):
    npm.ustaw(ATRAPA_BAJTY_BUILD="1")
    kopia = _kopia_z_webem(tmp_path)
    (kopia / "web" / "node_modules").mkdir()

    mutanty_reczne.zbuduj_front(kopia, 30)

    assert (kopia / "src" / "cockpit" / "static" / "index.html").is_file()


def test_nieczytelny_katalog_node_modules_to_blad_narzedzia_a_nie_wyjatek_systemowy(
    tmp_path, npm, monkeypatch
):
    kopia = _kopia_z_webem(tmp_path)

    def zabroniony(self, *args, **kwargs):
        raise PermissionError(13, "brak praw")

    # Od Pythona 3.14 `is_dir` polyka bledy systemowe, wiec galaz da sie wyzwolic tylko tak.
    with monkeypatch.context() as kontekst:
        kontekst.setattr(Path, "is_dir", zabroniony)
        with pytest.raises(BladNarzedzia, match="node_modules"):
            mutanty_reczne.zbuduj_front(kopia, 30)

    assert npm.polecenia == []


def test_nieczytelny_index_html_po_budowaniu_to_blad_narzedzia_a_nie_wyjatek_systemowy(
    tmp_path, npm, monkeypatch
):
    kopia = _kopia_z_webem(tmp_path)
    (kopia / "web" / "node_modules").mkdir()

    def zabroniony(self, *args, **kwargs):
        raise PermissionError(13, "brak praw")

    with monkeypatch.context() as kontekst:
        kontekst.setattr(Path, "is_file", zabroniony)
        with pytest.raises(BladNarzedzia, match="index.html"):
            mutanty_reczne.zbuduj_front(kopia, 30)


# --- kolejnosc budowania w przebiegu mutanta (main + atrapa npm + prawdziwy pytest) ---


def test_mutant_w_web_zabija_test_tylko_dzieki_przebudowaniu_frontu_po_mutacji(tmp_path, npm):
    # Test czyta static zbudowany z web/src/x.txt. Mutacja A -> B zmienia wynik testu wylacznie
    # wtedy, gdy front zostal zbudowany drugi raz: po mutacji i przed drugim przebiegiem testu.
    # Przebieg bazowy przechodzi tylko wtedy, gdy front zbudowano juz przed nim (inaczej kod 2).
    tabela = _mutant("A", "B", test=TEST_STATIC, plik=PLIK_WEB)
    repo = _repo_z_plikami(tmp_path, FRONT_I_MODUL, tabela)

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 0
    assert npm.polecenia == ["ci", "run build", "run build"]


def test_mutant_w_web_ktorego_test_nie_widzi_zmiany_przezywa_mimo_przebudowy(tmp_path, npm, capsys):
    # Mutacja "A\n" -> "A\n\n": test robi strip(), wiec static sie nie zmienia. Przebudowa
    # nie moze zrobic z niej zabitego mutanta.
    tabela = _mutant("A\n", "A\n\n", test=TEST_STATIC, plik=PLIK_WEB)
    repo = _repo_z_plikami(tmp_path, FRONT_I_MODUL, tabela)

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 1
    assert capsys.readouterr().out.startswith(f"{PLIK_WEB}: ")
    assert npm.polecenia == ["ci", "run build", "run build"]


def test_blad_drugiego_budowania_po_mutacji_to_kod_2_a_nie_zabity_mutant(tmp_path, npm, capsys):
    # Atrapa pada tylko gdy web/src/x.txt zawiera "B", czyli dopiero po mutacji.
    npm.ustaw(ATRAPA_PADNIJ_NA="B")
    tabela = _mutant("A", "B", test=TEST_STATIC, plik=PLIK_WEB)
    repo = _repo_z_plikami(tmp_path, FRONT_I_MODUL, tabela)

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 2
    assert capsys.readouterr().err.startswith("blad: ")


def test_blad_pierwszego_budowania_to_kod_2_i_budowanie_nie_jest_ponawiane(tmp_path, npm, capsys):
    npm.ustaw(ATRAPA_KOD_BUILD="1")
    tabela = _mutant("A", "B", test=TEST_STATIC, plik=PLIK_WEB)
    repo = _repo_z_plikami(tmp_path, FRONT_I_MODUL, tabela)

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 2
    assert capsys.readouterr().err.startswith("blad: ")
    assert npm.polecenia == ["ci", "run build"]


def test_mutant_poza_web_z_testem_spoza_e2e_w_ogole_nie_wola_npm(tmp_path, npm):
    repo = _repo(tmp_path, TABELA_ZABITY)

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 0
    assert npm.wywolania == []


def test_mutant_z_testem_e2e_i_plikiem_spoza_web_buduje_front_raz_przed_baza_bez_przebudowy(
    tmp_path, npm, capsys
):
    # Test e2e wymaga static/index.html juz w przebiegu bazowym; plik poza web/ nie zmienia
    # frontu, wiec po mutacji nie ma ponownego budowania.
    repo = _repo_z_plikami(tmp_path, FRONT_I_MODUL, _mutant(SZUKAJ, ZABIJA, test=TEST_E2E))

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 0, capsys.readouterr().err
    assert npm.polecenia == ["ci", "run build"]


def test_mutant_w_web_z_testem_e2e_buduje_front_dwa_razy_a_npm_ci_tylko_raz(tmp_path, npm):
    tabela = _mutant("A", "B", test=TEST_E2E, plik=PLIK_WEB)
    repo = _repo_z_plikami(tmp_path, FRONT_I_MODUL, tabela)

    _uruchom(repo, TABELA_W_REPO)

    assert npm.polecenia == ["ci", "run build", "run build"]


def test_kazdy_mutant_frontu_buduje_w_nowej_kopii_wiec_npm_ci_idzie_osobno_dla_kazdego(tmp_path, npm):
    tabela = _mutant("A", "B", test=TEST_STATIC, plik=PLIK_WEB) + _mutant(
        "A", "C", test=TEST_STATIC, plik=PLIK_WEB
    )
    repo = _repo_z_plikami(tmp_path, FRONT_I_MODUL, tabela)

    assert _uruchom(repo, TABELA_W_REPO) == 0

    assert npm.polecenia.count("ci") == 2
    assert npm.polecenia.count("run build") == 4


# --- --czy-front -------------------------------------------------------------------


def _zakaz_uruchamiania(monkeypatch) -> None:
    def zakazane(*args, **kwargs):
        raise AssertionError("tryb --czy-front nie uruchamia testow ani budowania")

    for nazwa in ("uruchom_test", "zbuduj_front", "_zabity"):
        monkeypatch.setattr(mutanty_reczne, nazwa, zakazane, raising=False)


TABELA_FRONT = _mutant("A", "B", test="tests/test_x.py::t", plik=PLIK_WEB)
TABELA_FRONT_PRZEZ_TEST = _mutant(SZUKAJ, ZABIJA, test=TEST_E2E)
TABELA_BEZ_FRONTU = _mutant(SZUKAJ, ZABIJA, test=TEST_DODAWANIE)


@pytest.mark.parametrize(
    ("tabela", "oczekiwane"),
    [
        pytest.param(TABELA_FRONT, "front=1\n", id="plik_w_web"),
        pytest.param(TABELA_FRONT_PRZEZ_TEST, "front=1\n", id="test_e2e"),
        pytest.param(TABELA_BEZ_FRONTU, "front=0\n", id="bez_frontu"),
        pytest.param(TABELA_BEZ_FRONTU + TABELA_FRONT, "front=1\n", id="front_w_ostatnim_wpisie"),
        pytest.param(TABELA_FRONT + TABELA_BEZ_FRONTU, "front=1\n", id="front_w_pierwszym_wpisie"),
    ],
)
def test_czy_front_wypisuje_dokladnie_jedna_linie_front_1_albo_front_0_i_konczy_sie_kodem_0(
    tmp_path, capsys, monkeypatch, tabela, oczekiwane
):
    repo = _repo(tmp_path, tabela)
    _zakaz_uruchamiania(monkeypatch)

    kod = _main([TABELA_W_REPO, "--korzen", str(repo), "--czy-front"])

    zrzut = capsys.readouterr()
    assert kod == 0
    assert zrzut.out == oczekiwane
    assert zrzut.err == ""


def test_czy_front_sprawdza_wszystkie_podane_tabele(tmp_path, capsys, monkeypatch):
    repo = _repo(tmp_path, TABELA_BEZ_FRONTU)
    (repo / "tests" / "mutanty_reczne" / "HAR-1.toml").write_text(TABELA_FRONT, encoding="utf-8")
    _zakaz_uruchamiania(monkeypatch)

    kod = _main([TABELA_W_REPO, "tests/mutanty_reczne/HAR-1.toml", "--korzen", str(repo), "--czy-front"])

    assert kod == 0
    assert capsys.readouterr().out == "front=1\n"


def test_czy_front_dla_zlej_tabeli_konczy_sie_kodem_2_bez_linii_front(tmp_path, capsys, monkeypatch):
    repo = _repo(tmp_path, "mutant = []\n")
    _zakaz_uruchamiania(monkeypatch)

    kod = _main([TABELA_W_REPO, "--korzen", str(repo), "--czy-front"])

    zrzut = capsys.readouterr()
    assert kod == 2
    assert "front=" not in zrzut.out
    assert zrzut.err.startswith("blad: ")


def test_czy_front_dla_brakujacej_tabeli_konczy_sie_kodem_2(tmp_path, capsys, monkeypatch):
    repo = _repo(tmp_path)
    _zakaz_uruchamiania(monkeypatch)

    assert _main(["tests/mutanty_reczne/nie-ma.toml", "--korzen", str(repo), "--czy-front"]) == 2
    assert capsys.readouterr().out == ""


def test_bez_opcji_czy_front_main_nie_wypisuje_linii_front(tmp_path, capsys):
    repo = _repo(tmp_path, TABELA_ZABITY)

    assert _uruchom(repo, TABELA_W_REPO) == 0
    assert "front=" not in capsys.readouterr().out


# --- --limit-budowy ---------------------------------------------------------------


def _podstawka_budowania(monkeypatch) -> list[tuple[Path, float]]:
    wywolania: list[tuple[Path, float]] = []
    monkeypatch.setattr(
        mutanty_reczne,
        "zbuduj_front",
        lambda kopia, limit: wywolania.append((kopia, limit)),
        raising=False,
    )
    return wywolania


TABELA_E2E_DO_PODSTAWKI = _mutant(SZUKAJ, "return a * b", test="tests/e2e/test_x.py::t")


def test_domyslny_limit_budowania_to_900_sekund():
    assert mutanty_reczne.LIMIT_BUDOWY == 900.0


def test_bez_opcji_limit_budowy_front_dostaje_domyslny_limit_budowania(tmp_path, monkeypatch):
    repo = _repo(tmp_path, TABELA_E2E_DO_PODSTAWKI)
    budowania = _podstawka_budowania(monkeypatch)
    _podstawka_testu(monkeypatch)

    assert mutanty_reczne.main([TABELA_W_REPO, "--korzen", str(repo)]) == 0

    assert [limit for _, limit in budowania] == [900.0]


def test_limit_budowy_z_wiersza_polecen_trafia_do_budowania_a_limit_testu_zostaje_osobny(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path, TABELA_E2E_DO_PODSTAWKI)
    budowania = _podstawka_budowania(monkeypatch)
    testy = _podstawka_testu(monkeypatch)

    kod = mutanty_reczne.main(
        [TABELA_W_REPO, "--korzen", str(repo), "--limit", "7", "--limit-budowy", "12.5"]
    )

    assert kod == 0
    assert [limit for _, limit in budowania] == [12.5]
    assert {limit for _, _, limit in testy} == {7}


def test_limit_budowy_trafia_tez_do_przebudowania_frontu_po_mutacji_w_web(tmp_path, monkeypatch):
    # Mutant w web/ buduje front dwa razy: przed przebiegiem bazowym i po mutacji.
    tabela = _mutant("A", "B", test=TEST_STATIC, plik=PLIK_WEB)
    repo = _repo_z_plikami(tmp_path, FRONT_I_MODUL, tabela)
    budowania = _podstawka_budowania(monkeypatch)
    _podstawka_testu(monkeypatch)

    mutanty_reczne.main(
        [TABELA_W_REPO, "--korzen", str(repo), "--limit", "7", "--limit-budowy", "12.5"]
    )

    assert [limit for _, limit in budowania] == [12.5, 12.5]


@pytest.mark.parametrize(
    "limit",
    [
        pytest.param("0", id="zero"),
        pytest.param("-1", id="ujemny"),
        pytest.param("abc", id="nie_liczba"),
        pytest.param("inf", id="nieskonczony"),
        pytest.param("nan", id="nie_liczba_ieee"),
    ],
)
def test_limit_budowy_niedodatni_albo_nie_liczba_jest_bledem_wiersza_polecen(
    tmp_path, monkeypatch, limit
):
    repo = _repo(tmp_path, TABELA_E2E_DO_PODSTAWKI)
    budowania = _podstawka_budowania(monkeypatch)
    testy = _podstawka_testu(monkeypatch)

    kod = _main([TABELA_W_REPO, "--korzen", str(repo), "--limit-budowy", limit])

    assert kod == 2
    assert budowania == []
    assert testy == []


# =============================================================================
# HAR-225: `--sprawdz-wszystkie` - kazda tabela, kazdy wiersz, bez nakladania mutacji
# =============================================================================

PREFIKS_TABEL = "tests/mutanty_reczne/"
OPCJA_WSZYSTKIE = "--sprawdz-wszystkie"
TEST_PARAMETRYZOWANY = "tests/test_param.py::test_parametryzowany"
TEST_PARAM = """\
import pytest


@pytest.mark.parametrize("wartosc", [1, 2])
def test_parametryzowany(wartosc):
    assert wartosc


def test_ab():
    assert True
"""
COMMIT_PRZED_NAPRAWA = "74e176e7937cfaad47b0f1a946ef150c623ca7b7"


def _tabela_w_repo(repo: Path, nazwa: str, tresc: str) -> None:
    (repo / "tests" / "mutanty_reczne" / nazwa).write_text(tresc, encoding="utf-8")


def _sprawdz_wszystkie(korzen: Path, *dodatkowe: str) -> int:
    return _main([OPCJA_WSZYSTKIE, "--korzen", str(korzen), *dodatkowe])


def _linie_tabel(tekst: str) -> list[str]:
    """Linie bledow tabel (`tests/mutanty_reczne/<tabela> #n: powod`) z wyjscia."""
    return [linia for linia in tekst.splitlines() if linia.startswith(PREFIKS_TABEL)]


def _adresy_bledow(tekst: str) -> list[str]:
    """Czesc linii bledu przed pierwszym dwukropkiem: `tabela #n` albo sama `tabela`."""
    return [linia.split(":", 1)[0] for linia in _linie_tabel(tekst)]


def _pliki_repo(repo: Path) -> dict[str, bytes]:
    return {k: v for k, v in _migawka(repo).items() if "__pycache__" not in Path(k).parts}


def _dobry_wiersz(test: str = TEST_DODAWANIE) -> str:
    return _mutant(SZUKAJ, ZABIJA, test)


def _zakaz_wykonywania_mutacji(monkeypatch) -> list[str]:
    """Podmienia wszystko, co naklada mutacje, kopiuje repo albo uruchamia test, na zapis wywolania + wyjatek."""
    wywolania: list[str] = []

    def zakaz(nazwa: str):
        def wolane(*args, **kwargs):
            wywolania.append(nazwa)
            raise AssertionError(f"tryb sprawdzenia nie moze wolac {nazwa}")

        return wolane

    for nazwa in ("zastosuj_mutacje", "uruchom_test", "skopiuj_repo", "_zabity", "zbuduj_front"):
        monkeypatch.setattr(mutanty_reczne, nazwa, zakaz(nazwa))
    return wywolania


def test_poprawne_tabele_daja_kod_0_z_liczba_wierszy_i_tabel_a_repozytorium_zostaje_bez_zmian(
    tmp_path, capsys, monkeypatch
):
    repo = _repo(tmp_path)
    _tabela_w_repo(repo, "HAR-1.toml", _dobry_wiersz() + _mutant("return a - b", "return a + b", TEST_ODEJMOWANIE))
    _tabela_w_repo(repo, "HAR-2.toml", _mutant(SZUKAJ, PRZEZYWA_A))
    # Plik niebedacy tabela (.toml) w katalogu tabel nie jest czytany jako tabela.
    _tabela_w_repo(repo, "notatki.md", "to nie jest toml [[[\n")
    przed = _pliki_repo(repo)
    wywolania = _zakaz_wykonywania_mutacji(monkeypatch)

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == 0, zrzut.err
    assert re.search(r"sprawdzono 3 \w+ w 2 \w+", zrzut.out), zrzut.out
    assert _linie_tabel(zrzut.err) == []
    assert wywolania == []
    assert _pliki_repo(repo) == przed


@pytest.mark.parametrize("numer", [1, 2, 3])
@pytest.mark.parametrize(
    ("szukaj", "ile"),
    [("return a - c", 0), ("return a", 2), ("b", MODUL.count("b"))],
)
def test_szukaj_wystepujace_inaczej_niz_raz_to_blad_wiersza_z_numerem_i_liczba_wystapien(
    tmp_path, capsys, numer, szukaj, ile
):
    wiersze = [_dobry_wiersz(), _dobry_wiersz(), _dobry_wiersz()]
    wiersze[numer - 1] = _mutant(szukaj, "zzz")
    repo = _repo(tmp_path, "".join(wiersze))

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    linie = _linie_tabel(zrzut.err)
    assert kod == 2
    assert len(linie) == 1, zrzut.err
    assert linie[0].startswith(f"{TABELA_W_REPO} #{numer}:")
    assert f"wystepuje {ile} razy" in linie[0]
    assert PLIK_MODULU in linie[0]


def test_bledy_z_kilku_tabel_i_kilku_wierszy_sa_zebrane_naraz_w_kolejnosci_nazw_tabel(tmp_path, capsys):
    repo = _repo(tmp_path)
    zly = _mutant("return a - c", "zzz")
    _tabela_w_repo(repo, "B.toml", _dobry_wiersz() + zly)
    _tabela_w_repo(repo, "A.toml", zly + _dobry_wiersz() + zly)

    kod = _sprawdz_wszystkie(repo)

    assert kod == 2
    assert _adresy_bledow(capsys.readouterr().err) == [
        f"{PREFIKS_TABEL}A.toml #1",
        f"{PREFIKS_TABEL}A.toml #3",
        f"{PREFIKS_TABEL}B.toml #2",
    ]


def test_wiersz_z_nieistniejacym_testem_to_kod_2_bez_nakladania_mutacji_i_bez_zmian_w_repozytorium(
    tmp_path, capsys, monkeypatch
):
    repo = _repo(tmp_path)
    nieistniejacy = "tests/test_modul.py::test_ktorego_nie_ma"
    _tabela_w_repo(repo, "HAR-1.toml", _dobry_wiersz() + _dobry_wiersz(nieistniejacy))
    przed = _pliki_repo(repo)
    wywolania = _zakaz_wykonywania_mutacji(monkeypatch)

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    linie = _linie_tabel(zrzut.err)
    assert kod == 2
    assert len(linie) == 1, zrzut.err
    assert linie[0].startswith(f"{PREFIKS_TABEL}HAR-1.toml #2:")
    assert "nie istnieje" in linie[0]
    assert nieistniejacy in linie[0]
    assert wywolania == []
    assert _pliki_repo(repo) == przed


def test_wiersz_z_plikiem_testu_ktorego_nie_ma_na_dysku_to_blad_tego_wiersza_a_reszta_jest_sprawdzana(
    tmp_path, capsys
):
    repo = _repo(tmp_path)
    _tabela_w_repo(
        repo, "HAR-1.toml", _dobry_wiersz() + _dobry_wiersz("tests/test_brak.py::test_x") + _dobry_wiersz()
    )

    kod = _sprawdz_wszystkie(repo)

    linie = _linie_tabel(capsys.readouterr().err)
    assert kod == 2
    assert len(linie) == 1, linie
    assert linie[0].startswith(f"{PREFIKS_TABEL}HAR-1.toml #2:")
    assert "test_brak.py" in linie[0]


@pytest.mark.parametrize(
    ("test", "oczekiwany_kod"),
    [
        (TEST_PARAMETRYZOWANY, 0),
        (f"{TEST_PARAMETRYZOWANY}[2]", 0),
        (f"{TEST_PARAMETRYZOWANY}[zly]", 2),
        ("tests/test_param.py::test_ab", 0),
        # Prefiks nazwy innej funkcji: bez zawezenia do `[` przeszedlby jako istniejacy.
        ("tests/test_param.py::test_a", 2),
        ("tests/test_param.py::test_parametryz", 2),
    ],
)
def test_node_id_funkcji_parametryzowanej_bez_nawiasu_istnieje_ale_prefiks_innej_funkcji_nie(
    tmp_path, capsys, test, oczekiwany_kod
):
    repo = _repo(tmp_path)
    (repo / "tests" / "test_param.py").write_text(TEST_PARAM, encoding="utf-8")
    _tabela_w_repo(repo, "HAR-1.toml", _dobry_wiersz(test))

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == oczekiwany_kod, zrzut.err
    if oczekiwany_kod == 2:
        linie = _linie_tabel(zrzut.err)
        assert len(linie) == 1
        assert linie[0].startswith(f"{PREFIKS_TABEL}HAR-1.toml #1:")
        assert "nie istnieje" in linie[0]


@pytest.mark.parametrize("zawartosc_katalogu", [{}, {"README.md": "notatki\n"}], ids=["pusty", "tylko_md"])
def test_katalog_tabel_bez_zadnej_tabeli_to_kod_2_a_nie_zielone(tmp_path, capsys, zawartosc_katalogu):
    repo = _repo(tmp_path)
    for nazwa, tresc in zawartosc_katalogu.items():
        _tabela_w_repo(repo, nazwa, tresc)

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.err.startswith("blad: ")
    # Fragment tylko ze strazy braku tabel; ogolna "zero sprawdzonych wierszy" go nie ma.
    assert "brak tabel" in zrzut.err
    assert "mutanty_reczne" in zrzut.err
    assert "sprawdzono" not in zrzut.out


def test_brak_katalogu_tabel_to_kod_2_a_nie_zielone(tmp_path, capsys):
    korzen = tmp_path / "pusty"
    korzen.mkdir()

    kod = _sprawdz_wszystkie(korzen)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.err.startswith("blad: ")
    assert "sprawdzono" not in zrzut.out


def test_tabela_ktorej_wiersze_przeszly_walidacje_ale_jest_ich_zero_to_kod_2_a_nie_zielone(
    tmp_path, capsys, monkeypatch
):
    repo = _repo(tmp_path, _dobry_wiersz())
    monkeypatch.setattr(mutanty_reczne, "wczytaj_tabele", lambda sciezka: [])

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.err.strip() != ""
    assert "sprawdzono" not in zrzut.out


def test_pusta_tabela_bez_wierszy_to_kod_2_z_linia_tabeli(tmp_path, capsys):
    repo = _repo(tmp_path, "mutant = []\n")

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert _adresy_bledow(zrzut.err) == [TABELA_W_REPO]
    assert "sprawdzono" not in zrzut.out


@pytest.mark.parametrize(
    "tresc",
    [
        "to nie jest toml [[[\n",
        "mutant = []\n",
        "",
        "inne = 1\n" + _mutant(SZUKAJ, ZABIJA),
    ],
    ids=["zly_toml", "pusta_lista", "pusty_plik", "nieznany_klucz_najwyzszego_poziomu"],
)
def test_blad_calej_tabeli_ma_linie_bez_numeru_a_pozostale_tabele_sa_dalej_sprawdzane(tmp_path, capsys, tresc):
    repo = _repo(tmp_path)
    _tabela_w_repo(repo, "A.toml", tresc)
    _tabela_w_repo(repo, "B.toml", _dobry_wiersz() + _mutant("return a - c", "zzz"))

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    linie = _linie_tabel(zrzut.err)
    assert kod == 2
    assert len(linie) == 2, zrzut.err
    assert linie[0].startswith(f"{PREFIKS_TABEL}A.toml:")
    assert linie[1].startswith(f"{PREFIKS_TABEL}B.toml #2:")


@pytest.mark.parametrize(
    ("wpis", "fragment"),
    [
        (_wpis(plik=PLIK_MODULU, szukaj=SZUKAJ, zamien=SZUKAJ, test=TEST_DODAWANIE), "tozsamosciowy"),
        (_wpis(plik=PLIK_MODULU, szukaj=SZUKAJ, zamien=ZABIJA), "brak pola 'test'"),
        (_wpis(plik=PLIK_MODULU, szukaj=SZUKAJ, zamien=5, test=TEST_DODAWANIE), "'zamien'"),
        (_wpis(plik="/etc/passwd", szukaj=SZUKAJ, zamien=ZABIJA, test=TEST_DODAWANIE), "/etc/passwd"),
        (_wpis(plik="../poza.py", szukaj=SZUKAJ, zamien=ZABIJA, test=TEST_DODAWANIE), "../poza.py"),
    ],
    ids=["szukaj_rowne_zamien", "brak_pola", "zly_typ", "plik_absolutny", "plik_z_kropkami"],
)
def test_niepoprawny_wpis_ma_linie_z_numerem_i_powodem_a_nastepne_wiersze_sa_dalej_sprawdzane(
    tmp_path, capsys, wpis, fragment
):
    repo = _repo(tmp_path, _dobry_wiersz() + wpis + _mutant("return a - c", "zzz"))

    kod = _sprawdz_wszystkie(repo)

    linie = _linie_tabel(capsys.readouterr().err)
    assert kod == 2
    assert len(linie) == 2, linie
    assert linie[0].startswith(f"{TABELA_W_REPO} #2:")
    assert fragment in linie[0]
    assert linie[1].startswith(f"{TABELA_W_REPO} #3:")
    assert "wystepuje 0 razy" in linie[1]


@pytest.mark.parametrize(
    "zepsuty_plik",
    ["def test_x(:\n    pass\n", "import modul_ktorego_nie_ma\n\n\ndef test_x():\n    pass\n"],
    ids=["blad_skladni", "blad_importu"],
)
def test_blad_kolekcji_pytesta_to_kod_2_a_nie_zielone(tmp_path, capsys, zepsuty_plik):
    repo = _repo(tmp_path)
    (repo / "tests" / "test_zepsuty.py").write_text(zepsuty_plik, encoding="utf-8")
    _tabela_w_repo(repo, "HAR-1.toml", _dobry_wiersz() + _dobry_wiersz("tests/test_zepsuty.py::test_x"))

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.err.strip() != ""
    assert "sprawdzono" not in zrzut.out


def test_przekroczony_limit_czasu_kolekcji_to_kod_2_a_nie_wyjatek_ani_zielone(tmp_path, capsys):
    repo = _repo(tmp_path, _dobry_wiersz())

    kod = _sprawdz_wszystkie(repo, "--limit", "0.001")

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.err.strip() != ""
    assert "sprawdzono" not in zrzut.out


def test_sprawdz_wszystkie_razem_z_tabela_pozycyjna_to_blad_wiersza_polecen(tmp_path, capsys, monkeypatch):
    repo = _repo(tmp_path, _dobry_wiersz())
    wywolania = _zakaz_wykonywania_mutacji(monkeypatch)

    kod = _main([OPCJA_WSZYSTKIE, TABELA_W_REPO, "--korzen", str(repo)])

    assert kod == 2
    assert wywolania == []
    assert "sprawdzono" not in capsys.readouterr().out


def test_skrypt_uruchomiony_jako_program_z_sprawdz_wszystkie_konczy_sie_kodem_2_i_wymienia_zepsuty_wiersz(tmp_path):
    repo = _repo(tmp_path, _dobry_wiersz() + _mutant("return a - c", "zzz"))

    kod, wyjscie, blad = _uruchom_skrypt(OPCJA_WSZYSTKIE, "--korzen", str(repo))

    assert kod == 2
    assert wyjscie == ""
    assert _adresy_bledow(blad) == [f"{TABELA_W_REPO} #2"]


def test_skrypt_uruchomiony_jako_program_z_sprawdz_wszystkie_konczy_sie_kodem_0_dla_poprawnych_tabel(tmp_path):
    repo = _repo(tmp_path, _dobry_wiersz())

    kod, wyjscie, blad = _uruchom_skrypt(OPCJA_WSZYSTKIE, "--korzen", str(repo))

    assert kod == 0, blad
    assert re.search(r"sprawdzono 1 \w+ w 1 \w+", wyjscie), wyjscie


# --- prawdziwe tabele repozytorium --------------------------------------------------


def _git_w_korzeniu(*argv: str) -> tuple[int, bytes, str]:
    proces = uruchom_proces_sync(
        ["git", *argv], cwd=KORZEN, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        wyjscie, blad = proces.communicate(timeout=120)
    finally:
        zakoncz_proces_sync(proces, nazwa=f"git {argv[0]} w korzeniu repozytorium")
    return proces.returncode, wyjscie, blad.decode("utf-8", errors="replace")


def _korzen_gita() -> Path:
    """Korzen repozytorium gita, takze pod mutmut, gdzie KORZEN to podkatalog mutants/ z kodem
    przepisanym przez mutmut (kazda funkcja w wielu kopiach) i bez czesci plikow repozytorium."""
    kod, wyjscie, blad = _git_w_korzeniu("rev-parse", "--show-toplevel")
    assert kod == 0, blad
    return Path(wyjscie.decode("utf-8").strip())


def _wypakuj_commit(commit: str, cel: Path, tabele: set[str]) -> None:
    """Wypakowuje pliki commitu do `cel`; z `tests/mutanty_reczne/` tylko wskazane tabele."""
    # git archive uruchomiony w podkatalogu repozytorium pakuje tylko ten podkatalog. Pod mutmut
    # KORZEN to mutants/, ktorego w starym commicie nie ma - tar bylby pusty.
    kod, tar_bajty, blad = _git_w_korzeniu("-C", str(_korzen_gita()), "archive", "--format=tar", commit)
    assert kod == 0, blad
    with tarfile.open(fileobj=io.BytesIO(tar_bajty)) as archiwum:
        for czlonek in archiwum.getmembers():
            if not czlonek.isfile():
                continue
            if czlonek.name.startswith(PREFIKS_TABEL) and czlonek.name not in tabele:
                continue
            plik = archiwum.extractfile(czlonek)
            assert plik is not None
            sciezka = cel / czlonek.name
            sciezka.parent.mkdir(parents=True, exist_ok=True)
            sciezka.write_bytes(plik.read())


def test_stan_sprzed_naprawy_ma_dokladnie_cztery_zepsute_wiersze_tabel_har_0_i_har_79(tmp_path, capsys):
    # Commit 74e176e to origin/main z czterema zepsutymi wierszami (HAR-225). Obiekt zostaje w historii
    # takze po naprawie; w plytkim klonie (checkout bez historii) nie ma go jak odczytac.
    kod_gita, _, _ = _git_w_korzeniu("cat-file", "-e", f"{COMMIT_PRZED_NAPRAWA}^{{commit}}")
    if kod_gita != 0:
        _, plytki, _ = _git_w_korzeniu("rev-parse", "--is-shallow-repository")
        if plytki.strip() == b"true":
            pytest.skip("plytki klon: brak commitu 74e176e - kryterium sprawdza job z pelna historia")
        pytest.fail(f"brak commitu {COMMIT_PRZED_NAPRAWA} w historii repozytorium")
    stary = tmp_path / "stary-main"
    stary.mkdir()
    _wypakuj_commit(
        COMMIT_PRZED_NAPRAWA, stary, {f"{PREFIKS_TABEL}HAR-0.toml", f"{PREFIKS_TABEL}HAR-79.toml"}
    )

    kod = _sprawdz_wszystkie(stary)

    zrzut = capsys.readouterr()
    assert kod == 2, zrzut.out
    assert sorted(_adresy_bledow(zrzut.err)) == sorted(
        [
            f"{PREFIKS_TABEL}HAR-0.toml #2",
            f"{PREFIKS_TABEL}HAR-0.toml #7",
            f"{PREFIKS_TABEL}HAR-0.toml #10",
            f"{PREFIKS_TABEL}HAR-79.toml #35",
        ]
    ), zrzut.err


def test_wszystkie_tabele_recznych_mutantow_w_repozytorium_wskazuja_istniejacy_kod_i_testy(capsys):
    # Tabele celuja w oryginalny kod: pod mutmut kopia w mutants/ ma funkcje powielone przez
    # mutmut, wiec `szukaj` wystepowaloby tam wiele razy.
    kod = _sprawdz_wszystkie(_korzen_gita())

    zrzut = capsys.readouterr()
    assert kod == 0, zrzut.err
    assert re.search(r"sprawdzono \d+ \w+ w \d+ \w+", zrzut.out), zrzut.out


# --- HAR-225, runda poprawkowa: plik wiersza, kolekcja, srodowisko kolekcji ------------


def _bledy_wierszy(tekst: str) -> dict[str, str]:
    """Adres (`tabela #n`) -> cala linia bledu."""
    return {linia.split(":", 1)[0]: linia for linia in _linie_tabel(tekst)}


def test_wiersz_z_plikiem_ktorego_nie_ma_w_repozytorium_to_blad_tego_wiersza_z_nazwa_pliku_a_reszta_jest_sprawdzana(
    tmp_path, capsys
):
    repo = _repo(tmp_path)
    _tabela_w_repo(
        repo,
        "HAR-1.toml",
        _dobry_wiersz() + _mutant(SZUKAJ, ZABIJA, plik="src/brak.py") + _dobry_wiersz(),
    )

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == 2, zrzut.err
    assert list(_bledy_wierszy(zrzut.err)) == [f"{PREFIKS_TABEL}HAR-1.toml #2"], zrzut.err
    assert "src/brak.py" in zrzut.err


def test_wiersz_z_plikiem_w_kodowaniu_innym_niz_utf8_to_blad_z_nazwa_pliku_a_nie_wyjatek(tmp_path, capsys):
    repo = _repo(tmp_path)
    # `szukaj` wystepuje w bajtach dokladnie raz: tylko kontrola UTF-8 odrzuca ten plik.
    (repo / "src" / "zly.py").write_bytes(b"return a + b\n# \xff\xfe\n")
    _tabela_w_repo(
        repo,
        "HAR-1.toml",
        _dobry_wiersz() + _mutant(SZUKAJ, ZABIJA, plik="src/zly.py") + _dobry_wiersz(),
    )

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    linie = _bledy_wierszy(zrzut.err)
    assert kod == 2, zrzut.err
    assert list(linie) == [f"{PREFIKS_TABEL}HAR-1.toml #2"], zrzut.err
    assert "UTF-8" in linie[f"{PREFIKS_TABEL}HAR-1.toml #2"]
    assert "src/zly.py" in linie[f"{PREFIKS_TABEL}HAR-1.toml #2"]


@pytest.mark.parametrize("zawartosc", [None, b"return a + b\n# \xff\xfe\n"], ids=["brak_pliku", "zle_kodowanie"])
def test_ten_sam_nieczytelny_plik_wskazany_przez_dwa_wiersze_daje_blad_przy_obu(tmp_path, capsys, zawartosc):
    repo = _repo(tmp_path)
    if zawartosc is not None:
        (repo / "src" / "nieczytelny.py").write_bytes(zawartosc)
    wiersz = _mutant(SZUKAJ, ZABIJA, plik="src/nieczytelny.py")
    _tabela_w_repo(repo, "HAR-1.toml", wiersz + _dobry_wiersz() + wiersz)

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == 2, zrzut.err
    assert list(_bledy_wierszy(zrzut.err)) == [
        f"{PREFIKS_TABEL}HAR-1.toml #1",
        f"{PREFIKS_TABEL}HAR-1.toml #3",
    ], zrzut.err
    # Komunikat OSError powtarza pelna sciezke, wiec liczymy linie, a nie wystapienia.
    assert sum("src/nieczytelny.py" in linia for linia in zrzut.err.splitlines()) == 2


def test_plik_testu_ktorego_dysk_nie_pozwala_sprawdzic_to_blad_tego_wiersza_a_reszta_jest_sprawdzana(
    tmp_path, capsys, monkeypatch
):
    repo = _repo(tmp_path)
    (repo / "tests" / "test_nieczytelny.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    _tabela_w_repo(
        repo,
        "HAR-1.toml",
        _dobry_wiersz() + _dobry_wiersz("tests/test_nieczytelny.py::test_x") + _dobry_wiersz(),
    )
    oryginal = Path.is_file

    def is_file(self, *args, **kwargs):
        if self.name == "test_nieczytelny.py":
            raise PermissionError(13, "brak praw")
        return oryginal(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", is_file)

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == 2, zrzut.err
    assert "Traceback" not in zrzut.err
    assert list(_bledy_wierszy(zrzut.err)) == [f"{PREFIKS_TABEL}HAR-1.toml #2"], zrzut.err
    assert "nie da sie sprawdzic pliku testu" in zrzut.err
    assert "tests/test_nieczytelny.py" in zrzut.err
    assert "sprawdzono" not in zrzut.out


def test_kolekcja_zakonczona_kodem_innym_niz_zero_to_kod_2_nawet_gdy_wszystkie_node_id_zostaly_zebrane(
    tmp_path, capsys
):
    # Sprawdzone w zrodlach pytesta (main.py, wrap_session): `pytest_sessionfinish` moze ustawic
    # `session.exitstatus`, a wolajacy dostaje wlasnie ten kod. Node id sa wypisane, kod jest 1.
    repo = _repo(tmp_path, _dobry_wiersz())
    (repo / "tests" / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 1\n", encoding="utf-8"
    )

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == 2, zrzut.out
    assert "blad: kolekcja testow zakonczyla sie kodem 1" in zrzut.err
    # Koncowka wyjscia pytesta (zebrany node id) jest w komunikacie bledu.
    assert TEST_DODAWANIE in zrzut.err
    assert _linie_tabel(zrzut.err) == []
    assert "sprawdzono" not in zrzut.out


def test_test_oznaczony_markerem_wykluczonym_w_addopts_projektu_jest_zebrany_wiec_wiersz_istnieje(
    tmp_path, capsys
):
    # PYPROJECT_MINI ma `addopts = "-m 'not wolny'"`. Odznaczone testy nie sa wypisane przez
    # `--collect-only -q` (mark/__init__.py: deselect_by_mark usuwa je z `items`), wiec bez
    # `-o addopts=` wiersz dostalby "nie istnieje".
    repo = _repo(tmp_path)
    (repo / "tests" / "test_wolny.py").write_text(
        "import pytest\n\n\n@pytest.mark.wolny\ndef test_wolny():\n    assert True\n", encoding="utf-8"
    )
    _tabela_w_repo(repo, "HAR-1.toml", _dobry_wiersz("tests/test_wolny.py::test_wolny"))

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == 0, zrzut.err
    assert _linie_tabel(zrzut.err) == []


def test_kolekcja_widzi_modul_z_katalogu_src_repozytorium_bez_instalacji_i_bez_pythonpath_z_zewnatrz(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    repo = _repo(tmp_path)
    (repo / "src" / "tylko_w_src.py").write_text("WARTOSC = 1\n", encoding="utf-8")
    (repo / "tests" / "test_import_z_src.py").write_text(
        "from tylko_w_src import WARTOSC\n\n\ndef test_import():\n    assert WARTOSC == 1\n", encoding="utf-8"
    )
    _tabela_w_repo(repo, "HAR-1.toml", _dobry_wiersz("tests/test_import_z_src.py::test_import"))

    kod = _sprawdz_wszystkie(repo)

    assert kod == 0, capsys.readouterr().err


def test_kolekcja_zachowuje_pythonpath_wolajacego_obok_katalogu_src_repozytorium(tmp_path, capsys, monkeypatch):
    zewnetrzny = tmp_path / "zewnetrzny"
    zewnetrzny.mkdir()
    (zewnetrzny / "modul_zewnetrzny.py").write_text("WARTOSC = 2\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(zewnetrzny))
    repo = _repo(tmp_path)
    (repo / "tests" / "test_dwa_zrodla.py").write_text(
        "import modul_zewnetrzny\nfrom modul import dodaj\n\n\n"
        "def test_oba():\n    assert modul_zewnetrzny.WARTOSC == dodaj(1, 1)\n",
        encoding="utf-8",
    )
    _tabela_w_repo(repo, "HAR-1.toml", _dobry_wiersz("tests/test_dwa_zrodla.py::test_oba"))

    kod = _sprawdz_wszystkie(repo)

    assert kod == 0, capsys.readouterr().err


def test_sprawdzenie_z_blednym_plikiem_testu_nie_zostawia_cache_pytesta_w_repozytorium_uzytkownika(
    tmp_path, capsys
):
    # Sprawdzone w zrodlach (cacheprovider.py, LFPlugin.pytest_collectreport): nieudana kolekcja modulu
    # zapisuje `cache/lastfailed`, czyli bez `-p no:cacheprovider` powstaje `.pytest_cache` w repo.
    repo = _repo(tmp_path)
    (repo / "tests" / "test_zepsuty.py").write_text("def test_x(:\n    pass\n", encoding="utf-8")
    _tabela_w_repo(repo, "HAR-1.toml", _dobry_wiersz() + _dobry_wiersz("tests/test_zepsuty.py::test_x"))
    przed = set(_migawka(repo))

    kod = _sprawdz_wszystkie(repo)

    nowe = sorted(set(_migawka(repo)) - przed)
    assert kod == 2, capsys.readouterr().err
    assert [n for n in nowe if ".pytest_cache" in Path(n).parts] == []


def test_sprawdzenie_nie_zostawia_bajtkodu_w_repozytorium_uzytkownika(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    repo = _repo(tmp_path, _dobry_wiersz())
    przed = set(_migawka(repo))

    kod = _sprawdz_wszystkie(repo)

    nowe = sorted(set(_migawka(repo)) - przed)
    assert kod == 0, capsys.readouterr().err
    assert [n for n in nowe if "__pycache__" in Path(n).parts] == []


def test_sprawdz_wszystkie_razem_z_czy_front_to_blad_wiersza_polecen_a_nie_ciche_pominiecie_opcji(
    tmp_path, capsys, monkeypatch
):
    # Poprawne tabele: gdyby opcja `--czy-front` byla ignorowana, wynik bylby kodem 0.
    repo = _repo(tmp_path, _dobry_wiersz())
    wywolania = _zakaz_wykonywania_mutacji(monkeypatch)

    kod = _main([OPCJA_WSZYSTKIE, "--czy-front", "--korzen", str(repo)])

    zrzut = capsys.readouterr()
    assert kod == 2
    assert wywolania == []
    assert "sprawdzono" not in zrzut.out
    assert "front=" not in zrzut.out


# --- kryterium 0b2ea143 bez historii gita (dziala tez w plytkim klonie CI) ------------

WYPELNIACZ_PLIK = "src/wypelniacz.py"
WYPELNIACZ_TEST = "tests/test_wypelniacz.py::test_wypelniacz"
# Doslowne wiersze z origin/main 74e176e (HAR-0 #2, #7, #10 i HAR-79 #35), przed naprawa.
STARE_WIERSZE = {
    ("HAR-0.toml", 2): {
        "plik": "tools/mutanty_reczne.py",
        "szukaj": '"-o", "addopts=", test]',
        "zamien": "test]",
        "test": "tests/test_mutanty_reczne.py::test_test_oznaczony_markerem_wykluczonym_w_addopts_projektu_nadal_sie_uruchamia",
    },
    ("HAR-0.toml", 7): {
        "plik": "tools/mutanty_reczne.py",
        "szukaj": "if uruchom_test(kopia, mutant.test, limit) != 0:",
        "zamien": "if False:",
        "test": "tests/test_mutanty_reczne.py::test_test_czerwony_juz_bez_mutacji_konczy_sie_kodem_2_zamiast_falszywie_zabitym_mutantem",
    },
    ("HAR-0.toml", 10): {
        "plik": ".github/workflows/mutation.yml",
        "szukaj": '--no-renames --diff-filter=d "origin/$BASE_REF...HEAD"',
        "zamien": '--no-renames "origin/$BASE_REF...HEAD"',
        "test": "tests/test_workflow_mutacje.py::test_tabele_sa_wybierane_zapytaniem_gita_ograniczonym_do_katalogu_tabel_bez_usunietych",
    },
    ("HAR-79.toml", 35): {
        "plik": "src/cockpit/api/app.py",
        "szukaj": "responses=odpowiedzi_bledow(401, 403, 404, 409, 422))",
        "zamien": "responses=odpowiedzi_bledow(401, 404, 409, 422))",
        "test": "tests/test_kontrakt_api.py::test_trasa_opisuje_kody_bledow_ktore_faktycznie_moze_zwrocic",
    },
}
LICZBA_WIERSZY_STARYCH_TABEL = {"HAR-0.toml": 10, "HAR-79.toml": 35}


def _stary_stan_z_aktualnych_plikow(repo: Path) -> None:
    """Tabele HAR-0 i HAR-79 o dawnej numeracji: cztery stare wiersze, reszta to poprawne wypelniacze.

    Stare wiersze celuja w AKTUALNE pliki repozytorium (kopiowane bajt w bajt); pliki testow sa
    atrapami z samymi nazwami funkcji, bo sprawdzenie tylko zbiera node id."""
    (repo / "tests" / "mutanty_reczne").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (repo / WYPELNIACZ_PLIK).parent.mkdir(parents=True, exist_ok=True)
    (repo / WYPELNIACZ_PLIK).write_text("x = 1\n", encoding="utf-8")
    testy: dict[str, set[str]] = {"tests/test_wypelniacz.py": {"test_wypelniacz"}}
    korzen = _korzen_gita()
    for pola in STARE_WIERSZE.values():
        cel = repo / pola["plik"]
        cel.parent.mkdir(parents=True, exist_ok=True)
        cel.write_bytes((korzen / pola["plik"]).read_bytes())
        plik_testu, _, nazwa = pola["test"].partition("::")
        testy.setdefault(plik_testu, set()).add(nazwa)
    for plik_testu, nazwy in testy.items():
        (repo / plik_testu).write_text(
            "".join(f"def {n}():\n    pass\n\n\n" for n in sorted(nazwy)), encoding="utf-8"
        )
    wypelniacz = {"plik": WYPELNIACZ_PLIK, "szukaj": "x = 1", "zamien": "x = 2", "test": WYPELNIACZ_TEST}
    for tabela, ile in LICZBA_WIERSZY_STARYCH_TABEL.items():
        tresc = "".join(_wpis(**STARE_WIERSZE.get((tabela, n), wypelniacz)) for n in range(1, ile + 1))
        _tabela_w_repo(repo, tabela, tresc)


def test_stan_sprzed_naprawy_zbudowany_z_aktualnych_plikow_daje_kod_2_z_dokladnie_czterema_zepsutymi_wierszami(
    tmp_path, capsys
):
    repo = tmp_path / "stary-stan"
    repo.mkdir()
    _stary_stan_z_aktualnych_plikow(repo)

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    linie = _bledy_wierszy(zrzut.err)
    assert kod == 2, zrzut.out
    assert sorted(linie) == sorted(
        [
            f"{PREFIKS_TABEL}HAR-0.toml #2",
            f"{PREFIKS_TABEL}HAR-0.toml #7",
            f"{PREFIKS_TABEL}HAR-0.toml #10",
            f"{PREFIKS_TABEL}HAR-79.toml #35",
        ]
    ), zrzut.err
    assert "wystepuje 0 razy" in linie[f"{PREFIKS_TABEL}HAR-0.toml #2"]
    assert "wystepuje 0 razy" in linie[f"{PREFIKS_TABEL}HAR-0.toml #7"]
    # Liczba wystapien rosnie z kazda nowa trasa o tej samej linii (HAR-172 dodal trzecia),
    # wiec oczekiwanie liczymy z aktualnego pliku; wiersz psuje sie, gdy wystapien jest wiecej niz jedno.
    for klucz in (("HAR-0.toml", 10), ("HAR-79.toml", 35)):
        pola = STARE_WIERSZE[klucz]
        ile = (_korzen_gita() / pola["plik"]).read_text(encoding="utf-8").count(pola["szukaj"])
        assert ile > 1, pola
        assert f"wystepuje {ile} razy" in linie[f"{PREFIKS_TABEL}{klucz[0]} #{klucz[1]}"]


# =============================================================================
# HAR-228: `--pokrycie-kod3` - kazda linia z kodu 3 ma wiersz [[mutant]] albo [[rownowazny]]
# =============================================================================

OPCJA_POKRYCIE = "--pokrycie-kod3"
NAGLOWEK_KODU_3 = "bez mutantów — sprawdź ręcznie"
PLIK_P = "src/a.py"
INNY_PLIK_P = "src/b.py"
# Pierwsza linia ze znakami spoza ASCII: przesuniecia w bajtach i w znakach sie rozjezdzaja.
TRESC_P = 'a = "zażółć"\nb = 2\nc = 3\nd = 4\ne = 5\n'
LINII_W_P = 5
UZASADNIENIE = "zadne wejscie nie odroznia tej linii od wersji bez niej"
# Wiersz [[mutant]] dla pliku spoza listy: tabela musi miec [[mutant]], a ten niczego nie pokrywa.
WIERSZ_NIEZWIAZANY = _mutant("x = 1", "x = 2", plik="src/inny.py")


def _rownowazny(**zmiany) -> str:
    pola = {"plik": PLIK_P, "linia_tekst": "c = 3", "uzasadnienie": UZASADNIENIE}
    for nazwa, wartosc in zmiany.items():
        if wartosc is None:
            del pola[nazwa]
        else:
            pola[nazwa] = wartosc
    return "[[rownowazny]]\n" + "".join(f"{k} = {_toml(v)}\n" for k, v in pola.items()) + "\n"


def _lista_kodu_3(*wpisy: str) -> str:
    return NAGLOWEK_KODU_3 + "\n" + "".join(f"{w}\n" for w in wpisy)


def _pokrycie(
    tmp_path: Path,
    capsys,
    lista: str | bytes | tuple[str, ...] | list[str] | None,
    tabele: tuple[str | bytes, ...] = (),
    *,
    pliki: dict[str, str | bytes] | None = None,
    na_dysku: tuple[str, ...] = (),
    lista_absolutna: bool = False,
) -> tuple[int, str, str]:
    """Uruchamia `main --pokrycie-kod3` w korzeniu w `tmp_path`. `lista`: tuple/list = wpisy z poprawnym
    naglowkiem, str/bytes = surowa zawartosc pliku, None = brak pliku. `tabele` leza w katalogu tabel
    i sa podane na wejsciu; `na_dysku` leza tam tez, ale nie sa podane."""
    korzen = tmp_path / "k"
    for sciezka, tresc in (pliki if pliki is not None else {PLIK_P: TRESC_P, INNY_PLIK_P: TRESC_P}).items():
        (korzen / sciezka).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(tresc, bytes):
            (korzen / sciezka).write_bytes(tresc)
        else:
            (korzen / sciezka).write_text(tresc, encoding="utf-8")
    katalog_tabel = korzen / "tests" / "mutanty_reczne"
    katalog_tabel.mkdir(parents=True, exist_ok=True)
    wejscie: list[str] = []
    for numer, tresc in enumerate(tabele):
        nazwa = f"T{numer}.toml"
        (katalog_tabel / nazwa).write_bytes(tresc if isinstance(tresc, bytes) else tresc.encode("utf-8"))
        wejscie.append(f"tests/mutanty_reczne/{nazwa}")
    for numer, tresc in enumerate(na_dysku):
        (katalog_tabel / f"Dysk{numer}.toml").write_text(tresc, encoding="utf-8")
    plik_listy = korzen / "lista.txt"
    if isinstance(lista, (tuple, list)):
        plik_listy.write_text(_lista_kodu_3(*lista), encoding="utf-8")
    elif isinstance(lista, bytes):
        plik_listy.write_bytes(lista)
    elif isinstance(lista, str):
        plik_listy.write_text(lista, encoding="utf-8")
    argument_listy = str(plik_listy) if lista_absolutna else "lista.txt"
    kod = _main([OPCJA_POKRYCIE, argument_listy, "--korzen", str(korzen), *wejscie])
    zrzut = capsys.readouterr()
    return kod, zrzut.out, zrzut.err


def _brak_dowodu(ile: int) -> str:
    return (
        f"brak dowodu: {ile} linii bez wiersza [[mutant]] i bez [[rownowazny]]"
        " - .claude/rules/test-odroznia-regule.md"
    )


# --- kod 0: linia jest pokryta -------------------------------------------------------------


def test_linia_w_jednoliniowym_szukaj_wiersza_jest_pokryta_i_narzedzie_podaje_liczbe_linii(
    tmp_path, capsys
):
    tabela = _mutant("c = 3", "c = 4", plik=PLIK_P)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,))

    assert kod == 0, blad
    assert wyjscie == "pokryto 1 linii bez mutantów\n"
    assert blad == ""


@pytest.mark.parametrize("linia", [2, 3, 4, 5])
def test_kazda_linia_wieloliniowego_szukaj_od_pierwszej_do_ostatniej_jest_pokryta(tmp_path, capsys, linia):
    # Ostatnia linia zakresu zawiera tylko jeden znak `e` z `e = 5`: szukaj konczy sie w srodku linii.
    tabela = _mutant("b = 2\nc = 3\nd = 4\ne", "b = 0", plik=PLIK_P)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:{linia}"], (tabela,))

    assert kod == 0, blad
    assert wyjscie == "pokryto 1 linii bez mutantów\n"


def test_wszystkie_linie_zakresu_wieloliniowego_szukaj_liczone_sa_do_podsumowania(tmp_path, capsys):
    tabela = _mutant("b = 2\nc = 3\nd = 4", "b = 0", plik=PLIK_P)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:2", f"{PLIK_P}:3", f"{PLIK_P}:4"], (tabela,))

    assert kod == 0, blad
    assert wyjscie == "pokryto 3 linii bez mutantów\n"


def test_szukaj_konczacy_sie_znakiem_nowej_linii_obejmuje_ostatnia_linie_z_trescia(tmp_path, capsys):
    tabela = _mutant("c = 3\n", "c = 4\n", plik=PLIK_P)

    kod, _, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,))

    assert kod == 0, blad


def test_linia_ze_znakami_spoza_ascii_przed_szukaj_nie_przesuwa_numerow_linii(tmp_path, capsys):
    tabela = _mutant("c = 3", "c = 4", plik=PLIK_P)

    kod, _, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,))
    kod_pierwszej, _, blad_pierwszej = _pokrycie(
        tmp_path / "druga", capsys, [f"{PLIK_P}:1"], (_mutant('a = "zażółć"', "a = 1", plik=PLIK_P),)
    )

    assert kod == 0, blad
    assert kod_pierwszej == 0, blad_pierwszej


@pytest.mark.parametrize("tresc", [TRESC_P, TRESC_P.rstrip("\n")], ids=["z_koncowym_enterem", "bez_koncowego_enter"])
def test_ostatnia_linia_pliku_jest_dopuszczalna_niezaleznie_od_koncowego_znaku_nowej_linii(
    tmp_path, capsys, tresc
):
    tabela = _mutant("e = 5", "e = 6", plik=PLIK_P)

    kod, _, blad = _pokrycie(
        tmp_path, capsys, [f"{PLIK_P}:{LINII_W_P}"], (tabela,), pliki={PLIK_P: tresc}
    )

    assert kod == 0, blad


def test_linia_rownowazna_jest_pokryta_gdy_tekst_linii_pliku_ma_wciecie_a_wpis_go_nie_ma(tmp_path, capsys):
    pliki = {PLIK_P: "def f():\n    zwrot = 5   \n"}
    tabela = WIERSZ_NIEZWIAZANY + _rownowazny(linia_tekst="zwrot = 5")

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:2"], (tabela,), pliki=pliki)

    assert kod == 0, blad
    assert wyjscie == "pokryto 1 linii bez mutantów\n"


def test_linia_rownowazna_jest_pokryta_gdy_tekst_we_wpisie_ma_biale_znaki_a_linia_pliku_ich_nie_ma(
    tmp_path, capsys
):
    pliki = {PLIK_P: "zwrot = 5\n"}
    tabela = WIERSZ_NIEZWIAZANY + _rownowazny(linia_tekst="\t  zwrot = 5 \t")

    kod, _, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:1"], (tabela,), pliki=pliki)

    assert kod == 0, blad


def test_pokrycie_z_dwoch_tabel_sumuje_sie_wiersz_mutant_z_jednej_i_rownowazny_z_drugiej(tmp_path, capsys):
    pierwsza = _mutant("b = 2", "b = 3", plik=PLIK_P)
    druga = WIERSZ_NIEZWIAZANY + _rownowazny(linia_tekst="d = 4")

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:4", f"{PLIK_P}:2"], (pierwsza, druga))

    assert kod == 0, blad
    assert wyjscie == "pokryto 2 linii bez mutantów\n"


def test_rownowazny_z_pierwszej_tabeli_nie_ginie_gdy_druga_tabela_ma_tylko_wiersz_mutant(tmp_path, capsys):
    pierwsza = WIERSZ_NIEZWIAZANY + _rownowazny(linia_tekst="d = 4")
    druga = _mutant("b = 2", "b = 3", plik=PLIK_P)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:4", f"{PLIK_P}:2"], (pierwsza, druga))

    assert kod == 0, blad
    assert wyjscie == "pokryto 2 linii bez mutantów\n"


def test_lista_podana_sciezka_bezwzgledna_jest_czytana_tak_samo_jak_wzgledna(tmp_path, capsys):
    tabela = _mutant("c = 3", "c = 4", plik=PLIK_P)

    kod, wyjscie, blad = _pokrycie(
        tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,), lista_absolutna=True
    )

    assert kod == 0, blad
    assert wyjscie == "pokryto 1 linii bez mutantów\n"


def test_tryb_pokrycia_nie_kopiuje_repozytorium_nie_uruchamia_testow_ani_podprocesow(
    tmp_path, capsys, monkeypatch
):
    wywolania = _zakaz_wykonywania_mutacji(monkeypatch)
    for nazwa in ("uruchom", "_uruchom_proces", "_zbierz_node_id"):
        def zakaz(*args, _nazwa=nazwa, **kwargs):
            wywolania.append(_nazwa)
            raise AssertionError(f"tryb pokrycia nie moze wolac {_nazwa}")

        monkeypatch.setattr(mutanty_reczne, nazwa, zakaz)
    tabela = _mutant("c = 3", "c = 4", plik=PLIK_P)

    kod, _, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,))

    assert kod == 0, blad
    assert wywolania == []


# --- kod 1: brak dowodu ---------------------------------------------------------------------


def test_linia_tuz_przed_poczatkiem_zakresu_szukaj_nie_jest_pokryta(tmp_path, capsys):
    tabela = _mutant("c = 3\nd = 4", "c = 0", plik=PLIK_P)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:2"], (tabela,))

    assert kod == 1
    assert wyjscie == f"{PLIK_P}:2\n"
    assert blad.rstrip("\n") == _brak_dowodu(1)


def test_linia_tuz_za_koncem_zakresu_szukaj_nie_jest_pokryta(tmp_path, capsys):
    tabela = _mutant("b = 2\nc = 3", "b = 0", plik=PLIK_P)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:4"], (tabela,))

    assert kod == 1
    assert wyjscie == f"{PLIK_P}:4\n"
    assert blad.rstrip("\n") == _brak_dowodu(1)


def test_szukaj_konczacy_sie_znakiem_nowej_linii_nie_obejmuje_nastepnej_linii(tmp_path, capsys):
    tabela = _mutant("c = 3\n", "c = 4\n", plik=PLIK_P)

    kod, wyjscie, _ = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:4"], (tabela,))

    assert kod == 1
    assert wyjscie == f"{PLIK_P}:4\n"


def test_wiersz_z_tym_samym_szukaj_dla_innego_pliku_nie_pokrywa_linii(tmp_path, capsys):
    tabela = _mutant("c = 3", "c = 4", plik=INNY_PLIK_P)

    kod, wyjscie, _ = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,))

    assert kod == 1
    assert wyjscie == f"{PLIK_P}:3\n"


def test_rownowazny_z_innym_tekstem_linii_nie_pokrywa_linii(tmp_path, capsys):
    tabela = WIERSZ_NIEZWIAZANY + _rownowazny(linia_tekst="c = 33")

    kod, wyjscie, _ = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,))

    assert kod == 1
    assert wyjscie == f"{PLIK_P}:3\n"


def test_rownowazny_dla_innego_pliku_z_tym_samym_tekstem_nie_pokrywa_linii(tmp_path, capsys):
    tabela = WIERSZ_NIEZWIAZANY + _rownowazny(plik=INNY_PLIK_P, linia_tekst="c = 3")

    kod, wyjscie, _ = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,))

    assert kod == 1
    assert wyjscie == f"{PLIK_P}:3\n"


def test_rownowazny_nie_pokrywa_innej_linii_o_innym_tekscie_w_tym_samym_pliku(tmp_path, capsys):
    tabela = WIERSZ_NIEZWIAZANY + _rownowazny(linia_tekst="c = 3")

    kod, wyjscie, _ = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3", f"{PLIK_P}:4"], (tabela,))

    assert kod == 1
    assert wyjscie == f"{PLIK_P}:4\n"


def test_bez_zadnej_tabeli_kazda_linia_z_listy_jest_niepokryta_w_kolejnosci_listy(tmp_path, capsys):
    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:4", f"{INNY_PLIK_P}:2", f"{PLIK_P}:2"])

    assert kod == 1
    assert wyjscie == f"{PLIK_P}:4\n{INNY_PLIK_P}:2\n{PLIK_P}:2\n"
    assert blad.rstrip("\n") == _brak_dowodu(3)


def test_tabela_lezaca_w_katalogu_ale_niepodana_na_wejsciu_nie_pokrywa_linii(tmp_path, capsys):
    na_dysku = _mutant("c = 3", "c = 4", plik=PLIK_P)

    kod, wyjscie, _ = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], na_dysku=(na_dysku,))

    assert kod == 1
    assert wyjscie == f"{PLIK_P}:3\n"


def test_na_wyjsciu_sa_tylko_niepokryte_linie_a_komunikat_liczy_tylko_je(tmp_path, capsys):
    tabela = _mutant("c = 3", "c = 4", plik=PLIK_P)

    kod, wyjscie, blad = _pokrycie(
        tmp_path, capsys, [f"{PLIK_P}:5", f"{PLIK_P}:3", f"{PLIK_P}:2"], (tabela,)
    )

    assert kod == 1
    assert wyjscie == f"{PLIK_P}:5\n{PLIK_P}:2\n"
    assert blad.rstrip("\n") == _brak_dowodu(2)


def test_wiersz_dla_pliku_spoza_listy_nie_jest_sprawdzany_nawet_gdy_jego_szukaj_wystepuje_dwa_razy(
    tmp_path, capsys
):
    pliki = {PLIK_P: TRESC_P, INNY_PLIK_P: "x = 1\nx = 1\n"}
    tabela = _mutant("x = 1", "x = 2", plik=INNY_PLIK_P)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,), pliki=pliki)

    assert kod == 1, blad
    assert wyjscie == f"{PLIK_P}:3\n"


# --- pokrycie: wejscia, na ktorych decyduje tylko jeden element reguly -------------------------------


def test_wiersz_jednego_pliku_z_listy_nie_pokrywa_tej_samej_linii_drugiego_pliku_z_listy(tmp_path, capsys):
    tabela = _mutant("c = 3", "c = 4", plik=INNY_PLIK_P)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3", f"{INNY_PLIK_P}:3"], (tabela,))

    assert kod == 1
    assert wyjscie == f"{PLIK_P}:3\n"
    assert blad.rstrip("\n") == _brak_dowodu(1)


def test_szukaj_zaczynajacy_sie_znakiem_nowej_linii_obejmuje_linie_ktora_ten_znak_konczy(tmp_path, capsys):
    tabela = _mutant("\nc = 3", "\nc = 4", plik=PLIK_P)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:1", f"{PLIK_P}:2", f"{PLIK_P}:3"], (tabela,))

    assert kod == 1
    assert wyjscie == f"{PLIK_P}:1\n"
    assert blad.rstrip("\n") == _brak_dowodu(1)


def test_szukaj_z_polskimi_znakami_w_pierwszej_linii_obejmuje_nastepna_linie_ale_nie_kolejna(tmp_path, capsys):
    # Dlugosc `szukaj` liczona w znakach (14) zamiast w bajtach (18) ucielaby zakres przed znakiem nowej linii.
    szukaj = 'a = "zażółć"\nb'
    tabela = _mutant(szukaj, "a = 0\nb", plik=PLIK_P)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:1", f"{PLIK_P}:2", f"{PLIK_P}:3"], (tabela,))

    assert kod == 1
    assert wyjscie == f"{PLIK_P}:3\n"
    assert blad.rstrip("\n") == _brak_dowodu(1)


def test_znak_podzialu_strony_w_pliku_nie_przesuwa_numerow_linii_ani_ich_liczby(tmp_path, capsys):
    # `splitlines()` tnie takze po \x0c: linii byloby trzy, a edytor i `\n` mowia, ze dwie.
    pliki = {PLIK_P: "a = 1\x0c\nb = 2\n"}
    tabela = WIERSZ_NIEZWIAZANY + _rownowazny(linia_tekst="b = 2")

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:2"], (tabela,), pliki=pliki)
    kod_za_koncem, wyjscie_za_koncem, blad_za_koncem = _pokrycie(
        tmp_path / "druga", capsys, [f"{PLIK_P}:3"], (tabela,), pliki=pliki
    )

    assert kod == 0, blad
    assert wyjscie == "pokryto 1 linii bez mutantów\n"
    assert kod_za_koncem == 2
    assert wyjscie_za_koncem == ""
    assert blad_za_koncem == _blad_narzedzia(f"{PLIK_P}:3: plik ma tylko 2 linii")


def test_plik_wymieniony_na_liscie_kilka_razy_jest_czytany_z_dysku_raz(tmp_path, capsys, monkeypatch):
    odczyty: list[str] = []
    oryginal = mutanty_reczne._odczytaj_utf8

    def zliczaj(sciezka, nazwa):
        odczyty.append(nazwa)
        return oryginal(sciezka, nazwa)

    monkeypatch.setattr(mutanty_reczne, "_odczytaj_utf8", zliczaj)
    tabela = _mutant("b = 2\nc = 3", "b = 0", plik=PLIK_P)

    kod, _, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:2", f"{PLIK_P}:3"], (tabela,))

    assert kod == 0, blad
    assert odczyty.count(PLIK_P) == 1


def test_plik_z_dwukropkiem_w_nazwie_jest_rozpoznany_po_ostatnim_dwukropku_listy(tmp_path, capsys):
    plik = "src/a:b.py"
    tabela = _mutant("c = 3", "c = 4", plik=plik)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{plik}:3"], (tabela,), pliki={plik: TRESC_P})

    assert kod == 0, blad
    assert wyjscie == "pokryto 1 linii bez mutantów\n"


@pytest.mark.parametrize(
    "linia_tekst",
    [
        pytest.param("c =", id="fragment_linii"),
        pytest.param("c = 3 # dopisek", id="dluzszy_tekst_zawierajacy_linie"),
        pytest.param("c = 33", id="inny_tekst_o_wspolnym_poczatku"),
    ],
)
def test_rownowazny_pokrywa_linie_tylko_gdy_tekst_jest_identyczny_a_nie_gdy_jest_jej_fragmentem(
    tmp_path, capsys, linia_tekst
):
    tabela = WIERSZ_NIEZWIAZANY + _rownowazny(linia_tekst=linia_tekst)

    kod, wyjscie, _ = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,))

    assert kod == 1
    assert wyjscie == f"{PLIK_P}:3\n"


# --- kod 2: blad narzedzia - lista --------------------------------------------------------------


def _nie_wpis(wpis: str) -> str:
    return f"wpis {wpis!r} nie ma postaci plik:linia"


def _zla_sciezka(plik: str) -> str:
    return f"plik {plik!r} musi byc niepusta sciezka wzgledna bez segmentu '..'"


def _zly_numer(cyfry: str) -> str:
    return f"numer linii {cyfry!r} musi byc liczba calkowita >= 1 (same cyfry ASCII)"


# Wpis i dokladny powod; lista powstaje niezaleznie od kodu pod testem (retro M1.7).
ZLE_WPISY_LISTY = [
    pytest.param(PLIK_P, _nie_wpis(PLIK_P), id="bez_dwukropka"),
    pytest.param("", _nie_wpis(""), id="pusta_linia_w_srodku"),
    pytest.param(f"{PLIK_P}:abc", _zly_numer("abc"), id="numer_nie_jest_liczba"),
    pytest.param(f"{PLIK_P}:0", _zly_numer("0"), id="numer_zero"),
    pytest.param(f"{PLIK_P}:-1", _zly_numer("-1"), id="numer_ujemny"),
    pytest.param(f"{PLIK_P}:+3", _zly_numer("+3"), id="numer_ze_znakiem_plus"),
    pytest.param(f"{PLIK_P}: 3", _zly_numer(" 3"), id="numer_ze_spacja_z_przodu"),
    pytest.param(f"{PLIK_P}:3 ", _zly_numer("3 "), id="numer_ze_spacja_z_tylu"),
    pytest.param(f"{PLIK_P}:٣", _zly_numer("٣"), id="numer_cyframi_arabsko_indyjskimi"),
    pytest.param(f"{PLIK_P}:", _zly_numer(""), id="pusty_numer"),
    pytest.param(":3", _zla_sciezka(""), id="pusta_sciezka"),
    pytest.param("/etc/passwd:3", _zla_sciezka("/etc/passwd"), id="sciezka_bezwzgledna"),
    pytest.param("../a.py:3", _zla_sciezka("../a.py"), id="sciezka_wychodzi_nad_korzen"),
    pytest.param("src/../src/a.py:3", _zla_sciezka("src/../src/a.py"), id="segment_wyjscia_w_srodku_sciezki"),
]


@pytest.mark.parametrize(("zly", "powod"), ZLE_WPISY_LISTY)
def test_niepoprawny_wpis_listy_kodu_3_to_blad_narzedzia_z_numerem_linii_listy_i_powodem(
    tmp_path, capsys, zly, powod
):
    # Dwa poprawne, pokryte wpisy i zly na linii 4 (nagłówek to linia 1): pominiecie zlego wpisu dalo by 0.
    tabela = _mutant("b = 2\nc = 3", "b = 0", plik=PLIK_P)
    lista = _lista_kodu_3(f"{PLIK_P}:2", f"{PLIK_P}:3", zly)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, lista, (tabela,))

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad == _blad_narzedzia(f"lista kodu 3, linia 4: {powod}")


def test_przy_kilku_niepoprawnych_wpisach_listy_blad_dotyczy_pierwszego_z_nich(tmp_path, capsys):
    lista = _lista_kodu_3(f"{PLIK_P}:abc", "/etc/passwd:3")

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, lista)

    assert kod == 2
    assert wyjscie == ""
    assert blad == _blad_narzedzia(f"lista kodu 3, linia 2: {_zly_numer('abc')}")


def test_lista_bez_naglowka_to_blad_narzedzia_mimo_pokrytych_wpisow(tmp_path, capsys):
    tabela = _mutant("c = 3", "c = 4", plik=PLIK_P)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, f"{PLIK_P}:3\n", (tabela,))

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad == _blad_narzedzia(f"lista kodu 3: pierwsza linia musi byc dokladnie {NAGLOWEK_KODU_3!r}")


@pytest.mark.parametrize(
    "naglowek",
    [
        pytest.param("bez mutantow — sprawdz recznie", id="bez_polskich_znakow"),
        pytest.param(NAGLOWEK_KODU_3 + " ", id="spacja_na_koncu"),
        pytest.param(NAGLOWEK_KODU_3.upper(), id="wielkie_litery"),
        pytest.param("# " + NAGLOWEK_KODU_3, id="przedrostek"),
    ],
)
def test_zly_naglowek_listy_to_blad_narzedzia_mimo_pokrytych_wpisow(tmp_path, capsys, naglowek):
    tabela = _mutant("c = 3", "c = 4", plik=PLIK_P)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, f"{naglowek}\n{PLIK_P}:3\n", (tabela,))

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad == _blad_narzedzia(f"lista kodu 3: pierwsza linia musi byc dokladnie {NAGLOWEK_KODU_3!r}")


def test_lista_z_samym_naglowkiem_to_blad_narzedzia_brak_dowodu(tmp_path, capsys):
    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, NAGLOWEK_KODU_3 + "\n")

    assert kod == 2
    assert wyjscie == ""
    assert blad == _blad_narzedzia("brak dowodu: lista kodu 3 bez wpisów")


def test_pusty_plik_listy_to_blad_narzedzia_o_braku_naglowka_a_nie_zero_linii_do_pokrycia(tmp_path, capsys):
    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, "")

    assert kod == 2
    assert wyjscie == ""
    assert blad == _blad_narzedzia(f"lista kodu 3: pierwsza linia musi byc dokladnie {NAGLOWEK_KODU_3!r}")


def test_brak_pliku_listy_to_blad_narzedzia_z_nazwa_pliku(tmp_path, capsys):
    plik = tmp_path / "k" / "lista.txt"

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, None)

    assert kod == 2
    assert wyjscie == ""
    assert blad == _blad_narzedzia(
        f"lista kodu 3 {plik}: nie da sie odczytac: [Errno 2] No such file or directory: {str(plik)!r}"
    )


def test_lista_w_zlym_utf8_to_blad_narzedzia_a_nie_wyjatek(tmp_path, capsys):
    plik = tmp_path / "k" / "lista.txt"

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, b"\xff\xfe" + f"\n{PLIK_P}:3\n".encode())

    assert kod == 2
    assert wyjscie == ""
    assert blad == _blad_narzedzia(
        f"lista kodu 3 {plik}: to nie jest UTF-8: "
        "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"
    )


# --- kod 2: blad narzedzia - plik z listy ---------------------------------------------------------


def test_plik_z_listy_ktorego_nie_ma_na_dysku_to_blad_narzedzia_z_nazwa_pliku(tmp_path, capsys):
    tabela = _mutant("c = 3", "c = 4", plik="src/brak.py")
    brak = tmp_path / "k" / "src" / "brak.py"

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, ["src/brak.py:3"], (tabela,))

    assert kod == 2
    assert wyjscie == ""
    assert blad == _blad_narzedzia(
        f"src/brak.py: nie da sie odczytac: [Errno 2] No such file or directory: {str(brak)!r}"
    )


def test_plik_z_listy_w_zlym_utf8_to_blad_narzedzia_z_nazwa_pliku(tmp_path, capsys):
    tabela = _mutant("c = 3", "c = 4", plik=PLIK_P)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,), pliki={PLIK_P: b"\xff\xfe"})

    assert kod == 2
    assert wyjscie == ""
    assert blad == _blad_narzedzia(
        f"{PLIK_P}: to nie jest UTF-8: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"
    )


@pytest.mark.parametrize("tresc", [TRESC_P, TRESC_P.rstrip("\n")], ids=["z_koncowym_enterem", "bez_koncowego_enter"])
def test_numer_linii_wiekszy_niz_liczba_linii_pliku_to_blad_narzedzia_a_nie_niepokryta_linia(
    tmp_path, capsys, tresc
):
    tabela = _mutant("e = 5", "e = 6", plik=PLIK_P)

    kod, wyjscie, blad = _pokrycie(
        tmp_path, capsys, [f"{PLIK_P}:{LINII_W_P + 1}"], (tabela,), pliki={PLIK_P: tresc}
    )

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad == _blad_narzedzia(f"{PLIK_P}:{LINII_W_P + 1}: plik ma tylko {LINII_W_P} linii")


def test_pusty_plik_z_listy_ma_zero_linii_wiec_kazdy_numer_to_blad_narzedzia(tmp_path, capsys):
    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:1"], pliki={PLIK_P: ""})

    assert kod == 2
    assert wyjscie == ""
    assert blad == _blad_narzedzia(f"{PLIK_P}:1: plik ma tylko 0 linii")


@pytest.mark.parametrize(
    ("szukaj", "ile"),
    [pytest.param("c = 33", 0, id="zero_wystapien"), pytest.param("= ", 5, id="kilka_wystapien")],
)
def test_szukaj_wystepujacy_w_pliku_z_listy_inaczej_niz_raz_to_blad_narzedzia(tmp_path, capsys, szukaj, ile):
    tabela = _mutant(szukaj, "zzz", plik=PLIK_P)
    assert TRESC_P.count(szukaj) == ile

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,))

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad == _blad_narzedzia(f"{PLIK_P}: szukaj {szukaj!r} wystepuje {ile} razy, a ma dokladnie raz")


# --- kod 2: blad narzedzia - tabela ---------------------------------------------------------------


def _tabela_p(tmp_path: Path, numer: int = 0) -> Path:
    """Sciezka tabeli o numerze `numer` zapisanej przez `_pokrycie`; w komunikatach jest bezwzgledna."""
    return tmp_path / "k" / "tests" / "mutanty_reczne" / f"T{numer}.toml"


def _blad_narzedzia(powod: str) -> str:
    return f"blad: {powod}\n"


def test_tabela_z_bledem_toml_to_blad_narzedzia_mimo_pokrytej_linii_w_drugiej_tabeli(tmp_path, capsys):
    dobra = _mutant("c = 3", "c = 4", plik=PLIK_P)
    zla = "[[mutant\nplik = \n"
    with pytest.raises(tomllib.TOMLDecodeError) as przyczyna:
        tomllib.loads(zla)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (dobra, zla))

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad == _blad_narzedzia(f"{_tabela_p(tmp_path, 1)}: nie da sie odczytac tabeli: {przyczyna.value}")


def test_tabela_w_zlym_utf8_to_blad_narzedzia_w_trybie_pokrycia(tmp_path, capsys):
    dobra = _mutant("c = 3", "c = 4", plik=PLIK_P)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (dobra, b"\xff\xfe\xfd"))

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad == _blad_narzedzia(
        f"{_tabela_p(tmp_path, 1)}: nie da sie odczytac tabeli: "
        "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"
    )


def test_tabela_podana_na_wejsciu_ktorej_nie_ma_na_dysku_to_blad_narzedzia(tmp_path, capsys):
    korzen = tmp_path / "k"
    (korzen / "src").mkdir(parents=True)
    (korzen / PLIK_P).write_text(TRESC_P, encoding="utf-8")
    (korzen / "lista.txt").write_text(_lista_kodu_3(f"{PLIK_P}:3"), encoding="utf-8")
    brak = korzen / "tests" / "mutanty_reczne" / "brak.toml"

    kod = _main([OPCJA_POKRYCIE, "lista.txt", "--korzen", str(korzen), "tests/mutanty_reczne/brak.toml"])

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.out == ""
    assert zrzut.err == _blad_narzedzia(
        f"{brak}: nie da sie odczytac tabeli: [Errno 2] No such file or directory: {str(brak)!r}"
    )


def _zle_rownowazne() -> list:
    """Wadliwy wpis `[[rownowazny]]` i dokladny powod (bez nazwy tabeli i numeru wpisu)."""
    przypadki = []
    for pole in ("plik", "linia_tekst", "uzasadnienie"):
        przypadki.append(
            pytest.param(_rownowazny(**{pole: None}), f"wpis rownowazny: brak pola {pole!r}", id=f"brak_{pole}")
        )
        for wartosc, nazwa in ((5, "liczba"), (True, "bool"), (["c = 3"], "lista")):
            przypadki.append(
                pytest.param(
                    _rownowazny(**{pole: wartosc}),
                    f"wpis rownowazny: {pole!r} nie jest tekstem",
                    id=f"{pole}_{nazwa}",
                )
            )
        for wartosc, nazwa in (("", "pusty"), ("  \t ", "same_biale_znaki")):
            przypadki.append(
                pytest.param(
                    _rownowazny(**{pole: wartosc}), f"wpis rownowazny: {pole!r} jest pusty", id=f"{pole}_{nazwa}"
                )
            )
    przypadki.append(
        pytest.param(_rownowazny(powod="inaczej"), "wpis rownowazny: nieznany klucz 'powod'", id="nieznane_pole")
    )
    przypadki.append(
        pytest.param(
            _rownowazny(zeta="1", alfa="2"),
            "wpis rownowazny: nieznany klucz 'alfa'",
            id="dwa_nieznane_pola_pierwsze_alfabetycznie",
        )
    )
    for plik in ("/etc/passwd", "../a.py", "src/../a.py"):
        przypadki.append(
            pytest.param(
                _rownowazny(plik=plik),
                f"plik {plik!r} musi byc sciezka wzgledna w repozytorium",
                id=f"plik_{plik.replace('/', '_')}",
            )
        )
    return przypadki


ZLE_ROWNOWAZNE = _zle_rownowazne()


@pytest.mark.parametrize(("wadliwy", "powod"), ZLE_ROWNOWAZNE)
def test_niepoprawny_rownowazny_to_blad_narzedzia_z_numerem_wpisu_i_pelnym_powodem(
    tmp_path, capsys, wadliwy, powod
):
    # Linia 3 jest pokryta wierszem [[mutant]]: bez walidacji kod bylby 0, nie 2.
    tabela = _mutant("c = 3", "c = 4", plik=PLIK_P) + wadliwy

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,))

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad == _blad_narzedzia(f"{_tabela_p(tmp_path)}: rownowazny 1: {powod}")


def test_rownowazny_wadliwy_po_poprawnym_ma_numer_drugi_w_komunikacie(tmp_path, capsys):
    tabela = _mutant("c = 3", "c = 4", plik=PLIK_P) + _rownowazny() + _rownowazny(uzasadnienie="  ")

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,))

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad == _blad_narzedzia(f"{_tabela_p(tmp_path)}: rownowazny 2: wpis rownowazny: 'uzasadnienie' jest pusty")


@pytest.mark.parametrize(
    ("poczatek", "koniec"),
    [
        pytest.param("rownowazny = 5\n", "", id="liczba"),
        pytest.param('rownowazny = "napis"\n', "", id="napis"),
        pytest.param("", '[rownowazny]\nplik = "src/a.py"\n', id="tabela_a_nie_lista_tabel"),
    ],
)
def test_rownowazny_ktory_nie_jest_lista_tabel_to_blad_narzedzia_w_trybie_pokrycia(
    tmp_path, capsys, poczatek, koniec
):
    # Klucz `rownowazny = 5` musi stac przed pierwsza tabela, inaczej TOML przypisze go do `[[mutant]]`.
    tabela = poczatek + _mutant("c = 3", "c = 4", plik=PLIK_P) + koniec

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,))

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad == _blad_narzedzia(f"{_tabela_p(tmp_path)}: rownowazny musi byc lista [[rownowazny]]")


def test_rownowazny_bedacy_lista_liczb_a_nie_tabel_to_blad_narzedzia_o_wpisie(tmp_path, capsys):
    tabela = "rownowazny = [5]\n" + _mutant("c = 3", "c = 4", plik=PLIK_P)

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,))

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad == _blad_narzedzia(f"{_tabela_p(tmp_path)}: rownowazny 1: wpis rownowazny nie jest tabela")


@pytest.mark.parametrize(
    "tabela",
    [
        pytest.param(_rownowazny(), id="tylko_rownowazny"),
        pytest.param(_rownowazny() + _rownowazny(linia_tekst="d = 4"), id="kilka_rownowazny"),
    ],
)
def test_tabela_z_samymi_wpisami_rownowazny_bez_mutant_to_blad_narzedzia_takze_w_trybie_pokrycia(
    tmp_path, capsys, tabela
):
    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,))

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad == _blad_narzedzia(f"{_tabela_p(tmp_path)}: brak listy [[mutant]]")


def test_nieznany_klucz_najwyzszego_poziomu_tabeli_to_blad_narzedzia_takze_obok_rownowazny(tmp_path, capsys):
    tabela = _mutant("c = 3", "c = 4", plik=PLIK_P) + _rownowazny() + '[[inny]]\nplik = "x"\n'

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3"], (tabela,))

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad == _blad_narzedzia(f"{_tabela_p(tmp_path)}: nieznany klucz najwyzszego poziomu 'inny'")


def test_blad_tabeli_wygrywa_z_niepokryta_linia_bo_blad_narzedzia_to_kod_2_a_nie_1(tmp_path, capsys):
    tabela = _mutant("c = 3", "c = 4", plik=PLIK_P) + _rownowazny(uzasadnienie="  ")

    kod, wyjscie, blad = _pokrycie(tmp_path, capsys, [f"{PLIK_P}:3", f"{PLIK_P}:4"], (tabela,))

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad == _blad_narzedzia(f"{_tabela_p(tmp_path)}: rownowazny 1: wpis rownowazny: 'uzasadnienie' jest pusty")


def test_blad_pliku_z_listy_wygrywa_z_niepokryta_linia_bo_blad_narzedzia_to_kod_2_a_nie_1(tmp_path, capsys):
    tabela = _mutant("c = 3", "c = 4", plik=PLIK_P)
    brak = tmp_path / "k" / "src" / "brak.py"

    kod, wyjscie, blad = _pokrycie(
        tmp_path, capsys, [f"{PLIK_P}:4", "src/brak.py:1"], (tabela,)
    )

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad == _blad_narzedzia(
        f"src/brak.py: nie da sie odczytac: [Errno 2] No such file or directory: {str(brak)!r}"
    )


# --- wiersz polecenia -----------------------------------------------------------------------------

POKRYCIE_Z_INNYM_TRYBEM = (
    "mutanty_reczne.py: error: --pokrycie-kod3 nie laczy sie z --sprawdz-wszystkie ani z --czy-front"
)


def test_wywolanie_bez_tabeli_i_bez_opcji_pokrycia_to_blad_wiersza_polecen_o_braku_tabeli(tmp_path):
    kod, wyjscie, blad = _uruchom_skrypt("--korzen", str(tmp_path))

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad.splitlines()[-1] == (
        "mutanty_reczne.py: error: podaj co najmniej jedna tabele (albo --sprawdz-wszystkie): "
        "brak tabeli to brak dowodu"
    )


def test_pokrycie_kod3_razem_z_sprawdz_wszystkie_to_blad_wiersza_polecen(tmp_path):
    kod, wyjscie, blad = _uruchom_skrypt(OPCJA_POKRYCIE, "lista.txt", OPCJA_WSZYSTKIE, "--korzen", str(tmp_path))

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad.splitlines()[-1] == POKRYCIE_Z_INNYM_TRYBEM


def test_pokrycie_kod3_razem_z_czy_front_to_blad_wiersza_polecen(tmp_path):
    kod, wyjscie, blad = _uruchom_skrypt(
        OPCJA_POKRYCIE, "lista.txt", "--czy-front", TABELA_W_REPO, "--korzen", str(tmp_path)
    )

    assert kod == 2, blad
    assert wyjscie == ""
    assert blad.splitlines()[-1] == POKRYCIE_Z_INNYM_TRYBEM


def test_pokrycie_kod3_bez_czy_front_i_bez_sprawdz_wszystkie_dziala_z_tabela_pozycyjna(tmp_path, capsys):
    repo = _repo(tmp_path, _mutant("return a + b", "return a - b"))
    (repo / "lista.txt").write_text(_lista_kodu_3(f"{PLIK_MODULU}:2"), encoding="utf-8")

    kod = _main([OPCJA_POKRYCIE, "lista.txt", "--korzen", str(repo), TABELA_W_REPO])

    zrzut = capsys.readouterr()
    assert kod == 0, zrzut.err
    assert zrzut.out == "pokryto 1 linii bez mutantów\n"


def test_wiersz_polecen_przekazuje_trybowi_pokrycia_korzen_liste_i_tabele_jako_sciezki(monkeypatch):
    wywolania: list[tuple] = []

    def podstawka(korzen, lista, tabele):
        wywolania.append((korzen, lista, tabele))
        return 0

    monkeypatch.setattr(mutanty_reczne, "pokrycie_kod3", podstawka)

    kod = _main([OPCJA_POKRYCIE, "lista.txt", "--korzen", "/korzen", "a.toml", "b.toml"])
    kod_bez_tabel = _main([OPCJA_POKRYCIE, "druga.txt", "--korzen", "/korzen"])

    assert (kod, kod_bez_tabel) == (0, 0)
    assert wywolania == [
        (Path("/korzen"), Path("lista.txt"), [Path("a.toml"), Path("b.toml")]),
        (Path("/korzen"), Path("druga.txt"), []),
    ]


def test_pomoc_opisuje_opcje_pokrycia_kodu_3_z_nazwa_argumentu_lista(capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "300")

    kod = _main(["--help"])

    pomoc = " ".join(capsys.readouterr().out.split())
    assert kod == 0
    assert (
        "--pokrycie-kod3 LISTA HAR-228: sprawdz, ze kazda linia z wyjscia kodu 3 mutanty_zmian.py"
        " ma wiersz [[mutant]] albo [[rownowazny]]"
    ) in pomoc


# --- zgodnosc wstecz: pozostale tryby i [[rownowazny]] ------------------------------------------


def test_sprawdz_wszystkie_przyjmuje_tabele_z_poprawnym_rownowazny_i_nie_liczy_go_jako_wiersza(
    tmp_path, capsys
):
    repo = _repo(tmp_path)
    _tabela_w_repo(repo, "HAR-1.toml", _dobry_wiersz() + _rownowazny(plik=PLIK_MODULU, linia_tekst="return a - b"))

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == 0, zrzut.err
    assert re.search(r"sprawdzono 1 \w+ w 1 \w+", zrzut.out), zrzut.out


@pytest.mark.parametrize(("wadliwy", "powod"), ZLE_ROWNOWAZNE)
def test_sprawdz_wszystkie_podaje_pelna_linie_wadliwego_rownowazny_z_numerem_wpisu_rownowazny(
    tmp_path, capsys, wadliwy, powod
):
    repo = _repo(tmp_path)
    dobry = _rownowazny(plik=PLIK_MODULU, linia_tekst="return a - b")
    _tabela_w_repo(repo, "HAR-1.toml", _dobry_wiersz() + dobry + wadliwy)

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert _linie_tabel(zrzut.err) == [f"{PREFIKS_TABEL}HAR-1.toml rownowazny #2: {powod}"]
    assert zrzut.out == ""


def test_sprawdz_wszystkie_podaje_kazdy_wadliwy_rownowazny_osobna_linia_w_kolejnosci_wpisow(tmp_path, capsys):
    repo = _repo(tmp_path)
    _tabela_w_repo(
        repo,
        "HAR-1.toml",
        _dobry_wiersz()
        + _rownowazny(plik=PLIK_MODULU, uzasadnienie=None)
        + _rownowazny(plik=PLIK_MODULU, linia_tekst="return a - b")
        + _rownowazny(plik=PLIK_MODULU, linia_tekst=" "),
    )

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert _linie_tabel(zrzut.err) == [
        f"{PREFIKS_TABEL}HAR-1.toml rownowazny #1: wpis rownowazny: brak pola 'uzasadnienie'",
        f"{PREFIKS_TABEL}HAR-1.toml rownowazny #3: wpis rownowazny: 'linia_tekst' jest pusty",
    ]


def test_sprawdz_wszystkie_podaje_bledy_wierszy_mutant_przed_bledami_wpisow_rownowazny(tmp_path, capsys):
    repo = _repo(tmp_path)
    zly_mutant = _wpis(**_pola(szukaj="x = 1", zamien="x = 1"))
    _tabela_w_repo(
        repo, "HAR-1.toml", zly_mutant + _rownowazny(uzasadnienie=" ") + _dobry_wiersz()
    )

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert _linie_tabel(zrzut.err) == [
        f"{PREFIKS_TABEL}HAR-1.toml #1: zamien jest rowne szukaj (mutant tozsamosciowy)",
        f"{PREFIKS_TABEL}HAR-1.toml rownowazny #1: wpis rownowazny: 'uzasadnienie' jest pusty",
    ]


def test_sprawdz_wszystkie_rownowazny_bedacy_lista_liczb_to_blad_wpisu_a_nie_wyjatek(tmp_path, capsys):
    repo = _repo(tmp_path)
    _tabela_w_repo(repo, "HAR-1.toml", "rownowazny = [5]\n" + _dobry_wiersz())

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert _linie_tabel(zrzut.err) == [f"{PREFIKS_TABEL}HAR-1.toml rownowazny #1: wpis rownowazny nie jest tabela"]


@pytest.mark.parametrize(
    ("poczatek", "koniec"),
    [
        pytest.param("rownowazny = 5\n", "", id="liczba"),
        pytest.param("", '[rownowazny]\nplik = "src/a.py"\n', id="tabela_a_nie_lista_tabel"),
    ],
)
def test_sprawdz_wszystkie_rownowazny_ktory_nie_jest_lista_tabel_to_blad_calej_tabeli(
    tmp_path, capsys, poczatek, koniec
):
    repo = _repo(tmp_path)
    _tabela_w_repo(repo, "HAR-1.toml", poczatek + _dobry_wiersz() + koniec)

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert _linie_tabel(zrzut.err) == [f"{PREFIKS_TABEL}HAR-1.toml: rownowazny musi byc lista [[rownowazny]]"]
    assert zrzut.out == ""


def test_sprawdz_wszystkie_tabela_z_samym_rownowazny_to_blad_tabeli(tmp_path, capsys):
    repo = _repo(tmp_path)
    _tabela_w_repo(repo, "HAR-1.toml", _rownowazny(plik=PLIK_MODULU, linia_tekst="return a - b"))

    kod = _sprawdz_wszystkie(repo)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert _linie_tabel(zrzut.err) == [f"{PREFIKS_TABEL}HAR-1.toml: brak listy [[mutant]]"]


def test_wpis_rownowazny_jest_niezmienny_po_wczytaniu(tmp_path):
    tabela = _zapisz_tabele(tmp_path, _dobry_wiersz() + _rownowazny())

    _, rownowazne = mutanty_reczne.wczytaj_tabele_z_rownowaznymi(tabela)

    assert rownowazne == [mutanty_reczne.Rownowazny(PLIK_P, "c = 3", UZASADNIENIE)]
    with pytest.raises(FrozenInstanceError):
        rownowazne[0].linia_tekst = "z"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("tresc", "przyczyna"),
    [
        pytest.param(
            _dobry_wiersz() + _rownowazny(uzasadnienie=" "),
            "wpis rownowazny: 'uzasadnienie' jest pusty",
            id="wadliwy_wpis_rownowazny",
        ),
        pytest.param(_rownowazny(), "brak listy [[mutant]]", id="tabela_bez_wiersza_mutant"),
    ],
)
def test_blad_tabeli_z_rownowaznymi_zachowuje_przyczyne_bez_nazwy_tabeli_i_numeru_wpisu(
    tmp_path, tresc, przyczyna
):
    tabela = _zapisz_tabele(tmp_path, tresc)

    with pytest.raises(BladNarzedzia) as wyjatek:
        mutanty_reczne.wczytaj_tabele_z_rownowaznymi(tabela)

    assert isinstance(wyjatek.value.__cause__, BladNarzedzia)
    assert str(wyjatek.value.__cause__) == przyczyna


def test_wpis_rownowazny_ma_pola_w_kolejnosci_plik_linia_tekst_uzasadnienie():
    wpis = mutanty_reczne.Rownowazny("a.py", "x = 1", "bo tak")

    assert (wpis.plik, wpis.linia_tekst, wpis.uzasadnienie) == ("a.py", "x = 1", "bo tak")


def test_tryb_wykonywania_nie_wykonuje_wpisow_rownowazny_i_zwraca_wynik_samych_mutantow(tmp_path, capsys):
    tabela = TABELA_PRZEZYWA + _rownowazny(plik=PLIK_MODULU, linia_tekst="return a + b")
    repo = _repo(tmp_path, tabela)

    kod = _uruchom(repo, TABELA_W_REPO)

    zrzut = capsys.readouterr()
    assert kod == 1, zrzut.err
    assert zrzut.out == f"{PLIK_MODULU}: {SZUKAJ} → {PRZEZYWA_A}\n"


def test_tryb_wykonywania_z_zabitym_mutantem_i_rownowazny_daje_kod_0(tmp_path, capsys):
    tabela = TABELA_ZABITY + _rownowazny(plik=PLIK_MODULU, linia_tekst="return a + b")
    repo = _repo(tmp_path, tabela)

    kod = _uruchom(repo, TABELA_W_REPO)

    assert kod == 0, capsys.readouterr().err


def test_tryb_wykonywania_odrzuca_wadliwy_rownowazny_kodem_2_bez_uruchamiania_testow(
    tmp_path, capsys, monkeypatch
):
    tabela = TABELA_ZABITY + _rownowazny(plik=PLIK_MODULU, uzasadnienie="  ")
    repo = _repo(tmp_path, tabela)
    wywolania = _zakaz_wykonywania_mutacji(monkeypatch)

    kod = _uruchom(repo, TABELA_W_REPO)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.out == ""
    assert zrzut.err.startswith("blad: ")
    assert wywolania == []


def test_tryb_wykonywania_odrzuca_tabele_z_samym_rownowazny_kodem_2(tmp_path, capsys, monkeypatch):
    repo = _repo(tmp_path, _rownowazny(plik=PLIK_MODULU, linia_tekst="return a + b"))
    wywolania = _zakaz_wykonywania_mutacji(monkeypatch)

    kod = _uruchom(repo, TABELA_W_REPO)

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.out == ""
    assert wywolania == []


# --- prawdziwy proces ------------------------------------------------------------------------------


def test_skrypt_uruchomiony_jako_program_z_pokrycie_kod3_konczy_sie_kodem_1_i_wypisuje_niepokryta_linie(
    tmp_path,
):
    korzen = tmp_path / "k"
    (korzen / "src").mkdir(parents=True)
    (korzen / "tests" / "mutanty_reczne").mkdir(parents=True)
    (korzen / PLIK_P).write_text(TRESC_P, encoding="utf-8")
    (korzen / "tests" / "mutanty_reczne" / "HAR-1.toml").write_text(
        _mutant("c = 3", "c = 4", plik=PLIK_P), encoding="utf-8"
    )
    (korzen / "lista.txt").write_text(_lista_kodu_3(f"{PLIK_P}:3", f"{PLIK_P}:5"), encoding="utf-8")

    kod, wyjscie, blad = _uruchom_skrypt(
        OPCJA_POKRYCIE, "lista.txt", "--korzen", str(korzen), "tests/mutanty_reczne/HAR-1.toml"
    )

    assert kod == 1, blad
    assert wyjscie == f"{PLIK_P}:5\n"
    assert blad.rstrip("\n") == _brak_dowodu(1)


# =============================================================================
# HAR-236: `-j K` / `--procesy K` - wiersze zwykle w puli watkow, front/e2e/baza po kolei
# =============================================================================
#
# Zaslepka `uruchom_test` zastepuje prawdziwy pytest (50 wierszy z prawdziwym pytestem
# trwaloby minuty), a reszta `_zabity` jest prawdziwa: kopia repo, nakladanie mutacji, ocena
# kodu. Zaslepka widzi mutacje w kopii: `= 99999` zabija mutanta, kazda inna zmiana (sam
# komentarz) pozwala mu przezyc; `# s<ms>` wydluza test, `blad<n>` zglasza BladNarzedzia.
# Dzieki temu kolejnosc konca wierszy w puli jest inna niz kolejnosc w tabeli.

PLIK_WIELE = "src/wiele.py"
PLIK_WEB_WIELE = "web/src/wiele.txt"
PLIK_TESTU_PULI = "tests/test_pula.py"
PLIK_TESTU_BAZY = "tests/test_baza.py"
PLIK_TESTU_Z_SLOWEM_BAZA = "tests/test_baza_slowo.py"
PLIK_TESTU_E2E = "tests/e2e/test_front.py"
LICZBA_WIERSZY_TABELI = 50
BAZOWA_PAUZA_S = 0.01
PAUZA_WIERSZA_SZEREGOWEGO_MS = 60

TRESC_WIELE = "".join(f"ZMIENNA_{i:02d} = {i}\n" for i in range(LICZBA_WIERSZY_TABELI))
TRESC_WEB_WIELE = "".join(f"W_{i:02d} = {i}\n" for i in range(LICZBA_WIERSZY_TABELI))
PLIKI_PULI = {
    PLIK_WIELE: TRESC_WIELE,
    PLIK_WEB_WIELE: TRESC_WEB_WIELE,
    PLIK_TESTU_PULI: "def test_pula():\n    assert True\n",
    # `mark.baza` jest tylko w tym pliku: wiersze z innymi plikami testow nie moga przez niego
    # trafic do kolejki szeregowej.
    PLIK_TESTU_BAZY: "import pytest\n\n\n@pytest.mark.baza\ndef test_baza():\n    assert True\n",
    # Slowo `baza` bez `mark.baza`: tylko dokladny napis `mark.baza` jest sygnalem testu bazy.
    PLIK_TESTU_Z_SLOWEM_BAZA: "# testy bazy: baza, mark - ale bez markera\n\n\ndef test_slowo():\n    assert True\n",
    PLIK_TESTU_E2E: "def test_front():\n    assert True\n",
}


def _linia_zmiennej(i: int, plik: str = PLIK_WIELE) -> str:
    return f"{'W' if plik == PLIK_WEB_WIELE else 'ZMIENNA'}_{i:02d} = {i}"


def _wiersz_puli(
    i: int,
    *,
    test: str = PLIK_TESTU_PULI,
    plik: str = PLIK_WIELE,
    przezywa: bool = False,
    pauza_ms: int = 0,
    blad: bool = False,
) -> str:
    """Wiersz tabeli dla linii `i`; node id testu ma sufiks `::test_<i>` (unikalny w tabeli)."""
    szukaj = _linia_zmiennej(i, plik)
    wartosc = i if przezywa else 99999
    zamien = szukaj.rsplit(" = ", 1)[0] + f" = {wartosc}  # s{pauza_ms}" + (f" blad{i}" if blad else "")
    return _mutant(szukaj, zamien, test=f"{test}::test_{i}", plik=plik)


def _repo_puli(tmp_path: Path, tabela: str) -> Path:
    repo = tmp_path / "repo"
    for sciezka, tresc in {**PLIKI_PULI, TABELA_W_REPO: tabela}.items():
        cel = repo / sciezka
        cel.parent.mkdir(parents=True, exist_ok=True)
        cel.write_text(tresc, encoding="utf-8")
    _git(repo, "init", "-q")
    return repo


class _SledzenieWierszy:
    """Zaslepka `uruchom_test` z dziennikiem: ile wywolan trwa naraz i kiedy kazde trwalo."""

    def __init__(self) -> None:
        self.blokada = threading.Lock()
        self.aktywne = 0
        self.max_aktywnych = 0
        self.kolejnosc: list[str] = []
        self.przedzialy: dict[str, list[tuple[float, float]]] = {}

    def __call__(self, kopia: Path, test: str, limit: float) -> int:
        tekst = "".join(
            (kopia / plik).read_text(encoding="utf-8") for plik in (PLIK_WIELE, PLIK_WEB_WIELE)
        )
        pauza = re.search(r"# s(\d+)", tekst)
        start = time.monotonic()
        with self.blokada:
            self.aktywne += 1
            self.max_aktywnych = max(self.max_aktywnych, self.aktywne)
            self.kolejnosc.append(test)
        try:
            time.sleep(BAZOWA_PAUZA_S + (int(pauza.group(1)) / 1000 if pauza else 0))
            blad = re.search(r"blad(\d+)", tekst)
            if blad:
                raise BladNarzedzia(f"blad wiersza {blad.group(1)}")
            wyjatek = re.search(r"wyjatek(\d+)", tekst)
            if wyjatek:
                raise RuntimeError(f"wyjatek wiersza {wyjatek.group(1)}")
            return 1 if "= 99999" in tekst else 0
        finally:
            koniec = time.monotonic()
            with self.blokada:
                self.aktywne -= 1
                self.przedzialy.setdefault(test, []).append((start, koniec))

    def zakres_wiersza(self, test: str) -> tuple[float, float]:
        """Od startu pierwszego do konca ostatniego wywolania testu (przebieg bazowy i po mutacji)."""
        przedzialy = self.przedzialy[test]
        return min(s for s, _ in przedzialy), max(k for _, k in przedzialy)


def _zaslepki_puli(monkeypatch) -> _SledzenieWierszy:
    sledzenie = _SledzenieWierszy()
    monkeypatch.setattr(mutanty_reczne, "uruchom_test", sledzenie)
    # Front nie jest tematem tych testow (wiersze frontu maja swoje testy powyzej).
    monkeypatch.setattr(mutanty_reczne, "zbuduj_front", lambda kopia, limit: None)
    return sledzenie


def _przebieg_puli(repo: Path, *opcje: str) -> int:
    return _main([TABELA_W_REPO, "--korzen", str(repo), "--limit", "60", *opcje])


def _zakresy_rozlaczne(zakresy: list[tuple[float, float]]) -> bool:
    uporzadkowane = sorted(zakresy)
    return all(a[1] <= b[0] for a, b in zip(uporzadkowane, uporzadkowane[1:]))


def _linia_przezywajacego(i: int, pauza_ms: int) -> str:
    szukaj = _linia_zmiennej(i)
    return f"{PLIK_WIELE}: {szukaj} → ZMIENNA_{i:02d} = {i}  # s{pauza_ms}"


def test_tabela_z_piecdziesieciu_wierszy_na_czterech_procesach_daje_ten_sam_wynik_co_szeregowo(
    tmp_path, capsys, monkeypatch
):
    przezywajace = {7: 150, 11: 0, 21: 0, 40: 100}  # nr wiersza -> pauza (ms) jego testu
    wiersze = []
    for i in range(LICZBA_WIERSZY_TABELI):
        wiersze.append(
            _wiersz_puli(
                i,
                # 11 jest wierszem szeregowym (test e2e) i przezywa: lista ma zachowac kolejnosc tabeli
                test=PLIK_TESTU_E2E if i in (11, 12) else PLIK_TESTU_PULI,
                przezywa=i in przezywajace,
                pauza_ms=przezywajace.get(i, 0),
            )
        )
    assert len(wiersze) >= 50
    _zaslepki_puli(monkeypatch)
    repo = _repo_puli(tmp_path, "".join(wiersze))
    oczekiwane = "".join(f"{_linia_przezywajacego(i, ms)}\n" for i, ms in przezywajace.items())

    kod_szeregowo = _przebieg_puli(repo, "-j", "1")
    szeregowo = capsys.readouterr()
    kod_rownolegle = _przebieg_puli(repo, "-j", "4")
    rownolegle = capsys.readouterr()

    assert (kod_szeregowo, kod_rownolegle) == (1, 1)
    assert szeregowo.out == oczekiwane  # test ma co porownywac: lista nie jest pusta ani przypadkowa
    assert rownolegle.out == szeregowo.out
    assert rownolegle.err == szeregowo.err == ""


def test_bez_opcji_j_przebieg_jest_identyczny_jak_z_j_1_i_idzie_po_kolei(
    tmp_path, capsys, monkeypatch
):
    wiersze = [_wiersz_puli(i, przezywa=i in (2, 5), pauza_ms=5) for i in range(8)]
    sledzenie = _zaslepki_puli(monkeypatch)
    repo = _repo_puli(tmp_path, "".join(wiersze))

    kod_domyslny = _przebieg_puli(repo)
    domyslny = capsys.readouterr()
    kolejnosc_domyslna = list(sledzenie.kolejnosc)
    maks_domyslny = sledzenie.max_aktywnych
    sledzenie.kolejnosc.clear()
    kod_j1 = _przebieg_puli(repo, "-j", "1")
    j1 = capsys.readouterr()

    assert (kod_domyslny, kod_j1) == (1, 1)
    assert domyslny.out == j1.out
    assert domyslny.out.count("\n") == 2
    # dwa wywolania na wiersz (bez mutacji i po niej), wiersz po wierszu, w kolejnosci tabeli
    assert kolejnosc_domyslna == [f"{PLIK_TESTU_PULI}::test_{i}" for i in range(8) for _ in (0, 1)]
    assert sledzenie.kolejnosc == kolejnosc_domyslna
    assert maks_domyslny == 1
    assert sledzenie.max_aktywnych == 1


@pytest.mark.parametrize(
    ("opcja", "procesy"),
    [
        pytest.param("-j", 2, id="j_2"),
        pytest.param("-j", 4, id="j_4"),
        pytest.param("--procesy", 3, id="procesy_3"),
    ],
)
def test_zwykle_wiersze_biegna_rownolegle_ale_nie_na_wiecej_niz_k_procesach(
    tmp_path, capsys, monkeypatch, opcja, procesy
):
    wiersze = [_wiersz_puli(i, pauza_ms=30) for i in range(16)]
    sledzenie = _zaslepki_puli(monkeypatch)
    repo = _repo_puli(tmp_path, "".join(wiersze))

    kod = _przebieg_puli(repo, opcja, str(procesy))

    assert kod == 0, capsys.readouterr().err
    assert 2 <= sledzenie.max_aktywnych <= procesy
    assert len(sledzenie.kolejnosc) == 32  # kazdy wiersz dwa razy: nic nie zgubiono w puli


def test_kontrola_detektora_nakladania_zwykle_wiersze_w_puli_nakladaja_sie_w_czasie(
    tmp_path, monkeypatch
):
    """Bez tej kontroli test szeregowosci ponizej moglby przechodzic, bo detektor nigdy nie
    widzi nakladania: tu ten sam ksztalt wierszy, ale zwyklych, i zakresy MUSZA sie przecinac."""
    wiersze = [_wiersz_puli(i, pauza_ms=PAUZA_WIERSZA_SZEREGOWEGO_MS) for i in range(6)]
    sledzenie = _zaslepki_puli(monkeypatch)
    repo = _repo_puli(tmp_path, "".join(wiersze))

    assert _przebieg_puli(repo, "-j", "4") == 0

    zakresy = [sledzenie.zakres_wiersza(f"{PLIK_TESTU_PULI}::test_{i}") for i in range(6)]
    assert not _zakresy_rozlaczne(zakresy)


@pytest.mark.parametrize(
    "wiersz_szeregowy",
    [
        pytest.param(
            lambda i: _wiersz_puli(i, test=PLIK_TESTU_E2E, pauza_ms=PAUZA_WIERSZA_SZEREGOWEGO_MS),
            id="test_pod_tests_e2e",
        ),
        pytest.param(
            lambda i: _wiersz_puli(
                i, plik=PLIK_WEB_WIELE, pauza_ms=PAUZA_WIERSZA_SZEREGOWEGO_MS
            ),
            id="plik_pod_web",
        ),
        pytest.param(
            lambda i: _wiersz_puli(
                i, test=PLIK_TESTU_BAZY, pauza_ms=PAUZA_WIERSZA_SZEREGOWEGO_MS
            ),
            id="plik_testu_z_mark_baza",
        ),
    ],
)
def test_wiersze_frontu_e2e_i_bazy_nie_nakladaja_sie_w_czasie_mimo_czterech_procesow(
    tmp_path, capsys, monkeypatch, wiersz_szeregowy
):
    # Parzyste numery: wiersze szeregowe, nieparzyste: zwykle, w jednej tabeli i w jednej puli.
    numery_szeregowe = [0, 2, 4, 6, 8, 10]
    wiersze = [
        wiersz_szeregowy(i) if i % 2 == 0 else _wiersz_puli(i, pauza_ms=30) for i in range(12)
    ]
    sledzenie = _zaslepki_puli(monkeypatch)
    repo = _repo_puli(tmp_path, "".join(wiersze))

    kod = _przebieg_puli(repo, "-j", "4")

    assert kod == 0, capsys.readouterr().err
    # node id kazdego wiersza konczy sie `_<numer wiersza>`, niezaleznie od wariantu wiersza
    zakresy = [
        sledzenie.zakres_wiersza(test)
        for test in sledzenie.przedzialy
        if int(test.rsplit("_", 1)[1]) in numery_szeregowe
    ]
    assert len(zakresy) == len(numery_szeregowe)  # wszystkie wiersze szeregowe sie wykonaly
    assert _zakresy_rozlaczne(zakresy)


def test_plik_testu_ze_slowem_baza_bez_mark_baza_nie_jest_wierszem_szeregowym(
    tmp_path, capsys, monkeypatch
):
    wiersze = [
        _wiersz_puli(i, test=PLIK_TESTU_Z_SLOWEM_BAZA, pauza_ms=PAUZA_WIERSZA_SZEREGOWEGO_MS)
        for i in range(8)
    ]
    sledzenie = _zaslepki_puli(monkeypatch)
    repo = _repo_puli(tmp_path, "".join(wiersze))

    kod = _przebieg_puli(repo, "-j", "4")

    assert kod == 0, capsys.readouterr().err
    assert sledzenie.max_aktywnych >= 2


def test_blad_narzedzia_w_wierszu_puli_daje_kod_2_z_komunikatem_pierwszego_wiersza_tabeli(
    tmp_path, capsys, monkeypatch
):
    # Wiersz 2 zglasza blad po 300 ms, wiersz 3 od razu: w czasie konca pierwszy jest 3, w tabeli 2.
    # Wiersz 4 trwa jeszcze, gdy blad wiersza 2 jest juz znany: pula nie zostawia go w tle.
    wiersze = [
        _wiersz_puli(0, przezywa=True),
        _wiersz_puli(1, przezywa=True),
        _wiersz_puli(2, pauza_ms=300, blad=True),
        _wiersz_puli(3, blad=True),
        _wiersz_puli(4, pauza_ms=500),
        _wiersz_puli(5),
        _wiersz_puli(6),
    ]
    sledzenie = _zaslepki_puli(monkeypatch)
    repo = _repo_puli(tmp_path, "".join(wiersze))

    kod = _przebieg_puli(repo, "-j", "4")

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.err.strip() == "blad: blad wiersza 2"
    assert zrzut.out == ""  # przezywajace wiersze 0 i 1 nie sa wypisywane: blad ma pierwszenstwo
    assert sledzenie.aktywne == 0  # zadne wywolanie nie zostalo w tle po powrocie z main


@pytest.mark.parametrize("opcja", ["-j", "--procesy"])
# "٣" przechodzi isdigit() i int(); "+3", " 3" i "1_0" przechodza int(): kazdy odrzuca tylko
# jeden warunek `_procesy`.
@pytest.mark.parametrize(
    "wartosc",
    [
        "0",
        "abc",
        "-1",
        "1.5",
        "",
        pytest.param("٣", id="cyfra_arabsko_indyjska"),
        pytest.param("+3", id="plus_przed_liczba"),
        pytest.param(" 3", id="spacja_przed_liczba"),
        pytest.param("1_0", id="podkreslnik_w_liczbie"),
    ],
)
def test_zla_liczba_procesow_to_blad_argumentow_kodem_2_bez_uruchamiania_testow(
    tmp_path, monkeypatch, opcja, wartosc
):
    sledzenie = _zaslepki_puli(monkeypatch)
    repo = _repo_puli(tmp_path, _wiersz_puli(0))

    kod = _przebieg_puli(repo, opcja, wartosc)

    assert kod == 2
    assert sledzenie.kolejnosc == []


def test_wiersz_z_nieczytelnym_plikiem_testu_jest_szeregowy_a_nie_w_puli(
    tmp_path, capsys, monkeypatch
):
    # Brak pliku testu w kopii: bez dowodu, ze nie ma w nim `mark.baza`, wiersz nie wchodzi do puli.
    wiersze = [
        _wiersz_puli(i, test="tests/test_nie_istnieje.py", pauza_ms=30) for i in range(8)
    ]
    sledzenie = _zaslepki_puli(monkeypatch)
    repo = _repo_puli(tmp_path, "".join(wiersze))

    kod = _przebieg_puli(repo, "-j", "4")

    assert kod == 0, capsys.readouterr().err
    assert len(sledzenie.kolejnosc) == 16  # kazdy wiersz dwa razy
    assert sledzenie.max_aktywnych == 1


def test_blad_narzedzia_w_wierszu_szeregowym_wczesniejszym_w_tabeli_wygrywa_z_bledem_z_puli(
    tmp_path, capsys, monkeypatch
):
    wiersze = [
        _wiersz_puli(0),
        _wiersz_puli(1),
        _wiersz_puli(2, test=PLIK_TESTU_E2E, blad=True),  # szeregowy, wczesniejszy w tabeli
        _wiersz_puli(3),
        _wiersz_puli(4),
        _wiersz_puli(5, blad=True),  # z puli, pozniejszy w tabeli
        _wiersz_puli(6),
    ]
    _zaslepki_puli(monkeypatch)
    repo = _repo_puli(tmp_path, "".join(wiersze))

    kod = _przebieg_puli(repo, "-j", "4")

    zrzut = capsys.readouterr()
    assert kod == 2
    assert zrzut.err.strip() == "blad: blad wiersza 2"
    assert zrzut.out == ""


def test_wiersz_szeregowy_za_bledem_narzedzia_z_puli_nie_jest_uruchamiany(
    tmp_path, capsys, monkeypatch
):
    wiersze = [
        _wiersz_puli(0),
        _wiersz_puli(1, blad=True),
        _wiersz_puli(2),
        _wiersz_puli(3, test=PLIK_TESTU_E2E),
    ]
    sledzenie = _zaslepki_puli(monkeypatch)
    repo = _repo_puli(tmp_path, "".join(wiersze))

    kod = _przebieg_puli(repo, "-j", "4")

    assert kod == 2
    assert capsys.readouterr().err.strip() == "blad: blad wiersza 1"
    assert f"{PLIK_TESTU_E2E}::test_3" not in sledzenie.kolejnosc


def test_nieoczekiwany_wyjatek_w_wierszu_puli_nie_jest_polykany(tmp_path, monkeypatch):
    wiersze = [_wiersz_puli(i) for i in range(4)]
    wiersze[2] = wiersze[2].replace("# s0", "# s0 wyjatek2")
    _zaslepki_puli(monkeypatch)
    repo = _repo_puli(tmp_path, "".join(wiersze))

    with pytest.raises(RuntimeError, match="wyjatek wiersza 2"):
        _przebieg_puli(repo, "-j", "4")
