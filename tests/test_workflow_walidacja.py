"""Walidator grafu kroków, artefaktów i pętli naprawczej (HAR-8).

Nazwy testów w języku dziedziny, nie technologii. Rejestr z `rozstrzygnij_rejestr`
na tmp_path, workflow przez prawdziwy YAML i `wczytaj_workflow`, bez PostgreSQL.
Kody porównujemy ze stałymi modułu; treść komunikatu tylko tam, gdzie ticket ją
specyfikuje (id, nazwy cyklu, id skilla, artefakt, źródło i cel retry, ostrzeżenie).
"""
from pathlib import Path

import pytest

from cockpit.services.skills import Rejestr, rozstrzygnij_rejestr
from cockpit.services.workflow import Poziom, Problem, Workflow, ma_bledy, wczytaj_workflow
from cockpit.services.workflow_walidacja import (
    BRAK_ARTEFAKTU,
    CONSENSUS_PONAD_FANOUT,
    CYKL,
    FOREACH_BEZ_PRODUCENTA,
    FOREACH_ZLY_FORMAT,
    NIEPRZENOSNY_SKILL,
    NIEZNANA_ZALEZNOSC,
    NIEZNANY_SKILL,
    RETRY_NIE_WSTECZ,
    waliduj_workflow,
)

# --- pomocnicze ---------------------------------------------------------


def napisz_skill(
    zrodlo: Path,
    id: str,
    *,
    consumes: list[str] | None = None,
    produces: list[str] | None = None,
    needs: list[str] | None = None,
    role: str | None = "author",
) -> Path:
    """Tworzy <zrodlo>/<id>/SKILL.md w formacie kokpitu."""
    katalog = zrodlo / id
    katalog.mkdir(parents=True, exist_ok=True)
    linie = [f"id: {id}", f"name: {id}"]
    if role is not None:
        linie.append(f"role: {role}")
    for pole, wartosci in (("consumes", consumes), ("produces", produces), ("needs", needs)):
        if wartosci:
            linie.append(f"{pole}: [{', '.join(wartosci)}]")
    plik = katalog / "SKILL.md"
    plik.write_text("---\n" + "\n".join(linie) + "\n---\nBrief.\n", encoding="utf-8")
    return plik


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    return r


@pytest.fixture
def dom(tmp_path: Path) -> Path:
    d = tmp_path / "dom"
    d.mkdir()
    return d


@pytest.fixture
def skille_repo(repo: Path) -> Path:
    return repo / ".cockpit" / "skills"


def rejestr_z(repo: Path, dom: Path) -> Rejestr:
    return rozstrzygnij_rejestr(repo, dom)


def workflow_z(tmp_path: Path, tresc: str) -> Workflow:
    plik = tmp_path / "workflow.yaml"
    plik.write_text(tresc, encoding="utf-8")
    workflow, problemy = wczytaj_workflow(plik)
    assert workflow is not None and problemy == []
    return workflow


def kody(problemy: list[Problem], krok: str | None = None) -> list[str]:
    return [p.kod for p in problemy if krok is None or p.krok == krok]


def problem(problemy: list[Problem], kod: str, krok: str) -> Problem:
    trafione = [p for p in problemy if p.kod == kod and p.krok == krok]
    assert trafione, f"brak problemu {kod} dla kroku {krok}: {problemy}"
    return trafione[0]


# --- przypadki z ticketu ------------------------------------------------


def test_cykl_w_needs_wskazuje_kroki_tworzace_petle_po_nazwach(tmp_path, repo, dom, skille_repo):
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  plan: {skill: s, needs: [review]}
  review: {skill: s, needs: [plan]}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    for krok in ("plan", "review"):
        p = problem(problemy, CYKL, krok)
        assert "plan" in p.komunikat and "review" in p.komunikat
        assert "→" in p.komunikat
    assert ma_bledy(problemy)


