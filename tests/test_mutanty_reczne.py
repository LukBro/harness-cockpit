"""Wykonywalna tabela recznych mutantow (HAR-150): `tools/mutanty_reczne.py`.

Skrypt czyta `tests/mutanty_reczne/<ticket>.toml`, nakłada każdą mutację w NOWEJ kopii
repozytorium (katalog tymczasowy) i sprawdza, że wskazany test oblewa. Kody: 0 wszystkie
mutanty zabite, 1 przeżyły (lista `plik: szukaj → zamien`), 2 błąd (brak dowodu).

Testy używają prawdziwego kodu skryptu. Prawdziwy pytest i git działają wyłącznie w
mini-repozytoriach w `tmp_path`; zaślepka `uruchom_test` jest użyta tylko tam, gdzie badamy
parametry wiersza poleceń (limit czasu), nie wynik testu.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from procesy import czy_zyje, uruchom_proces_sync, zabij_proces, zakoncz_proces_sync

KORZEN = Path(__file__).resolve().parents[1]
SKRYPT = KORZEN / "tools" / "mutanty_reczne.py"
sys.path.insert(0, str(KORZEN / "tools"))
import mutanty_reczne  # noqa: E402

BladNarzedzia = mutanty_reczne.BladNarzedzia
Mutant = mutanty_reczne.Mutant

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


def test_po_przekroczeniu_limitu_ginie_takze_proces_potomny_testu(tmp_path):
    kopia = _kopia_testowa(
        tmp_path / "kopia",
        {
            "test_wnuk.py": (
                "import pathlib\nimport subprocess\nimport sys\nimport time\n\n\n"
                "def test_spi_z_wnukiem():\n"
                "    wnuk = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "    pathlib.Path('wnuk.pid').write_text(str(wnuk.pid))\n"
                "    time.sleep(60)\n"
            )
        },
    )
    pid = None
    try:
        with pytest.raises(BladNarzedzia):
            mutanty_reczne.uruchom_test(kopia, "tests/test_wnuk.py::test_spi_z_wnukiem", 4)

        plik_pid = kopia / "wnuk.pid"
        assert plik_pid.exists(), "test nie zdazyl uruchomic potomka przed limitem"
        pid = int(plik_pid.read_text(encoding="utf-8"))
        koniec = time.monotonic() + 5
        while czy_zyje(pid) and time.monotonic() < koniec:
            time.sleep(0.1)
        assert not czy_zyje(pid)
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
      echo "$!" > "$ATRAPA_PID_NPM"
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


def test_przekroczony_limit_budowania_to_blad_narzedzia_a_proces_npm_ginie(tmp_path, npm):
    npm.ustaw(ATRAPA_USPIJ="60")
    kopia = _kopia_z_webem(tmp_path)
    (kopia / "web" / "node_modules").mkdir()
    pid = None
    start = time.monotonic()
    try:
        with pytest.raises(BladNarzedzia, match="przekroczony"):
            # Limit z zapasem na obciazona maszyne: potomek atrapy zapisuje PID zaraz po starcie,
            # wiec przy kilku sekundach zawsze zdazy, a atrapa spi 60 s - bez zabicia grupy zyje dalej.
            mutanty_reczne.zbuduj_front(kopia, 5)

        assert time.monotonic() - start < 50
        assert npm.pid.exists(), "atrapa npm nie zdazyla wystartowac potomka przed limitem"
        pid = int(npm.pid.read_text(encoding="utf-8"))
        koniec = time.monotonic() + 5
        while czy_zyje(pid) and time.monotonic() < koniec:
            time.sleep(0.1)
        assert not czy_zyje(pid)
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
