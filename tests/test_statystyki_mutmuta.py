"""Statystyki mutmuta liczone w shardach i scalane w jeden plik (HAR-255).

Skrypt `tools/statystyki_mutmuta.py` skraca job `mutation` na PR: statystyki mutmuta (testy calego
zestawu) licza rownolegle shardy, a job koncowy scala je w `mutants/mutmut-stats.json` i przy
`tools/mutanty_zmian.py --katalog-dowodow` zastepuje "clean tests" mutmuta dowodem z shardow.
Dowod to plik `shard-<indeks>.json` w katalogu dowodow: SHA commita, odcisk zrodel i konfiguracji,
lista uruchomionych testow, kod pytesta oraz statystyki mutmuta tego shardu.

Testy czyste (podzial, odcisk, wczytanie i sprawdzenie dowodow, zlozenie statystyk, podmiana clean
tests na atrapie runnera) wola funkcje modulu bezposrednio. Testy sciezki CLI (`shard`, `scal`,
`mutmut-run`) uruchamiaja prawdziwy skrypt i prawdziwego mutmuta na miniaturowym projekcie z gitem
w katalogu tymczasowym; atrapa mutmuta nie dowiodlaby, ze scalone statystyki przyjmuje prawdziwy
`load_stats` ani ze clean tests faktycznie sie nie uruchamiaja.
Modul importujemy w fixture, wiec brak implementacji oblewa kazdy test osobno.
"""
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from procesy import uruchom_i_zbierz

KORZEN = Path(__file__).resolve().parents[1]
SKRYPT = KORZEN / "tools" / "statystyki_mutmuta.py"
sys.path.insert(0, str(KORZEN / "tools"))


@pytest.fixture
def sm():
    import statystyki_mutmuta

    return statystyki_mutmuta


# --- dane wspolne testow czystych --------------------------------------------------------------

SHA = "a" * 40
ODCISK = "b" * 64
T = [f"tests/test_x.py::test_{i}" for i in range(6)]
PODZIAL_NA_3 = {0: [T[0], T[3]], 1: [T[1], T[4]], 2: [T[2], T[5]]}
POLA_DOWODU = [
    "indeks",
    "liczba_shardow",
    "sha",
    "odcisk",
    "testy",
    "kod_pytesta",
    "tests_by_mangled_function_name",
    "duration_by_test",
    "stats_time",
    "function_hashes",
    "function_dependencies",
]


def _dowod(indeks: int, *, liczba: int = 3, testy: list[str] | None = None, **nadpisz) -> dict:
    testy = list(PODZIAL_NA_3[indeks] if testy is None else testy)
    dowod = {
        "indeks": indeks,
        "liczba_shardow": liczba,
        "sha": SHA,
        "odcisk": ODCISK,
        "testy": testy,
        "kod_pytesta": 0,
        "tests_by_mangled_function_name": {f"pakiet.modul.x_f{indeks}": list(testy)},
        "duration_by_test": {t: 0.5 for t in testy},
        "stats_time": 1.5,
        "function_hashes": {f"pakiet.modul.x_f{indeks}": f"h{indeks}"},
        "function_dependencies": {},
    }
    dowod.update(nadpisz)
    return dowod


def _zapisz(katalog: Path, dowod: dict, nazwa: str | None = None) -> None:
    katalog.mkdir(parents=True, exist_ok=True)
    (katalog / (nazwa or f"shard-{dowod['indeks']}.json")).write_text(json.dumps(dowod), encoding="utf-8")


def _katalog(
    tmp_path: Path,
    *,
    bez: tuple[int, ...] = (),
    nadpisz: dict[int, dict] | None = None,
    testy: dict[int, list[str]] | None = None,
) -> Path:
    """Katalog dowodow trzech shardow (kolejnosc zapisu 2, 0, 1); `bez` pomija pliki."""
    katalog = tmp_path / "dowody"
    katalog.mkdir()
    for indeks in (2, 0, 1):
        if indeks in bez:
            continue
        argumenty = {"testy": (testy or {}).get(indeks), **(nadpisz or {}).get(indeks, {})}
        # `indeks` z nadpisania trafia do pola w pliku, a nazwa pliku zostaje wedlug petli.
        pole_indeksu = argumenty.pop("indeks", indeks)
        dowod = _dowod(indeks, **argumenty)
        dowod["indeks"] = pole_indeksu
        _zapisz(katalog, dowod, f"shard-{indeks}.json")
    return katalog


def _wczytaj(sm, katalog: Path, **kw):
    return sm.wczytaj_dowody(katalog, sha=kw.get("sha", SHA), odcisk=kw.get("odcisk", ODCISK))


def _sprawdz(sm, katalog: Path, *, wszystkie=None, zmian=()):
    return sm.sprawdz_dowody_shardow(
        katalog,
        sha=SHA,
        odcisk=ODCISK,
        wszystkie_testy=set(T if wszystkie is None else wszystkie),
        testy_zmian=set(zmian),
    )


def _blad(sm, wywolanie) -> str:
    with pytest.raises(sm.BladDowodu) as wyjatek:
        wywolanie()
    return str(wyjatek.value)


# =================================================================================================
# Podzial testow na shardy
# =================================================================================================

SIEDEM = [f"tests/t.py::test_{i}" for i in range(7)]


@pytest.mark.parametrize(
    ("indeks", "oczekiwane"),
    [
        pytest.param(0, [SIEDEM[0], SIEDEM[3], SIEDEM[6]], id="shard_0"),
        pytest.param(1, [SIEDEM[1], SIEDEM[4]], id="shard_1"),
        pytest.param(2, [SIEDEM[2], SIEDEM[5]], id="shard_2"),
    ],
)
def test_shard_dostaje_co_trzeci_test_z_posortowanej_listy_niezaleznie_od_kolejnosci_wejscia(
    sm, indeks, oczekiwane
):
    wymieszane = [SIEDEM[i] for i in (4, 0, 6, 2, 5, 1, 3)]

    assert sm.podziel_testy(wymieszane, indeks, 3) == oczekiwane


def test_powtorzony_identyfikator_testu_trafia_do_shardu_raz(sm):
    assert sm.podziel_testy(["tests/b.py::t", "tests/a.py::t", "tests/b.py::t"], 0, 1) == [
        "tests/a.py::t",
        "tests/b.py::t",
    ]


@pytest.mark.parametrize("liczba", [1, 2, 3, 5, 7, 9])
def test_shardy_sa_rozlaczne_i_razem_daja_wszystkie_testy(sm, liczba):
    czesci = [sm.podziel_testy(SIEDEM, indeks, liczba) for indeks in range(liczba)]

    sklejone = [t for czesc in czesci for t in czesc]
    assert sorted(sklejone) == SIEDEM


def test_brak_testow_daje_pusty_shard_a_nie_blad(sm):
    assert sm.podziel_testy([], 0, 2) == []


def test_liczba_shardow_zero_to_blad_narzedzia_o_liczbie_a_nie_o_indeksie(sm):
    with pytest.raises(sm.BladNarzedzia) as wyjatek:
        sm.podziel_testy(SIEDEM, 0, 0)

    assert str(wyjatek.value) == "liczba shardow musi byc co najmniej 1, jest 0"


def test_ujemna_liczba_shardow_to_blad_narzedzia(sm):
    with pytest.raises(sm.BladNarzedzia) as wyjatek:
        sm.podziel_testy(SIEDEM, 0, -2)

    assert str(wyjatek.value) == "liczba shardow musi byc co najmniej 1, jest -2"


@pytest.mark.parametrize("indeks", [3, 4])
def test_indeks_rowny_liczbie_shardow_lub_wiekszy_to_blad_narzedzia(sm, indeks):
    with pytest.raises(sm.BladNarzedzia) as wyjatek:
        sm.podziel_testy(SIEDEM, indeks, 3)

    assert str(wyjatek.value) == f"indeks shardu {indeks} poza zakresem 0..2"


def test_ujemny_indeks_shardu_to_blad_narzedzia(sm):
    with pytest.raises(sm.BladNarzedzia) as wyjatek:
        sm.podziel_testy(SIEDEM, -1, 3)

    assert str(wyjatek.value) == "indeks shardu -1 poza zakresem 0..2"


# =================================================================================================
# Odcisk zrodel i konfiguracji
# =================================================================================================

PYPROJECT_ODCISKU = """\
[project]
name = "x"

[tool.mutmut]
source_paths = ["src/pakiet/"]
also_copy = ["alembic.ini", "deploy/"]
"""


def _projekt_odcisku(korzen: Path) -> Path:
    pliki = {
        "pyproject.toml": PYPROJECT_ODCISKU,
        "src/pakiet/a.py": "def f():\n    return 1\n",
        "src/pakiet/sub/b.py": "def g():\n    return 2\n",
        "src/pakiet/sub/c.py": "def h():\n    return 3\n",
        "alembic.ini": "[alembic]\n",
        "deploy/hooks.json": "{}\n",
        "tests/test_a.py": "def test_a():\n    pass\n",
        "README.md": "opis\n",
        "docs/notatka.md": "notatka\n",
    }
    for sciezka, tresc in pliki.items():
        cel = korzen / sciezka
        cel.parent.mkdir(parents=True, exist_ok=True)
        cel.write_text(tresc, encoding="utf-8")
    return korzen


def test_odcisk_zrodel_tego_samego_drzewa_jest_taki_sam_i_niepusty(sm, tmp_path):
    projekt = _projekt_odcisku(tmp_path)

    pierwszy = sm.odcisk_zrodel(projekt)

    assert isinstance(pierwszy, str) and pierwszy
    assert sm.odcisk_zrodel(projekt) == pierwszy


@pytest.mark.parametrize(
    "plik",
    [
        pytest.param("src/pakiet/a.py", id="zrodlo_z_source_paths"),
        pytest.param("src/pakiet/sub/b.py", id="zrodlo_w_podkatalogu_source_paths"),
        pytest.param("tests/test_a.py", id="test"),
        pytest.param("alembic.ini", id="plik_z_also_copy"),
        pytest.param("deploy/hooks.json", id="plik_w_katalogu_z_also_copy"),
    ],
)
def test_zmiana_tresci_pliku_objetego_przez_mutmuta_zmienia_odcisk(sm, tmp_path, plik):
    projekt = _projekt_odcisku(tmp_path)
    przed = sm.odcisk_zrodel(projekt)

    (projekt / plik).write_text("zmieniona tresc\n", encoding="utf-8")

    assert sm.odcisk_zrodel(projekt) != przed


@pytest.mark.parametrize(
    "plik",
    [
        pytest.param("src/pakiet/nowy.py", id="w_zrodlach"),
        pytest.param("tests/test_nowy.py", id="w_testach"),
        pytest.param("deploy/nowy.json", id="w_also_copy"),
    ],
)
def test_nowy_plik_w_objetym_katalogu_zmienia_odcisk(sm, tmp_path, plik):
    projekt = _projekt_odcisku(tmp_path)
    przed = sm.odcisk_zrodel(projekt)

    (projekt / plik).write_text("x = 1\n", encoding="utf-8")

    assert sm.odcisk_zrodel(projekt) != przed


def test_zmiana_nazwy_pliku_przy_tej_samej_tresci_zmienia_odcisk(sm, tmp_path):
    projekt = _projekt_odcisku(tmp_path)
    przed = sm.odcisk_zrodel(projekt)

    (projekt / "src/pakiet/a.py").rename(projekt / "src/pakiet/b.py")

    assert sm.odcisk_zrodel(projekt) != przed


@pytest.mark.parametrize("plik", ["README.md", "docs/notatka.md", "docs/nowy.md"])
def test_zmiana_pliku_poza_zasiegiem_mutmuta_nie_zmienia_odcisku(sm, tmp_path, plik):
    projekt = _projekt_odcisku(tmp_path)
    przed = sm.odcisk_zrodel(projekt)

    (projekt / plik).write_text("inna tresc\n", encoding="utf-8")

    assert sm.odcisk_zrodel(projekt) == przed


@pytest.mark.parametrize(
    "katalog",
    [
        pytest.param("src/pakiet/__pycache__", id="w_zrodlach"),
        pytest.param("tests/__pycache__", id="w_testach"),
    ],
)
def test_skompilowane_pliki_z_pycache_nie_zmieniaja_odcisku(sm, tmp_path, katalog):
    # Shardy i job koncowy uruchamiaja testy, wiec ich `__pycache__` roznia sie; odcisk ma byc taki sam.
    projekt = _projekt_odcisku(tmp_path)
    przed = sm.odcisk_zrodel(projekt)

    (projekt / katalog).mkdir()
    (projekt / katalog / "a.cpython-313.pyc").write_bytes(b"\x00\x01")

    assert sm.odcisk_zrodel(projekt) == przed


