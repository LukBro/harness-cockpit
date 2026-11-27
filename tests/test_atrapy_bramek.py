"""HAR-246: bramka, ktora nie moze zawiesc - ocena komendy, wyniku i plikow repozytorium (TDD).

Prawdziwa funkcja `runner.atrapy_bramek.ocen_bramke` na plikach w tmp_path. Nazwy sygnalow sa
tu wpisane doslownie (lista niezalezna od kodu). Kazdy sygnal ma przypadek dodatni i ujemny;
"nie oceniono skryptu" ma osobny test dla kazdej przyczyny. Funkcja niczego nie uruchamia.
"""
from __future__ import annotations

import dataclasses
import errno
import json
import os
import threading
from pathlib import Path

import pytest

BRAK_TESTOW = "brak zebranych testów"
PRZECHODZA_BEZ = "testy przechodzą bez testów"
WYMUSZONE_ZERO = "kod wyjścia wymuszony na zero"
WSZYSTKIE_POMINIETE = "wszystkie testy pominięte"
BLAD_IGNOROWANY = "błąd kroku ignorowany"
ATRAPA_NPM = "atrapa npm init"
BEZ_ASERCJI = "testy bez asercji"
LINT_BEZ_REGUL = "lint bez reguł"
NIE_OCENIONO = "nie oceniono skryptu"

# teksty poprawek wpisane doslownie: czlowiek czyta je w ostrzezeniu
POPRAWKI = {
    BRAK_TESTOW: "wskaż komendzie katalog z testami albo dodaj test, który pytest zbierze.",
    PRZECHODZA_BEZ: "usuń --passWithNoTests, żeby brak testów oblewał bramkę.",
    WYMUSZONE_ZERO: "usuń wymuszenie kodu 0, żeby błąd komendy oblewał bramkę.",
    WSZYSTKIE_POMINIETE: "usuń warunek pomijania albo uruchom testy tam, gdzie mogą się wykonać.",
    BLAD_IGNOROWANY: "usuń continue-on-error: true.",
    ATRAPA_NPM: "zastąp skrypt z npm init prawdziwą komendą testów.",
    BEZ_ASERCJI: "dodaj do testów asercje sprawdzające wynik.",
    LINT_BEZ_REGUL: "włącz w konfiguracji lintera co najmniej jedną regułę.",
    NIE_OCENIONO: "sprawdź ten plik ręcznie albo wskaż czytelny plik wewnątrz repozytorium.",
}

LIMIT_CYTATU = 200
NPM_INIT = 'echo "Error: no test specified" && exit 1'