def test_krok_konsumujacy_plan_ktorego_nikt_wczesniej_nie_produkuje_nazywa_brakujacy_artefakt(
    tmp_path, repo, dom, skille_repo
):
    napisz_skill(skille_repo, "pisarz", produces=["raport.md"])
    napisz_skill(skille_repo, "wykonawca", consumes=["plan.md"])
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  pisz: {skill: pisarz}
  wykonaj: {skill: wykonawca, needs: [pisz]}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    assert "plan.md" in problem(problemy, BRAK_ARTEFAKTU, "wykonaj").komunikat
    assert BRAK_ARTEFAKTU not in kody(problemy, "pisz")


def test_odwolanie_do_nieistniejacego_skilla_podaje_jego_id(tmp_path, repo, dom, skille_repo):
    napisz_skill(skille_repo, "istniejacy")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  a: {skill: widmo}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    assert "widmo" in problem(problemy, NIEZNANY_SKILL, "a").komunikat


def test_consensus_trzy_przy_fanout_dwa_wskazuje_ten_krok(tmp_path, repo, dom, skille_repo):
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  ok: {skill: s}
  glosuj: {skill: s, fanout: 2, consensus: 3}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    problem(problemy, CONSENSUS_PONAD_FANOUT, "glosuj")
    assert kody(problemy, "ok") == []


def test_retry_do_kroku_pozniejszego_wskazuje_oba_kroki(tmp_path, repo, dom, skille_repo):
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  plan:
    skill: s
    on_fail: {retry: review, max: 2}
  review: {skill: s, needs: [plan]}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    p = problem(problemy, RETRY_NIE_WSTECZ, "plan")
    assert "plan" in p.komunikat and "review" in p.komunikat
    assert CYKL not in kody(problemy)


def test_retry_do_samego_siebie_wskazuje_ten_krok(tmp_path, repo, dom, skille_repo):
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  naprawa:
    skill: s
    on_fail: {retry: naprawa, max: 1}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    assert "naprawa" in problem(problemy, RETRY_NIE_WSTECZ, "naprawa").komunikat
    assert CYKL not in kody(problemy)


def test_foreach_na_artefakcie_ktorego_nikt_nie_produkuje_jest_bledem(
    tmp_path, repo, dom, skille_repo
):
    napisz_skill(skille_repo, "pisarz", produces=["plan.json"])
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  pisz: {skill: pisarz}
  rozdziel: {skill: s, needs: [pisz], foreach: "zadania.json#lista"}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    problem(problemy, FOREACH_BEZ_PRODUCENTA, "rozdziel")


def test_workflow_z_dwoma_bledami_pokazuje_oba(tmp_path, repo, dom, skille_repo):
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  a: {skill: s, needs: [nikt]}
  b: {skill: s, fanout: 1, consensus: 2}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    problem(problemy, NIEZNANA_ZALEZNOSC, "a")
    problem(problemy, CONSENSUS_PONAD_FANOUT, "b")


def test_skill_z_rejestru_globalnego_daje_ostrzezenie_nie_blad(tmp_path, repo, dom):
    napisz_skill(dom / ".cockpit" / "skills", "globalny")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  a: {skill: globalny}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    p = problem(problemy, NIEPRZENOSNY_SKILL, "a")
    assert p.poziom == Poziom.ostrzezenie
    assert p.komunikat == "nieprzenośny — skill spoza repozytorium: globalny"
    assert not ma_bledy(problemy)


def test_poprawny_workflow_daje_pusta_liste_problemow(tmp_path, repo, dom, skille_repo):
    napisz_skill(skille_repo, "planista", produces=["plan.md", "plan.json"])
    napisz_skill(skille_repo, "wykonawca", consumes=["plan.md"], produces=["kod.diff"])
    napisz_skill(skille_repo, "recenzent", consumes=["kod.diff"], produces=["uwagi.md"])
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  plan: {skill: planista}
  wykonaj: {skill: wykonawca, needs: [plan], foreach: "plan.json#zadania", fanout: 3, consensus: 3}
  review:
    skill: recenzent
    needs: [wykonaj]
    on_fail: {retry: plan, max: 2}
