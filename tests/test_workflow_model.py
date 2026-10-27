"""Model pliku workflow (.cockpit/workflows/<nazwa>.yaml) i zapis round-trip.

Testy przed implementacją (HAR-7). Bez bazy danych, wszystko na tmp_path.
Kody problemów porównujemy ze stałymi modułu, nie z treścią komunikatów.
"""

from pathlib import Path

import pytest

from cockpit.services.workflow import (
    BLAD_ODCZYTU,
    BLAD_POLA,
    BRAK_KROKOW,
    BRAK_SKILLA,
    NIEPOPRAWNY_YAML,
    ZDUPLIKOWANY_KLUCZ,
    Poziom,
    Problem,
    ma_bledy,
    wczytaj_workflow,
    zapisz_workflow,
)

PELNY_PRZYKLAD = """\
# komentarz zostaje po zapisie z kanwy
name: plan-i-recenzja
steps:
  plan:
    skill: planner
  review:
    skill: plan-reviewer
    needs: [plan]
    fanout: 3
    consensus: 2
    require_distinct_vendors: true
  implement:
    skill: implementer
    needs: [review]
    foreach: plan.json#zadania
    approval: true
    on_fail: {retry: plan, max: 2}
"""

# Celowo niekanoniczne: listy bez offsetu, puste linie, różne cudzysłowy,
# komentarze w środku, spacje przed komentarzem.
PLIK_NIEKANONICZNY = """\
# naglowek   z dziwnymi spacjami


name:   'plan-i-recenzja'
steps:

  plan:
      skill:   "planner"     # komentarz przy skillu
  review:
      skill: plan-reviewer
      needs:
      - plan
      fanout: 3


  # komentarz przed krokiem
  implement:
      skill: implementer
      needs: [  review ]
      on_fail: {retry:   plan,   max: 2}
"""

PLIK_Z_KOMENTARZAMI_I_CUDZYSLOWAMI = """\
# komentarz naglowkowy
name: 'plan-i-recenzja'
steps:
  plan:
    skill: "planner"
  # komentarz przy kroku review
  review:
    skill: "plan-reviewer"
    needs:
      - plan
    fanout: 3
    consensus: 2
  implement:
    skill: implementer
    needs:
      - review
"""

PLIK_KANONICZNY = """\
# naglowek
name: plan-i-recenzja
steps:
  plan:
    skill: planner
  review:
    skill: plan-reviewer
    needs:
      - plan
    fanout: 3
    consensus: 2
  implement:
    skill: implementer
    needs:
      - review
    approval: true
"""


def _zapisz(tmp_path: Path, tresc: str, nazwa: str = "plan-i-recenzja.yaml") -> Path:
    plik = tmp_path / nazwa
    plik.write_text(tresc, encoding="utf-8")
    return plik


def _krok(workflow, id_kroku: str):
    return next(k for k in workflow.kroki if k.id == id_kroku)


def test_zapis_bez_zmian_jest_identyczny_z_oryginalem(tmp_path):
    plik = _zapisz(tmp_path, PLIK_NIEKANONICZNY)
    workflow, problemy = wczytaj_workflow(plik)
    assert problemy == []
    assert workflow is not None

    wyjscie = tmp_path / "kopia.yaml"
    zapisz_workflow(workflow, wyjscie)

    assert wyjscie.read_bytes() == PLIK_NIEKANONICZNY.encode("utf-8")


def test_zmiana_pola_kroku_zachowuje_komentarze_kolejnosc_kluczy_i_cudzyslowy(tmp_path):
    plik = _zapisz(tmp_path, PLIK_Z_KOMENTARZAMI_I_CUDZYSLOWAMI)
    workflow, problemy = wczytaj_workflow(plik)
    assert problemy == []
    assert workflow is not None

    _krok(workflow, "review").fanout = 5
    zapisz_workflow(workflow, plik)
    tekst = plik.read_text(encoding="utf-8")

    assert "# komentarz naglowkowy" in tekst
    assert "# komentarz przy kroku review" in tekst
    assert "name: 'plan-i-recenzja'" in tekst
    assert 'skill: "planner"' in tekst
    assert 'skill: "plan-reviewer"' in tekst
    assert "fanout: 5" in tekst
    assert "fanout: 3" not in tekst

    # kolejność kroków i kluczy bez zmian
    assert tekst.index("  plan:") < tekst.index("  review:") < tekst.index("  implement:")
    review = tekst[tekst.index("  review:") : tekst.index("  implement:")]
    assert review.index("skill:") < review.index("needs:") < review.index("fanout:") < review.index("consensus:")

    # po ponownym wczytaniu model niesie nową wartość
    ponownie, problemy2 = wczytaj_workflow(plik)
    assert problemy2 == []
    assert ponownie is not None
    assert _krok(ponownie, "review").fanout == 5
    assert [k.id for k in ponownie.kroki] == ["plan", "review", "implement"]


