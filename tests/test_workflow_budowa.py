"""Workflow budowa (HAR-88): kroki wolaja skille budowy, bramka po planie, krok lekcji.

Czytamy prawdziwy plik `.cockpit/workflows/budowa.yaml` i prawdziwy rejestr
tego repozytorium przez produkcyjne `wczytaj_workflow`, `rozstrzygnij_rejestr`
i `waliduj_workflow`. Katalog domowy jest pusty, zeby skille uzytkownika nie
wplywaly na wynik.
"""
import tomllib
from pathlib import Path

from cockpit.services.invariants import Rola
from cockpit.services.skills import Rejestr, rozstrzygnij_rejestr
from cockpit.services.workflow import Poziom, Workflow, ma_bledy, wczytaj_workflow
from cockpit.services.workflow_walidacja import (
    NIEPRZENOSNY_SKILL,
    POLE_BEZ_KONSUMENTA,
    waliduj_workflow,
)

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


def test_mutmut_kopiuje_workflow_bo_test_budowy_czyta_plik_z_repozytorium():
    konfiguracja = tomllib.loads((KORZEN_REPO / "pyproject.toml").read_text("utf-8"))
    assert ".cockpit/workflows/" in konfiguracja["tool"]["mutmut"]["also_copy"]