def test_czas_modyfikacji_plikow_nie_wplywa_na_odcisk(sm, tmp_path):
    projekt = _projekt_odcisku(tmp_path)
    przed = sm.odcisk_zrodel(projekt)

    os.utime(projekt / "src/pakiet/a.py", (1_000_000_000, 1_000_000_000))

    assert sm.odcisk_zrodel(projekt) == przed


def test_zmiana_konfiguracji_mutmuta_przy_tych_samych_plikach_zmienia_odcisk(sm, tmp_path):
    projekt = _projekt_odcisku(tmp_path)
    przed = sm.odcisk_zrodel(projekt)

    pyproject = projekt / "pyproject.toml"
    pyproject.write_text(PYPROJECT_ODCISKU + 'pytest_add_cli_args = ["-x"]\n', encoding="utf-8")

    assert sm.odcisk_zrodel(projekt) != przed


def test_odcisk_jest_taki_sam_w_procesach_o_roznym_losowaniu_kolejnosci_zbiorow(sm, tmp_path):
    # Shardy i job koncowy to rozne procesy: odcisk nie moze zalezec od kolejnosci przegladania zbioru plikow.
    projekt = _projekt_odcisku(tmp_path)
    kod = (
        "import sys; sys.path.insert(0, sys.argv[1]); import statystyki_mutmuta as s; "
        "from pathlib import Path; print(s.odcisk_zrodel(Path(sys.argv[2])))"
    )

    odciski = {
        uruchom_i_zbierz(
            [sys.executable, "-c", kod, str(KORZEN / "tools"), str(projekt)],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(KORZEN),
            env={**os.environ, "PYTHONHASHSEED": ziarno},
            timeout=60,
        ).stdout.strip()
        for ziarno in ("1", "2", "3", "4")
    }

    assert len(odciski) == 1, odciski


@pytest.mark.skipif(os.geteuid() == 0, reason="root czyta pliki bez wzgledu na prawa")
def test_nieczytelny_plik_zrodlowy_to_blad_narzedzia_z_nazwa_pliku(sm, tmp_path):
    projekt = _projekt_odcisku(tmp_path)
    plik = projekt / "src/pakiet/a.py"
    plik.chmod(0o000)
    try:
        with pytest.raises(sm.BladNarzedzia) as wyjatek:
            sm.odcisk_zrodel(projekt)
    finally:
        plik.chmod(0o644)

    assert str(wyjatek.value).startswith("src/pakiet/a.py: nie da sie odczytac")


def test_uszkodzony_plik_pyproject_to_blad_narzedzia_z_przyczyna_odczytu(sm, tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.mutmut\nsource_paths = ", encoding="utf-8")

    with pytest.raises(sm.BladNarzedzia) as wyjatek:
        sm.odcisk_zrodel(tmp_path)

    assert str(wyjatek.value).startswith(f"{tmp_path / 'pyproject.toml'}: nie da sie odczytac konfiguracji")


def test_brak_pyproject_to_blad_narzedzia_z_przyczyna_odczytu(sm, tmp_path):
    with pytest.raises(sm.BladNarzedzia) as wyjatek:
        sm.odcisk_zrodel(tmp_path)

    assert str(wyjatek.value).startswith(f"{tmp_path / 'pyproject.toml'}: nie da sie odczytac konfiguracji")


def test_pyproject_bez_sekcji_mutmut_to_blad_narzedzia(sm, tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")

    with pytest.raises(sm.BladNarzedzia) as wyjatek:
        sm.odcisk_zrodel(tmp_path)

    assert str(wyjatek.value) == f"{tmp_path / 'pyproject.toml'}: brak sekcji [tool.mutmut]"


@pytest.mark.parametrize(
    "tresc",
    [
        pytest.param("[tool.mutmut]\n", id="brak_source_paths"),
        pytest.param('[tool.mutmut]\nsource_paths = "src/"\n', id="source_paths_to_napis"),
        pytest.param("[tool.mutmut]\nsource_paths = [1]\n", id="source_paths_z_liczba"),
    ],
)
def test_zle_source_paths_to_blad_narzedzia(sm, tmp_path, tresc):
    (tmp_path / "pyproject.toml").write_text(tresc, encoding="utf-8")

    with pytest.raises(sm.BladNarzedzia) as wyjatek:
        sm.odcisk_zrodel(tmp_path)

    assert str(wyjatek.value) == f"{tmp_path / 'pyproject.toml'}: [tool.mutmut] source_paths nie jest lista napisow"


@pytest.mark.parametrize(
    "wartosc", [pytest.param('"alembic.ini"', id="napis"), pytest.param("[1]", id="lista_z_liczba")]
)
def test_zle_also_copy_to_blad_narzedzia(sm, tmp_path, wartosc):
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.mutmut]\nsource_paths = ["src/"]\nalso_copy = {wartosc}\n', encoding="utf-8"
    )

    with pytest.raises(sm.BladNarzedzia) as wyjatek:
        sm.odcisk_zrodel(tmp_path)

    assert str(wyjatek.value) == f"{tmp_path / 'pyproject.toml'}: [tool.mutmut] also_copy nie jest lista napisow"


def test_brak_also_copy_nie_jest_bledem_a_zmiana_testow_nadal_zmienia_odcisk(sm, tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["src/"]\n', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
    przed = sm.odcisk_zrodel(tmp_path)

    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): assert True\n", encoding="utf-8")

    assert sm.odcisk_zrodel(tmp_path) != przed


# =================================================================================================
# Wczytanie dowodow shardow
# =================================================================================================


def test_poprawne_dowody_wszystkich_shardow_sa_zwrocone_wedlug_indeksu(sm, tmp_path):
    katalog = _katalog(tmp_path)

    assert _wczytaj(sm, katalog) == [_dowod(0), _dowod(1), _dowod(2)]


def test_dowody_jedenastu_shardow_sa_zwrocone_wedlug_numeru_a_nie_wedlug_nazwy_pliku(sm, tmp_path):
    # Nazwy "shard-10.json" i "shard-2.json" sortuja sie tekstowo inaczej niz numerycznie.
    katalog = tmp_path / "dowody"
    for indeks in range(11):
        _zapisz(katalog, _dowod(indeks, liczba=11, testy=[f"tests/t.py::test_{indeks}"]))

    dowody = _wczytaj(sm, katalog)

    assert [d["indeks"] for d in dowody] == list(range(11))


def test_stats_time_jako_liczba_calkowita_i_czas_testu_jako_liczba_calkowita_sa_poprawne(sm, tmp_path):
    katalog = _katalog(
        tmp_path,
        nadpisz={0: {"stats_time": 2, "duration_by_test": {T[0]: 1, T[3]: 0}}},
    )

    assert _wczytaj(sm, katalog)[0]["stats_time"] == 2


def test_katalog_dowodow_ktory_nie_istnieje_to_brak_dowodu(sm, tmp_path):
    katalog = tmp_path / "nie-ma"

    assert _blad(sm, lambda: _wczytaj(sm, katalog)) == f"brak dowodu: katalog dowodow {katalog} nie istnieje"


def test_zwykly_plik_zamiast_katalogu_dowodow_to_brak_dowodu(sm, tmp_path):
    plik = tmp_path / "dowody"
    plik.write_text("x", encoding="utf-8")

    assert _blad(sm, lambda: _wczytaj(sm, plik)) == f"brak dowodu: katalog dowodow {plik} nie istnieje"


def test_katalog_bez_plikow_shardow_to_brak_dowodu_a_cudze_pliki_sie_nie_licza(sm, tmp_path):
    katalog = tmp_path / "dowody"
    katalog.mkdir()
    (katalog / "inne.json").write_text("{}", encoding="utf-8")
    (katalog / "README.md").write_text("opis", encoding="utf-8")
    (katalog / "shard-0.txt").write_text("{}", encoding="utf-8")
    (katalog / "shard-0.json.bak").write_text("{}", encoding="utf-8")
    (katalog / "kopia-shard-0.json").write_text("{}", encoding="utf-8")
    (katalog / "shard-0_json").write_text("{}", encoding="utf-8")

    assert (
        _blad(sm, lambda: _wczytaj(sm, katalog))
        == f"brak dowodu: katalog dowodow {katalog} nie zawiera plikow shard-N.json"
    )


@pytest.mark.parametrize("brakujacy", [0, 1, 2])
def test_brak_pliku_jednego_shardu_to_brak_dowodu_z_jego_numerem_takze_gdy_to_ostatni_shard(
    sm, tmp_path, brakujacy
):
    # Liczba shardow pochodzi z pol `liczba_shardow` w plikach, nie z liczby plikow w katalogu.
    katalog = _katalog(tmp_path, bez=(brakujacy,))

    assert _blad(sm, lambda: _wczytaj(sm, katalog)) == f"brak dowodu: brak pliku shardu {brakujacy} z 3"


def test_pliki_zglaszajace_rozne_liczby_shardow_to_brak_dowodu(sm, tmp_path):
    katalog = _katalog(tmp_path, nadpisz={1: {"liczba_shardow": 4}})

    assert (
        _blad(sm, lambda: _wczytaj(sm, katalog))
        == "brak dowodu: pliki shardow zglaszaja rozne liczby shardow: [3, 4]"
    )


def test_indeks_w_pliku_inny_niz_w_nazwie_pliku_to_brak_dowodu(sm, tmp_path):
    katalog = _katalog(tmp_path, nadpisz={1: {"indeks": 0}})

    assert _blad(sm, lambda: _wczytaj(sm, katalog)) == "brak dowodu: shard-1.json: indeks w pliku to 0"


def test_plik_shardu_o_numerze_spoza_zakresu_liczby_shardow_to_brak_dowodu(sm, tmp_path):
    katalog = _katalog(tmp_path)
    dodatkowy = _dowod(0, testy=[T[5]])
    dodatkowy["indeks"] = 3
    _zapisz(katalog, dodatkowy)

    assert _blad(sm, lambda: _wczytaj(sm, katalog)) == "brak dowodu: shard-3.json: indeks 3 poza zakresem 0..2"


@pytest.mark.parametrize(
    "zawartosc",
    [
        pytest.param(b"", id="pusty_plik"),
        pytest.param(b"{to nie jest json", id="uszkodzony_json"),
        pytest.param(b"\xff\xfe\x00", id="bajty_spoza_utf8"),
    ],
)
def test_nieczytelny_plik_dowodu_to_brak_dowodu_z_nazwa_pliku(sm, tmp_path, zawartosc):
    katalog = _katalog(tmp_path, bez=(0,))
    (katalog / "shard-0.json").write_bytes(zawartosc)

    assert _blad(sm, lambda: _wczytaj(sm, katalog)).startswith("brak dowodu: shard-0.json: nie da sie odczytac: ")


def test_katalog_zamiast_pliku_dowodu_to_brak_dowodu_z_nazwa_pliku(sm, tmp_path):
    katalog = _katalog(tmp_path, bez=(0,))
    (katalog / "shard-0.json").mkdir()

    assert _blad(sm, lambda: _wczytaj(sm, katalog)).startswith("brak dowodu: shard-0.json: nie da sie odczytac: ")


@pytest.mark.parametrize(
    "tresc",
    [
        pytest.param("[]", id="lista"),
        pytest.param('"tekst"', id="napis"),
        pytest.param("3", id="liczba"),
        pytest.param("null", id="null"),
    ],
)
def test_dowod_ktory_nie_jest_obiektem_json_to_brak_dowodu(sm, tmp_path, tresc):
    katalog = _katalog(tmp_path, bez=(0,))
    (katalog / "shard-0.json").write_text(tresc, encoding="utf-8")

    assert _blad(sm, lambda: _wczytaj(sm, katalog)) == "brak dowodu: shard-0.json: oczekiwano obiektu JSON"


@pytest.mark.parametrize("pole", POLA_DOWODU)
def test_dowod_bez_pola_to_brak_dowodu_z_nazwa_pola(sm, tmp_path, pole):
    katalog = _katalog(tmp_path, bez=(0,))
    dowod = _dowod(0)
    del dowod[pole]
    _zapisz(katalog, dowod, "shard-0.json")

    assert _blad(sm, lambda: _wczytaj(sm, katalog)) == f"brak dowodu: shard-0.json: brak pola '{pole}'"


