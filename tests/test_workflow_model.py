"""Model pliku workflow (.cockpit/workflows/<nazwa>.yaml) i zapis round-trip.

Testy przed implementacją (HAR-7). Bez bazy danych, wszystko na tmp_path.
Kody problemów porównujemy ze stałymi modułu, nie z treścią komunikatów.
"""

import tempfile
from pathlib import Path

import pytest

from cockpit.services.workflow import (
    BLAD_ODCZYTU,
    BLAD_POLA,
    BRAK_KROKOW,
    BRAK_SKILLA,
    NIEPOPRAWNY_YAML,
    ZDUPLIKOWANY_KLUCZ,
    Krok,
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
        "fail_on_survivors: yes",
        "fail_on_survivors: 1",
        "fail_on_survivors: 'true'",
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


# --- krok systemowy (HAR-176): `system: diff` zamiast `skill` ---------------------------------

PLIK_Z_KROKIEM_SYSTEMOWYM = """\
# komentarz zostaje
name: w
steps:
  autor:
    skill: pisarz
    fanout: 2
  diff:
    system: diff
    needs:
      - autor
  rec:
    skill: recenzent
    needs:
      - diff
"""


def test_krok_z_samym_polem_system_wczytuje_sie_bez_problemow_i_bez_skilla(tmp_path):
    plik = _zapisz(tmp_path, PLIK_Z_KROKIEM_SYSTEMOWYM)

    workflow, problemy = wczytaj_workflow(plik)

    assert problemy == []
    assert workflow is not None
    diff = _krok(workflow, "diff")
    assert (diff.system, diff.skill, diff.needs) == ("diff", None, ["autor"])
    assert _krok(workflow, "autor").system is None
    assert workflow.niepoprawne == []


def test_krok_bez_skill_i_bez_system_ma_problem_brak_skilla_z_komunikatem_o_obu_polach(tmp_path):
    plik = _zapisz(tmp_path, "name: w\nsteps:\n  pusty:\n    fanout: 2\n")

    workflow, problemy = wczytaj_workflow(plik)

    assert workflow is not None
    assert problemy == [
        Problem("pusty", BRAK_SKILLA, "krok nie ma pola skill ani system", Poziom.blad)]
    assert workflow.niepoprawne == ["pusty"]


def test_zmiana_pola_innego_kroku_nie_dotyka_kroku_systemowego_w_pliku(tmp_path):
    plik = _zapisz(tmp_path, PLIK_Z_KROKIEM_SYSTEMOWYM)
    workflow, _ = wczytaj_workflow(plik)
    assert workflow is not None

    _krok(workflow, "autor").fanout = 3
    zapisz_workflow(workflow, plik)
    tekst = plik.read_text(encoding="utf-8")

    roznice = [
        (a, b)
        for a, b in zip(
            PLIK_Z_KROKIEM_SYSTEMOWYM.splitlines(), tekst.splitlines(), strict=True)
        if a != b
    ]
    assert roznice == [("    fanout: 2", "    fanout: 3")]
    assert _blok_kroku(tekst, "diff") == ["  diff:", "    system: diff", "    needs:", "      - autor"]


def test_nowy_krok_systemowy_dodany_w_modelu_zapisuje_sie_jako_system_a_nie_skill_null(tmp_path):
    plik = _zapisz(tmp_path, "name: w\nsteps:\n  autor:\n    skill: pisarz\n")
    workflow, _ = wczytaj_workflow(plik)
    assert workflow is not None

    workflow.kroki.append(Krok(id="diff", system="diff", needs=["autor"]))
    zapisz_workflow(workflow, plik)
    tekst = plik.read_text(encoding="utf-8")

    blok = _blok_kroku(tekst, "diff")
    # caly blok: bez zadnego obcego klucza, `system` przed `needs`
    assert blok == ["  diff:", "    system: diff", "    needs:", "      - autor"]
    assert "null" not in tekst and "None" not in tekst
    ponownie, problemy = wczytaj_workflow(plik)
    assert problemy == []
    assert ponownie is not None
    diff = _krok(ponownie, "diff")
    assert (diff.system, diff.skill, diff.needs) == ("diff", None, ["autor"])
    assert _krok(ponownie, "autor").skill == "pisarz"


def test_nowy_krok_ze_skillem_dodany_w_modelu_nadal_zapisuje_skill_i_nie_dopisuje_system(tmp_path):
    plik = _zapisz(tmp_path, "name: w\nsteps:\n  autor:\n    skill: pisarz\n")
    workflow, _ = wczytaj_workflow(plik)
    assert workflow is not None

    workflow.kroki.append(Krok(id="rec", skill="recenzent", needs=["autor"]))
    zapisz_workflow(workflow, plik)

    blok = _blok_kroku(plik.read_text(encoding="utf-8"), "rec")
    assert blok == ["  rec:", "    skill: recenzent", "    needs:", "      - autor"]


# --- nazwa workflow, bool vs int, bledy krokow (HAR-176, mutanty zmienionych funkcji) ----------


def test_zmiana_nazwy_workflow_zastepuje_name_na_jego_miejscu_w_pliku(tmp_path):
    plik = _zapisz(tmp_path, "# k\nsteps:\n  plan:\n    skill: planner\nname: stara\n")
    workflow, problemy = wczytaj_workflow(plik)
    assert problemy == [] and workflow is not None

    workflow.nazwa = "nowa"
    zapisz_workflow(workflow, plik)
    tekst = plik.read_text(encoding="utf-8")

    assert tekst.splitlines()[-1] == "name: nowa"
    assert tekst.count("name:") == 1
    assert tekst.index("steps:") < tekst.index("name:")
    assert "null" not in tekst


def test_nazwa_rowna_nazwie_pliku_zapisuje_sie_gdy_name_w_pliku_jest_inne(tmp_path):
    plik = _zapisz(tmp_path, "name: alfa\nsteps:\n  plan:\n    skill: planner\n", nazwa="beta.yaml")
    workflow, problemy = wczytaj_workflow(plik)
    assert problemy == [] and workflow is not None

    workflow.nazwa = "beta"
    zapisz_workflow(workflow, plik)

    assert plik.read_text(encoding="utf-8").splitlines()[0] == "name: beta"


def test_zapis_pliku_bez_klucza_name_nie_dopisuje_name_gdy_nazwa_nie_zmieniona(tmp_path):
    plik = _zapisz(tmp_path, "steps:\n  plan:\n    skill: planner\n")
    workflow, problemy = wczytaj_workflow(plik)
    assert problemy == [] and workflow is not None

    _krok(workflow, "plan").fanout = 2
    zapisz_workflow(workflow, plik)

    tekst = plik.read_text(encoding="utf-8")
    assert "name" not in tekst
    assert "fanout: 2" in tekst


def test_zmiana_nazwy_w_pliku_bez_klucza_name_dopisuje_name_jako_pierwszy_klucz(tmp_path):
    plik = _zapisz(tmp_path, "steps:\n  plan:\n    skill: planner\n")
    workflow, problemy = wczytaj_workflow(plik)
    assert problemy == [] and workflow is not None

    workflow.nazwa = "nowa"
    zapisz_workflow(workflow, plik)

    assert plik.read_text(encoding="utf-8").splitlines()[:2] == ["name: nowa", "steps:"]


def test_wartosc_logiczna_w_modelu_zastepuje_rowna_jej_liczbe_w_pliku(tmp_path):
    # True == 1, ale to inna wartosc w YAML (`true` a `1`): zapis nie moze ich mylic
    plik = _zapisz(tmp_path, "name: w\nsteps:\n  plan:\n    skill: planner\n    fanout: 1\n")
    workflow, problemy = wczytaj_workflow(plik)
    assert problemy == [] and workflow is not None

    _krok(workflow, "plan").fanout = True           # przypisanie nie jest walidowane
    workflow.nazwa = "w2"                            # model_dump nie widzi roznicy True/1
    zapisz_workflow(workflow, plik)

    tekst = plik.read_text(encoding="utf-8")
    assert "fanout: true" in tekst
    assert "fanout: 1" not in tekst


def test_krok_niebedacy_mapa_nie_przerywa_wczytywania_kolejnych_krokow(tmp_path):
    plik = _zapisz(tmp_path, "name: w\nsteps:\n  zly: 5\n  dobry:\n    skill: x\n")

    workflow, problemy = wczytaj_workflow(plik)

    assert workflow is not None
    assert [(p.krok, p.kod, p.komunikat) for p in problemy] == [
        ("zly", BLAD_POLA, "krok musi byc mapa pol")]
    assert workflow.niepoprawne == ["zly"]
    assert [k.id for k in workflow.kroki] == ["dobry"]


def test_zduplikowany_krok_ma_komunikat_z_jego_identyfikatorem(tmp_path):
    plik = _zapisz(tmp_path, "name: w\nsteps:\n  1:\n    skill: a\n  '1':\n    skill: b\n")

    workflow, problemy = wczytaj_workflow(plik)

    assert workflow is None
    assert problemy == [Problem(None, ZDUPLIKOWANY_KLUCZ, "zduplikowany klucz kroku: 1")]


def test_blad_zagniezdzonego_pola_kroku_ma_sciezke_pola_rozdzielona_kropka(tmp_path):
    plik = _zapisz(
        tmp_path,
        "name: w\nsteps:\n  a:\n    skill: x\n  b:\n    skill: y\n    on_fail: {retry: a, max: 0}\n",
    )

    workflow, problemy = wczytaj_workflow(plik)

    assert workflow is not None
    assert [(p.krok, p.kod) for p in problemy] == [("b", BLAD_POLA)]
    assert problemy[0].komunikat.startswith("on_fail.max: ")


# --- HAR-181: wariant tekstowy (kokpit czyta i zapisuje plik przez runnera, wiec nie ma sciezki) ---
# `wczytaj_workflow_z_tekstu(tekst) -> (Workflow | None, list[Problem])` - ta sama konwencja
# co `wczytaj_workflow`; plik bez pola `name` ma nazwe "" (a nie nazwe pliku);
# `_tekst_po_zapisie(workflow)` to tresc, ktora `zapisz_workflow` zapisuje do pliku. Importy w testach: brak funkcji to czerwony test,
# a nie blad zbierania calego pliku.

PLIKI_DO_PARYTETU = {
    "niekanoniczny": PLIK_NIEKANONICZNY,
    "komentarze_i_cudzyslowy": PLIK_Z_KOMENTARZAMI_I_CUDZYSLOWAMI,
    "kanoniczny": PLIK_KANONICZNY,
    "pelny_przyklad": PELNY_PRZYKLAD,
    "krok_niepoprawny": PLIK_Z_KROKIEM_NIEPOPRAWNYM,
    "krok_systemowy": PLIK_Z_KROKIEM_SYSTEMOWYM,
    "bez_name": "steps:\n  plan:\n    skill: planner\n",
    "krok_bez_skilla": "name: w\nsteps:\n  bez-skilla:\n    fanout: 2\n  dobry:\n    skill: y\n",
    "niepoprawny_yaml": "name: w\nsteps:\n  plan: [niezamkniete\n    skill: x\n",
    "zduplikowany_krok": "name: w\nsteps:\n  plan:\n    skill: a\n  plan:\n    skill: b\n",
    "brak_steps": "name: w\n",
    "pusty_tekst": "",
}


def _opis_modelu(workflow, z_nazwa=True):
    if workflow is None:
        return None
    return (workflow.nazwa if z_nazwa else None, [k.model_dump() for k in workflow.kroki],
            list(workflow.niepoprawne))


def _tekst_po_zapisie(workflow) -> str:
    """Tekst, ktory `zapisz_workflow` zapisuje do pliku (kokpit zapisuje przez runnera, test przez dysk)."""
    with tempfile.TemporaryDirectory() as katalog:
        plik = Path(katalog) / "w.yaml"
        zapisz_workflow(workflow, plik)
        return plik.read_text(encoding="utf-8")


@pytest.mark.parametrize("tresc", PLIKI_DO_PARYTETU.values(), ids=PLIKI_DO_PARYTETU.keys())
def test_wczytanie_z_tekstu_daje_to_samo_co_wczytanie_z_pliku_o_tej_nazwie(tmp_path, tresc):
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    z_pliku, problemy_pliku = wczytaj_workflow(_zapisz(tmp_path, tresc, "plan-i-recenzja.yaml"))

    z_tekstu, problemy_tekstu = wczytaj_workflow_z_tekstu(tresc)

    assert problemy_tekstu == problemy_pliku
    # nazwa z pliku bez pola `name` to nazwa pliku, z tekstu to ""
    assert _opis_modelu(z_tekstu, "name:" in tresc) == _opis_modelu(z_pliku, "name:" in tresc)




def test_nazwa_workflow_z_tekstu_pochodzi_z_pola_name():
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, _ = wczytaj_workflow_z_tekstu(
        "name: z-pliku\nsteps:\n  plan:\n    skill: planner\n")

    assert workflow is not None
    assert workflow.nazwa == "z-pliku"


@pytest.mark.parametrize("tresc", [
    PLIK_NIEKANONICZNY, PLIK_Z_KOMENTARZAMI_I_CUDZYSLOWAMI, PLIK_Z_KROKIEM_NIEPOPRAWNYM,
    PLIK_Z_KROKIEM_SYSTEMOWYM,
], ids=["niekanoniczny", "komentarze_i_cudzyslowy", "krok_niepoprawny", "krok_systemowy"])
def test_tekst_workflow_bez_zmian_modelu_wraca_bajt_w_bajt(tresc):
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, _ = wczytaj_workflow_z_tekstu(tresc)
    assert workflow is not None

    assert _tekst_po_zapisie(workflow) == tresc


def test_zmiana_pola_kroku_w_tekscie_zachowuje_komentarze_kolejnosc_kluczy_i_cudzyslowy():
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, problemy = wczytaj_workflow_z_tekstu(
        PLIK_Z_KOMENTARZAMI_I_CUDZYSLOWAMI)
    assert problemy == []
    assert workflow is not None

    _krok(workflow, "review").fanout = 5
    tekst = _tekst_po_zapisie(workflow)

    assert "# komentarz naglowkowy" in tekst
    assert "# komentarz przy kroku review" in tekst
    assert "name: 'plan-i-recenzja'" in tekst
    assert 'skill: "planner"' in tekst
    assert "fanout: 5" in tekst
    assert "fanout: 3" not in tekst
    review = tekst[tekst.index("  review:") : tekst.index("  implement:")]
    assert review.index("skill:") < review.index("needs:") < review.index("fanout:") < review.index("consensus:")
    ponownie, problemy2 = wczytaj_workflow_z_tekstu(tekst)
    assert problemy2 == []
    assert ponownie is not None
    assert _krok(ponownie, "review").fanout == 5


def test_tekst_po_zmianie_pola_rozni_sie_od_oryginalu_tylko_tym_polem():
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, _ = wczytaj_workflow_z_tekstu(PLIK_KANONICZNY)
    assert workflow is not None

    _krok(workflow, "review").fanout = 4
    nowe = _tekst_po_zapisie(workflow).splitlines()

    stare = PLIK_KANONICZNY.splitlines()
    assert len(nowe) == len(stare)
    assert [(a, b) for a, b in zip(stare, nowe, strict=True) if a != b] == [
        ("    fanout: 3", "    fanout: 4")]


def test_odwrocenie_kolejnosci_krokow_w_tekscie_zachowuje_komentarze_przy_swoich_kluczach():
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, problemy = wczytaj_workflow_z_tekstu(
        "# naglowek zostaje\nname: w\nsteps:\n"
        "  pierwszy:  # k1\n    skill: a\n"
        "  drugi:  # k2\n    skill: b\n"
        "  trzeci:  # k3\n    skill: c\n")
    assert problemy == []
    assert workflow is not None

    workflow.kroki = list(reversed(workflow.kroki))
    tekst = _tekst_po_zapisie(workflow)

    assert tekst.startswith("# naglowek zostaje\n")
    assert _klucze_krokow(tekst) == ["  trzeci:  # k3", "  drugi:  # k2", "  pierwszy:  # k1"]




def test_krok_niepoprawny_zostaje_w_tekscie_po_zmianie_innego_kroku():
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, _ = wczytaj_workflow_z_tekstu(PLIK_Z_KROKIEM_NIEPOPRAWNYM)
    assert workflow is not None

    _krok(workflow, "pierwszy").fanout = 3
    tekst = _tekst_po_zapisie(workflow)

    assert "  zly:  # reczna poprawka uzytkownika\n    skill: z\n    fanout: 0  # do poprawy\n" in tekst
    assert "    cos_nowego: [1, 2]\n" in tekst
    assert "fanout: 3" in tekst


# --- HAR-181: poprawka 1 - komunikaty, galezie bledow i stan po renderowaniu --------------------

DWA_KROKI = "name: w\nsteps:\n  plan:\n    skill: a\n  review:\n    skill: b\n    fanout: 3\n"


def test_niepoprawny_yaml_w_tekscie_ma_komunikat_z_numerem_linii():
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, problemy = wczytaj_workflow_z_tekstu(
        "name: w\nsteps:\n  plan: [niezamkniete\n    skill: x\n")

    assert workflow is None
    assert problemy == [Problem(None, NIEPOPRAWNY_YAML, "niepoprawny YAML (linia 4)")]


def test_zduplikowany_klucz_w_tekscie_ma_komunikat_z_numerem_linii_drugiego_wystapienia():
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, problemy = wczytaj_workflow_z_tekstu(
        "name: w\nsteps:\n  plan:\n    skill: a\n  plan:\n    skill: b\n")

    assert workflow is None
    assert problemy == [Problem(None, ZDUPLIKOWANY_KLUCZ, "zduplikowany klucz (linia 5)")]


@pytest.mark.parametrize("wartosc", ["2024-02-30", "2024-13-01 10:00:00"],
                         ids=["dzien_spoza_miesiaca", "miesiac_spoza_roku"])
def test_wartosc_ktorej_nie_da_sie_skonstruowac_to_problem_yaml_z_opisem_a_nie_wyjatek(wartosc):
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, problemy = wczytaj_workflow_z_tekstu(
        f"name: w\nutworzono: {wartosc}\nsteps:\n  plan:\n    skill: a\n")

    assert workflow is None
    assert problemy == [
        Problem(None, NIEPOPRAWNY_YAML, "niepoprawny YAML: nieprawidlowa wartosc")]


def test_wartosc_ktorej_nie_da_sie_skonstruowac_w_pliku_to_ten_sam_problem_co_w_tekscie(tmp_path):
    plik = _zapisz(tmp_path, "name: w\nutworzono: 2024-02-30\nsteps:\n  plan:\n    skill: a\n")

    workflow, problemy = wczytaj_workflow(plik)

    assert workflow is None
    assert problemy == [
        Problem(None, NIEPOPRAWNY_YAML, "niepoprawny YAML: nieprawidlowa wartosc")]


def test_liczba_calkowita_bez_cyfr_to_problem_yaml_z_opisem_a_nie_indexerror():
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, problemy = wczytaj_workflow_z_tekstu("steps:\n  a: !!int 0b\n")

    assert workflow is None
    assert problemy == [
        Problem(None, NIEPOPRAWNY_YAML, "niepoprawny YAML: nieprawidlowa wartosc")]


def test_liczba_calkowita_bez_cyfr_w_pliku_to_ten_sam_problem_co_w_tekscie(tmp_path):
    plik = _zapisz(tmp_path, "steps:\n  a: !!int 0b\n")

    workflow, problemy = wczytaj_workflow(plik)

    assert workflow is None
    assert problemy == [
        Problem(None, NIEPOPRAWNY_YAML, "niepoprawny YAML: nieprawidlowa wartosc")]


def test_data_spoza_kalendarza_to_problem_yaml_a_nie_wyjatek_value_error():
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, problemy = wczytaj_workflow_z_tekstu("steps:\n  a: 2024-02-30\n")

    assert workflow is None
    assert problemy == [
        Problem(None, NIEPOPRAWNY_YAML, "niepoprawny YAML: nieprawidlowa wartosc")]


TEKST_BEZ_NAME = "steps:\n  plan:\n    skill: a\n  review:\n    skill: b\n    fanout: 3\n"


def test_workflow_z_tekstu_bez_pola_name_i_bez_podanej_nazwy_ma_pusta_nazwe():
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, problemy = wczytaj_workflow_z_tekstu(TEKST_BEZ_NAME)

    assert problemy == []
    assert workflow is not None
    assert workflow.nazwa == ""




def test_zmiana_pola_przy_pustej_nazwie_nie_dopisuje_pola_name_do_tekstu():
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, _ = wczytaj_workflow_z_tekstu(TEKST_BEZ_NAME)
    assert workflow is not None
    _krok(workflow, "review").fanout = 5

    tekst = _tekst_po_zapisie(workflow)

    assert tekst == TEKST_BEZ_NAME.replace("fanout: 3", "fanout: 5")
    assert "name" not in tekst


ZLE_WARTOSCI_YAML = {
    "klucz_slownika_tagu_bool": ("KeyError", "steps:\n  a: !!bool x\n"),
    "tag_zbioru_z_napisem": ("AttributeError", "steps:\n  a: !!set x\n"),
    "liczba_calkowita_bez_cyfr": ("IndexError", "steps:\n  a: !!int 0b\n"),
    "data_spoza_kalendarza": ("ValueError", "steps:\n  a: 2024-02-30\n"),
}


@pytest.mark.parametrize(("rodzaj", "tresc"), ZLE_WARTOSCI_YAML.values(),
                         ids=ZLE_WARTOSCI_YAML.keys())
def test_wartosc_ze_zlym_tagiem_yaml_to_problem_yaml_a_nie_wyjatek_biblioteki(rodzaj, tresc):
    from cockpit.services.workflow import _nowy_yaml, wczytaj_workflow_z_tekstu

    # wejscie naprawde rzuca wskazanym wyjatkiem biblioteki, wiec kazdy element krotki `except` ma wlasny test
    with pytest.raises(Exception) as surowy:
        _nowy_yaml().load(tresc)
    assert type(surowy.value).__name__ == rodzaj

    workflow, problemy = wczytaj_workflow_z_tekstu(tresc)

    assert workflow is None
    assert problemy == [
        Problem(None, NIEPOPRAWNY_YAML, "niepoprawny YAML: nieprawidlowa wartosc")]


@pytest.mark.parametrize(("rodzaj", "tresc"), ZLE_WARTOSCI_YAML.values(),
                         ids=ZLE_WARTOSCI_YAML.keys())
def test_wartosc_ze_zlym_tagiem_yaml_w_pliku_to_ten_sam_problem_co_w_tekscie(tmp_path, rodzaj, tresc):
    workflow, problemy = wczytaj_workflow(_zapisz(tmp_path, tresc))

    assert workflow is None
    assert problemy == [
        Problem(None, NIEPOPRAWNY_YAML, "niepoprawny YAML: nieprawidlowa wartosc")]


def test_zmiana_nazwy_przy_pustej_nazwie_dopisuje_pole_name_jako_pierwszy_klucz():
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, _ = wczytaj_workflow_z_tekstu(TEKST_BEZ_NAME)
    assert workflow is not None
    workflow.nazwa = "nowa"

    assert _tekst_po_zapisie(workflow).startswith("name: nowa\nsteps:")


def test_zbyt_gleboko_zagniezdzony_yaml_ma_komunikat_o_zagniezdzeniu():
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, problemy = wczytaj_workflow_z_tekstu(
        "name: w\nsteps:\n  plan:\n    skill: a\n    x: " + "[" * 5000 + "]" * 5000 + "\n")

    assert workflow is None
    assert problemy == [Problem(
        None, NIEPOPRAWNY_YAML, "niepoprawny YAML: zbyt gleboko zagniezdzona struktura")]


def test_przekroczenie_glebokosci_rekurencji_przy_budowie_krokow_to_problem_yaml_z_tym_samym_komunikatem(
    monkeypatch,
):
    from cockpit.services import workflow as modul

    def za_gleboko(_kroki):
        raise RecursionError

    monkeypatch.setattr(modul, "_zbuduj_kroki", za_gleboko)

    workflow, problemy = modul.wczytaj_workflow_z_tekstu(DWA_KROKI)

    assert workflow is None
    assert problemy == [Problem(
        None, NIEPOPRAWNY_YAML, "niepoprawny YAML: zbyt gleboko zagniezdzona struktura")]


def test_tekst_z_samotnym_surogatem_nie_jest_poprawnym_utf8_i_daje_jeden_problem_odczytu():
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, problemy = wczytaj_workflow_z_tekstu("name: w\n\ud800\nsteps:\n")

    assert workflow is None
    assert problemy == [Problem(None, BLAD_ODCZYTU, "tekst nie jest poprawnym UTF-8")]




def test_drugi_zapis_niezmienionego_po_pierwszym_zapisie_workflow_oddaje_te_same_bajty(tmp_path):
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, _ = wczytaj_workflow_z_tekstu(PLIK_Z_KOMENTARZAMI_I_CUDZYSLOWAMI)
    assert workflow is not None
    _krok(workflow, "review").fanout = 5
    pierwszy, drugi = tmp_path / "pierwszy.yaml", tmp_path / "drugi.yaml"

    zapisz_workflow(workflow, pierwszy)
    zapisz_workflow(workflow, drugi)

    assert b"fanout: 5" in pierwszy.read_bytes()
    assert drugi.read_bytes() == pierwszy.read_bytes()
    assert not workflow._zmieniony()


def test_plik_workflow_z_bajtami_niebedacymi_utf8_to_problem_odczytu_a_nie_wyjatek(tmp_path):
    plik = tmp_path / "zly.yaml"
    plik.write_bytes(b"\xff\xfe")

    workflow, problemy = wczytaj_workflow(plik)

    assert workflow is None
    assert [(p.krok, p.kod) for p in problemy] == [(None, BLAD_ODCZYTU)]
    assert problemy[0].komunikat.startswith("nie mozna odczytac pliku: ")


def test_nieudany_zapis_zmienionego_workflow_zostawia_zmiane_niezapisana_a_ponowny_zapis_ja_zapisuje(
    tmp_path,
):
    from cockpit.services.workflow import wczytaj_workflow_z_tekstu

    workflow, _ = wczytaj_workflow_z_tekstu(PLIK_Z_KOMENTARZAMI_I_CUDZYSLOWAMI)
    assert workflow is not None
    _krok(workflow, "review").fanout = 5
    katalog_zamiast_pliku = tmp_path / "cel.yaml"
    katalog_zamiast_pliku.mkdir()

    with pytest.raises(OSError):
        zapisz_workflow(workflow, katalog_zamiast_pliku)

    assert workflow._zmieniony()
    poprawny = tmp_path / "poprawny.yaml"
    zapisz_workflow(workflow, poprawny)
    assert b"fanout: 5" in poprawny.read_bytes()
    assert not workflow._zmieniony()

