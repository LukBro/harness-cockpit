"""Workflow budowa (HAR-88): kroki wolaja skille budowy, bramka po planie, krok lekcji.

Czytamy prawdziwy plik `.cockpit/workflows/budowa.yaml` i prawdziwy rejestr
tego repozytorium przez produkcyjne `wczytaj_workflow`, `rozstrzygnij_rejestr`
i `waliduj_workflow`. Katalog domowy jest pusty, zeby skille uzytkownika nie
wplywaly na wynik.
"""
import tomllib
from pathlib import Path

import pytest

from cockpit.services.invariants import Rola
from cockpit.services.skills import Rejestr, rozstrzygnij_rejestr
from cockpit.services.workflow import Poziom, Workflow, ma_bledy, wczytaj_workflow
from cockpit.services.workflow_walidacja import (
    NIEPRZENOSNY_SKILL,
    POLE_BEZ_KONSUMENTA,
    waliduj_workflow,
)
from runner.graf import PowodPominiecia, StanKroku, nastepne

# parents[1], nie `git rev-parse`: pod mutmut testy czytaja kopie w `mutants/`.
KORZEN_REPO = Path(__file__).resolve().parents[1]
PLIK_WORKFLOW = KORZEN_REPO / ".cockpit" / "workflows" / "budowa.yaml"

SKILLE_BUDOWY = {
    "tdd-task",
    "e2e-scenario",
    "contract-check",
    "explain-change",
    "review-unknown",
    "smoke",
}


def wczytaj(tmp_path: Path) -> tuple[Workflow, Rejestr, Path]:
    dom = tmp_path / "dom"
    dom.mkdir()
    rejestr = rozstrzygnij_rejestr(KORZEN_REPO, dom)
    workflow, problemy_wczytania = wczytaj_workflow(PLIK_WORKFLOW)
    assert workflow is not None, problemy_wczytania
    return workflow, rejestr, dom


def krok_ze_skillem(workflow: Workflow, skill: str):
    kroki = [k for k in workflow.kroki if k.skill == skill]
    assert len(kroki) == 1, f"oczekiwano jednego kroku ze skillem {skill}: {kroki}"
    return kroki[0]


def test_workflow_budowy_przechodzi_walidacje_bez_bledow_na_rejestrze_repozytorium(tmp_path):
    workflow, rejestr, dom = wczytaj(tmp_path)
    problemy = waliduj_workflow(workflow, rejestr, dom)
    bledy = [p for p in problemy if p.poziom == Poziom.blad]
    assert bledy == []
    assert not ma_bledy(problemy)


def test_workflow_budowy_nie_wola_skilli_zaleznych_od_maszyny_autora(tmp_path):
    workflow, rejestr, dom = wczytaj(tmp_path)
    problemy = waliduj_workflow(workflow, rejestr, dom)
    assert [p for p in problemy if p.kod == NIEPRZENOSNY_SKILL] == []


def test_jedynym_ostrzezeniem_workflow_budowy_jest_pole_optional_bez_konsumenta(tmp_path):
    workflow, rejestr, dom = wczytaj(tmp_path)
    problemy = waliduj_workflow(workflow, rejestr, dom)
    ostrzezenia = {(p.krok, p.kod) for p in problemy if p.poziom == Poziom.ostrzezenie}
    assert ostrzezenia == {("capture-lesson", POLE_BEZ_KONSUMENTA)}


def test_workflow_budowy_uzywa_wszystkich_szesciu_skilli_budowy(tmp_path):
    workflow, _, _ = wczytaj(tmp_path)
    uzyte = {krok.skill for krok in workflow.kroki}
    assert SKILLE_BUDOWY <= uzyte, f"brakuje skilli: {sorted(SKILLE_BUDOWY - uzyte)}"


def test_workflow_budowy_ma_bramke_zatwierdzenia_zaraz_po_planie(tmp_path):
    workflow, _, _ = wczytaj(tmp_path)
    nastepniki = [k for k in workflow.kroki if "plan" in k.needs]
    assert nastepniki, "po kroku plan nie ma zadnego kroku"
    assert [k.id for k in nastepniki if k.approval is not True] == []


def test_krok_planu_nie_czeka_na_zatwierdzenie_bo_bramka_jest_po_nim(tmp_path):
    # Bramka dotyczy przejscia z planu do budowy; sam plan (pierwszy krok) jej nie ma.
    workflow, _, _ = wczytaj(tmp_path)
    krok = krok_ze_skillem(workflow, "plan")
    assert krok.needs == []
    assert krok.approval is False


def test_krok_lekcji_jest_opcjonalny_i_uzywa_skilla_capture_lesson(tmp_path):
    workflow, _, _ = wczytaj(tmp_path)
    krok = krok_ze_skillem(workflow, "capture-lesson")
    assert krok.optional is True


def test_tylko_krok_lekcji_jest_opcjonalny_reszta_budowy_jest_obowiazkowa(tmp_path):
    workflow, _, _ = wczytaj(tmp_path)
    opcjonalne = [k.skill for k in workflow.kroki if k.optional]
    assert opcjonalne == ["capture-lesson"]


def test_skill_lekcji_ma_role_autora_bo_dopisuje_do_pliku_lekcji(tmp_path):
    _, rejestr, _ = wczytaj(tmp_path)
    assert rejestr.skille["capture-lesson"].role == Rola.author


def test_skill_lekcji_wskazuje_plik_i_format_wpisu_z_lekcji_repozytorium(tmp_path):
    _, rejestr, _ = wczytaj(tmp_path)
    brief = rejestr.skille["capture-lesson"].brief
    for fragment in (".ai/lessons.md", "zakres:", "krok=", "ścieżki=", "od:", "L-NNN"):
        assert fragment in brief, f"brief capture-lesson nie wspomina {fragment!r}"