@pytest.mark.parametrize(
    ("pole", "wartosc"),
    [
        pytest.param("indeks", "0", id="indeks_napis"),
        pytest.param("indeks", True, id="indeks_bool"),
        pytest.param("liczba_shardow", "3", id="liczba_napis"),
        pytest.param("liczba_shardow", True, id="liczba_bool"),
        pytest.param("sha", 5, id="sha_liczba"),
        pytest.param("odcisk", None, id="odcisk_null"),
        pytest.param("testy", T[0], id="testy_napis"),
        pytest.param("testy", [1], id="testy_z_liczba"),
        pytest.param("kod_pytesta", "0", id="kod_napis"),
        pytest.param("kod_pytesta", False, id="kod_bool_false"),
        pytest.param("kod_pytesta", True, id="kod_bool_true"),
        pytest.param("tests_by_mangled_function_name", [], id="mapa_lista"),
        pytest.param("tests_by_mangled_function_name", {"f": T[0]}, id="mapa_wartosc_napis"),
        pytest.param("tests_by_mangled_function_name", {"f": [1]}, id="mapa_wartosc_z_liczba"),
        pytest.param("duration_by_test", [], id="czasy_lista"),
        pytest.param("duration_by_test", {T[0]: "1"}, id="czas_napis"),
        pytest.param("duration_by_test", {T[0]: True}, id="czas_bool"),
        pytest.param("stats_time", "1", id="stats_time_napis"),
        pytest.param("stats_time", True, id="stats_time_bool"),
        pytest.param("function_hashes", [], id="hashe_lista"),
        pytest.param("function_hashes", {"f": 1}, id="hash_liczba"),
        pytest.param("function_dependencies", [], id="zaleznosci_lista"),
        pytest.param("function_dependencies", {"f": "g"}, id="zaleznosc_napis"),
        pytest.param("function_dependencies", {"f": [1]}, id="zaleznosc_z_liczba"),
    ],
)
def test_pole_dowodu_o_zlym_typie_to_brak_dowodu_z_nazwa_pola(sm, tmp_path, pole, wartosc):
    katalog = _katalog(tmp_path, bez=(0,))
    dowod = _dowod(0)
    dowod[pole] = wartosc
    _zapisz(katalog, dowod, "shard-0.json")

    assert _blad(sm, lambda: _wczytaj(sm, katalog)) == f"brak dowodu: shard-0.json: pole '{pole}' ma zly typ"


@pytest.mark.parametrize(
    "zly_czasy",
    [
        pytest.param({T[0]: 0.5}, id="brak_czasu_jednego_testu"),
        pytest.param({T[0]: 0.5, T[3]: 0.5, T[1]: 0.5}, id="czas_testu_spoza_listy"),
    ],
)
def test_czasy_testow_niezgodne_z_lista_testow_shardu_to_brak_dowodu(sm, tmp_path, zly_czasy):
    katalog = _katalog(tmp_path, nadpisz={0: {"duration_by_test": zly_czasy}})

    assert (
        _blad(sm, lambda: _wczytaj(sm, katalog))
        == "brak dowodu: shard-0.json: duration_by_test nie zgadza sie z lista testow"
    )


def test_mapa_funkcji_wskazujaca_test_spoza_listy_testow_shardu_to_brak_dowodu(sm, tmp_path):
    katalog = _katalog(tmp_path, nadpisz={0: {"tests_by_mangled_function_name": {"pakiet.modul.x_f": [T[1]]}}})

    assert (
        _blad(sm, lambda: _wczytaj(sm, katalog))
        == "brak dowodu: shard-0.json: tests_by_mangled_function_name wskazuje test spoza listy testow shardu"
    )


def test_mapa_funkcji_wskazujaca_podzbior_testow_shardu_jest_poprawna(sm, tmp_path):
    # Test, ktory nie dotyka zadnej mutowanej funkcji, nie wystepuje w mapie - to nie jest brak dowodu.
    katalog = _katalog(tmp_path, nadpisz={0: {"tests_by_mangled_function_name": {"pakiet.modul.x_f": [T[0]]}}})

    assert len(_wczytaj(sm, katalog)) == 3


@pytest.mark.parametrize("zly", [0, 1, 2])
def test_sha_inny_niz_w_jobie_koncowym_to_brak_dowodu_z_nazwa_pliku_shardu(sm, tmp_path, zly):
    katalog = _katalog(tmp_path, nadpisz={zly: {"sha": "cafe"}})

    assert (
        _blad(sm, lambda: _wczytaj(sm, katalog))
        == f"brak dowodu: shard-{zly}.json: sha cafe, a job koncowy ma {SHA}"
    )


@pytest.mark.parametrize("zly", [0, 1, 2])
def test_odcisk_inny_niz_w_jobie_koncowym_to_brak_dowodu_z_nazwa_pliku_shardu(sm, tmp_path, zly):
    katalog = _katalog(tmp_path, nadpisz={zly: {"odcisk": "inny"}})

    assert (
        _blad(sm, lambda: _wczytaj(sm, katalog))
        == f"brak dowodu: shard-{zly}.json: odcisk zrodel inny, a job koncowy ma {ODCISK}"
    )


@pytest.mark.parametrize("kod", [1, 2, 5, -1])
def test_shard_z_niezerowym_kodem_pytesta_to_brak_dowodu_mimo_listy_testow(sm, tmp_path, kod):
    katalog = _katalog(tmp_path, nadpisz={1: {"kod_pytesta": kod}})

    assert _blad(sm, lambda: _wczytaj(sm, katalog)) == f"brak dowodu: shard-1.json: pytest zakonczyl sie kodem {kod}"


def test_shard_bez_zadnego_testu_z_kodem_zero_to_brak_dowodu(sm, tmp_path):
    katalog = _katalog(
        tmp_path,
        nadpisz={1: {"testy": [], "tests_by_mangled_function_name": {}, "duration_by_test": {}}},
    )

    assert _blad(sm, lambda: _wczytaj(sm, katalog)) == "brak dowodu: shard-1.json: shard nie uruchomil zadnego testu"


# =================================================================================================
# Sprawdzenie dowodow wzgledem testow joba koncowego
# =================================================================================================


def test_dowody_pokrywajace_dokladnie_wszystkie_testy_i_testy_zmian_przechodza(sm, tmp_path):
    katalog = _katalog(tmp_path)

    assert _sprawdz(sm, katalog, zmian={T[1], T[4]}) is None


def test_dowody_przy_pustej_liscie_testow_zmian_przechodza(sm, tmp_path):
    # `tests_for_mutant_names` dla wzorcow z gwiazdka bywa puste: wtedy sprawdzenie (d) jest bez przedmiotu.
    assert _sprawdz(sm, _katalog(tmp_path), zmian=()) is None


def test_brak_pliku_shardu_to_brak_dowodu_nawet_gdy_pozostale_shardy_pokrywaja_wszystkie_testy(sm, tmp_path):
    # Shard 0 niesie tez testy shardu 1, wiec suma list jest pelna i bez duplikatow: decyduje tylko brak pliku.
    katalog = _katalog(tmp_path, bez=(1,), testy={0: [T[0], T[3], T[1], T[4]]})

    assert _blad(sm, lambda: _sprawdz(sm, katalog)) == "brak dowodu: brak pliku shardu 1 z 3"


def test_sha_innego_commitu_w_dowodzie_to_brak_dowodu_takze_przy_pelnej_sumie_testow(sm, tmp_path):
    katalog = _katalog(tmp_path, nadpisz={2: {"sha": "cafe"}})

    assert _blad(sm, lambda: _sprawdz(sm, katalog)) == f"brak dowodu: shard-2.json: sha cafe, a job koncowy ma {SHA}"


def test_odcisk_innych_zrodel_w_dowodzie_to_brak_dowodu_takze_przy_pelnej_sumie_testow(sm, tmp_path):
    katalog = _katalog(tmp_path, nadpisz={2: {"odcisk": "inny"}})

    assert (
        _blad(sm, lambda: _sprawdz(sm, katalog))
        == f"brak dowodu: shard-2.json: odcisk zrodel inny, a job koncowy ma {ODCISK}"
    )


def test_niezerowy_kod_pytesta_shardu_to_brak_dowodu_takze_przy_pelnej_sumie_testow(sm, tmp_path):
    katalog = _katalog(tmp_path, nadpisz={0: {"kod_pytesta": 1}})

    assert _blad(sm, lambda: _sprawdz(sm, katalog)) == "brak dowodu: shard-0.json: pytest zakonczyl sie kodem 1"


def test_shard_bez_testow_to_brak_dowodu_nawet_gdy_jego_testy_uruchomil_inny_shard(sm, tmp_path):
    katalog = _katalog(
        tmp_path,
        nadpisz={2: {"tests_by_mangled_function_name": {}}},
        testy={0: [T[0], T[3], T[2], T[5]], 2: []},
    )

    assert _blad(sm, lambda: _sprawdz(sm, katalog)) == "brak dowodu: shard-2.json: shard nie uruchomil zadnego testu"


def test_test_uruchomiony_w_dwoch_shardach_to_brak_dowodu_z_jego_nazwa_a_suma_jest_pelna(sm, tmp_path):
    katalog = _katalog(tmp_path, testy={0: [T[0], T[3], T[2]]})

    assert (
        _blad(sm, lambda: _sprawdz(sm, katalog)) == f"brak dowodu: test uruchomiony w kilku shardach: {T[2]}"
    )


def test_kilka_duplikatow_jest_wypisanych_posortowanych_po_przecinku(sm, tmp_path):
    katalog = _katalog(tmp_path, testy={0: [T[0], T[3], T[2], T[1]]})

    assert (
        _blad(sm, lambda: _sprawdz(sm, katalog))
        == f"brak dowodu: test uruchomiony w kilku shardach: {T[1]}, {T[2]}"
    )


def test_test_powtorzony_w_liscie_jednego_shardu_tez_jest_duplikatem(sm, tmp_path):
    katalog = _katalog(
        tmp_path,
        nadpisz={0: {"duration_by_test": {T[0]: 0.5, T[3]: 0.5}}},
        testy={0: [T[0], T[3], T[0]]},
    )

    assert _blad(sm, lambda: _sprawdz(sm, katalog)) == f"brak dowodu: test uruchomiony w kilku shardach: {T[0]}"


def test_test_z_listy_joba_koncowego_bez_shardu_to_brak_dowodu(sm, tmp_path):
    katalog = _katalog(tmp_path, testy={2: [T[2]]})

    assert (
        _blad(sm, lambda: _sprawdz(sm, katalog))
        == f"brak dowodu: testy z listy joba koncowego bez shardu: {T[5]}"
    )


# Osiem identyfikatorow w kolejnosci niealfabetycznej: kolejnosc iteracji zbioru lacznie trafia w porzadek
# alfabetyczny z prawdopodobienstwem 1/40320, wiec tylko `sorted` daje wypisana liste.
OSIEM_TESTOW = [
    "tests/test_x.py::test_q7",
    "tests/test_x.py::test_c2",
    "tests/test_x.py::test_zm",
    "tests/test_x.py::test_a9",
    "tests/test_x.py::test_k4",
    "tests/test_x.py::test_b1",
    "tests/test_x.py::test_t6",
    "tests/test_x.py::test_f3",
]
OSIEM_POSORTOWANYCH = ", ".join(sorted(OSIEM_TESTOW))


def test_kilka_testow_bez_shardu_jest_wypisanych_posortowanych(sm, tmp_path):
    katalog = _katalog(tmp_path)

    assert (
        _blad(sm, lambda: _sprawdz(sm, katalog, wszystkie=[*T, *OSIEM_TESTOW]))
        == f"brak dowodu: testy z listy joba koncowego bez shardu: {OSIEM_POSORTOWANYCH}"
    )


def test_test_uruchomiony_przez_shard_a_spoza_listy_joba_koncowego_to_brak_dowodu(sm, tmp_path):
    obcy = "tests/test_x.py::test_obcy"
    katalog = _katalog(tmp_path, testy={2: [T[2], T[5], obcy]})

    assert (
        _blad(sm, lambda: _sprawdz(sm, katalog))
        == f"brak dowodu: shardy uruchomily testy spoza listy joba koncowego: {obcy}"
    )


