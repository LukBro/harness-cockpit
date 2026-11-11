"""Plan wykonania grafu kroków: needs, pominięcia, porażka gałęzi (HAR-43).

Nazwy testów w języku dziedziny, nie technologii. Workflow z prawdziwego YAML
(`wczytaj_workflow`), rejestr z `rozstrzygnij_rejestr` na tmp_path, bez PostgreSQL.
Asercje porównują cały `Plan` albo całe listy; teksty powodów dosłownie.
"""
from pathlib import Path

import pytest

from cockpit.services.skills import Rejestr, rozstrzygnij_rejestr
from cockpit.services.workflow import Workflow, wczytaj_workflow
from runner.graf import (
    Plan,
    PowodPominiecia,
    Podsumowanie,
    StanKroku,
    nastepne,
    podsumowanie,
)

OCZEKUJE = StanKroku.OCZEKUJE
W_TOKU = StanKroku.W_TOKU
ZAKONCZONY = StanKroku.ZAKONCZONY
NIEUDANY = StanKroku.NIEUDANY
POMINIETY = StanKroku.POMINIETY

BRAK_CAP = PowodPominiecia.BRAK_CAPABILITY
BRAK_WEJSCIA = PowodPominiecia.BRAK_WEJSCIA
ZALEZNOSC = PowodPominiecia.ZALEZNOSC_NIEUDANA

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


def swiat(tmp_path: Path, kroki: list[tuple]) -> tuple[Workflow, Rejestr]:
    """Buduje workflow i rejestr. Krok: (id, needs_krokow) albo (id, needs_krokow, opcje_skilla).

    Opcje skilla: consumes, produces, needs (capabilities). Skill każdego kroku
    nazywa się `s_<id>`. Kolejność kroków w YAML = kolejność w `workflow.kroki`.
    Opcje `{"system": "diff"}` robią z kroku krok systemowy (HAR-176): bez skilla; `{"system":
    "check", "capability": "typecheck"}` to krok sprawdzający z komendą capability (HAR-177).
    """
    repo = tmp_path / "repo"
    dom = tmp_path / "dom"
    repo.mkdir()
    dom.mkdir()
    skille = repo / ".cockpit" / "skills"
    linie = ["name: w", "steps:"]
    for wpis in kroki:
        id, needs = wpis[0], wpis[1]
        opcje = wpis[2] if len(wpis) > 2 else {}
        zaleznosci = f", needs: [{', '.join(needs)}]" if needs else ""
        if "system" in opcje:
            capability = f", capability: {opcje['capability']}" if "capability" in opcje else ""
            linie.append(f"  {id}: {{system: {opcje['system']}{capability}{zaleznosci}}}")
            continue
        napisz_skill(skille, f"s_{id}", **opcje)
        linie.append(f"  {id}: {{skill: s_{id}{zaleznosci}}}")
    plik = tmp_path / "workflow.yaml"
    plik.write_text("\n".join(linie) + "\n", encoding="utf-8")
    workflow, problemy = wczytaj_workflow(plik)
    assert workflow is not None and problemy == []
    return workflow, rozstrzygnij_rejestr(repo, dom)


def plan(tmp_path, kroki, stany, capabilities=()):
    workflow, rejestr = swiat(tmp_path, kroki)
    return nastepne(workflow, rejestr, capabilities, stany)


PUSTY = Plan(do_startu=[], do_pominiecia={}, powody={})

# --- start po zakończeniu zależności ------------------------------------

ABC = [("a", []), ("b", []), ("c", ["a", "b"])]


def test_pierwszy_plan_startuje_kroki_bez_zaleznosci_a_krok_zalezny_od_obu_czeka(tmp_path):
    assert plan(tmp_path, ABC, {}) == Plan(do_startu=["a", "b"], do_pominiecia={}, powody={})


def test_krok_zalezny_od_dwoch_nie_startuje_gdy_jedna_zaleznosc_jest_w_toku(tmp_path):
    assert plan(tmp_path, ABC, {"a": ZAKONCZONY, "b": W_TOKU}) == PUSTY


def test_krok_zalezny_od_dwoch_startuje_gdy_obie_zaleznosci_sa_zakonczone(tmp_path):
    stany = {"a": ZAKONCZONY, "b": ZAKONCZONY}
    assert plan(tmp_path, ABC, stany) == Plan(do_startu=["c"], do_pominiecia={}, powody={})


@pytest.mark.parametrize("stan_zaleznosci", [W_TOKU, OCZEKUJE])
def test_krok_czeka_gdy_zaleznosc_nie_jest_jeszcze_zakonczona(tmp_path, stan_zaleznosci):
    kroki = [("x", []), ("a", ["x"]), ("b", ["a"])]
    stany = {"x": W_TOKU, "a": stan_zaleznosci}
    assert plan(tmp_path, kroki, stany) == PUSTY


def test_pominieta_zaleznosc_odblokowuje_krok_ktory_nic_z_niej_nie_konsumuje(tmp_path):
    kroki = [("a", []), ("b", ["a"])]
    stany = {"a": POMINIETY}
    assert plan(tmp_path, kroki, stany) == Plan(do_startu=["b"], do_pominiecia={}, powody={})


def test_krok_bez_wpisu_w_stanach_jest_traktowany_jak_oczekujacy(tmp_path):
    kroki = [("a", []), ("b", ["a"])]
    assert plan(tmp_path, kroki, {"a": ZAKONCZONY}) == Plan(
        do_startu=["b"], do_pominiecia={}, powody={}
    )


@pytest.mark.parametrize("stan", [ZAKONCZONY, W_TOKU, NIEUDANY, POMINIETY])
def test_krok_ktory_juz_ruszyl_albo_sie_skonczyl_nie_wraca_do_startu(tmp_path, stan):
    assert plan(tmp_path, [("a", [])], {"a": stan}) == PUSTY


@pytest.mark.parametrize("stan", [ZAKONCZONY, W_TOKU, NIEUDANY, POMINIETY])
def test_krok_ktory_juz_ruszyl_albo_sie_skonczyl_nie_jest_pomijany_ponownie(tmp_path, stan):
    kroki = [("a", [], {"needs": ["gpu"]})]
    assert plan(tmp_path, kroki, {"a": stan}) == PUSTY



