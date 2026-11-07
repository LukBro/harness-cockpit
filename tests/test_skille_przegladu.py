"""Skille przeglądu (HAR-74): contract-check, explain-change, review-unknown.

Czytamy prawdziwe pliki `.cockpit/skills/<id>/SKILL.md` tego repozytorium
przez produkcyjny `rozstrzygnij_rejestr`. Katalog domowy jest pusty, żeby
skille użytkownika nie wpływały na wynik.
"""
import re
from pathlib import Path

import pytest

from cockpit.services.invariants import Rola, profil_roli
from cockpit.services.skills import Rejestr, Skill, rozstrzygnij_rejestr
from cockpit.services.walidacja import KontraktWerdyktu, Naruszenie, Niezmiennik, Werdykt, sprawdz_werdykt

# parents[1], nie `git rev-parse`: pod mutmut testy czytają kopię w `mutants/`.
KORZEN_REPO = Path(__file__).resolve().parents[1]

OCZEKIWANE = {
    "contract-check": ["rozjazdy-kontraktu.md"],
    "explain-change": ["notatka-zmiany.md"],
    "review-unknown": ["zastrzezenia.md"],
}
IDENTYFIKATORY = list(OCZEKIWANE)


def wczytaj_rejestr(tmp_path: Path) -> Rejestr:
    dom = tmp_path / "dom"
    dom.mkdir()
    return rozstrzygnij_rejestr(KORZEN_REPO, dom)


def skill_przegladu(tmp_path: Path, id: str) -> Skill:
    rejestr = wczytaj_rejestr(tmp_path)
    assert id in rejestr.skille, f"brak skilla {id} w rejestrze: {sorted(rejestr.skille)}"
    return rejestr.skille[id]


def test_trzy_skille_przegladu_wczytuja_sie_z_repozytorium_bez_bledow(tmp_path):
    rejestr = wczytaj_rejestr(tmp_path)
    assert set(IDENTYFIKATORY) <= set(rejestr.skille)


@pytest.mark.parametrize("id", IDENTYFIKATORY)
def test_skill_przegladu_pochodzi_ze_zrodla_kokpitu_w_repozytorium(tmp_path, id):
    skill = skill_przegladu(tmp_path, id)
    assert skill.id == id
    assert skill.katalog == KORZEN_REPO / ".cockpit" / "skills" / id
    assert (skill.katalog / "SKILL.md").is_file()


@pytest.mark.parametrize("id", IDENTYFIKATORY)
def test_skill_przegladu_ma_niepusta_nazwe_i_tresc(tmp_path, id):
    skill = skill_przegladu(tmp_path, id)
    assert skill.name.strip()
    assert skill.brief.strip()


@pytest.mark.parametrize("id", IDENTYFIKATORY)
def test_skill_przegladu_jest_recenzentem_bez_prawa_zapisu(tmp_path, id):
    skill = skill_przegladu(tmp_path, id)
    assert skill.role == Rola.reviewer
    assert profil_roli(skill.role).zapis is False


@pytest.mark.parametrize("id", IDENTYFIKATORY)
def test_skill_przegladu_oddaje_wynik_jako_artefakt_runu(tmp_path, id):
    assert skill_przegladu(tmp_path, id).produces == OCZEKIWANE[id]


# HAR-176: recenzenci dostaja dowod z kroku diff; stala lista niezalezna od kodu.
OCZEKIWANE_WEJSCIA = {
    # HAR-177: dowody komend uruchamia silnik w krokach check, recenzent dostaje je jako wejscia
    "contract-check": ["zmiana.diff", "dowod-typecheck.json", "dowod-contract.json"],
    "explain-change": ["zmiana.diff", "zmienione-linie.json"],
    "review-unknown": ["zmiana.diff"],
}


@pytest.mark.parametrize("id", IDENTYFIKATORY)
def test_skill_przegladu_dostaje_diff_runu_jako_wejscie(tmp_path, id):
    skill = skill_przegladu(tmp_path, id)
    assert skill.consumes == OCZEKIWANE_WEJSCIA[id]


