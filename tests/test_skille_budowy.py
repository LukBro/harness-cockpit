"""Nazwy testow w jezyku dziedziny, nie technologii (BRO-95).

Trzy skille budowy (tdd-task, e2e-scenario, smoke) jako SKILL.md w
`.cockpit/skills/` tego repozytorium (HAR-73). Czytamy je przez prawdziwy rejestr.
"""
import subprocess
from pathlib import Path

import pytest

from cockpit.services.invariants import Rola, profil_roli
from cockpit.services.skills import Rejestr, rozstrzygnij_rejestr

IDENTYFIKATORY = ["tdd-task", "e2e-scenario", "smoke"]

# id -> (rola, consumes, produces)
OCZEKIWANE = {
    "tdd-task": (Rola.author, ["plan.md"], ["tdd.md"]),
    "e2e-scenario": (Rola.author, ["plan.md"], ["scenariusz-e2e.md"]),
    "smoke": (Rola.gatherer, [], ["smoke.md"]),
}


def korzen_repozytorium() -> Path:
    """Korzeń repozytorium git, także gdy test leży w kopii `mutants/` (mutmut
    nie kopiuje `.gitignore`, więc `parents[1]` wskazywałoby katalog bez niego)."""
    wynik = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent,
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(wynik.stdout.strip())


@pytest.fixture
def rejestr(tmp_path: Path) -> Rejestr:
    # pusty dom, zeby ~/.cockpit uzytkownika nie wplywal na wynik
    return rozstrzygnij_rejestr(korzen_repozytorium(), tmp_path / "dom")


def test_rejestr_repozytorium_zawiera_trzy_skille_budowy(rejestr):
    assert set(IDENTYFIKATORY) <= set(rejestr.skille)


def test_wczytanie_skilli_budowy_nie_pomija_zadnego_pliku(rejestr):
    assert rejestr.pominiete == []


@pytest.mark.parametrize("id", IDENTYFIKATORY)
def test_skill_budowy_ma_id_zgodne_z_nazwa_katalogu(rejestr, id):
    skill = rejestr.skille[id]
    assert skill.id == id
    assert skill.katalog.name == id


@pytest.mark.parametrize("id", IDENTYFIKATORY)
def test_skill_budowy_ma_ustalona_role(rejestr, id):
    assert rejestr.skille[id].role == OCZEKIWANE[id][0]


@pytest.mark.parametrize("id", IDENTYFIKATORY)
def test_skill_budowy_pobiera_ustalone_artefakty(rejestr, id):
    assert rejestr.skille[id].consumes == OCZEKIWANE[id][1]


@pytest.mark.parametrize("id", IDENTYFIKATORY)
def test_skill_budowy_wytwarza_ustalone_artefakty(rejestr, id):
    assert rejestr.skille[id].produces == OCZEKIWANE[id][2]


@pytest.mark.parametrize("id", ["tdd-task", "e2e-scenario"])
def test_skill_piszacy_testy_nie_jest_recenzentem_i_ma_prawo_zapisu(rejestr, id):
    rola = rejestr.skille[id].role
    assert rola != Rola.reviewer
    assert profil_roli(rola).zapis is True


def test_smoke_nie_ma_prawa_zapisu_w_repozytorium(rejestr):
    assert profil_roli(rejestr.skille["smoke"].role).zapis is False


def test_tdd_task_odsyla_do_zasad_testow_zamiast_je_kopiowac(rejestr):
    brief = rejestr.skille["tdd-task"].brief
    assert "CLAUDE.md" in brief
    assert ".ai/qa/AGENTS.md" in brief


def test_tdd_task_nie_kopiuje_przykladu_nazwy_testu_z_claude_md(rejestr):
    brief = rejestr.skille["tdd-task"].brief
    assert "test_run_zakonczony_sukcesem_ma_stan_do_odbioru_nie_gotowe" not in brief


def test_tdd_task_nie_kopiuje_zdania_reguly_nazewnictwa(rejestr):
    brief = rejestr.skille["tdd-task"].brief
    assert "Nazwy w języku dziedziny, nie technologii" not in brief


def test_e2e_scenario_kaze_uruchamiac_scenariusze_przez_pytest_m_e2e(rejestr):
    assert "pytest -m e2e" in rejestr.skille["e2e-scenario"].brief


def test_e2e_scenario_wskazuje_playwrighta(rejestr):
    assert "Playwright" in rejestr.skille["e2e-scenario"].brief


def test_smoke_wskazuje_skrypt_demo_szkieletu(rejestr):
    assert "tools/demo_szkielet.py" in rejestr.skille["smoke"].brief


def test_smoke_opisuje_wynik_tak_albo_nie(rejestr):
    brief = rejestr.skille["smoke"].brief
    assert "TAK" in brief
    assert "NIE" in brief