def test_plik_w_stylu_kanonicznym_po_zmianie_pola_rozni_sie_tylko_tym_polem(tmp_path):
    plik = _zapisz(tmp_path, PLIK_KANONICZNY)
    workflow, problemy = wczytaj_workflow(plik)
    assert problemy == []
    assert workflow is not None

    _krok(workflow, "review").fanout = 4
    zapisz_workflow(workflow, plik)
    nowy = plik.read_text(encoding="utf-8")

    stare_linie = PLIK_KANONICZNY.splitlines()
    nowe_linie = nowy.splitlines()
    assert len(nowe_linie) == len(stare_linie)
    roznice = [(a, b) for a, b in zip(stare_linie, nowe_linie, strict=True) if a != b]
    assert roznice == [("    fanout: 3", "    fanout: 4")]
    # pól domyślnych nieobecnych w pliku nie dopisujemy
    assert "optional" not in nowy
    assert "require_distinct_vendors" not in nowy


def test_krok_z_blednym_polem_trafia_do_niepoprawnych_a_reszta_do_modelu(tmp_path):
    plik = _zapisz(
        tmp_path,
        "name: w\nsteps:\n  plan:\n    skill: planner\n  review:\n    skill: reviewer\n    fanout: 0\n  koniec:\n    skill: finisher\n",
    )
    workflow, problemy = wczytaj_workflow(plik)

    assert workflow is not None
    assert workflow.niepoprawne == ["review"]
    assert [k.id for k in workflow.kroki] == ["plan", "koniec"]
    assert [(p.krok, p.kod) for p in problemy] == [("review", BLAD_POLA)]


def test_niepoprawny_yaml_to_problem_a_nie_wyjatek(tmp_path):
    plik = _zapisz(tmp_path, "name: w\nsteps:\n  plan: [niezamkniete\n    skill: x\n")

    workflow, problemy = wczytaj_workflow(plik)

    assert workflow is None
    assert len(problemy) == 1
    assert problemy[0].krok is None
    assert problemy[0].kod == NIEPOPRAWNY_YAML
    assert problemy[0].poziom == Poziom.blad


def test_zduplikowany_krok_to_problem(tmp_path):
    plik = _zapisz(
        tmp_path,
        "name: w\nsteps:\n  plan:\n    skill: a\n  plan:\n    skill: b\n",
    )

    workflow, problemy = wczytaj_workflow(plik)

    assert workflow is None
    assert len(problemy) == 1
    assert problemy[0].krok is None
    assert problemy[0].kod == ZDUPLIKOWANY_KLUCZ


def test_krok_bez_skilla_i_krok_ze_zlym_typem_daja_dwa_problemy(tmp_path):
    plik = _zapisz(
        tmp_path,
        "name: w\nsteps:\n"
        "  bez-skilla:\n    fanout: 2\n"
        "  zly-typ:\n    skill: x\n    approval: [to, nie, bool]\n"
        "  dobry:\n    skill: y\n",
    )

    workflow, problemy = wczytaj_workflow(plik)

    assert workflow is not None
    assert len(problemy) == 2
    assert {(p.krok, p.kod) for p in problemy} == {
        ("bez-skilla", BRAK_SKILLA),
        ("zly-typ", BLAD_POLA),
    }
    assert sorted(workflow.niepoprawne) == ["bez-skilla", "zly-typ"]
    assert [k.id for k in workflow.kroki] == ["dobry"]


def test_brak_mapy_steps_to_problem_i_brak_workflow(tmp_path):
    plik = _zapisz(tmp_path, "name: w\n")

    workflow, problemy = wczytaj_workflow(plik)

    assert workflow is None
    assert [(p.krok, p.kod) for p in problemy] == [(None, BRAK_KROKOW)]


def test_bez_pola_name_nazwa_workflow_pochodzi_z_nazwy_pliku(tmp_path):
    plik = _zapisz(tmp_path, "steps:\n  plan:\n    skill: planner\n", nazwa="moj-przeplyw.yaml")

    workflow, problemy = wczytaj_workflow(plik)

    assert problemy == []
    assert workflow is not None
    assert workflow.nazwa == "moj-przeplyw"