@pytest.mark.parametrize("id", IDENTYFIKATORY)
def test_skill_przegladu_nie_deklaruje_capability_bo_komendy_uruchamia_silnik(tmp_path, id):
    assert skill_przegladu(tmp_path, id).needs == []


def brief_znormalizowany(tmp_path: Path, id: str) -> str:
    # Briefy łamią zdania w liniach; asercje nie mogą zależeć od zawijania.
    return " ".join(skill_przegladu(tmp_path, id).brief.split())


def test_recenzent_kontraktu_czyta_dowody_silnika_z_wejsc_a_nie_uruchamia_komend(tmp_path):
    brief = brief_znormalizowany(tmp_path, "contract-check")
    assert "dowod-typecheck.json" in brief
    assert "dowod-contract.json" in brief
    assert "source: engine" in brief
    assert "exit_code" in brief
    assert "brak dowodu" in brief


def test_recenzent_kontraktu_nie_odsyla_do_dawnego_uruchamiania_komend_przez_poprzedni_krok(tmp_path):
    brief = brief_znormalizowany(tmp_path, "contract-check")
    assert "capability `contract`" not in brief
    assert "capability `typecheck`" not in brief
    assert "Komendy uruchamia wcześniejszy krok" not in brief


def test_wyjasnienie_zmiany_ma_notatke_w_ksztalcie_co_zmieniono_jakie_ryzyko_jak_to_sprawdzic(
    tmp_path,
):
    brief = brief_znormalizowany(tmp_path, "explain-change")
    # Szablon notatki (blok z treścią sekcji), nie wzmianki o nagłówkach w punktach.
    zmieniono = "## Co zmieniono <zachowanie widziane z zewnątrz"
    ryzyko = "## Jakie ryzyko - [plik:linia] scenariusz: <"
    sprawdzenie = "## Jak to sprawdzić <jeden krok"
    assert zmieniono in brief
    assert ryzyko in brief
    assert sprawdzenie in brief
    assert brief.index(zmieniono) < brief.index(ryzyko) < brief.index(sprawdzenie)


def test_wyjasnienie_zmiany_wymaga_kotwicy_plik_linia_z_diffu_przy_kazdym_ryzyku(tmp_path):
    brief = brief_znormalizowany(tmp_path, "explain-change")
    assert "Każde ryzyko zaczyna się od kotwicy `[plik:linia]` z `zmiana.diff`." in brief
    assert "Silnik odrzuci notatkę z kotwicą spoza zmienionych linii diffu" in brief


def test_wyjasnienie_zmiany_czyta_zmiane_ze_sciezek_diffu_i_zmienionych_linii(tmp_path):
    brief = brief_znormalizowany(tmp_path, "explain-change")
    assert "`zmiana.diff`" in brief
    assert "`zmienione-linie.json`" in brief


def test_sekcja_jak_to_sprawdzic_w_notatce_jest_obowiazkowa_a_nie_opcjonalna(tmp_path):
    brief = brief_znormalizowany(tmp_path, "explain-change")
    assert "`## Jak to sprawdzić` jest obowiązkowa" in brief
    assert "Opcjonalnie na końcu" not in brief


def test_wyjasnienie_zmiany_zakazuje_oceniac_skladnie_i_styl(tmp_path):
    brief = brief_znormalizowany(tmp_path, "explain-change")
    assert "Nie oceniasz składni ani stylu" in brief
    assert "To nie jest ryzyko." in brief


def test_wyjasnienie_zmiany_podaje_dobry_przyklad_ryzyka_z_warunkiem_skutkiem_i_osoba(tmp_path):
    brief = brief_znormalizowany(tmp_path, "explain-change")
    # HAR-176: dobry przykład niesie kotwicę `[plik:linia]` przed scenariuszem (np. `[src/x.py:10]`)
    assert re.search(r"\[[\w./-]+:\d+\] scenariusz: Run przerwany w trakcie zapisu zostawi plik werdyktu", brief)
    assert "Dotknie każdego, kto zatwierdza po samym kolorze kroku." in brief
    assert "Dobry przykład ma warunek, skutek i osobę, której to szkodzi." in brief


