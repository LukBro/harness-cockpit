"""HAR-236: testy wykluczone z faz pelnych mutmuta (`pytest_add_cli_args_test_selection`) nie moga
wywolywac kodu zrodlowego w procesie. Inaczej wypadlyby z bramki mutacji po cichu: mutmut wiaze test
z mutantem tylko wtedy, gdy test wywola mutowana funkcje w swoim procesie."""

import ast
import tomllib
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]
PAKIETY_ZRODLA = {"cockpit", "runner"}
PREFIKS = "--ignore="


def _importy(drzewo: ast.AST) -> set[str]:
    wynik = set()
    for wezel in ast.walk(drzewo):
        if isinstance(wezel, ast.Import):
            wynik |= {alias.name.split(".")[0] for alias in wezel.names}
        elif isinstance(wezel, ast.ImportFrom) and wezel.level == 0 and wezel.module:
            wynik.add(wezel.module.split(".")[0])
    return wynik


def _fixtury(drzewo: ast.AST) -> set[str]:
    return {
        f.name
        for f in ast.walk(drzewo)
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any("fixture" in ast.unparse(d) for d in f.decorator_list)
    }


def _argumenty(drzewo: ast.AST) -> set[str]:
    return {
        a.arg
        for f in ast.walk(drzewo)
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
        for a in f.args.args
    }


def wykluczone(pyproject: Path) -> list[str]:
    konfiguracja = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return konfiguracja["tool"]["mutmut"].get("pytest_add_cli_args_test_selection", [])


def bledy_wykluczen(korzen: Path, argumenty: list[str]) -> list[str]:
    if not argumenty:
        return ["lista wykluczen z mutmuta jest pusta"]
    bledy = []
    for argument in argumenty:
        if not argument.startswith(PREFIKS):
            bledy.append(f"{argument}: nieznany argument (oczekiwane {PREFIKS}<plik>)")
            continue
        plik = korzen / argument[len(PREFIKS):]
        if not plik.is_file():
            bledy.append(f"{argument}: plik nie istnieje")
            continue
        drzewo = ast.parse(plik.read_text(encoding="utf-8"))
        importy = sorted(_importy(drzewo) & PAKIETY_ZRODLA)
        if importy:
            bledy.append(f"{argument}: importuje {', '.join(importy)}")
        uzyte = _argumenty(drzewo)
        for katalog in plik.parents:
            conftest = katalog / "conftest.py"
            if conftest.is_file():
                drzewo_conftest = ast.parse(conftest.read_text(encoding="utf-8"))
                if _importy(drzewo_conftest) & PAKIETY_ZRODLA:
                    for fixture in sorted(_fixtury(drzewo_conftest) & uzyte):
                        bledy.append(f"{argument}: fixture {fixture} z {conftest.relative_to(korzen)}")
            if katalog == korzen:
                break
    return bledy


def test_testy_wykluczone_z_mutmuta_nie_wywoluja_kodu_zrodlowego_w_procesie():
    assert bledy_wykluczen(KORZEN, wykluczone(KORZEN / "pyproject.toml")) == []


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_czysty.py").write_text("import os\n\ndef test_a(tmp_path):\n    pass\n")
    (tmp_path / "tests" / "conftest.py").write_text(
        "import pytest\nfrom runner import client\n\n@pytest.fixture\ndef klient():\n    return client\n"
    )
    return tmp_path


def test_czysty_plik_bez_importu_zrodla_i_bez_skazonej_fixture_jest_poprawnym_wykluczeniem(repo):
    assert bledy_wykluczen(repo, ["--ignore=tests/test_czysty.py"]) == []


def test_pusta_lista_wykluczen_to_blad_a_nie_brak_problemow(repo):
    assert bledy_wykluczen(repo, []) == ["lista wykluczen z mutmuta jest pusta"]


def test_wykluczenie_nieistniejacego_pliku_to_blad(repo):
    assert bledy_wykluczen(repo, ["--ignore=tests/test_brak.py"]) == [
        "--ignore=tests/test_brak.py: plik nie istnieje"
    ]


def test_argument_inny_niz_ignore_to_blad(repo):
    assert bledy_wykluczen(repo, ["-k=wolne"]) == [
        "-k=wolne: nieznany argument (oczekiwane --ignore=<plik>)"
    ]


@pytest.mark.parametrize(
    "tresc",
    [
        "import cockpit.services.runs\n",
        "from runner.client import Runner\n",
        "def test_a():\n    from cockpit.api import app\n",
    ],
    ids=["import_na_poziomie_modulu", "from_import", "import_wewnatrz_funkcji"],
)
def test_wykluczony_plik_importujacy_kod_zrodlowy_to_blad(repo, tresc):
    (repo / "tests" / "test_x.py").write_text(tresc)
    blad = bledy_wykluczen(repo, ["--ignore=tests/test_x.py"])
    assert len(blad) == 1 and blad[0].startswith("--ignore=tests/test_x.py: importuje ")


def test_import_wzgledny_i_inne_pakiety_nie_sa_importem_kodu_zrodlowego(repo):
    (repo / "tests" / "test_x.py").write_text(
        "from .cockpit import pomocnik\nimport cockpitowy\nimport procesy\n"
    )
    assert bledy_wykluczen(repo, ["--ignore=tests/test_x.py"]) == []


def test_wykluczony_plik_uzywajacy_fixture_z_conftestu_importujacego_zrodlo_to_blad(repo):
    (repo / "tests" / "test_x.py").write_text("def test_a(klient):\n    pass\n")
    assert bledy_wykluczen(repo, ["--ignore=tests/test_x.py"]) == [
        "--ignore=tests/test_x.py: fixture klient z tests/conftest.py"
    ]


def test_fixture_z_conftestu_bez_importu_zrodla_nie_jest_bledem(repo):
    (repo / "tests" / "conftest.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef klient():\n    return 1\n"
    )
    (repo / "tests" / "test_x.py").write_text("def test_a(klient):\n    pass\n")
    assert bledy_wykluczen(repo, ["--ignore=tests/test_x.py"]) == []


def test_conftest_powyzej_korzenia_repozytorium_nie_jest_sprawdzany(repo):
    # conftest.py w repo (tests/conftest.py) importuje runner; korzeniem jest tu katalog tests/.
    (repo / "tests" / "glebiej").mkdir()
    (repo / "tests" / "glebiej" / "test_x.py").write_text("def test_a(klient):\n    pass\n")
    assert bledy_wykluczen(repo / "tests" / "glebiej", ["--ignore=test_x.py"]) == []


def test_conftest_z_katalogu_nadrzednego_tez_jest_sprawdzany(repo):
    (repo / "tests" / "glebiej").mkdir()
    (repo / "tests" / "glebiej" / "test_x.py").write_text("def test_a(klient):\n    pass\n")
    assert bledy_wykluczen(repo, ["--ignore=tests/glebiej/test_x.py"]) == [
        "--ignore=tests/glebiej/test_x.py: fixture klient z tests/conftest.py"
    ]