def test_kolejnosc_krokow_jest_kolejnoscia_kluczy_w_pliku(tmp_path):
    plik = _zapisz(
        tmp_path,
        "name: w\nsteps:\n  zeta:\n    skill: a\n  alfa:\n    skill: b\n  mi:\n    skill: c\n",
    )

    workflow, problemy = wczytaj_workflow(plik)

    assert problemy == []
    assert workflow is not None
    assert [k.id for k in workflow.kroki] == ["zeta", "alfa", "mi"]


def test_pelny_przyklad_z_ticketu_wczytuje_sie_bez_problemow_i_niesie_wszystkie_pola(tmp_path):
    plik = _zapisz(tmp_path, PELNY_PRZYKLAD)

    workflow, problemy = wczytaj_workflow(plik)

    assert problemy == []
    assert workflow is not None
    assert workflow.nazwa == "plan-i-recenzja"
    assert workflow.niepoprawne == []
    assert [k.id for k in workflow.kroki] == ["plan", "review", "implement"]

    plan = _krok(workflow, "plan")
    assert plan.skill == "planner"
    assert plan.needs == []
    assert plan.fanout == 1
    assert plan.consensus is None
    assert plan.foreach is None
    assert plan.approval is False
    assert plan.optional is False
    assert plan.require_distinct_vendors is False
    assert plan.on_fail is None

    review = _krok(workflow, "review")
    assert review.skill == "plan-reviewer"
    assert review.needs == ["plan"]
    assert review.fanout == 3
    assert review.consensus == 2
    assert review.require_distinct_vendors is True

    implement = _krok(workflow, "implement")
    assert implement.needs == ["review"]
    assert implement.foreach == "plan.json#zadania"
    assert implement.approval is True
    assert implement.on_fail is not None
    assert implement.on_fail.retry == "plan"
    assert implement.on_fail.max == 2


def test_nieznany_klucz_kroku_zostaje_w_surowej_mapie_i_nie_jest_problemem(tmp_path):
    plik = _zapisz(
        tmp_path,
        "name: w\nsteps:\n  plan:\n    skill: planner\n    cos_nowego: 1\n",
    )

    workflow, problemy = wczytaj_workflow(plik)

    assert problemy == []
    assert workflow is not None
    surowy = workflow.surowy_krok("plan")
    assert surowy["cos_nowego"] == 1
    assert surowy["skill"] == "planner"


def test_ma_bledy_jest_prawda_dla_bledu_a_falszem_dla_samego_ostrzezenia():
    blad = Problem(krok="a", kod=BLAD_POLA, komunikat="x")
    ostrzezenie = Problem(krok="a", kod=BLAD_POLA, komunikat="x", poziom=Poziom.ostrzezenie)

    assert blad.poziom == Poziom.blad
    assert ma_bledy([ostrzezenie, blad]) is True
    assert ma_bledy([ostrzezenie]) is False
    assert ma_bledy([]) is False


def test_napis_zamiast_liczby_i_yes_zamiast_bool_to_bledy_pol(tmp_path):
    plik = _zapisz(
        tmp_path,
        "name: w\nsteps:\n"
        "  liczba-napisem:\n    skill: a\n    fanout: '3'\n"
        "  bool-yes:\n    skill: b\n    approval: yes\n"
        "  dobry:\n    skill: c\n",
    )

    workflow, problemy = wczytaj_workflow(plik)

    assert workflow is not None
    assert {(p.krok, p.kod) for p in problemy} == {
        ("liczba-napisem", BLAD_POLA),
        ("bool-yes", BLAD_POLA),
    }
    assert sorted(workflow.niepoprawne) == ["bool-yes", "liczba-napisem"]
    assert [k.id for k in workflow.kroki] == ["dobry"]


def test_wyzerowanie_opcjonalnych_pol_obecnych_w_pliku_usuwa_je_z_pliku(tmp_path):
    plik = _zapisz(
        tmp_path,
        "name: w\nsteps:\n"
        "  plan:\n    skill: a\n"
        "  review:\n    skill: b\n    consensus: 2\n    on_fail: {retry: plan, max: 2}\n    fanout: 3\n",
    )
    workflow, problemy = wczytaj_workflow(plik)
    assert problemy == []
    assert workflow is not None
    review = _krok(workflow, "review")
    assert review.consensus == 2
    assert review.on_fail is not None

    review.consensus = None
    review.on_fail = None
    zapisz_workflow(workflow, plik)
    tekst = plik.read_text(encoding="utf-8")

    assert "consensus" not in tekst
    assert "on_fail" not in tekst
    assert "fanout: 3" in tekst
    ponownie, problemy2 = wczytaj_workflow(plik)
    assert problemy2 == []
    assert ponownie is not None
    assert _krok(ponownie, "review").consensus is None
    assert _krok(ponownie, "review").on_fail is None
    assert _krok(ponownie, "review").fanout == 3