def test_krok_za_pominietym_krokiem_startuje_mimo_ze_dalszy_przodek_wciaz_trwa(tmp_path):
    kroki = [("x", []), ("a", ["x"], {"needs": ["gpu"]}), ("b", ["a"])]
    assert plan(tmp_path, kroki, {"x": W_TOKU}) == Plan(
        do_startu=["b"],
        do_pominiecia={"a": "brak capability gpu"},
        powody={"a": BRAK_CAP},
    )


def test_krok_za_zakonczonym_krokiem_startuje_mimo_ze_dalszy_przodek_wciaz_trwa(tmp_path):
    kroki = [("x", []), ("a", ["x"]), ("b", ["a"])]
    assert plan(tmp_path, kroki, {"x": W_TOKU, "a": ZAKONCZONY}) == Plan(
        do_startu=["b"], do_pominiecia={}, powody={}
    )


# --- zależność nieudana --------------------------------------------------


def test_krok_po_porazce_bezposredniej_zaleznosci_jest_pominiety_z_jej_id(tmp_path):
    kroki = [("a", []), ("b", ["a"])]
    assert plan(tmp_path, kroki, {"a": NIEUDANY}) == Plan(
        do_startu=[],
        do_pominiecia={"b": "zależność nieudana: a"},
        powody={"b": ZALEZNOSC},
    )


def test_porazka_przodka_pomija_caly_lancuch_zaleznych_choc_nikt_nie_konsumuje_artefaktow(
    tmp_path,
):
    kroki = [("a", []), ("b", ["a"]), ("c", ["b"])]
    assert plan(tmp_path, kroki, {"a": NIEUDANY}) == Plan(
        do_startu=[],
        do_pominiecia={"b": "zależność nieudana: a", "c": "zależność nieudana: a"},
        powody={"b": ZALEZNOSC, "c": ZALEZNOSC},
    )


def test_krok_po_porazce_przodka_jest_pominiety_nawet_gdy_posredni_krok_jest_juz_pominiety(
    tmp_path,
):
    kroki = [("a", []), ("b", ["a"]), ("c", ["b"])]
    stany = {"a": NIEUDANY, "b": POMINIETY}
    assert plan(tmp_path, kroki, stany) == Plan(
        do_startu=[],
        do_pominiecia={"c": "zależność nieudana: a"},
        powody={"c": ZALEZNOSC},
    )


def test_porazka_galezi_nie_blokuje_niezaleznej_galezi(tmp_path):
    kroki = [("a", []), ("b", []), ("c", ["a"]), ("d", ["b"])]
    assert plan(tmp_path, kroki, {"a": NIEUDANY, "b": ZAKONCZONY}) == Plan(
        do_startu=["d"],
        do_pominiecia={"c": "zależność nieudana: a"},
        powody={"c": ZALEZNOSC},
    )


def test_nieudany_krok_ktory_nie_jest_przodkiem_nie_wplywa_na_inny_krok(tmp_path):
    kroki = [("a", []), ("b", []), ("c", ["b"])]
    assert plan(tmp_path, kroki, {"a": NIEUDANY, "b": ZAKONCZONY}) == Plan(
        do_startu=["c"], do_pominiecia={}, powody={}
    )


def test_przy_dwoch_nieudanych_przodkach_wskazany_jest_pierwszy_w_kolejnosci_workflow(tmp_path):
    kroki = [("x", []), ("y", []), ("z", ["y", "x"])]
    stany = {"y": NIEUDANY, "x": NIEUDANY}
    assert plan(tmp_path, kroki, stany) == Plan(
        do_startu=[],
        do_pominiecia={"z": "zależność nieudana: x"},
        powody={"z": ZALEZNOSC},
    )


def test_porazka_pomija_krok_od_razu_nie_czekajac_na_pozostale_zaleznosci(tmp_path):
    kroki = [("a", []), ("x", []), ("c", ["a", "x"])]
    assert plan(tmp_path, kroki, {"a": NIEUDANY, "x": W_TOKU}) == Plan(
        do_startu=[],
        do_pominiecia={"c": "zależność nieudana: a"},
        powody={"c": ZALEZNOSC},
    )


# --- brak wejścia --------------------------------------------------------


def test_krok_konsumujacy_artefakt_kroku_pominietego_jest_pominiety_a_nie_nieudany(tmp_path):
    kroki = [
        ("a", [], {"produces": ["plan.md"]}),
        ("b", ["a"], {"consumes": ["plan.md"]}),
    ]
    assert plan(tmp_path, kroki, {"a": POMINIETY}) == Plan(
        do_startu=[],
        do_pominiecia={"b": "brak wejścia: plan.md z kroku a"},
        powody={"b": BRAK_WEJSCIA},
    )


def test_pominiety_krok_nie_pomija_nastepnika_ktory_nie_konsumuje_jego_artefaktu(tmp_path):
    kroki = [
        ("a", [], {"produces": ["plan.md"]}),
        ("b", ["a"], {"consumes": ["inny.md"]}),
        ("c", ["a"]),
    ]
    assert plan(tmp_path, kroki, {"a": POMINIETY}) == Plan(
        do_startu=["b", "c"], do_pominiecia={}, powody={}
    )


def test_artefakt_pominietego_przodka_przechodniego_tez_pomija_konsumenta(tmp_path):
    kroki = [
        ("a", [], {"produces": ["plan.md"]}),
        ("m", ["a"]),
        ("b", ["m"], {"consumes": ["plan.md"]}),
    ]
    stany = {"a": POMINIETY, "m": ZAKONCZONY}
    assert plan(tmp_path, kroki, stany) == Plan(
        do_startu=[],
        do_pominiecia={"b": "brak wejścia: plan.md z kroku a"},
        powody={"b": BRAK_WEJSCIA},
    )


def test_pominiety_producent_spoza_przodkow_nie_pomija_konsumenta(tmp_path):
    kroki = [
        ("q", [], {"produces": ["plan.md"]}),
        ("b", [], {"consumes": ["plan.md"]}),
    ]
    assert plan(tmp_path, kroki, {"q": POMINIETY}) == Plan(
        do_startu=["b"], do_pominiecia={}, powody={}
    )


