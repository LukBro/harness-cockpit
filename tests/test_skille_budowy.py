"""Nazwy testow w jezyku dziedziny, nie technologii (BRO-95).

Trzy skille budowy (tdd-task, e2e-scenario, smoke) jako SKILL.md w
`.cockpit/skills/` tego repozytorium (HAR-73). Czytamy je przez prawdziwy rejestr.
"""
from pathlib import Path

import pytest

from cockpit.services.invariants import Rola, profil_roli
from cockpit.services.skills import Rejestr, rozstrzygnij_rejestr
from cockpit.services.walidacja import (
    KontraktWerdyktu,
    Naruszenie,
    Niezmiennik,
    Werdykt,
    sprawdz_werdykt,
)

IDENTYFIKATORY = ["tdd-task", "e2e-scenario", "smoke"]

# id -> (rola, consumes, produces, needs)
OCZEKIWANE = {
    "tdd-task": (Rola.author, ["plan.md"], ["tdd.md"], ["test_narrow"]),
    "e2e-scenario": (Rola.author, ["plan.md"], ["scenariusz-e2e.md"], ["test_e2e"]),
    "smoke": (Rola.gatherer, [], ["smoke.md"], ["smoke"]),
}


def korzen_repozytorium() -> Path:
    """Korzeń repozytorium liczony względem pliku testu (bez gita): kopia `mutants/` i kopia
    narzędzia ręcznych mutantów nie są repozytoriami git."""
    return Path(__file__).resolve().parents[1]


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


@pytest.mark.parametrize("id", IDENTYFIKATORY)
def test_skill_budowy_deklaruje_potrzebne_capability_projektu(rejestr, id):
    assert rejestr.skille[id].needs == OCZEKIWANE[id][3]


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


def test_e2e_scenario_kaze_uruchamiac_scenariusze_przez_capability_test_e2e(rejestr):
    assert "capability `test_e2e`" in rejestr.skille["e2e-scenario"].brief


def test_e2e_scenario_wskazuje_playwrighta(rejestr):
    assert "Playwright" in rejestr.skille["e2e-scenario"].brief


def test_smoke_kaze_uruchomic_capability_smoke(rejestr):
    assert "capability `smoke`" in rejestr.skille["smoke"].brief


def test_smoke_nadal_wymaga_bazy_testowej_przez_zmienna_srodowiska(rejestr):
    brief = rejestr.skille["smoke"].brief
    assert "COCKPIT_DATABASE_URL" in brief
    assert "bazy testowej" in brief


def test_smoke_opisuje_wynik_tak_albo_nie(rejestr):
    brief = rejestr.skille["smoke"].brief
    assert "TAK" in brief
    assert "NIE" in brief


# --- werdykt proby dymnej jako kontrakt skilla (HAR-177) ---------------------------------------


def test_proba_dymna_deklaruje_werdykt_tak_przechodzi_nie_oblewa(rejestr):
    assert rejestr.skille["smoke"].verdict == KontraktWerdyktu(pass_=("TAK",), fail=("NIE",))


@pytest.mark.parametrize("id", ["tdd-task", "e2e-scenario"])
def test_skill_piszacy_testy_nie_deklaruje_werdyktu(rejestr, id):
    assert rejestr.skille[id].verdict is None


def test_brief_proby_dymnej_wymaga_linii_werdyktu_na_poczatku_artefaktu(rejestr):
    brief = " ".join(rejestr.skille["smoke"].brief.split())
    assert "WERDYKT: TAK" in brief
    assert "WERDYKT: NIE" in brief
    assert "Pierwsza linia to `TAK` albo `NIE`" not in brief


def test_artefakt_proby_dymnej_z_linia_werdykt_tak_jest_zdany_a_z_nie_nie_jest(rejestr, tmp_path):
    kontrakt = rejestr.skille["smoke"].verdict
    assert kontrakt is not None
    tak = tmp_path / "tak" / "smoke.md"
    nie = tmp_path / "nie" / "smoke.md"
    for plik, linia in ((tak, "WERDYKT: TAK"), (nie, "WERDYKT: NIE")):
        plik.parent.mkdir()
        plik.write_text(linia + "\nkomenda: python tools/demo_szkielet.py\n", encoding="utf-8")

    assert sprawdz_werdykt(tak, kontrakt) == Werdykt("TAK", True)
    assert sprawdz_werdykt(nie, kontrakt) == Werdykt("NIE", False)


def test_dawny_format_proby_dymnej_z_samym_tak_w_pierwszej_linii_nie_jest_werdyktem(
    rejestr, tmp_path
):
    kontrakt = rejestr.skille["smoke"].verdict
    assert kontrakt is not None
    plik = tmp_path / "smoke.md"
    plik.write_text("TAK\nkomenda: python tools/demo_szkielet.py\n", encoding="utf-8")

    assert sprawdz_werdykt(plik, kontrakt) == Naruszenie(
        Niezmiennik.evidence_required, "linia 1 w smoke.md nie zaczyna się od WERDYKT:")