def test_zmiana_listy_needs_w_stylu_flow_zostaje_w_stylu_flow_a_reszta_pliku_bez_zmian(tmp_path):
    zrodlo = (
        "name: w\nsteps:\n"
        "  x:\n    skill: a\n"
        "  y:\n    skill: b\n"
        "  z:\n    skill: c\n"
        "  review:\n    skill: d\n    needs: [x, y]\n    fanout: 2\n"
    )
    plik = _zapisz(tmp_path, zrodlo)
    workflow, problemy = wczytaj_workflow(plik)
    assert problemy == []
    assert workflow is not None

    _krok(workflow, "review").needs = ["z"]
    zapisz_workflow(workflow, plik)
    tekst = plik.read_text(encoding="utf-8")

    stare = zrodlo.splitlines()
    nowe = tekst.splitlines()
    assert len(nowe) == len(stare)
    roznice = [(a, b) for a, b in zip(stare, nowe, strict=True) if a != b]
    assert roznice == [("    needs: [x, y]", "    needs: [z]")]


def test_nieznany_klucz_kroku_niebedacy_napisem_nie_psuje_wczytania(tmp_path):
    plik = _zapisz(
        tmp_path,
        "name: w\nsteps:\n  plan:\n    skill: planner\n    1: x\n  drugi:\n    skill: b\n    true: y\n",
    )

    workflow, problemy = wczytaj_workflow(plik)

    assert problemy == []
    assert workflow is not None
    assert [k.id for k in workflow.kroki] == ["plan", "drugi"]
    assert workflow.surowy_krok("plan")[1] == "x"
    assert workflow.surowy_krok("drugi")[True] == "y"


def test_bardzo_gleboko_zagniezdzona_wartosc_to_problem_yaml_a_nie_wyjatek(tmp_path):
    plik = _zapisz(
        tmp_path,
        "name: w\nsteps:\n  plan:\n    skill: a\n    x: " + "[" * 5000 + "]" * 5000 + "\n",
    )

    workflow, problemy = wczytaj_workflow(plik)

    assert workflow is None
    assert [(p.krok, p.kod) for p in problemy] == [(None, NIEPOPRAWNY_YAML)]


def test_odwrocenie_kolejnosci_krokow_w_modelu_zmienia_kolejnosc_w_pliku_a_komentarze_podrozuja_z_krokiem(tmp_path):
    plik = _zapisz(
        tmp_path,
        "# naglowek zostaje\nname: w\nsteps:\n"
        "  pierwszy:  # k1\n    skill: a\n"
        "  drugi:  # k2\n    skill: b\n"
        "  trzeci:  # k3\n    skill: c\n",
    )
    workflow, problemy = wczytaj_workflow(plik)
    assert problemy == []
    assert workflow is not None

    workflow.kroki = list(reversed(workflow.kroki))
    zapisz_workflow(workflow, plik)
    tekst = plik.read_text(encoding="utf-8")

    assert tekst.startswith("# naglowek zostaje\n")
    # komentarz przy kluczu kroku zostaje przy swoim kluczu, a nie przy pozycji
    assert _klucze_krokow(tekst) == ["  trzeci:  # k3", "  drugi:  # k2", "  pierwszy:  # k1"]
    ponownie, problemy2 = wczytaj_workflow(plik)
    assert problemy2 == []
    assert ponownie is not None
    assert [k.id for k in ponownie.kroki] == ["trzeci", "drugi", "pierwszy"]
    assert [_krok(ponownie, i).skill for i in ("trzeci", "drugi", "pierwszy")] == ["c", "b", "a"]


PLIK_Z_KROKIEM_NIEPOPRAWNYM = (
    "name: w\nsteps:\n"
    "  pierwszy:\n    skill: a\n    fanout: 2\n"
    "  zly:  # reczna poprawka uzytkownika\n    skill: z\n    fanout: 0  # do poprawy\n    cos_nowego: [1, 2]\n"
    "  drugi:\n    skill: b\n"
)


def _klucze_krokow(tekst: str) -> list[str]:
    """Linie kluczy krokow (wciecie 2) w kolejnosci z pliku."""
    return [wiersz for wiersz in tekst.splitlines() if wiersz.startswith("  ") and not wiersz.startswith("   ")]