def test_pominiety_producent_przodek_pomija_konsumenta_mimo_zakonczonego_producenta_spoza_przodkow(
    tmp_path,
):
    kroki = [
        ("p", [], {"produces": ["plan.md"]}),
        ("q", [], {"produces": ["plan.md"]}),
        ("b", ["p"], {"consumes": ["plan.md"]}),
    ]
    stany = {"p": POMINIETY, "q": ZAKONCZONY}
    assert plan(tmp_path, kroki, stany) == Plan(
        do_startu=[],
        do_pominiecia={"b": "brak wejścia: plan.md z kroku p"},
        powody={"b": BRAK_WEJSCIA},
    )


def test_konsument_startuje_gdy_choc_jeden_z_producentow_artefaktu_dostarczyl_wynik(tmp_path):
    kroki = [
        ("p1", [], {"produces": ["plan.md"]}),
        ("p2", [], {"produces": ["plan.md"]}),
        ("b", ["p1", "p2"], {"consumes": ["plan.md"]}),
    ]
    stany = {"p1": POMINIETY, "p2": ZAKONCZONY}
    assert plan(tmp_path, kroki, stany) == Plan(do_startu=["b"], do_pominiecia={}, powody={})


def test_konsument_jest_pominiety_gdy_wszyscy_producenci_pominieci_z_pierwszym_w_kolejnosci_workflow(
    tmp_path,
):
    kroki = [
        ("p1", [], {"produces": ["plan.md"]}),
        ("p2", [], {"produces": ["plan.md"]}),
        ("b", ["p2", "p1"], {"consumes": ["plan.md"]}),
    ]
    stany = {"p2": POMINIETY, "p1": POMINIETY}
    assert plan(tmp_path, kroki, stany) == Plan(
        do_startu=[],
        do_pominiecia={"b": "brak wejścia: plan.md z kroku p1"},
        powody={"b": BRAK_WEJSCIA},
    )


def test_przy_dwoch_utraconych_artefaktach_powod_wskazuje_pierwszy_z_listy_konsumowanych(
    tmp_path,
):
    kroki = [
        ("p", [], {"produces": ["a.md", "b.md"]}),
        ("k", ["p"], {"consumes": ["b.md", "a.md"]}),
    ]
    assert plan(tmp_path, kroki, {"p": POMINIETY}) == Plan(
        do_startu=[],
        do_pominiecia={"k": "brak wejścia: b.md z kroku p"},
        powody={"k": BRAK_WEJSCIA},
    )


def test_konsument_jednego_artefaktu_startuje_gdy_pominiety_przodek_produkuje_tylko_inny_artefakt(
    tmp_path,
):
    kroki = [
        ("p", [], {"produces": ["a.md"]}),
        ("q", [], {"produces": ["b.md"]}),
        ("k", ["p", "q"], {"consumes": ["a.md"]}),
    ]
    stany = {"p": ZAKONCZONY, "q": POMINIETY}
    assert plan(tmp_path, kroki, stany) == Plan(do_startu=["k"], do_pominiecia={}, powody={})


# --- brak capability ------------------------------------------------------


def test_krok_wymagajacy_brakujacej_capability_jest_pominiety_z_jej_nazwa(tmp_path):
    kroki = [("a", [], {"needs": ["docker"]})]
    assert plan(tmp_path, kroki, {}, capabilities={"git"}) == Plan(
        do_startu=[],
        do_pominiecia={"a": "brak capability docker"},
        powody={"a": BRAK_CAP},
    )


def test_krok_startuje_gdy_wszystkie_wymagane_capabilities_sa_dostepne(tmp_path):
    kroki = [("a", [], {"needs": ["docker", "gpu"]})]
    assert plan(tmp_path, kroki, {}, capabilities=["gpu", "docker", "git"]) == Plan(
        do_startu=["a"], do_pominiecia={}, powody={}
    )


def test_obecna_capability_nie_ratuje_kroku_gdy_druga_wymagana_brakuje(tmp_path):
    kroki = [("a", [], {"needs": ["docker", "gpu"]})]
    assert plan(tmp_path, kroki, {}, capabilities={"docker"}) == Plan(
        do_startu=[],
        do_pominiecia={"a": "brak capability gpu"},
        powody={"a": BRAK_CAP},
    )


def test_przy_kilku_brakujacych_capabilities_powod_wymienia_pierwsza_z_listy_skilla(tmp_path):
    kroki = [("a", [], {"needs": ["gpu", "docker"]})]
    assert plan(tmp_path, kroki, {}, capabilities=set()) == Plan(
        do_startu=[],
        do_pominiecia={"a": "brak capability gpu"},
        powody={"a": BRAK_CAP},
    )


def test_krok_po_pominietym_przez_brak_capability_startuje_gdy_nie_konsumuje_jego_artefaktu(
    tmp_path,
):
    kroki = [
        ("a", [], {"needs": ["gpu"], "produces": ["wynik.md"]}),
        ("b", ["a"]),
    ]
    assert plan(tmp_path, kroki, {}) == Plan(
        do_startu=["b"],
        do_pominiecia={"a": "brak capability gpu"},
        powody={"a": BRAK_CAP},
    )


def test_brak_capability_pomija_krok_od_razu_nie_czekajac_na_jego_zaleznosci(tmp_path):
    kroki = [("x", []), ("a", ["x"], {"needs": ["gpu"]})]
    assert plan(tmp_path, kroki, {"x": W_TOKU}) == Plan(
        do_startu=[],
        do_pominiecia={"a": "brak capability gpu"},
        powody={"a": BRAK_CAP},
    )


# --- pierwszeństwo reguł --------------------------------------------------


def test_brak_wejscia_wygrywa_z_porazka_przodka_ktory_nie_produkuje_artefaktu(tmp_path):
    # HAR-176: odwrocona kolejnosc - utracone wejscie jest wazniejsza przyczyna niz obcy nieudany przodek
    kroki = [
        ("a", []),
        ("p", [], {"produces": ["plan.md"]}),
        ("b", ["a", "p"], {"consumes": ["plan.md"]}),
    ]
    stany = {"a": NIEUDANY, "p": POMINIETY}
    assert plan(tmp_path, kroki, stany) == Plan(
        do_startu=[],
        do_pominiecia={"b": "brak wejścia: plan.md z kroku p"},
        powody={"b": BRAK_WEJSCIA},
    )