""",
    )
    assert waliduj_workflow(wf, rejestr_z(repo, dom), dom) == []


# --- przypadki brzegowe -------------------------------------------------


def test_zaleznosc_od_nieistniejacego_kroku_nazywa_jej_id(tmp_path, repo, dom, skille_repo):
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  a: {skill: s, needs: [duch]}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    assert "duch" in problem(problemy, NIEZNANA_ZALEZNOSC, "a").komunikat


def test_consensus_bez_fanout_jest_bledem_bo_domyslny_fanout_to_jeden(
    tmp_path, repo, dom, skille_repo
):
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  a: {skill: s, consensus: 2}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    problem(problemy, CONSENSUS_PONAD_FANOUT, "a")


def test_consensus_rowny_fanout_jest_poprawny(tmp_path, repo, dom, skille_repo):
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  a: {skill: s, fanout: 3, consensus: 3}
""",
    )
    assert waliduj_workflow(wf, rejestr_z(repo, dom), dom) == []


def test_foreach_bez_wskazania_pola_ma_zly_format_a_nie_brak_producenta(
    tmp_path, repo, dom, skille_repo
):
    napisz_skill(skille_repo, "pisarz", produces=["plan.json"])
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  pisz: {skill: pisarz}
  rozdziel: {skill: s, needs: [pisz], foreach: plan.json}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    problem(problemy, FOREACH_ZLY_FORMAT, "rozdziel")
    assert FOREACH_BEZ_PRODUCENTA not in kody(problemy, "rozdziel")


def test_foreach_na_artefakcie_od_przodka_przechodniego_jest_poprawny(
    tmp_path, repo, dom, skille_repo
):
    napisz_skill(skille_repo, "pisarz", produces=["plan.json"])
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  pisz: {skill: pisarz}
  posredni: {skill: s, needs: [pisz]}
  rozdziel: {skill: s, needs: [posredni], foreach: "plan.json#zadania"}
""",
    )
    assert waliduj_workflow(wf, rejestr_z(repo, dom), dom) == []


def test_retry_do_nieistniejacego_kroku_wskazuje_zrodlo_i_cel(tmp_path, repo, dom, skille_repo):
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  a: {skill: s}
  b:
    skill: s
    needs: [a]
    on_fail: {retry: zaginiony, max: 1}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    p = problem(problemy, RETRY_NIE_WSTECZ, "b")
    assert "b" in p.komunikat and "zaginiony" in p.komunikat


def test_retry_do_kroku_z_rownoleglej_galezi_jest_bledem_mimo_ze_jest_wczesniej_w_pliku(
    tmp_path, repo, dom, skille_repo
):
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  galaz: {skill: s}
  plan: {skill: s}
  review:
    skill: s
    needs: [plan]
    on_fail: {retry: galaz, max: 1}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    p = problem(problemy, RETRY_NIE_WSTECZ, "review")
    assert "review" in p.komunikat and "galaz" in p.komunikat


def test_retry_do_przodka_przechodniego_jest_poprawny_i_nie_tworzy_cyklu(
    tmp_path, repo, dom, skille_repo
):
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  plan: {skill: s}
  kod: {skill: s, needs: [plan]}
  review:
    skill: s
    needs: [kod]
    on_fail: {retry: plan, max: 3}
""",
    )
    assert waliduj_workflow(wf, rejestr_z(repo, dom), dom) == []


def test_artefakt_od_dziadka_w_needs_wystarcza_krokowi_konsumujacemu(
    tmp_path, repo, dom, skille_repo
):
    napisz_skill(skille_repo, "planista", produces=["plan.md"])
    napisz_skill(skille_repo, "posrednik")
    napisz_skill(skille_repo, "wykonawca", consumes=["plan.md"])
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  plan: {skill: planista}
  posredni: {skill: posrednik, needs: [plan]}
  wykonaj: {skill: wykonawca, needs: [posredni]}