def test_kilka_testow_spoza_listy_joba_koncowego_jest_wypisanych_posortowanych(sm, tmp_path):
    katalog = _katalog(tmp_path, testy={2: [T[2], T[5], *OSIEM_TESTOW]})

    assert (
        _blad(sm, lambda: _sprawdz(sm, katalog))
        == f"brak dowodu: shardy uruchomily testy spoza listy joba koncowego: {OSIEM_POSORTOWANYCH}"
    )


def test_test_zmienionej_funkcji_spoza_sumy_shardow_to_brak_dowodu(sm, tmp_path):
    brakujacy = "tests/test_y.py::test_poza"
    katalog = _katalog(tmp_path)

    assert (
        _blad(sm, lambda: _sprawdz(sm, katalog, zmian={T[1], brakujacy}))
        == f"brak dowodu: testy zmienionych funkcji bez dowodu: {brakujacy}"
    )


def test_kilka_testow_zmian_bez_dowodu_jest_wypisanych_posortowanych(sm, tmp_path):
    katalog = _katalog(tmp_path)

    assert (
        _blad(sm, lambda: _sprawdz(sm, katalog, zmian=OSIEM_TESTOW))
        == f"brak dowodu: testy zmienionych funkcji bez dowodu: {OSIEM_POSORTOWANYCH}"
    )


# =================================================================================================
# Zlozenie statystyk shardow w plik, ktory mutmut przyjmie
# =================================================================================================

KLUCZE_STATYSTYK_MUTMUTA = {
    "tests_by_mangled_function_name",
    "duration_by_test",
    "stats_time",
    "function_hashes",
    "function_dependencies",
    "config_fingerprint",
    "watched_file_hashes",
    "git_commit",
}
FINGERPRINT = {"test_execution": "e1", "test_selection": "e2", "timeout": "e3", "type_check": "e4"}


def _zloz(sm, dowody):
    return sm.zloz_statystyki(dowody, config_fingerprint=FINGERPRINT)


def test_zlozone_statystyki_maja_dokladnie_klucze_pliku_statystyk_mutmuta(sm):
    assert set(_zloz(sm, [_dowod(0), _dowod(1), _dowod(2)])) == KLUCZE_STATYSTYK_MUTMUTA


def test_ta_sama_funkcja_w_dwoch_shardach_ma_testy_z_obu_a_nie_tylko_z_ostatniego(sm):
    wspolna = "pakiet.modul.x_wspolna"
    dowody = [
        _dowod(0, tests_by_mangled_function_name={wspolna: [T[3], T[0]], "pakiet.modul.x_f0": [T[0]]}),
        _dowod(1, tests_by_mangled_function_name={wspolna: [T[1]]}),
        _dowod(2),
    ]

    mapa = _zloz(sm, dowody)["tests_by_mangled_function_name"]

    assert mapa[wspolna] == [T[0], T[1], T[3]]
    assert mapa["pakiet.modul.x_f0"] == [T[0]]
    assert mapa["pakiet.modul.x_f2"] == [T[2], T[5]]


def test_czasy_testow_ze_wszystkich_shardow_dopelniaja_sie_do_wszystkich_testow_z_zachowaniem_wartosci(sm):
    dowody = [
        _dowod(0, duration_by_test={T[0]: 0.25, T[3]: 0.75}),
        _dowod(1),
        _dowod(2, duration_by_test={T[2]: 2.0, T[5]: 3.0}),
    ]

    czasy = _zloz(sm, dowody)["duration_by_test"]

    assert czasy == {T[0]: 0.25, T[3]: 0.75, T[1]: 0.5, T[4]: 0.5, T[2]: 2.0, T[5]: 3.0}


def test_czas_statystyk_to_suma_czasow_shardow(sm):
    dowody = [_dowod(0, stats_time=1.0), _dowod(1, stats_time=2.5), _dowod(2, stats_time=4)]

    assert _zloz(sm, dowody)["stats_time"] == 7.5


def test_hashe_funkcji_ze_wszystkich_shardow_sa_w_jednym_slowniku(sm):
    dowody = [_dowod(0), _dowod(1), _dowod(2, function_hashes={"pakiet.modul.x_f2": "h2", "pakiet.modul.x_inna": "hi"})]

    assert _zloz(sm, dowody)["function_hashes"] == {
        "pakiet.modul.x_f0": "h0",
        "pakiet.modul.x_f1": "h1",
        "pakiet.modul.x_f2": "h2",
        "pakiet.modul.x_inna": "hi",
    }


def test_zaleznosci_funkcji_z_roznych_shardow_sa_sumowane_a_nie_nadpisywane(sm):
    dowody = [
        _dowod(0, function_dependencies={"pakiet.modul.x_f": ["pakiet.modul.x_b"], "pakiet.modul.x_g": ["pakiet.modul.x_a"]}),
        _dowod(1, function_dependencies={"pakiet.modul.x_f": ["pakiet.modul.x_a"]}),
        _dowod(2),
    ]

    assert _zloz(sm, dowody)["function_dependencies"] == {
        "pakiet.modul.x_f": ["pakiet.modul.x_a", "pakiet.modul.x_b"],
        "pakiet.modul.x_g": ["pakiet.modul.x_a"],
    }


def test_odcisk_konfiguracji_w_statystykach_pochodzi_z_argumentu_a_nie_z_dowodow(sm):
    dowody = [_dowod(0, config_fingerprint={"test_selection": "z-dowodu"}), _dowod(1), _dowod(2)]

    assert _zloz(sm, dowody)["config_fingerprint"] == FINGERPRINT


def test_statystyki_zapamietuja_commit_dowodow_i_pusta_liste_obserwowanych_plikow(sm):
    zlozone = _zloz(sm, [_dowod(0), _dowod(1), _dowod(2)])

    assert zlozone["git_commit"] == SHA
    assert zlozone["watched_file_hashes"] == {}


# =================================================================================================
# Podmiana clean tests mutmuta na dowod z shardow (atrapa runnera)
# =================================================================================================


class AtrapaRunnera:
    """Tyle runnera mutmuta, ile widzi podmiana: `list_all_tests` i `run_clean_tests(tests=...)`."""

    def __init__(self, ids):
        self.ids = set(ids)
        self.clean_tests_wywolania: list[set[str]] = []
        self.list_all_tests_wywolania = 0

    def list_all_tests(self):
        self.list_all_tests_wywolania += 1
        return SimpleNamespace(ids=set(self.ids))

    def run_clean_tests(self, tests):
        self.clean_tests_wywolania.append(set(tests))
        return 7


def test_po_podmianie_z_kompletnymi_dowodami_clean_tests_zwracaja_zero_bez_uruchamiania_testow(sm, tmp_path):
    runner = AtrapaRunnera(T)
    sm.podmien_clean_tests(runner, katalog=_katalog(tmp_path), sha=SHA, odcisk=ODCISK)

    assert runner.run_clean_tests(tests={T[1], T[4]}) == 0
    assert runner.clean_tests_wywolania == []


def test_podmienione_clean_tests_bez_dowodow_rzucaja_brak_dowodu_i_nie_uruchamiaja_testow(sm, tmp_path):
    runner = AtrapaRunnera(T)
    pusty = tmp_path / "pusty"
    pusty.mkdir()
    sm.podmien_clean_tests(runner, katalog=pusty, sha=SHA, odcisk=ODCISK)

    assert _blad(sm, lambda: runner.run_clean_tests(tests=set())) == (
        f"brak dowodu: katalog dowodow {pusty} nie zawiera plikow shard-N.json"
    )
    assert runner.clean_tests_wywolania == []


def test_podmienione_clean_tests_sprawdzaja_testy_zmienionych_funkcji_przekazane_przez_mutmuta(sm, tmp_path):
    runner = AtrapaRunnera(T)
    sm.podmien_clean_tests(runner, katalog=_katalog(tmp_path), sha=SHA, odcisk=ODCISK)

    assert _blad(sm, lambda: runner.run_clean_tests(tests={"tests/test_y.py::test_poza"})) == (
        "brak dowodu: testy zmienionych funkcji bez dowodu: tests/test_y.py::test_poza"
    )
    assert runner.clean_tests_wywolania == []


def test_podmienione_clean_tests_biora_liste_wszystkich_testow_z_runnera_a_nie_z_dowodow(sm, tmp_path):
    runner = AtrapaRunnera([t for t in T if t != T[5]])
    sm.podmien_clean_tests(runner, katalog=_katalog(tmp_path), sha=SHA, odcisk=ODCISK)

    assert _blad(sm, lambda: runner.run_clean_tests(tests=set())) == (
        f"brak dowodu: shardy uruchomily testy spoza listy joba koncowego: {T[5]}"
    )


def test_podmienione_clean_tests_porownuja_sha_i_odcisk_przekazane_przy_podmianie(sm, tmp_path):
    runner = AtrapaRunnera(T)
    sm.podmien_clean_tests(runner, katalog=_katalog(tmp_path), sha="f" * 40, odcisk=ODCISK)

    assert _blad(sm, lambda: runner.run_clean_tests(tests=set())) == (
        f"brak dowodu: shard-0.json: sha {SHA}, a job koncowy ma {'f' * 40}"
    )


def test_podmiana_nie_wola_clean_tests_przy_podmianie_tylko_przy_wywolaniu(sm, tmp_path):
    # Dowody sprawdza dopiero wywolanie clean tests (po zebraniu statystyk i liscie testow joba koncowego).
    runner = AtrapaRunnera(T)
    pusty = tmp_path / "pusty"
    pusty.mkdir()

    sm.podmien_clean_tests(runner, katalog=pusty, sha=SHA, odcisk=ODCISK)

    assert runner.clean_tests_wywolania == []


@pytest.mark.parametrize(
    "najpierw_clean_tests",
    [pytest.param(False, id="najpierw_lista_testow"), pytest.param(True, id="najpierw_clean_tests")],
)
def test_lista_wszystkich_testow_jest_zbierana_raz_a_kolejne_wolania_dostaja_ten_sam_wynik(
    sm, tmp_path, najpierw_clean_tests
):
    # Druga zbiorka pytesta w jednym procesie escapuje backslashe w node id drugi raz, wiec lista z pierwszej
    # zbiorki (mutmut w collect_or_load_stats) ma byc oddawana takze clean tests i kazdemu kolejnemu wolaniu.
    runner = AtrapaRunnera(T)
    sm.podmien_clean_tests(runner, katalog=_katalog(tmp_path), sha=SHA, odcisk=ODCISK)

    if najpierw_clean_tests:
        runner.run_clean_tests(tests={T[1]})
        pierwsza = runner.list_all_tests()
    else:
        pierwsza = runner.list_all_tests()
        runner.run_clean_tests(tests={T[1]})
    druga = runner.list_all_tests()

    assert druga is pierwsza
    assert pierwsza.ids == set(T)
    assert runner.list_all_tests_wywolania == 1


# =================================================================================================
# CLI: argumenty (w procesie)
# =================================================================================================


def test_shard_z_indeksem_poza_zakresem_konczy_sie_kodem_2_z_komunikatem_i_bez_pliku_dowodu(
    sm, tmp_path, capsys
):
    katalog = tmp_path / "dowody"

    kod = sm.main(["shard", "--indeks", "5", "--liczba", "3", "--katalog-dowodow", str(katalog)])

    assert kod == 2
    assert "blad: indeks shardu 5 poza zakresem 0..2" in capsys.readouterr().err.splitlines()
    assert not katalog.exists()


def test_shard_z_zerowa_liczba_shardow_konczy_sie_kodem_2_z_komunikatem(sm, tmp_path, capsys):
    kod = sm.main(["shard", "--indeks", "0", "--liczba", "0", "--katalog-dowodow", str(tmp_path / "d")])

    assert kod == 2
    assert "blad: liczba shardow musi byc co najmniej 1, jest 0" in capsys.readouterr().err.splitlines()


# =================================================================================================
# CLI na miniaturowym projekcie z prawdziwym mutmutem i gitem
# =================================================================================================