def _repo(tmp_path: Path, pliki: dict[str, str | bytes] | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    for nazwa, tresc in (pliki or {}).items():
        plik = repo / nazwa
        plik.parent.mkdir(parents=True, exist_ok=True)
        plik.write_bytes(tresc if isinstance(tresc, bytes) else tresc.encode("utf-8"))
    return repo


def _pakiet(**skrypty: str) -> dict[str, str]:
    return {"package.json": json.dumps({"name": "x", "scripts": skrypty})}


def _ocen(repo: Path, komenda: str, kod: int | None = 0, wyjscie: str = "ok\n") -> list:
    from runner.atrapy_bramek import ocen_bramke   # import leniwy: brak modulu czerwieni kazdy test

    return ocen_bramke(komenda, kod, wyjscie, repo)


def _nazwy(sygnaly) -> set[str]:
    return {s.nazwa for s in sygnaly}


def _sygnal(sygnaly, nazwa: str):
    pasujace = [s for s in sygnaly if s.nazwa == nazwa]
    assert pasujace, f"brak sygnalu {nazwa!r}: {sygnaly}"
    return pasujace[0]


def przypadek(opis, komenda, kod=0, wyjscie="ok\n", pliki=None):
    return pytest.param(komenda, kod, wyjscie, pliki or {}, id=opis)


# --- przypadki dodatnie: sygnal jest ---------------------------------------------------------

DODATNIE: list[tuple[str, list]] = [
    (BRAK_TESTOW, [
        przypadek("pytest kod 5", "pytest -q", 5, ""),
        przypadek("python -m pytest", "python -m pytest", 5, ""),
        przypadek("pytest w sh -c", 'sh -c "pytest -q"', 5, ""),
        przypadek("pytest w skrypcie", "sh t.sh", 5, "", {"t.sh": "pytest -q\n"}),
        przypadek("no tests ran przy kodzie 0", "pytest -q", 0, "no tests ran in 0.01s\n"),
    ]),
    (PRZECHODZA_BEZ, [
        przypadek("w komendzie", "npx jest --passWithNoTests"),
        przypadek("w skrypcie package.json", "npm test",
                  pliki=_pakiet(test="vitest run --passWithNoTests")),
        przypadek("komenda nie ruszyla", "npx jest --passWithNoTests", None, ""),
    ]),
    (WYMUSZONE_ZERO, [
        przypadek("--exit-zero", "ruff check --exit-zero ."),
        przypadek("--exit-zero w skrypcie", "sh t.sh", pliki={"t.sh": "ruff check --exit-zero\n"}),
        przypadek("sh -c || true", 'sh -c "pytest -q || true"'),
        przypadek("/bin/sh -c", '/bin/sh -c "pytest -q || true"'),
        przypadek("bash -c", 'bash -c "pytest -q || true"'),
        przypadek("bash -ec", 'bash -ec "pytest -q || true"'),
        przypadek("|| dwukropek", 'sh -c "pytest -q || :"'),
        przypadek("; exit 0", 'sh -c "pytest -q; exit 0"'),
        przypadek("komenda nie ruszyla", 'sh -c "pytest || true"', None, ""),
        przypadek("sh skrypt, ostatnia linia exit 0", "sh t.sh",
                  pliki={"t.sh": "pytest -q\nexit 0\n"}),
        przypadek("bash skrypt", "bash t.sh", pliki={"t.sh": "pytest -q\nexit 0\n"}),
        przypadek("./skrypt", "./t.sh", pliki={"t.sh": "#!/bin/sh\npytest -q\nexit 0\n"}),
        przypadek("exit 0 przed komentarzem i pusta linia", "sh t.sh",
                  pliki={"t.sh": "pytest -q\nexit 0\n\n# koniec\n"}),
        przypadek("|| true w skrypcie", "sh t.sh", pliki={"t.sh": "pytest -q || true\necho x\n"}),
        przypadek("npm test", "npm test", pliki=_pakiet(test="jest || true")),
        przypadek("npm run X", "npm run sprawdz", pliki=_pakiet(sprawdz="jest || true")),
        przypadek("pnpm test", "pnpm test", pliki=_pakiet(test="jest || true")),
        przypadek("pnpm run X", "pnpm run sprawdz", pliki=_pakiet(sprawdz="jest || true")),
        przypadek("yarn test", "yarn test", pliki=_pakiet(test="jest || true")),
        przypadek("yarn run X", "yarn run sprawdz", pliki=_pakiet(sprawdz="jest || true")),
    ]),
    (WSZYSTKIE_POMINIETE, [
        przypadek("pytest", "pytest -q", 0, "sss\n47 skipped in 0.01s\n"),
        przypadek("pytest z ramka", "pytest", 0, "===== 47 skipped in 0.01s =====\n"),
        przypadek("jest", "npx jest", 0,
                  "Test Suites: 1 skipped, 0 of 1 total\nTests:       47 skipped, 47 total\n"),
        przypadek("vitest", "npx vitest run", 0,
                  " Test Files  1 skipped (1)\n      Tests  47 skipped (47)\n"),
    ]),
    (BLAD_IGNOROWANY, [
        przypadek("w komendzie", "sh -c \"echo 'continue-on-error: true'\""),
        przypadek("w skrypcie", "sh t.sh",
                  pliki={"t.sh": "cat <<K\ncontinue-on-error: true\nK\n"}),
    ]),
    (ATRAPA_NPM, [
        przypadek("w skrypcie package.json", "npm test", 1, "", _pakiet(test=NPM_INIT)),
        przypadek("w wyjsciu", "make test", 1, "Error: no test specified\n"),
    ]),
    (BEZ_ASERCJI, [
        przypadek("pytest", "pytest -q", pliki={"tests/test_a.py": "def test_a():\n    pass\n"}),
        przypadek("jest", "npx jest", pliki={"a.test.js": "test('a', () => {});\n"}),
        przypadek("npm test", "npm test",
                  pliki={**_pakiet(test="vitest run"), "src/a.spec.ts": "it('a', () => {});\n"}),
    ]),
    (LINT_BEZ_REGUL, [
        przypadek("ruff pyproject", "ruff check .",
                  pliki={"pyproject.toml": "[tool.ruff.lint]\nselect = []\n"}),
        przypadek("ruff.toml", "ruff check .", pliki={"ruff.toml": "[lint]\nselect = []\n"}),
        przypadek("eslint --no-eslintrc", "npx eslint --no-eslintrc src"),
        przypadek("eslint --no-config-lookup", "npx eslint --no-config-lookup src"),
    ]),
]


@pytest.mark.parametrize(
    ("nazwa", "komenda", "kod", "wyjscie", "pliki"),
    [pytest.param(nazwa, *p.values, id=f"{nazwa}: {p.id}") for nazwa, lista in DODATNIE for p in lista],
)
def test_bramka_ktora_nie_moze_zawiesc_daje_sygnal_z_cytatem_i_poprawka(
    tmp_path, nazwa, komenda, kod, wyjscie, pliki,
):
    sygnal = _sygnal(_ocen(_repo(tmp_path, pliki), komenda, kod, wyjscie), nazwa)

    assert sygnal.cytat.strip()
    assert len(sygnal.cytat) <= LIMIT_CYTATU
    assert sygnal.poprawka == POPRAWKI[nazwa]


# --- przypadki ujemne: tego sygnalu nie ma ---------------------------------------------------

UJEMNE: list[tuple[str, list]] = [
    (BRAK_TESTOW, [
        przypadek("kod 5 innej komendy", "make test", 5, ""),
        przypadek("kod 5 skryptu bez pytest", "sh t.sh", 5, "", {"t.sh": "make\n"}),
        przypadek("pytest kod 0", "pytest -q", 0, "3 passed in 0.01s\n"),
        przypadek("pytest kod 1", "pytest -q", 1, "1 failed in 0.01s\n"),
        przypadek("kod 5 jesta", "npx jest", 5),
        przypadek("kod 5 vitesta", "npx vitest run", 5),
    ]),
    (PRZECHODZA_BEZ, [
        przypadek("jest bez flagi", "npx jest"),
        przypadek("flaga w niewolanym skrypcie", "npm test",
                  pliki=_pakiet(test="jest", inny="jest --passWithNoTests")),
    ]),
    (WYMUSZONE_ZERO, [
        # silnik uruchamia komende bez powloki: `|| true` to zwykle argumenty i niczego nie maskuje
        przypadek("|| true bez powloki", "pytest -q || true"),
        przypadek("; exit 0 bez powloki", "pytest -q ; exit 0"),
        przypadek("|| inne polecenie", 'sh -c "pytest -q || true_sprawdz"'),
        przypadek("|| false", 'sh -c "pytest -q || false"'),
        przypadek("; exit 1", 'sh -c "pytest -q; exit 1"'),
        przypadek("komentarz exit 0", "sh t.sh", pliki={"t.sh": "pytest -q\n# exit 0\n"}),
        przypadek("komentarz || true", "sh t.sh", pliki={"t.sh": "# pytest || true\npytest -q\n"}),
        przypadek("exit 0 nie na koncu", "sh t.sh",
                  pliki={"t.sh": "if [ -z \"$X\" ]; then\n  exit 0\nfi\npytest -q\n"}),
        przypadek("niewolany skrypt", "sh t.sh",
                  pliki={"t.sh": "pytest -q\n", "u.sh": "exit 0\n"}),
        przypadek("niewolany skrypt npm", "npm test",
                  pliki=_pakiet(test="jest", inny="jest || true")),
    ]),
    (WSZYSTKIE_POMINIETE, [
        przypadek("pytest passed", "pytest -q", 0, "3 passed, 2 skipped in 0.01s\n"),
        przypadek("pytest failed", "pytest -q", 1, "1 failed, 2 skipped in 0.01s\n"),
        przypadek("pytest error", "pytest -q", 1, "1 error, 2 skipped in 0.01s\n"),
        przypadek("pytest errors", "pytest -q", 1, "2 skipped, 3 errors in 0.01s\n"),
        przypadek("jest passed", "npx jest", 0, "Tests:       2 skipped, 3 passed, 5 total\n"),
        przypadek("vitest passed", "npx vitest run", 0, "      Tests  3 passed | 2 skipped (5)\n"),
        przypadek("vitest failed", "npx vitest run", 1, "      Tests  1 failed | 2 skipped (3)\n"),
    ]),
    (BLAD_IGNOROWANY, [
        przypadek("false w skrypcie", "sh t.sh",
                  pliki={"t.sh": "cat <<K\ncontinue-on-error: false\nK\n"}),
    ]),
    (ATRAPA_NPM, [
        przypadek("prawdziwy skrypt testow", "npm test", pliki=_pakiet(test="jest")),
        przypadek("atrapa w niewolanym skrypcie", "npm run build",
                  pliki=_pakiet(build="tsc", test=NPM_INIT)),
    ]),
    (BEZ_ASERCJI, [
        przypadek("assert", "pytest -q",
                  pliki={"tests/test_a.py": "def test_a():\n    assert 1\n"}),
        przypadek("expect(", "npx jest", pliki={"a.test.js": "test('a', () => expect(1));\n"}),
        przypadek("should", "npx jest", pliki={"a.test.js": "it('a', () => x.should.be.ok);\n"}),
        przypadek("jeden z dwoch plikow ma asercje", "pytest -q",
                  pliki={"tests/test_a.py": "def test_a():\n    pass\n",
                         "tests/test_b.py": "def test_b():\n    assert 1\n"}),
        przypadek("brak plikow testowych", "pytest -q", pliki={"a.py": "x = 1\n"}),
        przypadek("komenda nietestowa", "ruff check .",
                  pliki={"tests/test_a.py": "def test_a():\n    pass\n"}),
    ]),
    (LINT_BEZ_REGUL, [
        przypadek("ruff z regulami", "ruff check .",
                  pliki={"pyproject.toml": "[tool.ruff.lint]\nselect = [\"E\"]\n"}),
        przypadek("ruff.toml z regulami", "ruff check .",
                  pliki={"ruff.toml": "[lint]\nselect = [\"E\"]\n"}),
        przypadek("inna komenda niz ruff", "mypy .", pliki={"ruff.toml": "[lint]\nselect = []\n"}),
        przypadek("zwykly eslint", "npx eslint src"),
        przypadek("--no-eslintrc z --rule", "npx eslint --no-eslintrc --rule semi:error src"),
        przypadek("--no-config-lookup z --rule",
                  "npx eslint --no-config-lookup --rule semi:error src"),
    ]),
    (NIE_OCENIONO, [
        przypadek("sh skrypt", "sh t.sh", pliki={"t.sh": "make\n"}),
        przypadek("bash skrypt", "bash t.sh", pliki={"t.sh": "make\n"}),
        przypadek("./skrypt", "./t.sh", pliki={"t.sh": "make\n"}),
        przypadek("skrypt w podkatalogu", "sh sub/t.sh", pliki={"sub/t.sh": "make\n"}),
        przypadek("npm test", "npm test", pliki=_pakiet(test="jest")),
        przypadek("npm run X", "npm run sprawdz", pliki=_pakiet(sprawdz="tsc")),
        przypadek("ruff.toml", "ruff check .", pliki={"ruff.toml": "[lint]\nselect = [\"E\"]\n"}),
        przypadek("pyproject", "ruff check .",
                  pliki={"pyproject.toml": "[tool.ruff.lint]\nselect = [\"E\"]\n"}),
        przypadek("pliki testow", "pytest -q",
                  pliki={"tests/test_a.py": "def test_a():\n    assert 1\n"}),
        przypadek("komenda bez plikow", "make test"),
    ]),
]


@pytest.mark.parametrize(
    ("nazwa", "komenda", "kod", "wyjscie", "pliki"),
    [pytest.param(nazwa, *p.values, id=f"{nazwa}: {p.id}") for nazwa, lista in UJEMNE for p in lista],
)
def test_bramka_bez_tej_usterki_nie_dostaje_jej_sygnalu(
    tmp_path, nazwa, komenda, kod, wyjscie, pliki,
):
    assert nazwa not in _nazwy(_ocen(_repo(tmp_path, pliki), komenda, kod, wyjscie))


def test_zielony_pytest_z_asercjami_nie_dostaje_zadnego_sygnalu(tmp_path):
    repo = _repo(tmp_path, {"tests/test_a.py": "def test_a():\n    assert 1\n"})

    assert _ocen(repo, "pytest -q", 0, "...\n3 passed in 0.02s\n") == []


@pytest.mark.parametrize("liczba", ["47", "1"])
def test_wszystkie_testy_pominiete_podaja_w_cytacie_liczbe_pominietych(tmp_path, liczba):
    sygnaly = _ocen(_repo(tmp_path), "pytest -q", 0, f"{liczba} skipped in 0.02s\n")

    assert f"{liczba} skipped" in _sygnal(sygnaly, WSZYSTKIE_POMINIETE).cytat


def test_dwie_usterki_jednej_komendy_daja_dwa_sygnaly(tmp_path):
    sygnaly = _ocen(_repo(tmp_path), 'sh -c "npx jest --passWithNoTests || true"')

    assert {PRZECHODZA_BEZ, WYMUSZONE_ZERO} <= _nazwy(sygnaly)


# --- cytat -----------------------------------------------------------------------------------


def test_cytat_pokazuje_fragment_wymuszajacy_zero(tmp_path):
    sygnaly = _ocen(_repo(tmp_path), 'sh -c "pytest -q || true"')

    assert "|| true" in _sygnal(sygnaly, WYMUSZONE_ZERO).cytat


def test_cytat_z_dlugiej_komendy_ma_najwyzej_dwiescie_znakow_i_nadal_pokazuje_usterke(tmp_path):
    komenda = 'sh -c "echo ' + "a" * 600 + ' || true"'

    cytat = _sygnal(_ocen(_repo(tmp_path), komenda), WYMUSZONE_ZERO).cytat

    assert len(cytat) <= LIMIT_CYTATU
    assert "|| true" in cytat


def test_sekret_w_komendzie_jest_w_cytacie_zamaskowany(tmp_path):
    cytat = _sygnal(
        _ocen(_repo(tmp_path), 'sh -c "TOKEN=abc123 pytest || true"'), WYMUSZONE_ZERO).cytat

    assert "TOKEN=***" in cytat
    assert "abc123" not in cytat


def test_sekret_w_pliku_skryptu_nie_trafia_do_cytatu(tmp_path):
    repo = _repo(tmp_path, {"t.sh": "PASSWORD=hasloZpliku pytest || true\n"})

    cytat = _sygnal(_ocen(repo, "sh t.sh"), WYMUSZONE_ZERO).cytat

    assert "PASSWORD=***" in cytat
    assert "hasloZpliku" not in cytat


def test_sekret_z_wyjscia_komendy_nie_trafia_do_cytatu(tmp_path):
    wyjscie = "TOKEN=abc123 Error: no test specified\n"

    cytat = _sygnal(_ocen(_repo(tmp_path), "make test", 1, wyjscie), ATRAPA_NPM).cytat

    assert "abc123" not in cytat


def test_sekret_dluzszy_niz_cytat_nie_wycieka_po_przycieciu(tmp_path):
    # przyciecie przed maskowaniem odcieloby `TOKEN=` i zostawilo ogon wartosci
    komenda = 'sh -c "TOKEN=' + "Q7" * 300 + ' pytest || true"'

    cytat = _sygnal(_ocen(_repo(tmp_path), komenda), WYMUSZONE_ZERO).cytat

    assert "Q7Q7" not in cytat
    assert len(cytat) <= LIMIT_CYTATU


def test_sygnalu_bramki_nie_da_sie_zmienic_po_ocenie(tmp_path):
    sygnal = _sygnal(_ocen(_repo(tmp_path), "npx jest --passWithNoTests"), PRZECHODZA_BEZ)

    with pytest.raises(dataclasses.FrozenInstanceError):
        sygnal.nazwa = "inna"


# --- nie oceniono skryptu: kazda przyczyna osobno --------------------------------------------


@pytest.mark.parametrize(
    ("komenda", "pliki"),
    [
        pytest.param("sh brak.sh", {}, id="brak pliku"),
        pytest.param("bash brak.sh", {}, id="brak pliku bash"),
        pytest.param("./brak.sh", {}, id="brak pliku ./"),
        pytest.param("sh katalog.sh", {"katalog.sh/x": ""}, id="katalog zamiast pliku"),
        pytest.param("sh t.sh", {"t.sh": b"\xff\xfe\x00 exit 0"}, id="nie UTF-8"),
        pytest.param("sh " + "a" * 300 + ".sh", {}, id="nazwa za dluga dla systemu"),
        pytest.param("sh a\x00b.sh", {}, id="bajt zerowy w nazwie"),
        pytest.param("npm test", {}, id="brak package.json"),
        pytest.param("npm test", {"package.json": "{zle"}, id="zly JSON"),
        pytest.param("npm test", {"package.json": "[" * 100_000}, id="JSON bez konca zagniezdzen"),
        pytest.param("npm test", {"package.json": b"\xff\xfe"}, id="package.json nie UTF-8"),
        pytest.param("npm test", {"package.json": "[]"}, id="package.json to lista"),
        pytest.param("npm test", {"package.json": '{"scripts": []}'}, id="scripts to lista"),
        pytest.param("npm test", {"package.json": '{"scripts": {"test": 5}}'},
                     id="skrypt to liczba"),
        pytest.param("npm run brak", _pakiet(test="jest"), id="brak skryptu w package.json"),
        pytest.param("ruff check .", {"ruff.toml": "[zle"}, id="zly TOML"),
        pytest.param("pytest -q", {"tests/test_a.py": b"\xff\xfe"}, id="plik testow nie UTF-8"),
    ],
)
def test_skrypt_ktorego_nie_da_sie_przeczytac_daje_sygnal_nie_oceniono_zamiast_ciszy(
    tmp_path, komenda, pliki,
):
    sygnal = _sygnal(_ocen(_repo(tmp_path, pliki), komenda), NIE_OCENIONO)

    assert len(sygnal.cytat) <= LIMIT_CYTATU
    assert sygnal.poprawka == POPRAWKI[NIE_OCENIONO]


def test_nieczytelny_plik_testow_nie_jest_przedstawiany_jako_testy_bez_asercji(tmp_path):
    repo = _repo(tmp_path, {"tests/test_a.py": b"\xff\xfe"})

    assert BEZ_ASERCJI not in _nazwy(_ocen(repo, "pytest -q"))


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root czyta plik mimo praw 000")
def test_skrypt_bez_prawa_odczytu_daje_sygnal_nie_oceniono(tmp_path):
    repo = _repo(tmp_path, {"t.sh": "exit 0\n"})
    (repo / "t.sh").chmod(0o000)
    try:
        sygnaly = _ocen(repo, "sh t.sh")
    finally:
        (repo / "t.sh").chmod(0o600)

    assert NIE_OCENIONO in _nazwy(sygnaly)


def test_za_duzy_skrypt_daje_sygnal_nie_oceniono(tmp_path):
    repo = _repo(tmp_path, {"t.sh": "pytest\n"})
    os.truncate(repo / "t.sh", 64 * 1024 * 1024)     # plik rzadki: limit czytania ma byc mniejszy

    assert NIE_OCENIONO in _nazwy(_ocen(repo, "sh t.sh"))


def test_potok_nazwany_w_miejscu_skryptu_nie_zawiesza_oceny(tmp_path):
    repo = _repo(tmp_path)
    os.mkfifo(repo / "t.sh")
    wynik: list = []
    watek = threading.Thread(target=lambda: wynik.append(_ocen(repo, "sh t.sh")), daemon=True)

    watek.start()
    watek.join(timeout=10)
    zawisl = watek.is_alive()
    if zawisl:      # zwalnia zablokowane otwarcie, zeby watek nie przezyl testu
        os.close(os.open(repo / "t.sh", os.O_RDWR | os.O_NONBLOCK))
        watek.join(timeout=10)

    assert not zawisl, "ocena czeka na dane z potoku nazwanego"
    assert wynik and NIE_OCENIONO in _nazwy(wynik[0])


@pytest.mark.parametrize("sposob", ["dowiazanie", "sciezka z dwiema kropkami", "sciezka bezwzgledna"])
def test_skrypt_spoza_repozytorium_nie_jest_czytany_tylko_zgloszony_jako_nieoceniony(
    tmp_path, sposob,
):
    repo = _repo(tmp_path)
    obcy = tmp_path / "obcy.sh"
    obcy.write_text("sekret || true\nexit 0\n", encoding="utf-8")
    if sposob == "dowiazanie":
        (repo / "t.sh").symlink_to(obcy)
        komenda = "sh t.sh"
    elif sposob == "sciezka z dwiema kropkami":
        komenda = "sh ../obcy.sh"
    else:
        komenda = f"sh {obcy}"

    sygnaly = _ocen(repo, komenda)

    assert NIE_OCENIONO in _nazwy(sygnaly)
    assert WYMUSZONE_ZERO not in _nazwy(sygnaly)
    assert not any("sekret" in s.cytat for s in sygnaly)


def test_dowiazanie_do_skryptu_wewnatrz_repozytorium_jest_czytane_jak_zwykly_skrypt(tmp_path):
    repo = _repo(tmp_path, {"skrypty/prawdziwy.sh": "pytest || true\n"})
    (repo / "t.sh").symlink_to(repo / "skrypty" / "prawdziwy.sh")

    nazwy = _nazwy(_ocen(repo, "sh t.sh"))

    assert WYMUSZONE_ZERO in nazwy
    assert NIE_OCENIONO not in nazwy


# --- ocena nie rzuca i niczego nie uruchamia -------------------------------------------------


@pytest.mark.parametrize("komenda", ['pytest "', "", "   ", "sh", "sh -c", "npm", "npm run"])
def test_nieczytelna_albo_urwana_komenda_nie_przerywa_oceny(tmp_path, komenda):
    assert isinstance(_ocen(_repo(tmp_path), komenda, None, ""), list)


def test_ocena_niczego_nie_uruchamia_i_nie_zapisuje_w_repozytorium(tmp_path):
    repo = _repo(tmp_path, {"t.sh": "touch z-pliku\nexit 0\n"})

    def migawka():
        return sorted(
            (str(p.relative_to(repo)), p.stat().st_mode, p.stat().st_mtime_ns,
             p.read_bytes() if p.is_file() else None)
            for p in [repo, *repo.rglob("*")])

    przed = migawka()
    _ocen(repo, 'sh -c "touch z-komendy || true"')
    sygnaly = _ocen(repo, "sh t.sh")

    assert WYMUSZONE_ZERO in _nazwy(sygnaly)     # skrypt zostal przeczytany
    assert migawka() == przed


# --- elementy oceny: kazdy przypadek rozstrzyga jeden element wykrywania ---------------------


def w_skrypcie(opis, tresc, kod=0, wyjscie="ok\n", **pliki):
    return przypadek(opis, "sh t.sh", kod, wyjscie, {"t.sh": tresc, **pliki})


def lancuch(dlugosc: int) -> dict[str, str]:
    """Skrypty 1.sh -> 2.sh -> ... ; ostatni wymusza kod 0."""
    return {**{f"{i}.sh": f"sh {i + 1}.sh\n" for i in range(1, dlugosc)},
            f"{dlugosc}.sh": "exit 0\n"}


_ESC = "\x1b"
KOLOROWE_POMINIETE = (f"{_ESC}[33m===== {_ESC}[1;33m47 skipped{_ESC}[0m{_ESC}[33m in 0.01s"
                      f"{_ESC}[0m =====\n")

JEST_SYGNAL: list[tuple[str, list]] = [
    (WSZYSTKIE_POMINIETE, [
        przypadek("kolorowe podsumowanie", "pytest", 0, KOLOROWE_POMINIETE),
        przypadek("passedx to nie passed", "pytest", 0, "2 skipped, 1 passedx in 1s\n"),
    ]),
    (BRAK_TESTOW, [
        # pierwsza linia to samo przypisanie: komenda bez programu nie przerywa oceny
        w_skrypcie("przypisanie zmiennej przed programem", "X=1\nCI=1 pytest -q\n", 5, ""),
        przypadek("pelna sciezka pytest", "/usr/bin/pytest", 5, ""),
        przypadek("python z numerem wersji", "python3.12 -m pytest", 5, ""),
        przypadek("pelna sciezka interpretera", "/usr/bin/python3 -m pytest", 5, ""),
        przypadek("flaga interpretera przed -m", "python -B -m pytest", 5, ""),
        w_skrypcie("pytest przed jestem", "pytest; jest\n", 5, ""),
        w_skrypcie("srednik", "echo a;pytest\n", 5, ""),
        w_skrypcie("i-i", "echo a&&pytest\n", 5, ""),
        w_skrypcie("potok", "echo a|pytest\n", 5, ""),
        w_skrypcie("nawias otwierajacy", "(pytest)\n", 5, ""),
        w_skrypcie("nawias zamykajacy", "case $x in a) pytest ;; esac\n", 5, ""),
        w_skrypcie("podstawienie wejscia", "cat <(pytest)\n", 5, ""),
        w_skrypcie("podstawienie wyjscia", "tee >(pytest)\n", 5, ""),
        w_skrypcie("cudzyslow bez zamkniecia", 'pytest -q "otwarty\n', 5, ""),
    ]),
    (BEZ_ASERCJI, [
        przypadek("npx z pelna sciezka", "/usr/bin/npx jest", pliki={"a.test.js": "x\n"}),
        przypadek("plik z przyrostkiem _test", "pytest", pliki={"a_test.py": "pass\n"}),
        *[przypadek(nazwa, "npx jest", pliki={nazwa: "x\n"})
          for nazwa in ("a.test.mjs", "a.test.cjs", "a.test.jsx", "a.spec.tsx")],
        *[przypadek(f"asercje tylko w {katalog}", "pytest",
                    pliki={"tests/test_a.py": "pass\n", f"{katalog}/test_x.py": "assert 1\n"})
          for katalog in (".venv", "node_modules")],
    ]),
    (WYMUSZONE_ZERO, [
        przypadek("sh -c -e", 'sh -c -e "pytest || true"'),
        przypadek("dluga opcja z litera c to nie -c", "bash --norc t.sh",
                  pliki={"t.sh": "exit 0\n"}),
        przypadek("bez spacji po ||", 'sh -c "x ||true"'),
        przypadek("po true srednik", 'sh -c "x || true; y"'),
        przypadek("po true ampersand", 'sh -c "x || true&& y"'),
        przypadek("po true potok", 'sh -c "x || true| y"'),
        przypadek("po true nawias", 'sh -c "(x || true)"'),
        w_skrypcie("exit 0 ze srednikiem na koncu", "x\nexit 0;\n"),
        w_skrypcie("exit 0 z komentarzem w linii", "x\n  exit 0 # zawsze\n"),
        w_skrypcie("nazwa skryptu w cudzyslowie", 'sh "u.sh"\n', **{"u.sh": "exit 0\n"}),
        w_skrypcie("nazwa skryptu z plusem", "sh a+b.sh\n", **{"a+b.sh": "exit 0\n"}),
        przypadek("npm z opcja przed test", "npm --silent test", pliki=_pakiet(test="x || true")),
        przypadek("piec poziomow skryptow", "sh 1.sh", pliki=lancuch(5)),
    ]),
    (BLAD_IGNOROWANY, [
        przypadek("komenda bez powloki", 'narzedzie --krok "continue-on-error: true"'),
        w_skrypcie("bez spacji", "echo continue-on-error:true\n"),
    ]),
    (LINT_BEZ_REGUL, [
        przypadek("tool.ruff bez sekcji lint", "ruff check",
                  pliki={"pyproject.toml": "[tool.ruff]\nselect = []\n"}),
        przypadek("ruff.toml bez sekcji lint", "ruff check", pliki={"ruff.toml": "select = []\n"}),
        przypadek("ruff.toml przed pyproject", "ruff check",
                  pliki={"ruff.toml": "select = []\n",
                         "pyproject.toml": "[tool.ruff]\nselect = [\"E\"]\n"}),
    ]),
    (NIE_OCENIONO, [
        przypadek("szesc poziomow skryptow", "sh 1.sh", pliki=lancuch(6)),
        przypadek("-c liczy sie jako poziom", 'sh -c "sh 1.sh"', pliki=lancuch(5)),
        przypadek("skrypt npm liczy sie jako poziom", "npm test",
                  pliki={**lancuch(5), **_pakiet(test="sh 1.sh")}),
        przypadek("tool to nie tabela", "ruff check", pliki={"pyproject.toml": "tool = 5\n"}),
        przypadek("tool.ruff to nie tabela", "ruff check",
                  pliki={"pyproject.toml": "[tool]\nruff = 5\n"}),
        przypadek("lint to nie tabela", "ruff check", pliki={"ruff.toml": "lint = 5\n"}),
        przypadek("TOML bez konca zagniezdzen", "ruff check",
                  pliki={"ruff.toml": "a = " + "[" * 100_000}),
    ]),
]

NIE_MA_SYGNALU: list[tuple[str, list]] = [
    (WSZYSTKIE_POMINIETE, [
        przypadek("xpassed to wykonane", "pytest", 0, "2 skipped, 1 xpassed in 1s\n"),
        przypadek("xfailed to wykonane", "pytest", 0, "2 skipped, 1 xfailed in 1s\n"),
        przypadek("zero pominietych", "npx jest", 0, "Tests: 0 skipped, 0 total\n"),
        przypadek("slowo dluzsze niz skipped", "pytest", 0, "3 skippedx in 1s\n"),
        przypadek("wykonane w innym podsumowaniu", "pytest", 0,
                  "3 passed in 1s\n2 skipped in 1s\n"),
        przypadek("liczba w srodku zdania", "make", 0, "kolejka: 47 skipped in 1s\n"),
        przypadek("bez czasu trwania", "make", 0, "47 skipped\n"),
        przypadek("po in nie ma liczby", "make", 0, "47 skipped in queue\n"),
    ]),
    (BRAK_TESTOW, [
        przypadek("-m nalezy do skryptu", "python narzedzie.py -m pytest", 5, ""),
        przypadek("python bez -m", "python narzedzie.py", 5, ""),
    ]),
    (BEZ_ASERCJI, [
        przypadek("testy innego jezyka", "pytest", pliki={"a.test.js": "x\n"}),
        *[przypadek(nazwa, "pytest", pliki={nazwa: "x\n"})
          for nazwa in ("test_a.py.bak", "xtest_a.py", "a_test.pyc")],
        przypadek("migawka jesta", "npx jest", pliki={"a.test.js.snap": "x\n"}),
        przypadek("tylko cudze testy", "pytest",
                  pliki={".venv/test_x.py": "pass\n", "node_modules/test_y.py": "pass\n"}),
        przypadek("nieczytelny plik obok pliku z asercja", "pytest",
                  pliki={"test_a.py": b"\xff", "test_b.py": "assert 1\n"}),
        przypadek("nieczytelny plik obok pliku bez asercji", "pytest",
                  pliki={"test_a.py": b"\xff", "test_b.py": "pass\n"}),
    ]),
    (NIE_OCENIONO, [
        przypadek("nieczytelny plik obok pliku z asercja", "pytest",
                  pliki={"test_a.py": b"\xff", "test_b.py": "assert 1\n"}),
        w_skrypcie("skrypt wolajacy sam siebie", "sh t.sh\n"),
        przypadek("piec poziomow skryptow", "sh 1.sh", pliki=lancuch(5)),
        przypadek("argument skryptu to nie skrypt", "sh t.sh brak-pliku",
                  pliki={"t.sh": "make\n"}),
        przypadek("npm ci", "npm ci"),
        przypadek("program w repozytorium bez .sh", "./narzedzie",
                  pliki={"narzedzie": b"\xff"}),
        przypadek("pyproject bez ruffa", "ruff check",
                  pliki={"pyproject.toml": "[project]\nname = \"x\"\n"}),
    ]),
    (WYMUSZONE_ZERO, [
        przypadek("exit 01 po sredniku", 'sh -c "x; exit 01; y"'),
        w_skrypcie("ostatnia linia exit 01", "x\nexit 01\n"),
        w_skrypcie("echo exit 0 na koncu", "echo exit 0\n"),
        w_skrypcie("wciety komentarz", "  # x || true\nmake\n"),
        przypadek("sama nazwa skryptu idzie przez PATH", "t.sh", pliki={"t.sh": "exit 0\n"}),
    ]),
    (BLAD_IGNOROWANY, [w_skrypcie("trueish", "echo continue-on-error: trueish\n")]),
    (LINT_BEZ_REGUL, [
        przypadek("--rule ze znakiem rownosci",
                  "npx eslint --no-eslintrc --rule=semi:error src"),
        w_skrypcie("--rule w nastepnej linii",
                   "npx eslint --no-eslintrc \\\n  --rule semi:error\n"),
        w_skrypcie("pusty argument nie dzieli komendy",
                   'npx eslint --no-eslintrc "" --rule semi:error\n'),
        przypadek("ruff.toml z regulami wygrywa z pyproject", "ruff check",
                  pliki={"ruff.toml": "select = [\"E\"]\n",
                         "pyproject.toml": "[tool.ruff]\nselect = []\n"}),
    ]),
]


def _przypadki(tabela):
    return [pytest.param(nazwa, *p.values, id=f"{nazwa}: {p.id}")
            for nazwa, lista in tabela for p in lista]


@pytest.mark.parametrize(
    ("nazwa", "komenda", "kod", "wyjscie", "pliki"), _przypadki(JEST_SYGNAL))
def test_usterka_bramki_ukryta_w_nietypowym_zapisie_jest_zgloszona(
    tmp_path, nazwa, komenda, kod, wyjscie, pliki,
):
    assert nazwa in _nazwy(_ocen(_repo(tmp_path, pliki), komenda, kod, wyjscie))


@pytest.mark.parametrize(
    ("nazwa", "komenda", "kod", "wyjscie", "pliki"), _przypadki(NIE_MA_SYGNALU))
def test_zapis_podobny_do_usterki_nie_daje_falszywego_alarmu(
    tmp_path, nazwa, komenda, kod, wyjscie, pliki,
):
    assert nazwa not in _nazwy(_ocen(_repo(tmp_path, pliki), komenda, kod, wyjscie))


@pytest.mark.parametrize(("dodatkowe", "czytelny"), [(0, True), (1, False)])
def test_skrypt_do_jednego_mebibajta_jest_czytany_a_o_bajt_wiekszy_nie(
    tmp_path, dodatkowe, czytelny,
):
    koniec = "\nexit 0\n"
    tresc = "#" * (1024 * 1024 + dodatkowe - len(koniec)) + koniec

    nazwy = _nazwy(_ocen(_repo(tmp_path, {"t.sh": tresc}), "sh t.sh"))

    assert nazwy == {WYMUSZONE_ZERO if czytelny else NIE_OCENIONO}


def test_repozytorium_podane_przez_dowiazanie_jest_czytane(tmp_path):
    _repo(tmp_path, {"t.sh": "exit 0\n"})
    (tmp_path / "dowiazanie").symlink_to(tmp_path / "repo")

    assert _nazwy(_ocen(tmp_path / "dowiazanie", "sh t.sh")) == {WYMUSZONE_ZERO}


def test_zerwane_dowiazanie_konfiguracji_ruffa_to_nie_ocenione_a_nie_brak_pliku(tmp_path):
    repo = _repo(tmp_path)
    (repo / "ruff.toml").symlink_to(repo / "nie-ma.toml")

    assert NIE_OCENIONO in _nazwy(_ocen(repo, "ruff check"))


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root czyta katalog mimo praw 000")
def test_nieczytelny_katalog_z_testami_to_nie_ocenione_a_nie_testy_bez_asercji(tmp_path):
    repo = _repo(tmp_path, {"test_a.py": "pass\n", "zamkniety/test_b.py": "assert 1\n"})
    (repo / "zamkniety").chmod(0o000)
    try:
        nazwy = _nazwy(_ocen(repo, "pytest"))
    finally:
        (repo / "zamkniety").chmod(0o700)

    assert nazwy == {NIE_OCENIONO}


@pytest.mark.parametrize(
    ("komenda", "pliki", "cytat"),
    [
        pytest.param("sh sub/brak.sh", {}, "sub/brak.sh", id="sciezka skryptu"),
        pytest.param("./brak.sh", {}, "./brak.sh", id="sciezka programu .sh"),
        pytest.param("npm run brak", _pakiet(test="jest"),
                     "package.json dla komendy: npm run brak", id="package.json"),
        # za gleboko jest tresc szostego skryptu
        pytest.param("sh 1.sh", lancuch(6), "exit 0", id="limit zagniezdzenia"),
        pytest.param("pytest", {"tests/test_a.py": b"\xff"}, "tests/test_a.py", id="plik testow"),
        pytest.param("pytest", {"tests/test_b.py": b"\xff", "tests/test_a.py": b"\xff"},
                     "tests/test_a.py", id="pierwszy z dwoch plikow testow"),
        pytest.param("ruff check .", {"ruff.toml": "[zle"}, "ruff.toml", id="ruff.toml"),
        pytest.param("ruff check .", {"pyproject.toml": "[zle"}, "pyproject.toml",
                     id="pyproject.toml"),
    ],
)
def test_cytat_nieocenionego_skryptu_wskazuje_czego_nie_oceniono(tmp_path, komenda, pliki, cytat):
    assert _sygnal(_ocen(_repo(tmp_path, pliki), komenda), NIE_OCENIONO).cytat == cytat


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root czyta katalog mimo praw 000")
def test_cytat_nieczytelnego_katalogu_testow_to_sciezka_wzgledem_repozytorium(tmp_path):
    repo = _repo(tmp_path, {"tests/zamkniety/test_b.py": "assert 1\n"})
    (repo / "tests" / "zamkniety").chmod(0o000)
    try:
        sygnaly = _ocen(repo, "pytest")
    finally:
        (repo / "tests" / "zamkniety").chmod(0o700)

    assert _sygnal(sygnaly, NIE_OCENIONO).cytat == "tests/zamkniety"


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root przeszukuje katalog bez praw")
def test_repozytorium_bez_prawa_przeszukiwania_to_nie_ocenione_a_nie_brak_konfiguracji_ruffa(
    tmp_path,
):
    repo = _repo(tmp_path, {"ruff.toml": "select = []\n"})
    repo.chmod(0o600)
    try:
        sygnaly = _ocen(repo, "ruff check .")
    finally:
        repo.chmod(0o700)

    assert [(s.nazwa, s.cytat) for s in sygnaly] == [(NIE_OCENIONO, "ruff.toml")]


def test_blad_dysku_przy_sprawdzaniu_konfiguracji_ruffa_daje_tylko_nie_ocenione_bez_czytania_tresci(
    tmp_path, monkeypatch,
):
    repo = _repo(tmp_path, {"ruff.toml": "select = []\n"})
    prawdziwy = os.lstat

    def lstat(sciezka, *args, **kwargs):
        # tylko konfiguracja ruffa: `os.path.realpath` tez wola `lstat`
        if os.path.basename(os.fspath(sciezka)) == "ruff.toml":
            raise OSError(errno.EIO, "blad wejscia-wyjscia")
        return prawdziwy(sciezka, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)

    sygnaly = _ocen(repo, "ruff check .")

    assert [(s.nazwa, s.cytat) for s in sygnaly] == [(NIE_OCENIONO, "ruff.toml")]


def test_cytat_lintu_bez_regul_wskazuje_plik_konfiguracji_i_komende(tmp_path):
    repo = _repo(tmp_path, {"ruff.toml": "select = []\n"})

    cytat = _sygnal(_ocen(repo, "ruff check ."), LINT_BEZ_REGUL).cytat

    assert cytat == "ruff.toml: select = [] (ruff check .)"


@pytest.mark.parametrize(
    ("komenda", "pliki"),
    [
        pytest.param('sh -c ""', {}, id="pusty argument -c"),
        pytest.param("sh t.sh", {"t.sh": ""}, id="pusty skrypt"),
        pytest.param("sh t.sh", {"t.sh": "#!/bin/sh\n\n  # nic\n"}, id="same komentarze"),
        pytest.param("npm test", _pakiet(test=""), id="pusty skrypt w package.json"),
    ],
)
def test_skrypt_bez_zadnej_komendy_jest_oceniony_bez_sygnalu(tmp_path, komenda, pliki):
    assert _ocen(_repo(tmp_path, pliki), komenda) == []


def test_cytat_testow_bez_asercji_wymienia_pliki_w_stalej_kolejnosci(tmp_path):
    nazwy = [f"{katalog}/test_{litera}.py" for katalog in "dacb" for litera in "hcaf"]
    repo = _repo(tmp_path, {nazwa: "pass\n" for nazwa in nazwy})

    cytat = _sygnal(_ocen(repo, "pytest"), BEZ_ASERCJI).cytat

    assert cytat == f"pytest (pliki testów: {', '.join(sorted(nazwy))})"[:LIMIT_CYTATU]


def test_cytat_testow_bez_asercji_pokazuje_pierwsza_komende_testowa(tmp_path):
    repo = _repo(tmp_path, {"a.sh": "jest --pierwsza\n", "b.sh": "jest --druga\n",
                            "a.test.js": "x\n"})

    cytat = _sygnal(_ocen(repo, 'sh -c "sh a.sh; sh b.sh"'), BEZ_ASERCJI).cytat

    assert "--pierwsza" in cytat
    assert "--druga" not in cytat


def test_powtorzona_usterka_to_jeden_sygnal_z_cytatem_pierwszego_wystapienia(tmp_path):
    repo = _repo(tmp_path, {"a.sh": "pierwszy || true\n", "b.sh": "drugi || true\n"})

    sygnaly = _ocen(repo, 'sh -c "sh a.sh; sh b.sh"')

    assert [(s.nazwa, s.cytat) for s in sygnaly] == [(WYMUSZONE_ZERO, "pierwszy || true")]


def test_cytat_zaczyna_sie_od_linii_z_usterka_i_jest_jedna_linia(tmp_path):
    repo = _repo(tmp_path, {"t.sh": "echo pierwsza\nx  ||\ttrue\necho trzecia\n"})

    cytat = _sygnal(_ocen(repo, "sh t.sh"), WYMUSZONE_ZERO).cytat

    assert cytat == "x || true echo trzecia"


@pytest.mark.parametrize("ostatnia", ["exit 0;", "exit 0 # koniec", "exit 0", "exit 0 ;", "exit 0; # x"])
def test_cytat_koncowego_exit_0_pokazuje_ostatnia_linie_a_nie_wczesniejsza(tmp_path, ostatnia):
    repo = _repo(tmp_path, {"t.sh": f"if x; then\n  exit 0\nfi\n{ostatnia}\n"})

    assert _sygnal(_ocen(repo, "sh t.sh"), WYMUSZONE_ZERO).cytat == ostatnia


@pytest.mark.parametrize(
    ("nazwa", "komenda", "pliki", "fragment"),
    [
        pytest.param(WYMUSZONE_ZERO, f"ruff {'a' * 600} --exit-zero", {}, "--exit-zero",
                     id="exit-zero"),
        pytest.param(PRZECHODZA_BEZ, f"jest {'a' * 600} --passWithNoTests", {},
                     "--passWithNoTests", id="passWithNoTests"),
        pytest.param(LINT_BEZ_REGUL, f"eslint {'a' * 600} --no-eslintrc", {}, "--no-eslintrc",
                     id="eslint"),
        pytest.param(BLAD_IGNOROWANY, f"x {'a' * 600} 'continue-on-error: true'", {},
                     "continue-on-error: true", id="continue-on-error w komendzie"),
        pytest.param(BLAD_IGNOROWANY, "sh t.sh",
                     {"t.sh": f"echo {'a' * 600} continue-on-error: true\n"},
                     "continue-on-error: true", id="continue-on-error w skrypcie"),
        pytest.param(ATRAPA_NPM, "npm test", _pakiet(test=f"echo {'a' * 600}; {NPM_INIT}"),
                     "Error: no test specified", id="atrapa npm"),
    ],
)
def test_cytat_z_dlugiego_tekstu_pokazuje_miejsce_usterki(
    tmp_path, nazwa, komenda, pliki, fragment,
):
    assert fragment in _sygnal(_ocen(_repo(tmp_path, pliki), komenda), nazwa).cytat


@pytest.mark.parametrize(
    ("nazwa", "kod", "wyjscie", "cytat"),
    [
        (BRAK_TESTOW, 0, "ogonSekretu no tests ran in 1s\n", "no tests ran"),
        (BRAK_TESTOW, 5, "ogonSekretu\n", "pytest -q"),
        (ATRAPA_NPM, 1, "ogonSekretu Error: no test specified\n", "Error: no test specified"),
        (WSZYSTKIE_POMINIETE, 0, "47 skipped in 1s ogonSekretu\n", "47 skipped"),
    ],
)
def test_z_wyjscia_komendy_cytowane_jest_tylko_samo_dopasowanie(
    tmp_path, nazwa, kod, wyjscie, cytat,
):
    assert _sygnal(_ocen(_repo(tmp_path), "pytest -q", kod, wyjscie), nazwa).cytat == cytat


@pytest.mark.parametrize("znak", ["\\ud800", "\\u0000"], ids=["surogat", "NUL"])
def test_cytat_przyjmie_baza_i_json_mimo_dziwnych_znakow_w_skrypcie(tmp_path, znak):
    pliki = {"package.json": '{"scripts": {"test": "x' + znak + ' || true"}}'}

    cytat = _sygnal(_ocen(_repo(tmp_path, pliki), "npm test"), WYMUSZONE_ZERO).cytat

    assert "\x00" not in cytat
    assert cytat.encode("utf-8")
    assert "|| true" in cytat
