"""Skille przeglądu (HAR-74): contract-check, explain-change, review-unknown.

Czytamy prawdziwe pliki `.cockpit/skills/<id>/SKILL.md` tego repozytorium
przez produkcyjny `rozstrzygnij_rejestr`. Katalog domowy jest pusty, żeby
skille użytkownika nie wpływały na wynik.
"""
from pathlib import Path

import pytest

from cockpit.services.invariants import Rola, profil_roli
from cockpit.services.skills import Rejestr, Skill, rozstrzygnij_rejestr

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


@pytest.mark.parametrize("id", IDENTYFIKATORY)
def test_skill_przegladu_nie_wymaga_wejsc_ani_poprzednikow(tmp_path, id):
    skill = skill_przegladu(tmp_path, id)
    assert skill.consumes == []
    assert skill.needs == []


def test_recenzent_kontraktu_wskazuje_eksport_openapi_i_build_frontu_jako_dowod(tmp_path):
    brief = skill_przegladu(tmp_path, "contract-check").brief
    assert "python tools/eksport_openapi.py --sprawdz" in brief
    assert "npm run build" in brief


def brief_znormalizowany(tmp_path: Path, id: str) -> str:
    # Briefy łamią zdania w liniach; asercje nie mogą zależeć od zawijania.
    return " ".join(skill_przegladu(tmp_path, id).brief.split())


def test_recenzent_kontraktu_nie_stwierdza_zgodnosci_gdy_brakuje_wyniku_komendy(tmp_path):
    brief = brief_znormalizowany(tmp_path, "contract-check")
    assert (
        "Jeśli wyniku którejkolwiek komendy brakuje (pusty wynik, brak pliku), "
        "to jest `brak dowodu`: napisz to wprost i NIGDY nie stwierdzaj zgodności"
    ) in brief


def test_recenzent_kontraktu_pisze_zgodny_tylko_gdy_oba_wyniki_sa_zielone(tmp_path):
    brief = brief_znormalizowany(tmp_path, "contract-check")
    assert (
        "`zgodny` wolno ci napisać tylko, gdy oba wyniki komend są w wejściu "
        "i oba są zielone"
    ) in brief


def test_wyjasnienie_zmiany_ma_notatke_w_ksztalcie_co_zmieniono_jakie_ryzyko(tmp_path):
    brief = brief_znormalizowany(tmp_path, "explain-change")
    # Szablon notatki (blok z treścią sekcji), nie wzmianki o nagłówkach w punktach.
    zmieniono = "## Co zmieniono <zachowanie widziane z zewnątrz"
    ryzyko = "## Jakie ryzyko <konkretny scenariusz"
    assert zmieniono in brief
    assert ryzyko in brief
    assert brief.index(zmieniono) < brief.index(ryzyko)


def test_wyjasnienie_zmiany_zakazuje_oceniac_skladnie_i_styl(tmp_path):
    brief = brief_znormalizowany(tmp_path, "explain-change")
    assert "Nie oceniasz składni ani stylu" in brief
    assert "To nie jest ryzyko." in brief


def test_wyjasnienie_zmiany_podaje_dobry_przyklad_ryzyka_z_warunkiem_skutkiem_i_osoba(tmp_path):
    brief = brief_znormalizowany(tmp_path, "explain-change")
    assert "Dobry: „Run przerwany w trakcie zapisu zostawi plik werdyktu" in brief
    assert "Dotknie każdego, kto zatwierdza po samym kolorze kroku." in brief
    assert "Dobry przykład ma warunek, skutek i osobę, której to szkodzi." in brief


def test_wyjasnienie_zmiany_podaje_zly_przyklad_oceny_stylu_bez_warunku_i_skutku(tmp_path):
    brief = brief_znormalizowany(tmp_path, "explain-change")
    assert "Zły: „Funkcja jest długa i trudna do czytania.”" in brief
    assert "Zły nie ma żadnego z trzech." in brief
