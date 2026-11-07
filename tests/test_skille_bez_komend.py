"""Skille nie niosa komend narzedzi stosu (HAR-163).

Komenda mieszka w `.cockpit/project.yaml` jako capability; skill deklaruje `needs:`
i nazywa capability w briefie. Czytamy prawdziwe pliki tego repozytorium.
"""
import tomllib
from pathlib import Path

import pytest

from cockpit.services.projekt import BRAK_PROJEKTU, pominiete_kroki, wczytaj_projekt
from cockpit.services.skills import Rejestr, rozstrzygnij_rejestr
from cockpit.services.workflow import wczytaj_workflow

# parents[1], nie `git rev-parse`: pod mutmut testy czytaja kopie w `mutants/`.
KORZEN_REPO = Path(__file__).resolve().parents[1]
KATALOG_SKILLI = KORZEN_REPO / ".cockpit" / "skills"
PLIK_WORKFLOW = KORZEN_REPO / ".cockpit" / "workflows" / "budowa.yaml"

ZAKAZANE_WZORCE = [
    "pytest",
    "npm ",
    "python tools/",
    "bundle ",
    "go test",
    "ruff",
    "mypy",
    "tsc",
]

OCZEKIWANE_CAPABILITIES = {
    "test_narrow",
    "test_full",
    "test_e2e",
    "lint",
    "layers",
    "typecheck",
    "contract",
    "smoke",
}

OCZEKIWANE_NEEDS = {
    "smoke": ["smoke"],
    "e2e-scenario": ["test_e2e"],
    "contract-check": [],  # HAR-177: dowody komend daja kroki check silnika (R8)
    "tdd-task": ["test_narrow"],
    "plan": [],
    "review-unknown": [],
    "explain-change": [],
    "capture-lesson": [],
}


def znajdz_komendy(tekst: str) -> list[str]:
    """Zakazane wzorce komend narzedzi, ktore wystepuja w tekscie."""
    return [wzorzec for wzorzec in ZAKAZANE_WZORCE if wzorzec in tekst]


def pliki_skilli() -> list[Path]:
    return sorted(KATALOG_SKILLI.glob("*/SKILL.md"))


def mutmut_kopiuje(also_copy: list[str], sciezka: str) -> bool:
    """Plik w podkatalogu jest kopiowany tylko przez wpis-katalog (z `/`) bedacy przodkiem."""
    return any(wpis.endswith("/") and sciezka.startswith(wpis) for wpis in also_copy)


def wczytaj_rejestr(tmp_path: Path) -> Rejestr:
    dom = tmp_path / "dom"
    dom.mkdir()
    return rozstrzygnij_rejestr(KORZEN_REPO, dom)


# Literalne przypadki, niezalezne od ZAKAZANE_WZORCE: (komenda, jedyny wzorzec, ktory ja wykrywa).
# Usuniecie wzorca z listy oblewa dokladnie jego przypadek.
KOMENDY_I_ICH_WZORCE = [
    ("pytest -m e2e", "pytest"),
    ("npm run build", "npm "),
    ("python tools/demo_szkielet.py", "python tools/"),
    ("bundle exec rspec", "bundle "),
    ("go test ./...", "go test"),
    ("ruff check .", "ruff"),
    ("mypy src", "mypy"),
    ("tsc --noEmit", "tsc"),
]


@pytest.mark.parametrize(
    "komenda, wzorzec", KOMENDY_I_ICH_WZORCE, ids=[k for k, _ in KOMENDY_I_ICH_WZORCE]
)
def test_straznik_wykrywa_komende_narzedzia_gdy_wraca_do_skilla(komenda, wzorzec):
    tekst = f"## Jak sprawdzic\n\nUruchom `{komenda}` i opisz wynik.\n"
    assert znajdz_komendy(tekst) == [wzorzec]


def test_straznik_zna_dokladnie_te_wzorce_komend_ktore_maja_przypadek_testowy():
    assert sorted(ZAKAZANE_WZORCE) == sorted(w for _, w in KOMENDY_I_ICH_WZORCE)