""",
    )
    assert waliduj_workflow(wf, rejestr_z(repo, dom), dom) == []


def test_pierwszy_krok_konsumujacy_cos_z_zewnatrz_tez_dostaje_brak_artefaktu(
    tmp_path, repo, dom, skille_repo
):
    napisz_skill(skille_repo, "wykonawca", consumes=["zadanie.md"])
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  start: {skill: wykonawca}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    assert "zadanie.md" in problem(problemy, BRAK_ARTEFAKTU, "start").komunikat


def test_krok_zalezny_od_cyklu_ale_poza_nim_nie_dostaje_problemu_cyklu(
    tmp_path, repo, dom, skille_repo
):
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  a: {skill: s, needs: [b]}
  b: {skill: s, needs: [a]}
  dalej: {skill: s, needs: [a]}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    problem(problemy, CYKL, "a")
    problem(problemy, CYKL, "b")
    assert CYKL not in kody(problemy, "dalej")


def test_cykl_trzech_krokow_wymienia_wszystkie_trzy_po_nazwach(tmp_path, repo, dom, skille_repo):
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  alfa: {skill: s, needs: [gamma]}
  beta: {skill: s, needs: [alfa]}
  gamma: {skill: s, needs: [beta]}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    for krok in ("alfa", "beta", "gamma"):
        komunikat = problem(problemy, CYKL, krok).komunikat
        assert all(n in komunikat for n in ("alfa", "beta", "gamma"))


def test_krok_zapetlony_sam_na_siebie_jest_cyklem(tmp_path, repo, dom, skille_repo):
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  a: {skill: s, needs: [a]}
""",
    )
    problem(waliduj_workflow(wf, rejestr_z(repo, dom), dom), CYKL, "a")


def test_nieznany_skill_pomija_sprawdzenie_artefaktow_ale_reszta_kroku_jest_sprawdzana(
    tmp_path, repo, dom, skille_repo
):
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  a: {skill: widmo, consensus: 2}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    problem(problemy, NIEZNANY_SKILL, "a")
    problem(problemy, CONSENSUS_PONAD_FANOUT, "a")
    assert BRAK_ARTEFAKTU not in kody(problemy, "a")


def test_skill_pominiety_przy_wczytaniu_podaje_powod_pominiecia_i_id(
    tmp_path, repo, dom, skille_repo
):
    # plik w formacie innych narzędzi, bez pola role: trafia do rejestr.pominiete
    katalog = repo / ".claude" / "skills" / "cudzy"
    katalog.mkdir(parents=True)
    (katalog / "SKILL.md").write_text(
        "---\nname: cudzy\ndescription: Zwykly skill\n---\nOpis.\n", encoding="utf-8"
    )
    rejestr = rejestr_z(repo, dom)
    assert [p.plik.parent.name for p in rejestr.pominiete] == ["cudzy"]
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  a: {skill: cudzy}
""",
    )
    problemy = waliduj_workflow(wf, rejestr, dom)
    komunikat = problem(problemy, NIEZNANY_SKILL, "a").komunikat
    assert "cudzy" in komunikat
    assert "brak pola role" in komunikat


def test_krok_z_niepoprawnymi_polami_istnieje_jako_cel_zaleznosci(tmp_path, repo, dom, skille_repo):
    napisz_skill(skille_repo, "s")
    plik = tmp_path / "workflow.yaml"
    plik.write_text(
        """
name: w
steps:
  zepsuty: {skill: widmo, fanout: "3"}
  dalej: {skill: s, needs: [zepsuty]}