def test_porazka_przodka_pomija_krok_gdy_konsumowany_artefakt_dotarl(tmp_path):
    kroki = [
        ("a", []),
        ("p", [], {"produces": ["plan.md"]}),
        ("b", ["a", "p"], {"consumes": ["plan.md"]}),
    ]
    stany = {"a": NIEUDANY, "p": ZAKONCZONY}
    assert plan(tmp_path, kroki, stany) == Plan(
        do_startu=[],
        do_pominiecia={"b": "zależność nieudana: a"},
        powody={"b": ZALEZNOSC},
    )


def test_brak_wejscia_wygrywa_z_brakiem_capability(tmp_path):
    kroki = [
        ("p", [], {"produces": ["plan.md"]}),
        ("b", ["p"], {"consumes": ["plan.md"], "needs": ["gpu"]}),
    ]
    assert plan(tmp_path, kroki, {"p": POMINIETY}) == Plan(
        do_startu=[],
        do_pominiecia={"b": "brak wejścia: plan.md z kroku p"},
        powody={"b": BRAK_WEJSCIA},
    )


def test_porazka_przodka_wygrywa_z_brakiem_capability(tmp_path):
    kroki = [("a", []), ("b", ["a"], {"needs": ["gpu"]})]
    assert plan(tmp_path, kroki, {"a": NIEUDANY}) == Plan(
        do_startu=[],
        do_pominiecia={"b": "zależność nieudana: a"},
        powody={"b": ZALEZNOSC},
    )


# --- nieudany producent odbiera wejście (HAR-176) ---------------------------


def test_krok_konsumujacy_artefakt_nieudanego_producenta_jest_pominiety_z_brakiem_wejscia(tmp_path):
    kroki = [
        ("a", [], {"produces": ["plan.md"]}),
        ("b", ["a"], {"consumes": ["plan.md"]}),
    ]
    assert plan(tmp_path, kroki, {"a": NIEUDANY}) == Plan(
        do_startu=[],
        do_pominiecia={"b": "brak wejścia: plan.md z kroku a"},
        powody={"b": BRAK_WEJSCIA},
    )


def test_nieudany_producent_innego_artefaktu_daje_zaleznosc_nieudana_a_nie_brak_wejscia(tmp_path):
    kroki = [
        ("a", [], {"produces": ["plan.md"]}),
        ("z", [], {"produces": ["inny.md"]}),
        ("b", ["a", "z"], {"consumes": ["inny.md"]}),
    ]
    assert plan(tmp_path, kroki, {"a": NIEUDANY, "z": ZAKONCZONY}) == Plan(
        do_startu=[],
        do_pominiecia={"b": "zależność nieudana: a"},
        powody={"b": ZALEZNOSC},
    )


def test_nieudany_producent_nie_odbiera_wejscia_gdy_drugi_producent_dostarczyl_artefakt(tmp_path):
    kroki = [
        ("p1", [], {"produces": ["plan.md"]}),
        ("p2", [], {"produces": ["plan.md"]}),
        ("b", ["p1", "p2"], {"consumes": ["plan.md"]}),
    ]
    stany = {"p1": NIEUDANY, "p2": ZAKONCZONY}
    # artefakt dotarl od p2, wiec to nie brak wejscia; krok i tak ma nieudanego przodka
    assert plan(tmp_path, kroki, stany) == Plan(
        do_startu=[],
        do_pominiecia={"b": "zależność nieudana: p1"},
        powody={"b": ZALEZNOSC},
    )


def test_nieudany_producent_spoza_przodkow_nie_odbiera_wejscia_konsumentowi(tmp_path):
    kroki = [
        ("q", [], {"produces": ["plan.md"]}),
        ("b", [], {"consumes": ["plan.md"]}),
    ]
    assert plan(tmp_path, kroki, {"q": NIEUDANY}) == Plan(
        do_startu=["b"], do_pominiecia={}, powody={}
    )


def test_artefakt_traci_konsument_gdy_producenci_sa_czesciowo_nieudani_a_czesciowo_pominieci(
    tmp_path,
):
    kroki = [
        ("p2", [], {"produces": ["plan.md"]}),
        ("p1", [], {"produces": ["plan.md"]}),
        ("b", ["p1", "p2"], {"consumes": ["plan.md"]}),
    ]
    stany = {"p2": NIEUDANY, "p1": POMINIETY}
    assert plan(tmp_path, kroki, stany) == Plan(
        do_startu=[],
        do_pominiecia={"b": "brak wejścia: plan.md z kroku p2"},
        powody={"b": BRAK_WEJSCIA},
    )


def test_nieudany_producent_przodek_przechodni_odbiera_wejscie_konsumentowi(tmp_path):
    kroki = [
        ("a", [], {"produces": ["plan.md"]}),
        ("m", ["a"]),
        ("b", ["m"], {"consumes": ["plan.md"]}),
    ]
    assert plan(tmp_path, kroki, {"a": NIEUDANY, "m": POMINIETY}) == Plan(
        do_startu=[],
        do_pominiecia={"b": "brak wejścia: plan.md z kroku a"},
        powody={"b": BRAK_WEJSCIA},
    )


# --- krok systemowy diff (HAR-176) ---------------------------------------------

DIFF_I_RECENZENT = [
    ("autor", []),
    ("diff", ["autor"], {"system": "diff"}),
    ("rec", ["diff"], {"consumes": ["zmiana.diff"]}),
]


def test_krok_systemowy_diff_jest_planowany_jak_kazdy_krok_po_swoich_zaleznosciach(tmp_path):
    # swiat() tworzy katalogi w podanej sciezce: kazde wywolanie ma wlasny podkatalog
    for podkatalog in ("a", "b", "c"):
        (tmp_path / podkatalog).mkdir()
    assert plan(tmp_path / "a", DIFF_I_RECENZENT, {}) == Plan(
        do_startu=["autor"], do_pominiecia={}, powody={})
    assert plan(tmp_path / "b", DIFF_I_RECENZENT, {"autor": ZAKONCZONY}) == Plan(
        do_startu=["diff"], do_pominiecia={}, powody={})
    assert plan(
        tmp_path / "c", DIFF_I_RECENZENT, {"autor": ZAKONCZONY, "diff": ZAKONCZONY}
    ) == Plan(do_startu=["rec"], do_pominiecia={}, powody={})


def test_krok_systemowy_diff_nie_wymaga_capabilities_runnera(tmp_path):
    assert plan(tmp_path, DIFF_I_RECENZENT, {"autor": ZAKONCZONY}, capabilities=set()) == Plan(
        do_startu=["diff"], do_pominiecia={}, powody={})