@pytest.mark.parametrize(
    "tekst",
    [
        "Pakiet znajdziesz na npmjs.com.\n",
        "Zainstaluj bundler z gemow.\n",
        "Nazwa kroku to npm\n",
        "Ten bundle\n",
    ],
)
def test_straznik_nie_bierze_nazwy_pakietu_bez_spacji_za_komende(tekst):
    assert znajdz_komendy(tekst) == []


def test_straznik_nie_wykrywa_komendy_w_tekscie_z_samymi_nazwami_capability():
    tekst = (
        "Uruchom capability `test_e2e`, potem capability `smoke`.\n"
        "Capability `contract` i `typecheck` daja dowod zgodnosci.\n"
    )
    assert znajdz_komendy(tekst) == []


def test_katalog_skilli_repozytorium_zawiera_pliki_skilli():
    assert pliki_skilli(), f"brak plikow SKILL.md w {KATALOG_SKILLI}"


@pytest.mark.parametrize(
    "plik", pliki_skilli(), ids=lambda p: p.parent.name
)
def test_skill_repozytorium_nie_zawiera_komendy_narzedzia_stosu(plik):
    trafienia = znajdz_komendy(plik.read_text(encoding="utf-8"))
    assert trafienia == [], f"{plik.relative_to(KORZEN_REPO)} zawiera komende: {trafienia}"


def test_repozytorium_ma_projekt_z_capabilities_bez_problemow():
    projekt, problemy = wczytaj_projekt(KORZEN_REPO)
    assert [p.kod for p in problemy if p.kod == BRAK_PROJEKTU] == []
    assert problemy == []
    assert projekt is not None
    assert set(projekt.capabilities) == OCZEKIWANE_CAPABILITIES


def test_kazda_capability_projektu_ma_niepusta_komende():
    projekt, _ = wczytaj_projekt(KORZEN_REPO)
    assert projekt is not None
    assert [n for n, k in projekt.capabilities.items() if not k.strip()] == []


def test_kazdy_skill_repozytorium_wymaga_tylko_capability_obecnych_w_projekcie(tmp_path):
    projekt, _ = wczytaj_projekt(KORZEN_REPO)
    assert projekt is not None
    rejestr = wczytaj_rejestr(tmp_path)
    assert rejestr.skille, "pusty rejestr skilli"
    nieznane = {
        id: [c for c in skill.needs if c not in projekt.capabilities]
        for id, skill in rejestr.skille.items()
    }
    assert {id: brak for id, brak in nieznane.items() if brak} == {}


@pytest.mark.parametrize("id", sorted(OCZEKIWANE_NEEDS))
def test_skill_deklaruje_ustalone_capability_zamiast_komend(tmp_path, id):
    rejestr = wczytaj_rejestr(tmp_path)
    assert id in rejestr.skille, f"brak skilla {id}: {sorted(rejestr.skille)}"
    assert rejestr.skille[id].needs == OCZEKIWANE_NEEDS[id]


def test_workflow_budowa_nie_pomija_zadnego_kroku_z_braku_capability(tmp_path):
    rejestr = wczytaj_rejestr(tmp_path)
    projekt, _ = wczytaj_projekt(KORZEN_REPO)
    assert projekt is not None
    workflow, problemy = wczytaj_workflow(PLIK_WORKFLOW)
    assert workflow is not None, problemy
    assert pominiete_kroki(workflow, rejestr, projekt) == []


@pytest.mark.parametrize(
    "sciezka", [".cockpit/project.yaml", ".cockpit/skills/tdd-task/SKILL.md"]
)
def test_mutmut_kopiuje_projekt_i_skille_bo_strazniki_czytaja_je_z_repozytorium(sciezka):
    konfiguracja = tomllib.loads((KORZEN_REPO / "pyproject.toml").read_text("utf-8"))
    assert mutmut_kopiuje(konfiguracja["tool"]["mutmut"]["also_copy"], sciezka)


def test_wpis_pliku_albo_obcego_katalogu_nie_wystarcza_mutmutowi_do_kopii_projektu():
    assert not mutmut_kopiuje([".cockpit/project.yaml"], ".cockpit/skills/tdd-task/SKILL.md")
    assert not mutmut_kopiuje([".cockpit/workflows/"], ".cockpit/project.yaml")
    assert mutmut_kopiuje([".cockpit/"], ".cockpit/project.yaml")