def test_wyjasnienie_zmiany_podaje_zly_przyklad_oceny_stylu_bez_warunku_i_skutku(tmp_path):
    brief = brief_znormalizowany(tmp_path, "explain-change")
    assert "Zły: „Funkcja jest długa i trudna do czytania.”" in brief
    assert "Zły nie ma żadnego z trzech." in brief


# --- werdykt jako kontrakt skilla (HAR-177) -------------------------------------------------

# stala lista niezalezna od frontmatteru: (pass, fail) kazdego recenzenta z werdyktem
OCZEKIWANE_WERDYKTY = {
    "contract-check": (("zgodny",), ("rozjazd", "brak dowodu")),
    "review-unknown": (("brak zastrzeżeń",), ("zastrzeżenia",)),
}


@pytest.mark.parametrize("id", sorted(OCZEKIWANE_WERDYKTY))
def test_recenzent_deklaruje_werdykt_z_ustalonymi_wartosciami_przejscia_i_porazki(tmp_path, id):
    przechodzi, oblewa = OCZEKIWANE_WERDYKTY[id]

    assert skill_przegladu(tmp_path, id).verdict == KontraktWerdyktu(
        pass_=przechodzi, fail=oblewa)


def test_wyjasnienie_zmiany_nie_deklaruje_werdyktu_bo_jego_wynik_to_notatka(tmp_path):
    assert skill_przegladu(tmp_path, "explain-change").verdict is None


@pytest.mark.parametrize(
    ("id", "linia", "oczekiwany"),
    [
        ("contract-check", "WERDYKT: zgodny", Werdykt("zgodny", True)),
        ("contract-check", "WERDYKT: rozjazd", Werdykt("rozjazd", False)),
        ("contract-check", "WERDYKT: brak dowodu", Werdykt("brak dowodu", False)),
        ("review-unknown", "WERDYKT: brak zastrzeżeń", Werdykt("brak zastrzeżeń", True)),
        ("review-unknown", "WERDYKT: zastrzeżenia", Werdykt("zastrzeżenia", False)),
    ],
)
def test_linia_werdyktu_z_briefu_recenzenta_jest_przyjmowana_przez_jego_kontrakt(
    tmp_path, id, linia, oczekiwany
):
    skill = skill_przegladu(tmp_path, id)
    assert skill.verdict is not None
    plik = tmp_path / "artefakt" / skill.produces[0]
    plik.parent.mkdir()
    plik.write_text(linia + "\n- pozycja listy\n", encoding="utf-8")

    assert sprawdz_werdykt(plik, skill.verdict) == oczekiwany


def test_werdykt_zastrzezen_z_licznikiem_nie_miesci_sie_w_kontrakcie_review_unknown(tmp_path):
    skill = skill_przegladu(tmp_path, "review-unknown")
    assert skill.verdict is not None
    plik = tmp_path / "zastrzezenia.md"
    plik.write_text("WERDYKT: zastrzeżenia: 3\n- plik:1\n", encoding="utf-8")

    assert sprawdz_werdykt(plik, skill.verdict) == Naruszenie(
        Niezmiennik.evidence_required, "werdykt spoza kontraktu w zastrzezenia.md")


def test_brief_review_unknown_podaje_linie_werdyktu_bez_licznika(tmp_path):
    brief = brief_znormalizowany(tmp_path, "review-unknown")
    assert "WERDYKT: brak zastrzeżeń" in brief
    assert "WERDYKT: zastrzeżenia" in brief
    assert re.search(r"WERDYKT: zastrzeżenia:", brief) is None
    assert "zastrzeżenia: N" not in brief


def test_brief_recenzenta_kontraktu_podaje_trzy_linie_werdyktu_z_kontraktu(tmp_path):
    brief = brief_znormalizowany(tmp_path, "contract-check")
    for linia in ("WERDYKT: zgodny", "WERDYKT: rozjazd", "WERDYKT: brak dowodu"):
        assert linia in brief