def test_nieudany_krok_diff_odbiera_recenzentowi_wejscie_zmiana_diff(tmp_path):
    assert plan(tmp_path, DIFF_I_RECENZENT, {"autor": ZAKONCZONY, "diff": NIEUDANY}) == Plan(
        do_startu=[],
        do_pominiecia={"rec": "brak wejścia: zmiana.diff z kroku diff"},
        powody={"rec": BRAK_WEJSCIA},
    )


def test_pominiety_krok_diff_tez_odbiera_recenzentowi_wejscie_zmiana_diff(tmp_path):
    assert plan(tmp_path, DIFF_I_RECENZENT, {"autor": ZAKONCZONY, "diff": POMINIETY}) == Plan(
        do_startu=[],
        do_pominiecia={"rec": "brak wejścia: zmiana.diff z kroku diff"},
        powody={"rec": BRAK_WEJSCIA},
    )


def test_nieudany_krok_diff_odbiera_tez_drugi_artefakt_zmienione_linie_json(tmp_path):
    kroki = [
        ("diff", [], {"system": "diff"}),
        ("opis", ["diff"], {"consumes": ["zmienione-linie.json"]}),
    ]
    assert plan(tmp_path, kroki, {"diff": NIEUDANY}) == Plan(
        do_startu=[],
        do_pominiecia={"opis": "brak wejścia: zmienione-linie.json z kroku diff"},
        powody={"opis": BRAK_WEJSCIA},
    )


def test_nieudany_krok_diff_pomija_recenzenta_niekonsumujacego_diffu_jako_zaleznosc_nieudana(
    tmp_path,
):
    kroki = [
        ("autor", []),
        ("diff", ["autor"], {"system": "diff"}),
        ("rec", ["diff"]),
    ]
    assert plan(tmp_path, kroki, {"autor": ZAKONCZONY, "diff": NIEUDANY}) == Plan(
        do_startu=[],
        do_pominiecia={"rec": "zależność nieudana: diff"},
        powody={"rec": ZALEZNOSC},
    )


def test_nieudany_autor_pomija_krok_diff_jako_zaleznosc_nieudana_a_recenzent_traci_wejscie(
    tmp_path,
):
    wynik = plan(tmp_path, DIFF_I_RECENZENT, {"autor": NIEUDANY})

    assert wynik == Plan(
        do_startu=[],
        do_pominiecia={
            "diff": "zależność nieudana: autor",
            "rec": "brak wejścia: zmiana.diff z kroku diff",
        },
        powody={"diff": ZALEZNOSC, "rec": BRAK_WEJSCIA},
    )


# --- krok systemowy check (HAR-177) ----------------------------------------------

DOWOD_TYPY = "dowod-typecheck.json"
DOWOD_KONTRAKT = "dowod-contract.json"

CHECK_I_RECENZENT = [
    ("diff", [], {"system": "diff"}),
    ("typy", ["diff"], {"system": "check", "capability": "typecheck"}),
    ("rec", ["typy"], {"consumes": [DOWOD_TYPY]}),
]

DWA_CHECKI_I_RECENZENT = [
    ("diff", [], {"system": "diff"}),
    ("typy", ["diff"], {"system": "check", "capability": "typecheck"}),
    ("kontrakt", ["diff"], {"system": "check", "capability": "contract"}),
    ("rec", ["diff", "typy", "kontrakt"], {"consumes": ["zmiana.diff", DOWOD_TYPY, DOWOD_KONTRAKT]}),
]


def test_krok_check_jest_planowany_po_swoich_zaleznosciach_gdy_projekt_ma_jego_capability(tmp_path):
    assert plan(tmp_path, CHECK_I_RECENZENT, {"diff": ZAKONCZONY}, {"typecheck"}) == Plan(
        do_startu=["typy"], do_pominiecia={}, powody={})


def test_krok_check_bez_capability_w_projekcie_jest_pominiety_z_powodem_brak_capability(tmp_path):
    wynik = plan(tmp_path, CHECK_I_RECENZENT, {"diff": ZAKONCZONY}, capabilities={"contract"})

    assert wynik.do_pominiecia["typy"] == "brak capability typecheck"
    assert wynik.powody["typy"] == BRAK_CAP


def test_krok_check_wymaga_dokladnie_swojej_capability_a_nie_dowolnej(tmp_path):
    wynik = plan(tmp_path, DWA_CHECKI_I_RECENZENT, {"diff": ZAKONCZONY}, capabilities={"typecheck"})

    assert wynik.do_startu == ["typy"]
    assert wynik.do_pominiecia["kontrakt"] == "brak capability contract"
    assert wynik.powody["kontrakt"] == BRAK_CAP
    assert "typy" not in wynik.do_pominiecia


def test_nieudany_krok_check_pomija_konsumenta_dowodu_jako_zaleznosc_nieudana(tmp_path):
    wynik = plan(
        tmp_path, CHECK_I_RECENZENT, {"diff": ZAKONCZONY, "typy": NIEUDANY}, {"typecheck"})

    assert wynik == Plan(
        do_startu=[],
        do_pominiecia={"rec": "zależność nieudana: typy"},
        powody={"rec": ZALEZNOSC},
    )


def test_pominiety_krok_check_odbiera_konsumentowi_wejscie_dowod_jako_brak_wejscia(tmp_path):
    wynik = plan(
        tmp_path, CHECK_I_RECENZENT, {"diff": ZAKONCZONY, "typy": POMINIETY}, {"typecheck"})

    assert wynik == Plan(
        do_startu=[],
        do_pominiecia={"rec": "brak wejścia: dowod-typecheck.json z kroku typy"},
        powody={"rec": BRAK_WEJSCIA},
    )


def test_krok_check_pominiety_z_braku_capability_odbiera_konsumentowi_wejscie_w_jednym_planie(tmp_path):
    wynik = plan(tmp_path, CHECK_I_RECENZENT, {"diff": ZAKONCZONY}, capabilities=set())

    assert wynik == Plan(
        do_startu=[],
        do_pominiecia={
            "typy": "brak capability typecheck",
            "rec": "brak wejścia: dowod-typecheck.json z kroku typy",
        },
        powody={"typy": BRAK_CAP, "rec": BRAK_WEJSCIA},
    )