""",
        encoding="utf-8",
    )
    wf, problemy_wczytania = wczytaj_workflow(plik)
    # wczytanie samo zgłasza błąd pola kroku (HAR-7); tu jest to oczekiwane
    assert wf is not None
    assert [(p.krok, p.kod) for p in problemy_wczytania] == [("zepsuty", "blad_pola")]
    assert "zepsuty" in wf.niepoprawne
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    assert NIEZNANA_ZALEZNOSC not in kody(problemy)
    # pola kroku, który nie przeszedł walidacji typów, nie są sprawdzane
    assert kody(problemy, "zepsuty") == []


def test_needs_samego_skilla_nie_jest_zaleznoscia_kroku(tmp_path, repo, dom, skille_repo):
    napisz_skill(skille_repo, "pisarz", produces=["plan.md"])
    napisz_skill(skille_repo, "wykonawca", consumes=["plan.md"], needs=["pisarz"])
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  pisz: {skill: pisarz}
  wykonaj: {skill: wykonawca}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    # needs skilla nie łączy kroków: artefakt nie dociera do kroku bez własnego needs
    problem(problemy, BRAK_ARTEFAKTU, "wykonaj")


def test_skill_z_repozytorium_nie_daje_ostrzezenia_o_przenosnosci(
    tmp_path, repo, dom, skille_repo
):
    napisz_skill(skille_repo, "lokalny")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  a: {skill: lokalny}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    assert NIEPRZENOSNY_SKILL not in kody(problemy)


# --- reguły ograniczone zakresem (HAR-8, iteracja 3) ---------------------


def test_cykl_wymienia_tylko_kroki_petli_a_nie_wszystkich_przodkow(
    tmp_path, repo, dom, skille_repo
):
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  a: {skill: s, needs: [b, x]}
  b: {skill: s, needs: [a]}
  x: {skill: s}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    for krok in ("a", "b"):
        komunikat = problem(problemy, CYKL, krok).komunikat
        nazwy = komunikat.split(": ", 1)[1].split(" → ")
        assert nazwy in (["a", "b", "a"], ["b", "a", "b"])
        assert "x" not in nazwy
    assert CYKL not in kody(problemy, "x")


def test_foreach_na_artefakcie_z_rownoleglej_galezi_nie_ma_producenta(
    tmp_path, repo, dom, skille_repo
):
    napisz_skill(skille_repo, "pisarz", produces=["plan.json"])
    napisz_skill(skille_repo, "s")
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  galaz: {skill: pisarz}
  rozdziel: {skill: s, foreach: "plan.json#zadania"}
""",
    )
    problemy = waliduj_workflow(wf, rejestr_z(repo, dom), dom)
    problem(problemy, FOREACH_BEZ_PRODUCENTA, "rozdziel")
    assert FOREACH_BEZ_PRODUCENTA not in kody(problemy, "galaz")


@pytest.mark.parametrize("rejestr_przez_dowiazanie", [True, False])
def test_skill_globalny_jest_nieprzenosny_gdy_dom_i_rejestr_uzywaja_roznych_form_sciezki(
    tmp_path, repo, rejestr_przez_dowiazanie
):
    prawdziwy = tmp_path / "prawdziwy_dom"
    napisz_skill(prawdziwy / ".cockpit" / "skills", "globalny")
    dowiazanie = tmp_path / "dom_link"
    dowiazanie.symlink_to(prawdziwy, target_is_directory=True)
    wf = workflow_z(
        tmp_path,
        """
name: w
steps:
  a: {skill: globalny}
""",
    )
    dom_rejestru, dom_walidacji = (
        (dowiazanie, prawdziwy) if rejestr_przez_dowiazanie else (prawdziwy, dowiazanie)
    )
    rejestr = rozstrzygnij_rejestr(repo, dom_rejestru)
    problemy = waliduj_workflow(wf, rejestr, dom_walidacji)
    assert problem(problemy, NIEPRZENOSNY_SKILL, "a").poziom == Poziom.ostrzezenie