def _blok_kroku(tekst: str, id_kroku: str) -> list[str]:
    """Linie kroku od jego klucza do nastepnego klucza kroku."""
    linie = tekst.splitlines()
    start = next(i for i, wiersz in enumerate(linie) if wiersz.startswith(f"  {id_kroku}:"))
    koniec = next((i for i in range(start + 1, len(linie)) if not linie[i].startswith("   ")), len(linie))
    return linie[start:koniec]


def test_zapis_zmienionego_workflow_nie_kasuje_ani_nie_zmienia_kroku_niepoprawnego_w_pliku(tmp_path):
    plik = _zapisz(tmp_path, PLIK_Z_KROKIEM_NIEPOPRAWNYM)
    workflow, problemy = wczytaj_workflow(plik)
    assert workflow is not None
    assert [(p.krok, p.kod) for p in problemy] == [("zly", BLAD_POLA)]
    przed = _blok_kroku(PLIK_Z_KROKIEM_NIEPOPRAWNYM, "zly")
    assert len(przed) == 4

    _krok(workflow, "pierwszy").fanout = 5
    zapisz_workflow(workflow, plik)
    tekst = plik.read_text(encoding="utf-8")

    assert "fanout: 5" in tekst
    assert _blok_kroku(tekst, "zly") == przed
    ponownie, problemy2 = wczytaj_workflow(plik)
    assert ponownie is not None
    assert ponownie.niepoprawne == ["zly"]
    assert [(p.krok, p.kod) for p in problemy2] == [("zly", BLAD_POLA)]
    assert [k.id for k in ponownie.kroki] == ["pierwszy", "drugi"]
    assert _krok(ponownie, "pierwszy").fanout == 5


def test_przestawienie_krokow_poprawnych_zostawia_krok_niepoprawny_na_srodku_pliku(tmp_path):
    plik = _zapisz(tmp_path, PLIK_Z_KROKIEM_NIEPOPRAWNYM)
    workflow, problemy = wczytaj_workflow(plik)
    assert workflow is not None
    assert [(p.krok, p.kod) for p in problemy] == [("zly", BLAD_POLA)]
    przed = _blok_kroku(PLIK_Z_KROKIEM_NIEPOPRAWNYM, "zly")

    workflow.kroki = list(reversed(workflow.kroki))
    zapisz_workflow(workflow, plik)
    tekst = plik.read_text(encoding="utf-8")

    assert [wiersz.split(":")[0].strip() for wiersz in _klucze_krokow(tekst)] == ["drugi", "zly", "pierwszy"]
    assert _blok_kroku(tekst, "zly") == przed
    ponownie, problemy2 = wczytaj_workflow(plik)
    assert ponownie is not None
    assert ponownie.niepoprawne == ["zly"]
    assert [(p.krok, p.kod) for p in problemy2] == [("zly", BLAD_POLA)]
    assert [k.id for k in ponownie.kroki] == ["drugi", "pierwszy"]


@pytest.mark.parametrize(
    "pole_i_wartosc",
    [
        "optional: yes",
        "require_distinct_vendors: 'true'",
        "consensus: '2'",
        "consensus: 0",
        "on_fail: {retry: plan, max: '2'}",
        "on_fail: {retry: plan, max: 0}",
        "fanout: true",
        "approval: 1",
    ],
)
def test_wartosc_pola_w_zlym_typie_albo_ponizej_minimum_robi_krok_niepoprawnym(tmp_path, pole_i_wartosc):
    plik = _zapisz(
        tmp_path,
        f"name: w\nsteps:\n  zly:\n    skill: a\n    {pole_i_wartosc}\n  dobry:\n    skill: b\n",
    )

    workflow, problemy = wczytaj_workflow(plik)

    assert workflow is not None
    assert [(p.krok, p.kod) for p in problemy] == [("zly", BLAD_POLA)]
    assert workflow.niepoprawne == ["zly"]
    assert [k.id for k in workflow.kroki] == ["dobry"]


def test_brak_pliku_to_problem_odczytu_a_nie_wyjatek(tmp_path):
    workflow, problemy = wczytaj_workflow(tmp_path / "nie-istnieje.yaml")

    assert workflow is None
    assert [(p.krok, p.kod) for p in problemy] == [(None, BLAD_ODCZYTU)]


def test_klucze_krokow_1_i_napis_1_to_ten_sam_krok_zduplikowany(tmp_path):
    plik = _zapisz(
        tmp_path,
        "name: w\nsteps:\n  1:\n    skill: a\n  '1':\n    skill: b\n",
    )

    workflow, problemy = wczytaj_workflow(plik)

    assert workflow is None
    assert [(p.krok, p.kod) for p in problemy] == [(None, ZDUPLIKOWANY_KLUCZ)]