ZMIENNE_MUTMUTA = ("MUTANT_UNDER_TEST", "PY_IGNORE_IMPORTMISMATCH", "MUTMUT_DEPENDENCY_DEPTH")
PYPROJECT_MINI = """\
[tool.mutmut]
source_paths = ["src/pakiet/"]
also_copy = ["src/pakiet/__init__.py"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
"""
PYPROJECT_MINI_Z_KONFIGURACJA = """\
[tool.mutmut]
source_paths = ["src/pakiet/"]
also_copy = ["src/pakiet/__init__.py"]
pytest_add_cli_args_test_selection = ["tests"]
timeout_multiplier = 7.0

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
"""
MODUL_MINI = "def dodaj(a, b):\n    return a + b\n\n\ndef odejmij(a, b):\n    return a - b\n"
# Test dopisuje do log.txt (katalog biezacy: `mutants/`) wartosc MUTANT_UNDER_TEST: "stats" przy zbieraniu
# statystyk, "" przy clean tests, "fail" przy wymuszonym bledzie, nazwa mutanta przy mutacji.
TEST_A_MINI = """\
import os

from pakiet.liczby import dodaj


def test_dodaj():
    with open("log.txt", "a") as plik:
        plik.write(repr(os.environ.get("MUTANT_UNDER_TEST")) + "\\n")
    assert dodaj(1, 2) == 3
"""
TEST_B_MINI = TEST_A_MINI.replace("dodaj", "odejmij").replace("== 3", "== 99").replace("(1, 2)", "(3, 1)")
TEST_B_MINI_ZIELONY = TEST_B_MINI.replace("== 99", "== 2")
TEST_A = "tests/test_a.py::test_dodaj"
TEST_B = "tests/test_b.py::test_odejmij"
WZORCE_MINI = ["pakiet.liczby.x_dodaj__mutmut_*", "pakiet.liczby.x_odejmij__mutmut_*"]
KLUCZE_DOWODU_SHARDU = set(POLA_DOWODU)

KOD_STANU_MUTMUTA = """
import json
import sys

from mutmut.configuration import config
from mutmut.runners.harness import ListAllTestsResult
from mutmut.state import state
from mutmut.stats import load_stats

wszystkie = set(json.loads(sys.argv[1]))
zaladowano = load_stats()
print(json.dumps({
    "zaladowano": zaladowano,
    "nowe": sorted(ListAllTestsResult(ids=wszystkie).new_tests()),
    "stary": state().old_config_fingerprint,
    "obecny": config().config_fingerprint(),
    "mapa": {k: sorted(v) for k, v in state().tests_by_mangled_function_name.items()},
}))
"""
KOD_KLUCZY_MUTMUTA = """
import json
import os

from mutmut.stats import save_stats

os.makedirs("mutants", exist_ok=True)
save_stats()
print(json.dumps(sorted(json.load(open("mutants/mutmut-stats.json")))))
"""


def _srodowisko(projekt: Path) -> dict[str, str]:
    """Srodowisko dziecka: bez zmiennych mutmuta i pytesta zewnetrznego przebiegu, git deterministyczny."""
    env = {k: v for k, v in os.environ.items() if k not in ZMIENNE_MUTMUTA and not k.startswith("PYTEST_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CEILING_DIRECTORIES": str(projekt.parent),
            "GIT_AUTHOR_NAME": "LukBro",
            "GIT_AUTHOR_EMAIL": "lukaszbrowar@gmail.com",
            "GIT_COMMITTER_NAME": "LukBro",
            "GIT_COMMITTER_EMAIL": "lukaszbrowar@gmail.com",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
        }
    )
    return env


def _git(projekt: Path, *argv: str) -> str:
    wynik = uruchom_i_zbierz(
        ["git", *argv], capture_output=True, text=True, check=True, cwd=str(projekt), env=_srodowisko(projekt), timeout=60
    )
    return wynik.stdout.strip()


def _projekt(
    katalog: Path,
    *,
    pyproject: str = PYPROJECT_MINI,
    test_b: str = TEST_B_MINI_ZIELONY,
    git: bool = True,
    modul: str = MODUL_MINI,
) -> Path:
    pliki = {
        "pyproject.toml": pyproject,
        "src/pakiet/__init__.py": "",
        "src/pakiet/liczby.py": modul,
        "tests/test_a.py": TEST_A_MINI,
        "tests/test_b.py": test_b,
    }
    for sciezka, tresc in pliki.items():
        cel = katalog / sciezka
        cel.parent.mkdir(parents=True, exist_ok=True)
        cel.write_text(tresc, encoding="utf-8")
    if git:
        _git(katalog, "init", "-q")
        _git(katalog, "add", "-A")
        _git(katalog, "commit", "-q", "-m", "miniatura")
    return katalog


def _skrypt(projekt: Path, *argv: str, timeout: float = 180):
    """Prawdziwy skrypt z cwd = korzen repozytorium; projekt miniaturowy dostaje `--korzen`."""
    return uruchom_i_zbierz(
        [sys.executable, str(SKRYPT), *argv],
        capture_output=True,
        text=True,
        cwd=str(KORZEN),
        env=_srodowisko(projekt),
        timeout=timeout,
    )


def _shard(projekt: Path, indeks: int, liczba: int, katalog: Path):
    return _skrypt(
        projekt,
        "shard",
        "--korzen",
        str(projekt),
        "--indeks",
        str(indeks),
        "--liczba",
        str(liczba),
        "--katalog-dowodow",
        str(katalog),
    )


def _scal(projekt: Path, katalog: Path):
    return _skrypt(projekt, "scal", "--korzen", str(projekt), "--katalog-dowodow", str(katalog))


def _mutmut_run(projekt: Path, katalog: Path):
    return _skrypt(
        projekt, "mutmut-run", "--korzen", str(projekt), "--katalog-dowodow", str(katalog), *WZORCE_MINI
    )


def _wczytaj_dowod(katalog: Path, indeks: int) -> dict:
    return json.loads((katalog / f"shard-{indeks}.json").read_text(encoding="utf-8"))


def _linie_bledu(wynik) -> list[str]:
    return wynik.stderr.splitlines()


def _kopia(projekt: Path, tmp_path: Path) -> Path:
    cel = tmp_path / "kopia"
    shutil.copytree(projekt, cel, symlinks=True)
    return cel


def _log_testow(projekt: Path) -> list[str]:
    plik = projekt / "mutants" / "log.txt"
    return plik.read_text(encoding="utf-8").splitlines() if plik.exists() else []


def _w_projekcie(projekt: Path, kod: str, *argv: str) -> dict | list:
    wynik = uruchom_i_zbierz(
        [sys.executable, "-c", kod, *argv],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(projekt),
        env=_srodowisko(projekt),
        timeout=60,
    )
    return json.loads(wynik.stdout)


def _dowody_reczne(projekt: Path, katalog: Path, sm) -> None:
    """Dowody dwoch shardow zlozone recznie dla biezacego commitu i drzewa miniatury."""
    sha = _git(projekt, "rev-parse", "HEAD")
    odcisk = sm.odcisk_zrodel(projekt)
    for indeks, test, funkcja in [(0, TEST_A, "pakiet.liczby.x_dodaj"), (1, TEST_B, "pakiet.liczby.x_odejmij")]:
        _zapisz(
            katalog,
            {
                "indeks": indeks,
                "liczba_shardow": 2,
                "sha": sha,
                "odcisk": odcisk,
                "testy": [test],
                "kod_pytesta": 0,
                "tests_by_mangled_function_name": {funkcja: [test]},
                "duration_by_test": {test: 0.01},
                "stats_time": 0.5,
                "function_hashes": {"pakiet.liczby.x_dodaj": "h1", "pakiet.liczby.x_odejmij": "h2"},
                "function_dependencies": {},
            },
        )


@pytest.fixture(scope="module")
def projekt_po_shardach(tmp_path_factory):
    """Miniatura po dwoch prawdziwych shardach (`shard 0/2` i `shard 1/2`), dowody w `dowody/`."""
    projekt = _projekt(tmp_path_factory.mktemp("po-shardach") / "projekt")
    for indeks in range(2):
        wynik = _shard(projekt, indeks, 2, projekt / "dowody")
        assert wynik.returncode == 0, wynik.stderr
    return projekt


@pytest.mark.parametrize(
    ("indeks", "test", "funkcja"),
    [
        pytest.param(0, TEST_A, "pakiet.liczby.x_dodaj", id="shard_0"),
        pytest.param(1, TEST_B, "pakiet.liczby.x_odejmij", id="shard_1"),
    ],
)
def test_shard_zapisuje_dowod_z_testami_swojego_podzbioru_i_mapa_funkcji_tylko_tych_testow(
    sm, projekt_po_shardach, indeks, test, funkcja
):
    dowod = _wczytaj_dowod(projekt_po_shardach / "dowody", indeks)

    assert set(dowod) == KLUCZE_DOWODU_SHARDU
    assert dowod["indeks"] == indeks
    assert dowod["liczba_shardow"] == 2
    assert dowod["testy"] == [test]
    assert dowod["kod_pytesta"] == 0
    assert dowod["tests_by_mangled_function_name"] == {funkcja: [test]}
    assert set(dowod["duration_by_test"]) == {test}
    assert set(dowod["function_hashes"]) == {"pakiet.liczby.x_dodaj", "pakiet.liczby.x_odejmij"}


def test_dowod_shardu_niesie_sha_commita_i_odcisk_zrodel_miniatury(sm, projekt_po_shardach):
    dowod = _wczytaj_dowod(projekt_po_shardach / "dowody", 0)

    assert dowod["sha"] == _git(projekt_po_shardach, "rev-parse", "HEAD")
    assert dowod["odcisk"] == sm.odcisk_zrodel(projekt_po_shardach)


def test_shard_liczy_statystyki_od_zera_i_nie_wczytuje_starego_pliku_statystyk(sm, tmp_path):
    projekt = _projekt(tmp_path / "projekt")
    stare = {
        "tests_by_mangled_function_name": {"pakiet.liczby.x_odejmij": ["tests/test_zatruty.py::test_zatruty"]},
        "duration_by_test": {"tests/test_zatruty.py::test_zatruty": 1.0},
        "stats_time": 9.0,
        "function_hashes": {},
        "function_dependencies": {},
        "config_fingerprint": {},
        "watched_file_hashes": {},
        "git_commit": None,
    }
    (projekt / "mutants").mkdir()
    (projekt / "mutants" / "mutmut-stats.json").write_text(json.dumps(stare), encoding="utf-8")

    wynik = _shard(projekt, 0, 2, projekt / "dowody")

    assert wynik.returncode == 0, wynik.stderr
    dowod = _wczytaj_dowod(projekt / "dowody", 0)
    assert dowod["tests_by_mangled_function_name"] == {"pakiet.liczby.x_dodaj": [TEST_A]}
    assert dowod["testy"] == [TEST_A]
    assert "tests/test_zatruty.py::test_zatruty" not in dowod["duration_by_test"]


def test_shard_bez_zadnego_testu_konczy_sie_kodem_2_i_nie_zapisuje_dowodu(sm, tmp_path):
    projekt = _projekt(tmp_path / "projekt")
    katalog = projekt / "dowody"

    wynik = _shard(projekt, 2, 3, katalog)

    assert wynik.returncode == 2, wynik.stderr
    assert "blad: brak dowodu: shard 2 z 3 nie ma zadnego testu" in _linie_bledu(wynik)
    assert not (katalog / "shard-2.json").exists()


def test_shard_z_oblanym_testem_konczy_sie_kodem_2_i_nie_zapisuje_dowodu(sm, tmp_path):
    projekt = _projekt(tmp_path / "projekt", test_b=TEST_B_MINI)
    katalog = projekt / "dowody"

    wynik = _shard(projekt, 0, 1, katalog)

    assert wynik.returncode == 2, wynik.stderr
    assert "blad: brak dowodu: pytest zakonczyl sie kodem 1" in _linie_bledu(wynik)
    assert not (katalog / "shard-0.json").exists()


def test_shard_poza_repozytorium_git_konczy_sie_kodem_2_bez_dowodu(sm, tmp_path):
    projekt = _projekt(tmp_path / "projekt", git=False)
    katalog = projekt / "dowody"

    wynik = _shard(projekt, 0, 2, katalog)

    assert wynik.returncode == 2, wynik.stderr
    assert any(
        linia.startswith("blad: brak dowodu: git rev-parse HEAD nie powiodl sie") for linia in _linie_bledu(wynik)
    ), wynik.stderr
    assert not (katalog / "shard-0.json").exists()