def test_skill_planu_jest_recenzentem_bez_prawa_zapisu_i_tworzy_plan_md(tmp_path):
    _, rejestr, _ = wczytaj(tmp_path)
    plan = rejestr.skille["plan"]
    assert plan.role == Rola.reviewer
    assert "plan.md" in plan.produces


# --- krok diff (HAR-176): dowod dla recenzentow --------------------------------------------

RECENZENCI = ["contract-check", "review-unknown", "explain-change"]
CAPABILITIES_REPO = {
    "test_narrow", "test_full", "test_e2e", "lint", "layers", "typecheck", "contract", "smoke"}


def krok_po_id(workflow: Workflow, id_kroku: str):
    kroki = [k for k in workflow.kroki if k.id == id_kroku]
    assert len(kroki) == 1, f"oczekiwano jednego kroku {id_kroku}: {kroki}"
    return kroki[0]


def test_workflow_budowy_ma_krok_diff_systemowy_bez_skilla_zaraz_po_scenariuszu_e2e(tmp_path):
    workflow, _, _ = wczytaj(tmp_path)
    diff = krok_po_id(workflow, "diff")
    assert (diff.system, diff.skill, diff.needs) == ("diff", None, ["e2e-scenario"])
    assert diff.approval is False
    assert diff.optional is False


def test_krok_diff_jest_jedynym_krokiem_systemowym_budowy(tmp_path):
    workflow, _, _ = wczytaj(tmp_path)
    assert [k.id for k in workflow.kroki if k.system is not None] == ["diff"]


@pytest.mark.parametrize("id_kroku", RECENZENCI)
def test_recenzent_budowy_czeka_na_krok_diff_a_nie_na_scenariusz_e2e(tmp_path, id_kroku):
    workflow, _, _ = wczytaj(tmp_path)
    assert krok_po_id(workflow, id_kroku).needs == ["diff"]


def test_krok_diff_stoi_w_workflow_po_scenariuszu_e2e_i_przed_recenzentami(tmp_path):
    workflow, _, _ = wczytaj(tmp_path)
    kolejnosc = [k.id for k in workflow.kroki]
    assert kolejnosc.index("e2e-scenario") < kolejnosc.index("diff")
    assert all(kolejnosc.index("diff") < kolejnosc.index(r) for r in RECENZENCI)


def test_proba_dymna_nadal_czeka_na_trzech_recenzentow(tmp_path):
    workflow, _, _ = wczytaj(tmp_path)
    assert krok_po_id(workflow, "smoke").needs == RECENZENCI


def test_po_zakonczonym_kroku_diff_startuja_trzej_recenzenci_a_po_nieudanym_sa_pominieci_z_brakiem_wejscia(
    tmp_path,
):
    workflow, rejestr, _ = wczytaj(tmp_path)
    poczatek = {
        k.id: StanKroku.ZAKONCZONY
        for k in workflow.kroki
        if k.id in ("plan", "tdd-task", "e2e-scenario")
    }

    po_sukcesie = nastepne(
        workflow, rejestr, CAPABILITIES_REPO, {**poczatek, "diff": StanKroku.ZAKONCZONY})
    po_porazce = nastepne(
        workflow, rejestr, CAPABILITIES_REPO, {**poczatek, "diff": StanKroku.NIEUDANY})

    assert po_sukcesie.do_startu == RECENZENCI
    assert po_sukcesie.do_pominiecia == {}
    for id_kroku in RECENZENCI:
        assert po_porazce.powody[id_kroku] == PowodPominiecia.BRAK_WEJSCIA
        assert po_porazce.do_pominiecia[id_kroku] == "brak wejścia: zmiana.diff z kroku diff"
    assert po_porazce.do_startu == []


def mutmut_kopiuje(also_copy: list[str], sciezka: str) -> bool:
    """Czy mutmut skopiuje plik w podkatalogu: tylko gdy wpis jest katalogiem (z `/`)-przodkiem.

    Wpis samego pliku w podkatalogu nie wystarcza: mutmut kopiuje go bez katalogow nadrzednych.
    """
    return any(wpis.endswith("/") and sciezka.startswith(wpis) for wpis in also_copy)


def test_mutmut_kopiuje_workflow_bo_test_budowy_czyta_plik_z_repozytorium():
    konfiguracja = tomllib.loads((KORZEN_REPO / "pyproject.toml").read_text("utf-8"))
    assert mutmut_kopiuje(
        konfiguracja["tool"]["mutmut"]["also_copy"], ".cockpit/workflows/budowa.yaml"
    )


def test_wpis_katalogu_nadrzednego_sprawia_ze_mutmut_kopiuje_plik():
    assert mutmut_kopiuje([".cockpit/"], ".cockpit/workflows/budowa.yaml")


def test_wpis_katalogu_workflow_sprawia_ze_mutmut_kopiuje_plik():
    assert mutmut_kopiuje([".cockpit/workflows/"], ".cockpit/workflows/budowa.yaml")


def test_wpis_samego_pliku_w_podkatalogu_nie_wystarcza_mutmutowi_do_kopii():
    assert not mutmut_kopiuje(
        [".cockpit/workflows/budowa.yaml"], ".cockpit/workflows/budowa.yaml"
    )


def test_wpis_katalogu_obcego_rodzenstwa_nie_pokrywa_pliku_workflow():
    assert not mutmut_kopiuje([".cockpit/skills/"], ".cockpit/workflows/budowa.yaml")


def test_wpis_bedacy_tylko_czescia_nazwy_katalogu_nie_pokrywa_pliku():
    assert not mutmut_kopiuje([".cock/"], ".cockpit/workflows/budowa.yaml")
