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
    ZLE_TABELE[f"pole_{_pole}_jest_pustym_napisem"] = _wpis(**_pola(**{_pole: ""}))


@pytest.mark.parametrize(
    "tresc", [pytest.param(t, id=i) for i, t in ZLE_TABELE.items()]
)
def test_niepoprawna_tabela_konczy_sie_bledem_narzedzia(tmp_path, tresc):
    tabela = _zapisz_tabele(tmp_path, tresc)

    with pytest.raises(BladNarzedzia):
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