def test_shard_bez_binarki_git_konczy_sie_kodem_2_z_brakiem_dowodu_a_nie_tracebackiem(sm, tmp_path):
    projekt = _projekt(tmp_path / "projekt")
    pusty_path = tmp_path / "pusty-path"
    pusty_path.mkdir()
    srodowisko = {**_srodowisko(projekt), "PATH": str(pusty_path)}

    wynik = uruchom_i_zbierz(
        [
            sys.executable,
            str(SKRYPT),
            "shard",
            "--korzen",
            str(projekt),
            "--indeks",
            "0",
            "--liczba",
            "2",
            "--katalog-dowodow",
            str(projekt / "dowody"),
        ],
        capture_output=True,
        text=True,
        cwd=str(KORZEN),
        env=srodowisko,
        timeout=60,
    )

    assert wynik.returncode == 2, wynik.stderr
    assert any(
        linia.startswith("blad: brak dowodu: git rev-parse HEAD nie powiodl sie") for linia in _linie_bledu(wynik)
    ), wynik.stderr
    assert "Traceback" not in wynik.stderr


def test_scalone_statystyki_prawdziwych_shardow_przyjmuje_load_stats_mutmuta_bez_nowych_testow(
    sm, projekt_po_shardach, tmp_path
):
    kopia = _kopia(projekt_po_shardach, tmp_path)

    wynik = _scal(kopia, kopia / "dowody")

    assert wynik.returncode == 0, wynik.stderr
    stan = _w_projekcie(kopia, KOD_STANU_MUTMUTA, json.dumps([TEST_A, TEST_B]))
    assert stan["zaladowano"] is True
    assert stan["nowe"] == []
    assert stan["stary"] == stan["obecny"]
    assert stan["mapa"] == {"pakiet.liczby.x_dodaj": [TEST_A], "pakiet.liczby.x_odejmij": [TEST_B]}


def test_scalone_statystyki_maja_te_same_klucze_co_plik_zapisany_przez_mutmuta(sm, projekt_po_shardach, tmp_path):
    (tmp_path / "scalona").mkdir()
    scalona = _kopia(projekt_po_shardach, tmp_path / "scalona")
    assert _scal(scalona, scalona / "dowody").returncode == 0
    wzorcowa = _projekt(tmp_path / "wzorcowa")

    klucze_mutmuta = _w_projekcie(wzorcowa, KOD_KLUCZY_MUTMUTA)

    klucze_scalone = sorted(json.loads((scalona / "mutants" / "mutmut-stats.json").read_text(encoding="utf-8")))
    assert klucze_scalone == klucze_mutmuta


def test_scalone_statystyki_niosa_odcisk_konfiguracji_z_niezmodyfikowanego_pyproject_a_nie_staly(sm, tmp_path):
    projekt = _projekt(tmp_path / "projekt", pyproject=PYPROJECT_MINI_Z_KONFIGURACJA)
    _dowody_reczne(projekt, projekt / "dowody", sm)

    wynik = _scal(projekt, projekt / "dowody")

    assert wynik.returncode == 0, wynik.stderr
    stan = _w_projekcie(projekt, KOD_STANU_MUTMUTA, json.dumps([TEST_A, TEST_B]))
    assert stan["zaladowano"] is True
    assert stan["stary"] == stan["obecny"]
    assert stan["nowe"] == []


def test_scalenie_bez_pliku_jednego_shardu_konczy_sie_kodem_2_i_nie_tworzy_pliku_statystyk(sm, tmp_path):
    projekt = _projekt(tmp_path / "projekt")
    _dowody_reczne(projekt, tmp_path / "pelne", sm)
    czesc = tmp_path / "czesc"
    czesc.mkdir()
    shutil.copy(tmp_path / "pelne" / "shard-0.json", czesc / "shard-0.json")

    wynik = _scal(projekt, czesc)

    assert wynik.returncode == 2, wynik.stderr
    assert "blad: brak dowodu: brak pliku shardu 1 z 2" in _linie_bledu(wynik)
    assert not (projekt / "mutants" / "mutmut-stats.json").exists()


def test_scalenie_dowodow_z_innego_commitu_niz_biezacy_konczy_sie_kodem_2_bez_pliku_statystyk(sm, tmp_path):
    projekt = _projekt(tmp_path / "projekt")
    katalog = tmp_path / "dowody"
    _dowody_reczne(projekt, katalog, sm)
    dowod = _wczytaj_dowod(katalog, 0)
    dowod["sha"] = "0" * 40
    _zapisz(katalog, dowod)

    wynik = _scal(projekt, katalog)

    assert wynik.returncode == 2, wynik.stderr
    assert f"blad: brak dowodu: shard-0.json: sha {'0' * 40}, a job koncowy ma {_git(projekt, 'rev-parse', 'HEAD')}" in (
        _linie_bledu(wynik)
    )
    assert not (projekt / "mutants" / "mutmut-stats.json").exists()


def test_scalenie_dowodow_z_innych_zrodel_niz_biezace_konczy_sie_kodem_2_bez_pliku_statystyk(sm, tmp_path):
    projekt = _projekt(tmp_path / "projekt")
    katalog = tmp_path / "dowody"
    _dowody_reczne(projekt, katalog, sm)
    dowod = _wczytaj_dowod(katalog, 1)
    dowod["odcisk"] = "inny"
    _zapisz(katalog, dowod)

    wynik = _scal(projekt, katalog)

    assert wynik.returncode == 2, wynik.stderr
    assert f"blad: brak dowodu: shard-1.json: odcisk zrodel inny, a job koncowy ma {sm.odcisk_zrodel(projekt)}" in (
        _linie_bledu(wynik)
    )
    assert not (projekt / "mutants" / "mutmut-stats.json").exists()


def test_mutmut_z_dowodami_nie_uruchamia_clean_tests_ani_nie_zbiera_statystyk_a_mutanty_biegna(
    sm, projekt_po_shardach, tmp_path
):
    kopia = _kopia(projekt_po_shardach, tmp_path)
    assert _scal(kopia, kopia / "dowody").returncode == 0
    przed = _log_testow(kopia)

    wynik = _mutmut_run(kopia, kopia / "dowody")

    assert wynik.returncode == 0, wynik.stderr
    nowe = _log_testow(kopia)[len(przed) :]
    assert "''" not in nowe, nowe
    assert "'stats'" not in nowe, nowe
    assert {"'pakiet.liczby.x_dodaj__mutmut_1'", "'pakiet.liczby.x_odejmij__mutmut_1'"} <= set(nowe), nowe


def test_zwykly_mutmut_po_scaleniu_uruchamia_clean_tests_bez_zbierania_statystyk(sm, projekt_po_shardach, tmp_path):
    # Kontrola: bez opakowania te same statystyki daja clean tests (wpisy ''), wiec wpisy w poprzednim tescie
    # znikaja przez podmiane, a nie przez to, ze znacznik w logu nie dziala.
    kopia = _kopia(projekt_po_shardach, tmp_path)
    assert _scal(kopia, kopia / "dowody").returncode == 0
    przed = _log_testow(kopia)

    wynik = uruchom_i_zbierz(
        [sys.executable, "-m", "mutmut", "run", *WZORCE_MINI],
        capture_output=True,
        text=True,
        cwd=str(kopia),
        env=_srodowisko(kopia),
        timeout=180,
    )

    assert wynik.returncode == 0, wynik.stdout + wynik.stderr
    nowe = _log_testow(kopia)[len(przed) :]
    assert nowe.count("''") == 2, nowe
    assert "'stats'" not in nowe, nowe


def test_mutmut_z_niepelnymi_dowodami_konczy_sie_kodem_2_z_brakiem_dowodu_i_nie_uruchamia_testow(
    sm, projekt_po_shardach, tmp_path
):
    kopia = _kopia(projekt_po_shardach, tmp_path)
    assert _scal(kopia, kopia / "dowody").returncode == 0
    czesc = tmp_path / "czesc"
    czesc.mkdir()
    shutil.copy(kopia / "dowody" / "shard-0.json", czesc / "shard-0.json")
    przed = _log_testow(kopia)

    wynik = _mutmut_run(kopia, czesc)

    assert wynik.returncode == 2, wynik.stdout + wynik.stderr
    assert "blad: brak dowodu: brak pliku shardu 1 z 2" in _linie_bledu(wynik)
    assert _log_testow(kopia)[len(przed) :] == []


# =================================================================================================
# Poprawka HAR-255: granica bledow, niekanoniczne nazwy dowodow, kolejnosc, przywracanie stanu
# =================================================================================================

NIECZYTELNY_KATALOG_SKIP = pytest.mark.skipif(os.geteuid() == 0, reason="root czyta pliki bez wzgledu na prawa")
FUNKCJE_OSIEM = [f"pakiet.modul.x_{nazwa}" for nazwa in ("q7", "c2", "zm", "a9", "k4", "b1", "t6", "f3")]
MODUL_Z_ZALEZNOSCIA = (
    "def pomocnicza(a):\n    return a * 1\n\n\n"
    "def dodaj(a, b):\n    return pomocnicza(a) + b\n\n\n"
    "def odejmij(a, b):\n    return a - b\n"
)
PYPROJECT_MINI_ZLA_IZOLACJA = PYPROJECT_MINI.replace(
    "\n\n[tool.pytest", '\nprocess_isolation = "zle"\n\n[tool.pytest', 1
)
KOD_WYWOLANIA = """
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
import statystyki_mutmuta as s

try:
    eval(sys.argv[2])
except s.BladNarzedzia as blad:
    print("WYNIK " + json.dumps(str(blad)))
else:
    print("WYNIK null")
"""
KOD_MUTMUT_RUN_W_PROCESIE = """
import json
import os
import sys

sys.path.insert(0, sys.argv[1])
import statystyki_mutmuta as s
from mutmut import __main__ as m

runner_przed = m.get_mutant_runner
cwd_przed = os.getcwd()
kod = s.main(["mutmut-run", "--korzen", sys.argv[2], "--katalog-dowodow", sys.argv[3], sys.argv[4]])
print("WYNIK " + json.dumps({"kod": kod, "runner": m.get_mutant_runner is runner_przed, "cwd": os.getcwd() == cwd_przed}))
"""


def _blad_wywolania(projekt: Path, wyrazenie: str) -> str | None:
    """Komunikat `BladNarzedzia` z wyrazenia wykonanego w osobnym procesie z cwd = projekt (stan mutmuta nie wycieka)."""
    wynik = uruchom_i_zbierz(
        [sys.executable, "-c", KOD_WYWOLANIA, str(KORZEN / "tools"), wyrazenie],
        capture_output=True,
        text=True,
        cwd=str(projekt),
        env=_srodowisko(projekt),
        timeout=120,
    )
    linie = [linia for linia in wynik.stdout.splitlines() if linia.startswith("WYNIK ")]
    assert linie, wynik.stdout + wynik.stderr
    return json.loads(linie[-1][len("WYNIK ") :])


def _zapisz_pyproject(korzen: Path, tresc: str) -> Path:
    plik = korzen / "pyproject.toml"
    plik.write_text(tresc, encoding="utf-8")
    return plik


# --- odcisk: granica bledow ----------------------------------------------------------------------