def test_nieudany_krok_diff_nadal_odbiera_konsumentowi_diffu_wejscie_gdy_check_istnieje(tmp_path):
    kroki = [
        ("diff", [], {"system": "diff"}),
        ("typy", ["diff"], {"system": "check", "capability": "typecheck"}),
        ("rec", ["diff"], {"consumes": ["zmiana.diff"]}),
    ]

    wynik = plan(tmp_path, kroki, {"diff": NIEUDANY}, {"typecheck"})

    assert wynik.do_pominiecia["rec"] == "brak wejścia: zmiana.diff z kroku diff"
    assert wynik.powody["rec"] == BRAK_WEJSCIA
    assert wynik.do_pominiecia["typy"] == "zależność nieudana: diff"
    assert wynik.powody["typy"] == ZALEZNOSC


def test_nieudany_krok_check_nie_odbiera_wejscia_zmiana_diff_ktorego_nie_produkuje(tmp_path):
    kroki = [
        ("diff", [], {"system": "diff"}),
        ("typy", ["diff"], {"system": "check", "capability": "typecheck"}),
        ("rec", ["diff", "typy"], {"consumes": ["zmiana.diff", DOWOD_TYPY]}),
    ]

    wynik = plan(tmp_path, kroki, {"diff": ZAKONCZONY, "typy": NIEUDANY}, {"typecheck"})

    # diff zakonczony, dowod nieudanego checka nie jest utracony: zostaje sama nieudana zaleznosc
    assert wynik.do_pominiecia == {"rec": "zależność nieudana: typy"}
    assert wynik.powody == {"rec": ZALEZNOSC}


def test_konsument_dwoch_dowodow_z_jednym_czerwonym_a_drugim_pominietym_traci_wejscie_pominietego(
    tmp_path,
):
    stany = {"diff": ZAKONCZONY, "typy": NIEUDANY, "kontrakt": POMINIETY}

    wynik = plan(tmp_path, DWA_CHECKI_I_RECENZENT, stany, {"typecheck", "contract"})

    assert wynik.do_pominiecia == {"rec": "brak wejścia: dowod-contract.json z kroku kontrakt"}
    assert wynik.powody == {"rec": BRAK_WEJSCIA}


def test_konsument_dwoch_dowodow_z_jednym_czerwonym_wskazuje_ten_czerwony_krok(tmp_path):
    stany = {"diff": ZAKONCZONY, "typy": ZAKONCZONY, "kontrakt": NIEUDANY}

    wynik = plan(tmp_path, DWA_CHECKI_I_RECENZENT, stany, {"typecheck", "contract"})

    assert wynik.do_pominiecia == {"rec": "zależność nieudana: kontrakt"}
    assert wynik.powody == {"rec": ZALEZNOSC}


def test_konsument_startuje_gdy_oba_checki_sa_zakonczone(tmp_path):
    stany = {"diff": ZAKONCZONY, "typy": ZAKONCZONY, "kontrakt": ZAKONCZONY}

    wynik = plan(tmp_path, DWA_CHECKI_I_RECENZENT, stany, {"typecheck", "contract"})

    assert wynik == Plan(do_startu=["rec"], do_pominiecia={}, powody={})


def test_konsument_czeka_gdy_jeden_z_dwoch_checkow_jest_w_toku(tmp_path):
    stany = {"diff": ZAKONCZONY, "typy": ZAKONCZONY, "kontrakt": W_TOKU}

    assert plan(tmp_path, DWA_CHECKI_I_RECENZENT, stany, {"typecheck", "contract"}) == PUSTY


def test_nazwa_artefaktu_dowodu_zalezy_od_capability_a_nie_od_id_kroku(tmp_path):
    # krok `sprawdz` z capability contract produkuje dowod-contract.json, nie dowod-sprawdz.json
    kroki = [
        ("sprawdz", [], {"system": "check", "capability": "contract"}),
        ("rec", ["sprawdz"], {"consumes": [DOWOD_KONTRAKT]}),
        ("inny", ["sprawdz"], {"consumes": ["dowod-sprawdz.json"]}),
    ]

    wynik = plan(tmp_path, kroki, {"sprawdz": POMINIETY}, {"contract"})

    # `inny` czyta artefakt, ktorego nikt nie produkuje: pominiety krok nie odbiera mu wejscia
    assert wynik == Plan(
        do_startu=["inny"],
        do_pominiecia={"rec": "brak wejścia: dowod-contract.json z kroku sprawdz"},
        powody={"rec": BRAK_WEJSCIA},
    )


# --- propagacja pominięć w jednym wywołaniu --------------------------------


def test_pominiecie_propaguje_sie_przez_lancuch_artefaktow_w_jednym_planie(tmp_path):
    kroki = [
        ("a", [], {"needs": ["gpu"], "produces": ["x.md"]}),
        ("b", ["a"], {"consumes": ["x.md"], "produces": ["y.md"]}),
        ("c", ["b"], {"consumes": ["y.md"]}),
        ("d", ["b"]),
    ]
    assert plan(tmp_path, kroki, {}) == Plan(
        do_startu=["d"],
        do_pominiecia={
            "a": "brak capability gpu",
            "b": "brak wejścia: x.md z kroku a",
            "c": "brak wejścia: y.md z kroku b",
        },
        powody={"a": BRAK_CAP, "b": BRAK_WEJSCIA, "c": BRAK_WEJSCIA},
    )


def test_pominiecia_w_planie_maja_kolejnosc_kluczy_jak_w_workflow_a_powody_te_same_klucze(
    tmp_path,
):
    kroki = [
        ("z", [], {"needs": ["gpu"], "produces": ["x.md"]}),
        ("m", ["z"], {"consumes": ["x.md"]}),
        ("a", [], {"needs": ["docker"]}),
    ]
    wynik = plan(tmp_path, kroki, {})
    assert list(wynik.do_pominiecia) == ["z", "m", "a"]
    assert list(wynik.powody) == ["z", "m", "a"]


def test_krok_wymieniony_w_workflow_przed_swoja_zaleznoscia_tez_jest_pominiety_w_jednym_planie(
    tmp_path,
):
    kroki = [
        ("b", ["a"], {"consumes": ["x.md"]}),
        ("a", [], {"needs": ["gpu"], "produces": ["x.md"]}),
    ]
    wynik = plan(tmp_path, kroki, {})
    assert wynik == Plan(
        do_startu=[],
        do_pominiecia={"a": "brak capability gpu", "b": "brak wejścia: x.md z kroku a"},
        powody={"a": BRAK_CAP, "b": BRAK_WEJSCIA},
    )
    assert list(wynik.do_pominiecia) == ["b", "a"]
    assert list(wynik.powody) == ["b", "a"]


