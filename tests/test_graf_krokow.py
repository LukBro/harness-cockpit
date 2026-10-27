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
        napisz_skill(skille, f"s_{id}", **opcje)
        zaleznosci = f", needs: [{', '.join(needs)}]" if needs else ""
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


def test_porazka_przodka_wygrywa_z_brakiem_wejscia(tmp_path):
    kroki = [
        ("a", []),
        ("p", [], {"produces": ["plan.md"]}),
        ("b", ["a", "p"], {"consumes": ["plan.md"]}),
    ]
    stany = {"a": NIEUDANY, "p": POMINIETY}
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