def test_pyproject_z_bajtami_spoza_utf8_to_blad_narzedzia_z_przyczyna_odczytu(sm, tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_bytes(b"\xff\xfe[tool.mutmut]\n")

    with pytest.raises(sm.BladNarzedzia) as wyjatek:
        sm.odcisk_zrodel(tmp_path)

    assert str(wyjatek.value).startswith(f"{pyproject}: nie da sie odczytac konfiguracji")


def test_dowod_z_gleboko_zagniezdzonym_json_to_brak_dowodu_a_nie_wyjatek_rekurencji(sm, tmp_path):
    katalog = _katalog(tmp_path, bez=(0,))
    (katalog / "shard-0.json").write_text("[" * 100000, encoding="utf-8")

    assert _blad(sm, lambda: _wczytaj(sm, katalog)).startswith("brak dowodu: shard-0.json: nie da sie odczytac: ")


def test_sekcja_tool_niebedaca_tabela_to_brak_sekcji_mutmut(sm, tmp_path):
    pyproject = _zapisz_pyproject(tmp_path, 'tool = "x"\n')

    with pytest.raises(sm.BladNarzedzia) as wyjatek:
        sm.odcisk_zrodel(tmp_path)

    assert str(wyjatek.value) == f"{pyproject}: brak sekcji [tool.mutmut]"


def test_data_w_sekcji_mutmut_nie_psuje_odcisku(sm, tmp_path):
    projekt = _projekt_odcisku(tmp_path)
    pyproject = projekt / "pyproject.toml"
    pyproject.write_text(PYPROJECT_ODCISKU + "zaklad = 2026-01-01\n", encoding="utf-8")

    odcisk = sm.odcisk_zrodel(projekt)

    assert isinstance(odcisk, str) and odcisk
    assert sm.odcisk_zrodel(projekt) == odcisk


def test_zmiana_sekcji_pytest_w_pyproject_zmienia_odcisk_przy_tych_samych_plikach(sm, tmp_path):
    projekt = _projekt_odcisku(tmp_path)
    pyproject = projekt / "pyproject.toml"
    pyproject.write_text(PYPROJECT_ODCISKU + '\n[tool.pytest.ini_options]\naddopts = "-q"\n', encoding="utf-8")
    przed = sm.odcisk_zrodel(projekt)

    pyproject.write_text(PYPROJECT_ODCISKU + '\n[tool.pytest.ini_options]\naddopts = "-x"\n', encoding="utf-8")

    assert sm.odcisk_zrodel(projekt) != przed


def test_pusty_source_paths_w_katalogu_bez_zrodel_to_blad_narzedzia_z_konfiguracji_mutmuta(sm, tmp_path):
    _zapisz_pyproject(tmp_path, "[tool.mutmut]\nsource_paths = []\n")

    with pytest.raises(sm.BladNarzedzia) as wyjatek:
        sm.odcisk_zrodel(tmp_path)

    assert str(wyjatek.value).startswith("konfiguracja mutmuta: ")


def test_zla_wartosc_process_isolation_to_blad_narzedzia_z_konfiguracji_mutmuta(sm, tmp_path):
    (tmp_path / "src").mkdir()
    _zapisz_pyproject(tmp_path, '[tool.mutmut]\nsource_paths = ["src/"]\nprocess_isolation = "zle"\n')

    with pytest.raises(sm.BladNarzedzia) as wyjatek:
        sm.odcisk_zrodel(tmp_path)

    assert str(wyjatek.value).startswith("konfiguracja mutmuta: Invalid process_isolation")


def test_zepsuty_dowiazek_symboliczny_w_zrodlach_jest_pominiety_jak_nie_plik(sm, tmp_path):
    projekt = _projekt_odcisku(tmp_path)
    os.symlink("nie-ma-celu", projekt / "src/pakiet/zepsuty.py")

    assert isinstance(sm.odcisk_zrodel(projekt), str)


def test_wpis_also_copy_wskazujacy_nieistniejacy_plik_jest_pominiety(sm, tmp_path):
    projekt = _projekt_odcisku(tmp_path)
    _zapisz_pyproject(projekt, PYPROJECT_ODCISKU.replace('"deploy/"', '"deploy/", "nie-ma.ini"'))

    assert isinstance(sm.odcisk_zrodel(projekt), str)


@NIECZYTELNY_KATALOG_SKIP
@pytest.mark.parametrize(
    "podkatalog",
    [
        pytest.param("src/pakiet/sub", id="w_zrodlach"),
        pytest.param("tests/zagniezdzone", id="w_testach"),
    ],
)
def test_nieczytelny_podkatalog_objetego_katalogu_to_blad_narzedzia_a_nie_pominiecie(sm, tmp_path, podkatalog):
    projekt = _projekt_odcisku(tmp_path)
    (projekt / "tests/zagniezdzone").mkdir()
    (projekt / "tests/zagniezdzone/test_z.py").write_text("x = 1\n", encoding="utf-8")
    zamkniety = projekt / podkatalog
    zamkniety.chmod(0o000)
    try:
        with pytest.raises(sm.BladNarzedzia) as wyjatek:
            sm.odcisk_zrodel(projekt)
    finally:
        zamkniety.chmod(0o755)

    assert str(wyjatek.value).startswith(f"{podkatalog}: nie da sie odczytac")


@NIECZYTELNY_KATALOG_SKIP
def test_wpis_also_copy_w_nieczytelnym_katalogu_to_blad_narzedzia_z_nazwa_wpisu(sm, tmp_path):
    projekt = _projekt_odcisku(tmp_path)
    (projekt / "zamkniety").mkdir()
    (projekt / "zamkniety/plik.txt").write_text("x", encoding="utf-8")
    _zapisz_pyproject(projekt, PYPROJECT_ODCISKU.replace('"deploy/"', '"deploy/", "zamkniety/plik.txt"'))
    (projekt / "zamkniety").chmod(0o000)
    try:
        with pytest.raises(sm.BladNarzedzia) as wyjatek:
            sm.odcisk_zrodel(projekt)
    finally:
        (projekt / "zamkniety").chmod(0o755)

    assert str(wyjatek.value).startswith("zamkniety/plik.txt: nie da sie odczytac")


# --- katalog biezacy i import mutmuta -------------------------------------------------------------


def test_wejscie_do_katalogu_ktorego_nie_ma_to_blad_narzedzia(sm, tmp_path):
    brak = tmp_path / "brak"

    with pytest.raises(sm.BladNarzedzia) as wyjatek:
        with sm._w_katalogu(brak):
            pass

    assert str(wyjatek.value).startswith(f"{brak}: nie da sie wejsc do katalogu")


def test_katalog_biezacy_wraca_po_bloku_w_katalogu_projektu(sm, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cel = tmp_path / "cel"
    cel.mkdir()

    with sm._w_katalogu(cel):
        wewnatrz = Path.cwd().resolve()

    assert wewnatrz == cel.resolve()
    assert Path.cwd().resolve() == tmp_path.resolve()


def test_katalog_biezacy_wraca_takze_gdy_blok_rzuci_wyjatek(sm, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cel = tmp_path / "cel"
    cel.mkdir()

    with pytest.raises(RuntimeError):
        with sm._w_katalogu(cel):
            raise RuntimeError("blad w bloku")

    assert Path.cwd().resolve() == tmp_path.resolve()


@pytest.mark.parametrize(
    "tylko_konfiguracja", [pytest.param(True, id="tylko_konfiguracja"), pytest.param(False, id="pelny")]
)
def test_brak_mutmuta_w_interpreterze_to_blad_narzedzia_a_nie_wyjatek_importu(sm, monkeypatch, tylko_konfiguracja):
    monkeypatch.setitem(sys.modules, "mutmut.configuration", None)

    with pytest.raises(sm.BladNarzedzia) as wyjatek:
        sm._mutmut(tylko_konfiguracja=tylko_konfiguracja)

    assert str(wyjatek.value).startswith("brak mutmuta w interpreterze (")


# --- konfiguracja mutmuta i przygotowanie mutants/ w osobnym procesie ---------------------------------------


def test_odcisk_konfiguracji_mutmuta_bez_zrodel_to_blad_narzedzia_z_konfiguracji(sm, tmp_path):
    goly = tmp_path / "goly"
    goly.mkdir()
    _zapisz_pyproject(goly, "[tool.mutmut]\nsource_paths = []\n")

    komunikat = _blad_wywolania(goly, "s._odcisk_konfiguracji_mutmuta(Path.cwd())")

    assert komunikat is not None and komunikat.startswith("konfiguracja mutmuta: ")


def test_odcisk_konfiguracji_mutmuta_ze_zla_izolacja_to_blad_narzedzia_z_konfiguracji(sm, tmp_path):
    projekt = _projekt(tmp_path / "projekt", pyproject=PYPROJECT_MINI_ZLA_IZOLACJA, git=False)

    komunikat = _blad_wywolania(projekt, "s._odcisk_konfiguracji_mutmuta(Path.cwd())")

    assert komunikat is not None and komunikat.startswith("konfiguracja mutmuta: Invalid process_isolation")


# Walidacje `sha` i odcisku (przed `_mutmut()`) wylaczamy: sama konfiguracja mutmuta ujawnia sie dopiero przy
# imporcie `mutmut.__main__` (mutmut/utils/safe_setproctitle.py wola `config()` na poziomie modulu).
WYRAZENIE_MUTMUT_RUN_BEZ_WALIDACJI = (
    '(setattr(s, "_sha_commita", lambda k: "x"), setattr(s, "odcisk_zrodel", lambda k: "x"), '
    's._mutmut_run(SimpleNamespace(korzen=Path.cwd(), katalog_dowodow=Path("d"), wzorce=["x"])))'
)


def test_mutmut_run_ze_zla_izolacja_procesow_wykryta_przy_imporcie_mutmuta_to_blad_narzedzia(sm, tmp_path):
    projekt = _projekt(tmp_path / "projekt", pyproject=PYPROJECT_MINI_ZLA_IZOLACJA, git=False)

    komunikat = _blad_wywolania(projekt, WYRAZENIE_MUTMUT_RUN_BEZ_WALIDACJI)

    assert komunikat is not None and komunikat.startswith("konfiguracja mutmuta: Invalid process_isolation")


def test_mutmut_run_bez_zrodel_do_mutowania_wykryty_przy_imporcie_mutmuta_to_blad_narzedzia(sm, tmp_path):
    goly = tmp_path / "goly"
    goly.mkdir()
    _zapisz_pyproject(goly, "[tool.mutmut]\nsource_paths = []\n")

    komunikat = _blad_wywolania(goly, WYRAZENIE_MUTMUT_RUN_BEZ_WALIDACJI)

    assert komunikat is not None and komunikat.startswith("konfiguracja mutmuta: Could not figure out")


def test_przygotowanie_mutants_gdy_mutants_jest_zwyklym_plikiem_to_blad_narzedzia(sm, tmp_path):
    projekt = _projekt(tmp_path / "projekt", git=False)
    (projekt / "mutants").write_text("to nie katalog", encoding="utf-8")

    komunikat = _blad_wywolania(projekt, 's._dowod_shardu(SimpleNamespace(indeks=0, liczba=2), "sha", "odcisk")')

    assert komunikat is not None and komunikat.startswith("przygotowanie mutants/: ")


def test_przygotowanie_mutants_ze_zla_izolacja_procesow_to_blad_narzedzia(sm, tmp_path):
    projekt = _projekt(tmp_path / "projekt", pyproject=PYPROJECT_MINI_ZLA_IZOLACJA, git=False)

    komunikat = _blad_wywolania(projekt, 's._dowod_shardu(SimpleNamespace(indeks=0, liczba=2), "sha", "odcisk")')

    assert komunikat is not None and komunikat.startswith("przygotowanie mutants/: Invalid process_isolation")


# --- wczytanie dowodow: nazwy, kolejnosc, nieczytelny katalog ----------------------------------------------------


@pytest.mark.parametrize(
    "nazwa",
    [
        pytest.param("shard-x.json", id="litera"),
        pytest.param("shard-01.json", id="zero_wiodace"),
        pytest.param("shard-١.json", id="cyfra_arabska"),
        pytest.param("shard-.json", id="bez_numeru"),
        pytest.param("shard-1.5.json", id="kropka_w_numerze"),
    ],
)
def test_plik_pasujacy_do_shard_gwiazdka_json_o_niekanonicznej_nazwie_to_brak_dowodu(sm, tmp_path, nazwa):
    katalog = _katalog(tmp_path)
    (katalog / nazwa).write_text("{}", encoding="utf-8")

    assert (
        _blad(sm, lambda: _wczytaj(sm, katalog)) == f"brak dowodu: {nazwa}: nazwa pliku nie jest postaci shard-N.json"
    )


def test_dwa_uszkodzone_pliki_shardow_daja_blad_pierwszego_wedlug_nazwy(sm, tmp_path):
    # Trzydziesci plikow: kolejnosc wyliczania katalogu bez sortowania trafilaby w pierwszy wedlug nazwy z 1/30.
    katalog = tmp_path / "dowody"
    katalog.mkdir()
    for indeks in range(30):
        (katalog / f"shard-{indeks}.json").write_text("{nieczytelny", encoding="utf-8")

    assert _blad(sm, lambda: _wczytaj(sm, katalog)).startswith("brak dowodu: shard-0.json: nie da sie odczytac: ")


@NIECZYTELNY_KATALOG_SKIP
def test_katalog_dowodow_bez_prawa_odczytu_to_brak_dowodu_z_nazwa_katalogu(sm, tmp_path):
    katalog = _katalog(tmp_path)
    katalog.chmod(0o300)
    try:
        komunikat = _blad(sm, lambda: _wczytaj(sm, katalog))
    finally:
        katalog.chmod(0o755)

    assert komunikat.startswith(f"brak dowodu: katalog dowodow {katalog}: nie da sie wylistowac")


# --- zlozenie statystyk: kolejnosc ---------------------------------------------------------------------------------


def test_testy_i_zaleznosci_funkcji_w_zlozonych_statystykach_sa_posortowane_przy_wielu_elementach(sm):
    dowody = [
        _dowod(
            0,
            tests_by_mangled_function_name={"pakiet.modul.x_f": OSIEM_TESTOW[:4]},
            function_dependencies={"pakiet.modul.x_f": OSIEM_TESTOW[:4]},
        ),
        _dowod(
            1,
            tests_by_mangled_function_name={"pakiet.modul.x_f": OSIEM_TESTOW[4:]},
            function_dependencies={"pakiet.modul.x_f": OSIEM_TESTOW[4:]},
        ),
        _dowod(2),
    ]

    zlozone = _zloz(sm, dowody)

    assert zlozone["tests_by_mangled_function_name"]["pakiet.modul.x_f"] == sorted(OSIEM_TESTOW)
    assert zlozone["function_dependencies"]["pakiet.modul.x_f"] == sorted(OSIEM_TESTOW)


def test_funkcje_w_zlozonych_statystykach_sa_posortowane_niezaleznie_od_kolejnosci_w_dowodzie(sm):
    dowod = _dowod(
        0,
        tests_by_mangled_function_name={funkcja: [T[0]] for funkcja in FUNKCJE_OSIEM},
        function_dependencies={funkcja: [T[0]] for funkcja in FUNKCJE_OSIEM},
    )

    zlozone = _zloz(sm, [dowod, _dowod(1), _dowod(2)])

    klucze_testow = list(zlozone["tests_by_mangled_function_name"])
    assert len(klucze_testow) >= 10
    assert klucze_testow == sorted(klucze_testow)
    assert list(zlozone["function_dependencies"]) == sorted(FUNKCJE_OSIEM)


# --- CLI ---------------------------------------------------------------------------------------------------------


def test_zly_indeks_shardu_jest_zglaszany_zanim_narzedzie_zajrzy_do_gita_i_konfiguracji(sm, tmp_path, capsys):
    kod = sm.main(
        ["shard", "--korzen", str(tmp_path), "--indeks", "5", "--liczba", "3", "--katalog-dowodow", str(tmp_path / "d")]
    )

    assert kod == 2
    assert "blad: indeks shardu 5 poza zakresem 0..2" in capsys.readouterr().err.splitlines()


def test_shard_gdy_katalog_dowodow_jest_zwyklym_plikiem_konczy_sie_kodem_2_z_nazwa_pliku_dowodu(sm, tmp_path):
    projekt = _projekt(tmp_path / "projekt")
    (projekt / "dowody").write_text("to nie katalog", encoding="utf-8")

    wynik = _shard(projekt, 0, 2, projekt / "dowody")

    assert wynik.returncode == 2, wynik.stderr
    assert any(
        linia.startswith("blad: ") and "shard-0.json: nie da sie zapisac" in linia for linia in _linie_bledu(wynik)
    ), wynik.stderr


def test_scal_gdy_mutants_jest_zwyklym_plikiem_konczy_sie_kodem_2_z_nazwa_pliku_statystyk(sm, tmp_path):
    projekt = _projekt(tmp_path / "projekt")
    (projekt / "mutants").write_text("to nie katalog", encoding="utf-8")
    _dowody_reczne(projekt, projekt / "dowody", sm)

    wynik = _scal(projekt, projekt / "dowody")

    assert wynik.returncode == 2, wynik.stderr
    assert any(
        linia.startswith("blad: ") and "mutmut-stats.json: nie da sie zapisac" in linia for linia in _linie_bledu(wynik)
    ), wynik.stderr


def test_shard_gdy_testy_nie_daja_sie_zebrac_konczy_sie_kodem_2_z_komunikatem_o_kolekcji(sm, tmp_path):
    projekt = _projekt(tmp_path / "projekt", test_b="def test_zepsuty(:\n")

    wynik = _shard(projekt, 0, 2, projekt / "dowody")

    assert wynik.returncode == 2, wynik.stdout + wynik.stderr
    assert "blad: nie udalo sie zebrac listy testow (pytest --collect-only)" in _linie_bledu(wynik)
    assert not (projekt / "dowody" / "shard-0.json").exists()


def test_dowod_shardu_niesie_zaleznosci_funkcji_wywolanych_przez_inne_funkcje(sm, tmp_path):
    projekt = _projekt(tmp_path / "projekt", modul=MODUL_Z_ZALEZNOSCIA)

    wynik = _shard(projekt, 0, 2, projekt / "dowody")

    assert wynik.returncode == 0, wynik.stderr
    dowod = _wczytaj_dowod(projekt / "dowody", 0)
    assert dowod["function_dependencies"] == {"pakiet.liczby.x_pomocnicza": ["pakiet.liczby.x_dodaj"]}


def test_mutmut_z_dowodami_dziala_gdy_katalog_dowodow_jest_podany_wzglednie_a_korzen_to_inny_katalog(
    sm, projekt_po_shardach, tmp_path
):
    kopia = _kopia(projekt_po_shardach, tmp_path)
    assert _scal(kopia, kopia / "dowody").returncode == 0
    wzgledny = os.path.relpath(kopia / "dowody", KORZEN)

    wynik = _skrypt(kopia, "mutmut-run", "--korzen", str(kopia), "--katalog-dowodow", wzgledny, *WZORCE_MINI)

    assert wynik.returncode == 0, wynik.stdout + wynik.stderr


def test_po_bledzie_dowodu_mutmut_run_przywraca_runnera_mutmuta_i_katalog_biezacy(
    sm, projekt_po_shardach, tmp_path
):
    kopia = _kopia(projekt_po_shardach, tmp_path)
    assert _scal(kopia, kopia / "dowody").returncode == 0
    czesc = tmp_path / "czesc"
    czesc.mkdir()
    shutil.copy(kopia / "dowody" / "shard-0.json", czesc / "shard-0.json")

    wynik = uruchom_i_zbierz(
        [sys.executable, "-c", KOD_MUTMUT_RUN_W_PROCESIE, str(KORZEN / "tools"), str(kopia), str(czesc), WZORCE_MINI[0]],
        capture_output=True,
        text=True,
        cwd=str(KORZEN),
        env=_srodowisko(kopia),
        timeout=180,
    )

    linie = [linia for linia in wynik.stdout.splitlines() if linia.startswith("WYNIK ")]
    assert linie, wynik.stdout + wynik.stderr
    assert json.loads(linie[-1][len("WYNIK ") :]) == {"kod": 2, "runner": True, "cwd": True}


# --- identyfikatory testow z backslashem: jedna zbiorka pytesta w procesie rodzica ----------------------------

# Id przypadkow: `a\nb` (znak nowej linii zapisany jako backslash + n) i `zaż\xf3...` (znaki spoza ASCII).
# Jawne `ids=` (tu `ids=str`) sprawia, ze druga zbiorka pytesta w tym samym procesie escapuje te id po raz drugi;
# przy domyslnych id (bez `ids=`) obie zbiorki daja to samo i test regresji nie odroznialby poprawki.
TEST_C_MINI = """\
import pytest

from pakiet.liczby import dodaj


@pytest.mark.parametrize("x", ["a\\nb", "zażółć"], ids=str)
def test_dodaj_z_backslashem_w_id(x):
    assert dodaj(1, 1) == 2
"""


def _projekt_z_backslashem_w_id_testu(katalog: Path) -> Path:
    projekt = _projekt(katalog, git=False)
    (projekt / "tests" / "test_c.py").write_text(TEST_C_MINI, encoding="utf-8")
    _git(projekt, "init", "-q")
    _git(projekt, "add", "-A")
    _git(projekt, "commit", "-q", "-m", "miniatura z backslashem w id")
    return projekt


def test_shard_i_mutmut_z_dowodami_przechodza_gdy_identyfikator_testu_zawiera_backslash(sm, tmp_path):
    # Przed poprawka druga zbiorka pytesta w tym samym procesie escapowala backslash w node id drugi raz
    # (`a\nb` -> `a\\nb`): lista z `list_all_tests` nie pasowala do `collect_stats` (pytest kod 4, "not found").
    projekt = _projekt_z_backslashem_w_id_testu(tmp_path / "projekt")
    katalog = projekt / "dowody"

    wynik_shardu = _shard(projekt, 0, 1, katalog)
    assert wynik_shardu.returncode == 0, wynik_shardu.stdout + wynik_shardu.stderr
    testy_c = [t for t in _wczytaj_dowod(katalog, 0)["testy"] if t.startswith("tests/test_c.py::")]
    assert len(testy_c) == 2, testy_c
    assert "tests/test_c.py::test_dodaj_z_backslashem_w_id[a\\nb]" in testy_c
    assert _scal(projekt, katalog).returncode == 0
    przed = _log_testow(projekt)

    wynik = _mutmut_run(projekt, katalog)

    assert wynik.returncode == 0, wynik.stdout + wynik.stderr
    nowe = _log_testow(projekt)[len(przed) :]
    assert "''" not in nowe, nowe
    # Mutanta `dodaj` zabija najpierw test_c (najszybszy, bez wpisu w logu), wiec w logu jest tylko `odejmij`.
    assert "'pakiet.liczby.x_odejmij__mutmut_1'" in nowe, nowe


# --- lista testow z forkowanego dziecka: blad dziecka i blad zbierania -------------------------------------------


class BladZebraniaTestowAtrapy(Exception):
    """Zastepuje `CollectTestsFailedException` mutmuta w atrapie `_mutmut`."""


def _atrapa_mutmuta(w_dziecku):
    def nic(*_argumenty):
        return None

    runner = SimpleNamespace(list_all_tests=lambda: SimpleNamespace(ids={TEST_A, TEST_B}))
    return SimpleNamespace(
        glowny=SimpleNamespace(
            copy_src_dir=nic,
            copy_also_copy_files=nic,
            setup_source_paths=nic,
            store_lines_covered_by_tests=nic,
            create_mutants=nic,
            get_mutant_runner=lambda _liczba: runner,
        ),
        config=lambda: SimpleNamespace(dependency_tracking_depth=0),
        set_mutant_under_test=nic,
        blad_zebrania_testow=BladZebraniaTestowAtrapy,
        w_dziecku=w_dziecku,
    )


def _shard_z_atrapa_mutmuta(sm, monkeypatch, tmp_path, w_dziecku, capsys):
    monkeypatch.setattr(sm, "_mutmut", lambda **_nazwane: _atrapa_mutmuta(w_dziecku))
    monkeypatch.setattr(sm, "_sha_commita", lambda _korzen: SHA)
    monkeypatch.setattr(sm, "odcisk_zrodel", lambda _korzen: ODCISK)
    monkeypatch.chdir(tmp_path)
    katalog = tmp_path / "dowody"

    kod = sm.main(["shard", "--korzen", str(tmp_path), "--indeks", "0", "--liczba", "2", "--katalog-dowodow", str(katalog)])

    return kod, capsys.readouterr().err.splitlines(), katalog


def test_blad_dziecka_zbierajacego_liste_testow_to_kod_2_z_komunikatem_o_kolekcji_i_bez_dowodu(
    sm, tmp_path, monkeypatch, capsys
):
    def dziecko_konczy_sie_bledem(_funkcja):
        raise ChildProcessError("Child exited with code 1")

    kod, bledy, katalog = _shard_z_atrapa_mutmuta(sm, monkeypatch, tmp_path, dziecko_konczy_sie_bledem, capsys)

    assert kod == 2
    assert "blad: nie udalo sie zebrac listy testow (pytest --collect-only)" in bledy
    assert not (katalog / "shard-0.json").exists()


def test_blad_zbierania_testow_zgloszony_przez_mutmuta_to_kod_2_z_komunikatem_o_kolekcji_i_bez_dowodu(
    sm, tmp_path, monkeypatch, capsys
):
    def zbieranie_testow_nie_powiodlo_sie(_funkcja):
        raise BladZebraniaTestowAtrapy("zbieranie")

    kod, bledy, katalog = _shard_z_atrapa_mutmuta(sm, monkeypatch, tmp_path, zbieranie_testow_nie_powiodlo_sie, capsys)

    assert kod == 2
    assert "blad: nie udalo sie zebrac listy testow (pytest --collect-only)" in bledy
    assert not (katalog / "shard-0.json").exists()


def test_inny_wyjatek_z_dziecka_zbierajacego_liste_testow_nie_jest_tlumaczony_na_blad_narzedzia(
    sm, tmp_path, monkeypatch, capsys
):
    def dziecko_rzuca_obcy_wyjatek(_funkcja):
        raise KeyError("obcy")

    with pytest.raises(KeyError):
        _shard_z_atrapa_mutmuta(sm, monkeypatch, tmp_path, dziecko_rzuca_obcy_wyjatek, capsys)