def test_krok_za_pominietym_lancuchem_startuje_w_tym_samym_planie_gdy_workflow_wymienia_go_najpierw(
    tmp_path,
):
    kroki = [
        ("c", ["b"]),
        ("b", ["a"], {"consumes": ["x.md"]}),
        ("a", [], {"needs": ["gpu"], "produces": ["x.md"]}),
    ]
    wynik = plan(tmp_path, kroki, {})
    assert wynik == Plan(
        do_startu=["c"],
        do_pominiecia={"a": "brak capability gpu", "b": "brak wejścia: x.md z kroku a"},
        powody={"a": BRAK_CAP, "b": BRAK_WEJSCIA},
    )
    assert list(wynik.do_pominiecia) == ["b", "a"]
    assert list(wynik.powody) == ["b", "a"]


def test_start_wielu_krokow_ma_kolejnosc_workflow_a_nie_kolejnosc_zaleznosci_ani_alfabetu(
    tmp_path,
):
    kroki = [("z", []), ("m", []), ("a", ["m", "z"])]
    assert plan(tmp_path, kroki, {}) == Plan(do_startu=["z", "m"], do_pominiecia={}, powody={})


def test_przy_dwoch_nieudanych_przodkach_wskazany_jest_pierwszy_w_workflow_nie_pierwszy_alfabetycznie(
    tmp_path,
):
    kroki = [("y", []), ("x", []), ("z", ["x", "y"])]
    stany = {"x": NIEUDANY, "y": NIEUDANY}
    assert plan(tmp_path, kroki, stany) == Plan(
        do_startu=[],
        do_pominiecia={"z": "zależność nieudana: y"},
        powody={"z": ZALEZNOSC},
    )


def test_przy_dwoch_pominietych_producentach_wskazany_jest_pierwszy_w_workflow_nie_pierwszy_alfabetycznie(
    tmp_path,
):
    kroki = [
        ("q2", [], {"produces": ["plan.md"]}),
        ("q1", [], {"produces": ["plan.md"]}),
        ("b", ["q1", "q2"], {"consumes": ["plan.md"]}),
    ]
    stany = {"q1": POMINIETY, "q2": POMINIETY}
    assert plan(tmp_path, kroki, stany) == Plan(
        do_startu=[],
        do_pominiecia={"b": "brak wejścia: plan.md z kroku q2"},
        powody={"b": BRAK_WEJSCIA},
    )


def test_plan_podaje_wlasciwy_kod_dla_kazdego_z_trzech_powodow_pominiecia(tmp_path):
    kroki = [
        ("f", []),
        ("na_porazce", ["f"]),
        ("cap", [], {"needs": ["gpu"], "produces": ["x.md"]}),
        ("bez_wejscia", ["cap"], {"consumes": ["x.md"]}),
    ]
    wynik = plan(tmp_path, kroki, {"f": NIEUDANY})
    assert wynik == Plan(
        do_startu=[],
        do_pominiecia={
            "na_porazce": "zależność nieudana: f",
            "cap": "brak capability gpu",
            "bez_wejscia": "brak wejścia: x.md z kroku cap",
        },
        powody={
            "na_porazce": ZALEZNOSC,
            "cap": BRAK_CAP,
            "bez_wejscia": BRAK_WEJSCIA,
        },
    )
    assert wynik.powody["na_porazce"] == "zaleznosc_nieudana"
    assert wynik.powody["cap"] == "brak_capability"
    assert wynik.powody["bez_wejscia"] == "brak_wejscia"


# --- zakres przodków: romb i krok jako własny producent ---------------------

ROMB = [
    ("a", []),
    ("x", []),
    ("b", ["a"]),
    ("c", ["a"]),
    ("e", ["x", "b", "c"]),
]


def test_porazka_przodka_za_rombem_pomija_krok_choc_wspolny_przodek_laczy_dwie_galezie(tmp_path):
    stany = {"a": ZAKONCZONY, "b": ZAKONCZONY, "c": ZAKONCZONY, "x": NIEUDANY}
    assert plan(tmp_path, ROMB, stany) == Plan(
        do_startu=[],
        do_pominiecia={"e": "zależność nieudana: x"},
        powody={"e": ZALEZNOSC},
    )


def test_pominiety_producent_za_rombem_odbiera_wejscie_konsumentowi(tmp_path):
    kroki = [
        ("a", []),
        ("x", [], {"produces": ["plan.md"]}),
        ("b", ["a"]),
        ("c", ["a"]),
        ("e", ["x", "b", "c"], {"consumes": ["plan.md"]}),
    ]
    stany = {"a": ZAKONCZONY, "b": ZAKONCZONY, "c": ZAKONCZONY, "x": POMINIETY}
    assert plan(tmp_path, kroki, stany) == Plan(
        do_startu=[],
        do_pominiecia={"e": "brak wejścia: plan.md z kroku x"},
        powody={"e": BRAK_WEJSCIA},
    )


def test_krok_produkujacy_i_konsumujacy_ten_sam_artefakt_jest_pominiety_gdy_pominiety_jest_jego_przodek(
    tmp_path,
):
    kroki = [
        ("p", [], {"produces": ["plan.md"]}),
        ("k", ["p"], {"consumes": ["plan.md"], "produces": ["plan.md"]}),
    ]
    assert plan(tmp_path, kroki, {"p": POMINIETY}) == Plan(
        do_startu=[],
        do_pominiecia={"k": "brak wejścia: plan.md z kroku p"},
        powody={"k": BRAK_WEJSCIA},
    )


# --- wartości zapisywane w stanie runu ---------------------------------------


def test_stany_krokow_i_powody_pominiec_maja_ustalone_wartosci_tekstowe():
    assert [s.value for s in StanKroku] == [
        "oczekuje",
        "w_toku",
        "zakonczony",
        "nieudany",
        "pominiety",
    ]
    assert [p.value for p in PowodPominiecia] == [
        "brak_capability",
        "pusty_foreach",
        "brak_wejscia",
        "zaleznosc_nieudana",
    ]


# --- podsumowanie runu -----------------------------------------------------


@pytest.mark.parametrize("stan", [OCZEKUJE, W_TOKU])
def test_run_nie_jest_skonczony_dopoki_jakis_krok_oczekuje_albo_jest_w_toku(stan):
    stany = {"a": ZAKONCZONY, "b": stan}
    assert podsumowanie(stany) == Podsumowanie(skonczony=False, nieudane=[])


def test_run_jest_skonczony_gdy_kazdy_krok_jest_zakonczony_pominiety_albo_nieudany():
    stany = {"a": ZAKONCZONY, "b": POMINIETY, "c": NIEUDANY}
    assert podsumowanie(stany) == Podsumowanie(skonczony=True, nieudane=["c"])


def test_run_bez_krokow_jest_skonczony_i_bez_porazek():
    assert podsumowanie({}) == Podsumowanie(skonczony=True, nieudane=[])


def test_podsumowanie_wymienia_nieudane_kroki_w_kolejnosci_stanow():
    stany = {"z": NIEUDANY, "m": ZAKONCZONY, "a": NIEUDANY, "p": POMINIETY}
    assert podsumowanie(stany) == Podsumowanie(skonczony=True, nieudane=["z", "a"])


def test_porazka_jednej_galezi_konczy_run_z_jej_id_gdy_druga_galaz_dobiegla_konca(tmp_path):
    kroki = [("a", []), ("b", []), ("c", ["a"]), ("d", ["b"])]
    pierwszy = plan(tmp_path, kroki, {"a": NIEUDANY, "b": ZAKONCZONY})
    assert pierwszy.do_startu == ["d"]
    stany = {"a": NIEUDANY, "b": ZAKONCZONY, "c": POMINIETY, "d": ZAKONCZONY}
    assert podsumowanie(stany) == Podsumowanie(skonczony=True, nieudane=["a"])


# --- kroki_do_powtorzenia: sciezka needs od celu do kroku nieudanego (HAR-109) ----------------------


def powtorz(tmp_path, kroki, cel, krok):
    from runner.graf import kroki_do_powtorzenia   # import leniwy: brak funkcji nie psuje reszty pliku

    workflow, _ = swiat(tmp_path, kroki)
    return kroki_do_powtorzenia(workflow, cel, krok)


LANCUCH = [("a", []), ("b", ["a"]), ("c", ["b"])]
DIAMENT = [("a", []), ("b", ["a"]), ("d", ["a"]), ("c", ["b", "d"])]


def test_powtorzenie_lancucha_od_pierwszego_kroku_obejmuje_wszystkie_kroki_do_nieudanego(tmp_path):
    assert powtorz(tmp_path, LANCUCH, "a", "c") == ["a", "b", "c"]


def test_powtorzenie_od_srodka_lancucha_nie_obejmuje_krokow_przed_celem(tmp_path):
    assert powtorz(tmp_path, LANCUCH, "b", "c") == ["b", "c"]


def test_powtorzenie_nie_obejmuje_krokow_po_nieudanym(tmp_path):
    kroki = [*LANCUCH, ("d", ["c"])]
    assert powtorz(tmp_path, kroki, "a", "b") == ["a", "b"]


def test_powtorzenie_w_diamencie_obejmuje_obie_galezie_miedzy_celem_a_nieudanym(tmp_path):
    assert powtorz(tmp_path, DIAMENT, "a", "c") == ["a", "b", "d", "c"]


def test_powtorzenie_w_diamencie_od_jednej_galezi_pomija_druga(tmp_path):
    assert powtorz(tmp_path, DIAMENT, "b", "c") == ["b", "c"]


def test_powtorzenie_pomija_potomka_celu_ktory_nie_jest_przodkiem_nieudanego(tmp_path):
    # d zalezy od celu `a`, ale nieudany krok `b` od `d` nie zalezy
    assert powtorz(tmp_path, DIAMENT, "a", "b") == ["a", "b"]


def test_powtorzenie_pomija_krok_niezalezny_od_sciezki(tmp_path):
    kroki = [*LANCUCH, ("e", [])]
    assert powtorz(tmp_path, kroki, "a", "c") == ["a", "b", "c"]


def test_powtorzenie_pomija_przodka_celu_spoza_sciezki(tmp_path):
    kroki = [("p", []), ("a", ["p"]), ("b", ["a"])]
    assert powtorz(tmp_path, kroki, "a", "b") == ["a", "b"]


def test_kolejnosc_powtarzanych_krokow_jest_kolejnoscia_workflow_a_nie_topologiczna(tmp_path):
    kroki = [("c", ["b"]), ("b", ["a"]), ("a", [])]
    assert powtorz(tmp_path, kroki, "a", "c") == ["c", "b", "a"]


def test_kolejnosc_galezi_diamentu_jest_kolejnoscia_workflow_a_nie_alfabetyczna(tmp_path):
    kroki = [("a", []), ("z", ["a"]), ("b", ["a"]), ("c", ["z", "b"])]
    assert powtorz(tmp_path, kroki, "a", "c") == ["a", "z", "b", "c"]


@pytest.mark.parametrize("cel, krok", [
    ("b", "d"),          # cel nie jest przodkiem nieudanego (galaz obok)
    ("c", "a"),          # cel jest potomkiem nieudanego
    ("c", "c"),          # cel jest nieudanym krokiem
    ("nie_ma", "c"),     # cel nie istnieje
])
def test_powtorzenie_do_celu_ktory_nie_jest_przodkiem_nieudanego_jest_bledem(tmp_path, cel, krok):
    with pytest.raises(ValueError):
        powtorz(tmp_path, DIAMENT, cel, krok)


def test_powtorzenie_nieistniejacego_kroku_nieudanego_ma_komunikat_nieznany_krok(tmp_path):
    with pytest.raises(ValueError) as blad:
        powtorz(tmp_path, DIAMENT, "a", "nie_ma")
    assert str(blad.value) == "nieznany krok: nie_ma"


def test_powtorzenie_do_celu_spoza_przodkow_ma_komunikat_ze_wskazaniem_celu_i_kroku(tmp_path):
    with pytest.raises(ValueError) as blad:
        powtorz(tmp_path, DIAMENT, "b", "d")
    assert str(blad.value) == "cel b nie jest przodkiem kroku d"
