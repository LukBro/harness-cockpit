"""HAR-181: workflowy projektu z kanwy - `GET /api/workflows`, `PUT /api/workflows/{name}`,
`GET /api/workflows/{name}/validate` (parametr `project`; plik `.cockpit/workflows/<name>.yaml`).

Kontrakt (opis w raporcie test-writera):
 * `GET /api/workflows` -> lista `WorkflowNaLiscie {name, liczba_krokow, liczba_bledow}` posortowana
   po `name` (nazwa pliku bez `.yaml`); czyta pliki przez runnera projektu. `liczba_krokow` to
   wszystkie kroki z mapy `steps` (takze niepoprawne), `liczba_bledow` to liczba problemow poziomu
   `blad` z tej samej walidacji, ktora zwraca `/validate` (ostrzezenia nie licza sie);
 * `GET /api/workflows/{name}/validate` -> `OdpowiedzWalidacji {problemy: [ProblemKroku {poziom,
   krok, komunikat}], run_startuje_ze: "HEAD", stan: "kopia robocza"}`; problemy z wczytania pliku
   (`wczytaj_workflow_z_tekstu`) idą PRZED problemami `waliduj_workflow`; plik, ktorego nie da sie
   wczytac, daje jeden problem poziomu `blad` z `krok: null`, a nie blad HTTP;
 * `PUT /api/workflows/{name}` z cialem `ZapisWorkflowu {tresc}` zapisuje tresc przez runnera
   projektu tak, jak ja przyslano (komentarze, kolejnosc kluczy i cudzyslowy zostaja); odpowiedz to
   `OdpowiedzWalidacji` zapisanego pliku; plik, ktorego nie da sie wczytac, to 422 z komunikatem
   pierwszego problemu i brak zapisu; workflow z bledami walidatora jest zapisywany;
 * rejestr skilli dla walidatora pochodzi z katalogu (tabela `definicja`) projektu; `dom` do
   ostrzezenia o skillu spoza repozytorium pochodzi z odpowiedzi runnera, nie z `Path.home()` serwera.
Runner robi PRAWDZIWE operacje na plikach w katalogu testu (`RunnerZPlikami`).
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from types import SimpleNamespace

from cockpit.api import bus
from cockpit.db.models import Project
from cockpit.services import workflowy_api
from cockpit.services.invariants import Rola
from cockpit.services.skills import Rejestr
from test_projekty_zmiana import (
    KOMUNIKAT_OFFLINE,
    KOMUNIKAT_POZA_ZAKRESEM,
    KOMUNIKAT_PROJEKT_NIEZNANY,
    swiat,
)
from test_skille_api import (
    _klient_z_katalogiem,
    _skill,
    _wpisy as _wpisy_skilli,
    _wstaw_definicje,
)
from test_skille_api import _lista as _lista_skilli
from test_workflow_model import PLIK_NIEKANONICZNY, PLIK_Z_KOMENTARZAMI_I_CUDZYSLOWAMI

FIXTURY_Z_INNYCH_TESTOW = (swiat,)   # fixture pytest zzaimportowana z innego pliku testow

PODKATALOG = ".cockpit/skills"
POLA_PROBLEMU = {"poziom", "krok", "komunikat"}
OSTRZEZENIE = "ostrzezenie"
BLAD = "blad"

WF_POPRAWNY = (
    "name: ok\nsteps:\n"
    "  plan:\n    skill: planner\n"
    "  review:\n    skill: recenzent\n    needs: [plan]\n")
WF_JEDEN_BLAD_I_JEDNO_OSTRZEZENIE = (
    "name: zeta\nsteps:\n"
    "  plan:\n    skill: planner\n"
    "  review:\n    skill: recenzent\n    needs: [plan]\n    optional: true\n"
    "  koniec:\n    skill: ghost\n    needs: [review]\n")
WF_DWA_BLEDY = (
    "name: dwa\nsteps:\n"
    "  a:\n    skill: ghost\n"
    "  b:\n    skill: planner\n    needs: [nie-ma]\n")
WF_KROK_BEZ_SKILLA_I_NIEZNANY_SKILL = (
    "name: mieszany\nsteps:\n"
    "  bez-skilla:\n    fanout: 2\n"
    "  dobry:\n    skill: ghost\n")
WF_NIEPOPRAWNY_YAML = "name: w\nsteps:\n  plan: [niezamkniete\n    skill: x\n"


def _plik_wf(swiat, nazwa: str) -> Path:
    return swiat.korzen / ".cockpit" / "workflows" / f"{nazwa}.yaml"


def _zapisz_wf_na_dysku(swiat, nazwa: str, tresc: str) -> Path:
    plik = _plik_wf(swiat, nazwa)
    plik.parent.mkdir(parents=True, exist_ok=True)
    plik.write_bytes(tresc.encode("utf-8"))
    return plik


def _skille_testowe(swiat) -> None:
    _skill(swiat.korzen, PODKATALOG, "planner", role="author", produces=("plan.md",))
    _skill(swiat.korzen, PODKATALOG, "recenzent", role="reviewer", consumes=("plan.md",),
           produces=("raport.md",))


async def _lista(klient, projekt):
    return await klient.get("/api/workflows", params={"project": projekt})


async def _waliduj(klient, nazwa, projekt):
    return await klient.get(f"/api/workflows/{nazwa}/validate", params={"project": projekt})


async def _zapisz(klient, nazwa, projekt, tresc):
    return await klient.put(
        f"/api/workflows/{nazwa}", params={"project": projekt}, json={"tresc": tresc})


def _problemy(odp) -> list[tuple]:
    assert odp.status_code == 200, odp.text
    assert all(set(p) == POLA_PROBLEMU for p in odp.json()["problemy"])
    return [(p["poziom"], p["krok"], p["komunikat"]) for p in odp.json()["problemy"]]


def _drzewo(swiat) -> list[str]:
    baza = swiat.korzen.parent
    return sorted(str(p.relative_to(baza)) for p in baza.rglob("*")
                  if not p.name.startswith("zmiana.db"))


def _typy_wywolan(swiat) -> list[str]:
    return swiat.runner.typy()


# =================================================================================
# 1. GET /api/workflows?project=<id>: lista z liczba krokow i bledow
# =================================================================================

async def test_lista_workflow_projektu_ma_nazwe_liczbe_krokow_i_liczbe_bledow(swiat):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "ok", WF_POPRAWNY)
    _zapisz_wf_na_dysku(swiat, "dwa", WF_DWA_BLEDY)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert odp.status_code == 200, odp.text
    assert all(set(w) == {"name", "liczba_krokow", "liczba_bledow"} for w in odp.json())
    assert odp.json() == [
        {"name": "dwa", "liczba_krokow": 2, "liczba_bledow": 2},
        {"name": "ok", "liczba_krokow": 2, "liczba_bledow": 0},
    ]


async def test_lista_workflow_jest_posortowana_po_nazwie_a_nie_po_kolejnosci_plikow(swiat):
    _skille_testowe(swiat)
    for nazwa in ("zeta", "alfa", "mid"):
        _zapisz_wf_na_dysku(swiat, nazwa, WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert [w["name"] for w in odp.json()] == ["alfa", "mid", "zeta"]


async def test_ostrzezenie_nie_liczy_sie_do_liczby_bledow_na_liscie(swiat):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "zeta", WF_JEDEN_BLAD_I_JEDNO_OSTRZEZENIE)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert odp.json() == [{"name": "zeta", "liczba_krokow": 3, "liczba_bledow": 1}]


async def test_workflow_z_samym_ostrzezeniem_ma_zero_bledow_na_liscie(swiat):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "tylko-ostrzezenie", (
        "name: tylko-ostrzezenie\nsteps:\n  plan:\n    skill: planner\n    optional: true\n"))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert odp.json() == [{"name": "tylko-ostrzezenie", "liczba_krokow": 1, "liczba_bledow": 0}]


async def test_liczba_krokow_obejmuje_kroki_niepoprawne_a_liczba_bledow_problemy_wczytania_i_walidacji(
    swiat,
):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "mieszany", WF_KROK_BEZ_SKILLA_I_NIEZNANY_SKILL)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert odp.json() == [{"name": "mieszany", "liczba_krokow": 2, "liczba_bledow": 2}]


async def test_plik_workflow_ktorego_nie_da_sie_wczytac_jest_na_liscie_z_zerem_krokow_i_jednym_bledem(
    swiat,
):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "zepsuty", WF_NIEPOPRAWNY_YAML)
    _zapisz_wf_na_dysku(swiat, "ok", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert odp.json() == [
        {"name": "ok", "liczba_krokow": 2, "liczba_bledow": 0},
        {"name": "zepsuty", "liczba_krokow": 0, "liczba_bledow": 1},
    ]


async def test_liczba_bledow_na_liscie_zgadza_sie_z_liczba_bledow_w_walidacji_kazdego_workflow(swiat):
    _skille_testowe(swiat)
    pliki = {"ok": WF_POPRAWNY, "dwa": WF_DWA_BLEDY, "zeta": WF_JEDEN_BLAD_I_JEDNO_OSTRZEZENIE,
             "mieszany": WF_KROK_BEZ_SKILLA_I_NIEZNANY_SKILL, "zepsuty": WF_NIEPOPRAWNY_YAML}
    for nazwa, tresc in pliki.items():
        _zapisz_wf_na_dysku(swiat, nazwa, tresc)
    klient = await _klient_z_katalogiem(swiat)

    lista = {w["name"]: w["liczba_bledow"] for w in (await _lista(klient, swiat.projekt_a)).json()}
    walidacja = {}
    for nazwa in pliki:
        walidacja[nazwa] = sum(
            1 for poziom, _, _ in _problemy(await _waliduj(klient, nazwa, swiat.projekt_a))
            if poziom == BLAD)

    assert lista == walidacja == {"ok": 0, "dwa": 2, "zeta": 1, "mieszany": 2, "zepsuty": 1}


async def test_na_liscie_sa_tylko_pliki_yaml_workflow_a_nie_inne_pliki_i_katalogi(swiat):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "ok", WF_POPRAWNY)
    katalog = swiat.korzen / ".cockpit" / "workflows"
    (katalog / "notatki.md").write_text("nie workflow", encoding="utf-8")
    (katalog / "stary.yml").write_text(WF_POPRAWNY, encoding="utf-8")
    (katalog / "katalog.yaml").mkdir()
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert [w["name"] for w in odp.json()] == ["ok"]


async def test_projekt_bez_katalogu_workflows_ma_pusta_liste_a_nie_blad(swiat):
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert (odp.status_code, odp.json()) == (200, [])


async def test_lista_workflow_pyta_runnera_projektu(swiat):
    _zapisz_wf_na_dysku(swiat, "ok", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)

    await _lista(klient, swiat.projekt_a)

    assert swiat.runner.wywolania
    assert {w[0] for w in swiat.runner.wywolania} == {swiat.runner_a}
    assert "write_file" not in _typy_wywolan(swiat)


async def test_lista_workflow_nie_obejmuje_plikow_workflow_projektu_innego_zespolu(swiat):
    _zapisz_wf_na_dysku(swiat, "ten-projekt", WF_POPRAWNY)
    (swiat.korzen_b / ".cockpit" / "workflows").mkdir(parents=True)
    (swiat.korzen_b / ".cockpit" / "workflows" / "cudzy.yaml").write_text(WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert [w["name"] for w in odp.json()] == ["ten-projekt"]


# =================================================================================
# 2. GET /api/workflows/{name}/validate: problemy z waliduj_workflow
# =================================================================================

async def test_poprawny_workflow_ma_pusta_liste_problemow_i_informacje_skad_startuje_run(swiat):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "ok", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _waliduj(klient, "ok", swiat.projekt_a)

    assert odp.status_code == 200, odp.text
    assert odp.json() == {"problemy": [], "run_startuje_ze": "HEAD", "stan": "kopia robocza"}


async def test_walidacja_zwraca_oba_bledy_naraz_w_kolejnosci_krokow_z_pliku(swiat):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "dwa", WF_DWA_BLEDY)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _waliduj(klient, "dwa", swiat.projekt_a)

    assert _problemy(odp) == [
        (BLAD, "a", "nieznany skill: ghost"),
        (BLAD, "b", "krok zalezy od nieznanego kroku: nie-ma"),
    ]
    assert (odp.json()["run_startuje_ze"], odp.json()["stan"]) == ("HEAD", "kopia robocza")


async def test_ostrzezenie_jest_ostrzezeniem_a_nie_bledem_i_idzie_obok_bledu(swiat):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "zeta", WF_JEDEN_BLAD_I_JEDNO_OSTRZEZENIE)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _waliduj(klient, "zeta", swiat.projekt_a)

    assert _problemy(odp) == [
        (OSTRZEZENIE, "review", "pole optional nie ma jeszcze skutku: silnik go nie odczytuje"),
        (BLAD, "koniec", "nieznany skill: ghost"),
    ]


async def test_workflow_z_samym_ostrzezeniem_ma_na_liscie_problemow_tylko_ostrzezenie(swiat):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "x", "name: x\nsteps:\n  plan:\n    skill: planner\n    optional: true\n")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _waliduj(klient, "x", swiat.projekt_a)

    assert _problemy(odp) == [
        (OSTRZEZENIE, "plan", "pole optional nie ma jeszcze skutku: silnik go nie odczytuje")]


async def test_rola_wpisana_w_kroku_jest_ostrzezeniem_z_rola_skilla_z_rejestru_projektu(swiat):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "x", "name: x\nsteps:\n  plan:\n    skill: planner\n    role: reviewer\n")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _waliduj(klient, "x", swiat.projekt_a)

    assert _problemy(odp) == [
        (OSTRZEZENIE, "plan", "role w kroku jest ignorowane: krok pojdzie z rola author")]


async def test_krok_bez_skilla_z_pliku_i_nieznany_skill_to_dwa_bledy_najpierw_ten_z_wczytania(swiat):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "mieszany", WF_KROK_BEZ_SKILLA_I_NIEZNANY_SKILL)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _waliduj(klient, "mieszany", swiat.projekt_a)

    assert _problemy(odp) == [
        (BLAD, "bez-skilla", "krok nie ma pola skill ani system"),
        (BLAD, "dobry", "nieznany skill: ghost"),
    ]


async def test_plik_ktorego_nie_da_sie_wczytac_to_jeden_blad_calego_pliku_a_nie_blad_http(swiat):
    _zapisz_wf_na_dysku(swiat, "zepsuty", WF_NIEPOPRAWNY_YAML)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _waliduj(klient, "zepsuty", swiat.projekt_a)

    problemy = _problemy(odp)
    assert len(problemy) == 1
    poziom, krok, komunikat = problemy[0]
    assert (poziom, krok) == (BLAD, None)
    assert komunikat.startswith("niepoprawny YAML")
    assert (odp.json()["run_startuje_ze"], odp.json()["stan"]) == ("HEAD", "kopia robocza")


@pytest.mark.parametrize(("tresc", "komunikat"), [
    ("name: w\nsteps:\n  plan:\n    skill: a\n  plan:\n    skill: b\n", None),
    ("name: w\n", "brak mapy steps"),
    ("", "brak mapy steps"),
], ids=["zduplikowany_krok", "brak_steps", "pusty_plik"])
async def test_inne_pliki_nie_do_wczytania_tez_daja_jeden_blad_calego_pliku(swiat, tresc, komunikat):
    _zapisz_wf_na_dysku(swiat, "zepsuty", tresc)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _waliduj(klient, "zepsuty", swiat.projekt_a)

    problemy = _problemy(odp)
    assert len(problemy) == 1
    assert problemy[0][:2] == (BLAD, None)
    if komunikat is not None:
        assert problemy[0][2] == komunikat


async def test_artefakt_konsumowany_przez_skill_z_rejestru_musi_byc_produkowany_przez_przodka(swiat):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "bez-planu", (
        "name: bez-planu\nsteps:\n  plan:\n    skill: planner\n  review:\n    skill: recenzent\n"))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _waliduj(klient, "bez-planu", swiat.projekt_a)

    assert _problemy(odp) == [
        (BLAD, "review", "artefakt plan.md nie jest produkowany przez zadnego przodka kroku")]


async def test_skill_z_bledem_w_pliku_nie_jest_dla_walidatora_poprawnym_skillem(swiat):
    _skille_testowe(swiat)
    _skill(swiat.korzen, PODKATALOG, "zepsuty", role="czarodziej")
    _zapisz_wf_na_dysku(swiat, "x", "name: x\nsteps:\n  plan:\n    skill: zepsuty\n")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _waliduj(klient, "x", swiat.projekt_a)

    problemy = _problemy(odp)
    assert len(problemy) == 1
    assert problemy[0][:2] == (BLAD, "plan")
    assert "zepsuty" in problemy[0][2]


async def test_skill_globalny_w_workflow_to_ostrzezenie_o_nieprzenosnosci_a_skill_z_repo_nie(swiat):
    _skille_testowe(swiat)
    _skill(swiat.dom, PODKATALOG, "glob", role="gatherer")
    _zapisz_wf_na_dysku(swiat, "x", (
        "name: x\nsteps:\n  plan:\n    skill: planner\n  zbierz:\n    skill: glob\n"))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _waliduj(klient, "x", swiat.projekt_a)

    assert _problemy(odp) == [
        (OSTRZEZENIE, "zbierz", "nieprzenośny — skill spoza repozytorium: glob")]


class RunnerZInnymDomem:
    """Runner z katalogiem domowym `dom` niezaleznym od HOME serwera: kazda operacja runnera
    idzie z `HOME=dom`, a po niej HOME wraca do wartosci serwera."""

    def __init__(self, wewnetrzny, dom: Path) -> None:
        self.wewnetrzny = wewnetrzny
        self.dom = dom

    async def __call__(self, runner_id, typ, dane, timeout_s):
        serwerowy = os.environ["HOME"]
        os.environ["HOME"] = str(self.dom)
        try:
            return await self.wewnetrzny(runner_id, typ, dane, timeout_s)
        finally:
            os.environ["HOME"] = serwerowy


async def test_katalog_domowy_do_ostrzezenia_pochodzi_od_runnera_a_nie_z_domu_serwera(
    swiat, monkeypatch,
):
    # dom serwera = korzen projektu: skill z repo lezy wtedy w `~/.cockpit/skills` SERWERA, a skill
    # z domu RUNNERA nie; walidator liczacy `dom` z Path.home() serwera pomylilby je
    monkeypatch.setattr(bus, "zapytaj", RunnerZInnymDomem(swiat.runner, swiat.dom))
    monkeypatch.setenv("HOME", str(swiat.korzen))
    _skille_testowe(swiat)
    _skill(swiat.dom, PODKATALOG, "glob", role="gatherer")
    _zapisz_wf_na_dysku(swiat, "x", (
        "name: x\nsteps:\n  plan:\n    skill: planner\n  zbierz:\n    skill: glob\n"))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _waliduj(klient, "x", swiat.projekt_a)

    assert _problemy(odp) == [
        (OSTRZEZENIE, "zbierz", "nieprzenośny — skill spoza repozytorium: glob")]


async def test_skill_o_id_nadpisanym_lokalnie_nie_jest_ostrzezeniem_o_nieprzenosnosci(swiat):
    _skille_testowe(swiat)
    _skill(swiat.dom, PODKATALOG, "planner", role="author", produces=("plan.md",))
    _zapisz_wf_na_dysku(swiat, "x", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _waliduj(klient, "x", swiat.projekt_a)

    assert _problemy(odp) == []


async def test_walidacja_nie_zapisuje_niczego_ani_nie_zmienia_pliku_workflow(swiat):
    _skille_testowe(swiat)
    plik = _zapisz_wf_na_dysku(swiat, "dwa", WF_DWA_BLEDY)
    klient = await _klient_z_katalogiem(swiat)
    przed = _drzewo(swiat)

    odp = await _waliduj(klient, "dwa", swiat.projekt_a)

    assert odp.status_code == 200, odp.text
    assert plik.read_bytes() == WF_DWA_BLEDY.encode()
    assert _drzewo(swiat) == przed
    assert not {"write_file", "delete_file"} & set(_typy_wywolan(swiat))


async def test_walidacja_czyta_biezacy_plik_a_nie_stan_sprzed_odswiezenia_katalogu(swiat):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "x", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)
    _zapisz_wf_na_dysku(swiat, "x", WF_DWA_BLEDY)   # zmiana po odswiezeniu katalogu

    odp = await _waliduj(klient, "x", swiat.projekt_a)

    assert [p[1] for p in _problemy(odp)] == ["a", "b"]


async def test_walidacja_workflow_ktorego_pliku_nie_ma_to_409_z_komunikatem_runnera(swiat):
    klient = await _klient_z_katalogiem(swiat)

    odp = await _waliduj(klient, "nie-ma", swiat.projekt_a)

    assert odp.status_code == 409
    assert odp.json() == {"detail": "sciezka nie istnieje"}


@pytest.mark.parametrize("wynik", [
    {"sciezka": "x"},
    {"sciezka": "x", "tresc": 5, "rozmiar": 1},
    {"sciezka": "x", "tresc": None, "rozmiar": 1},
], ids=["brak_tresci", "tresc_liczba", "tresc_null"])
async def test_zly_ksztalt_odpowiedzi_odczytu_workflow_to_409_z_opisem_a_nie_500(swiat, wynik):
    _zapisz_wf_na_dysku(swiat, "x", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wymuszone["read_file"] = {"ok": True, "wynik": wynik}

    odp = await _waliduj(klient, "x", swiat.projekt_a)

    assert odp.status_code == 409
    assert odp.json()["detail"].startswith("odpowiedz runnera ma zly ksztalt")


async def test_odmowa_runnera_przy_odczycie_workflow_poza_zakresem_to_403(swiat):
    _zapisz_wf_na_dysku(swiat, "x", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wymuszone["read_file"] = {
        "ok": False, "blad": KOMUNIKAT_POZA_ZAKRESEM, "kod": "poza_zakresem"}

    odp = await _waliduj(klient, "x", swiat.projekt_a)

    assert odp.status_code == 403
    assert odp.json() == {"detail": KOMUNIKAT_POZA_ZAKRESEM}


async def test_obserwator_zespolu_widzi_liste_i_walidacje_workflow(swiat):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "ok", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat, swiat.obserwator_a)

    lista = await _lista(klient, swiat.projekt_a)
    walidacja = await _waliduj(klient, "ok", swiat.projekt_a)

    assert (lista.status_code, walidacja.status_code) == (200, 200)


# =================================================================================
# 3. PUT /api/workflows/{name}: zapis YAML przez runnera, komentarze zachowane
# =================================================================================

async def test_zapis_workflow_przez_runnera_zachowuje_komentarze_cudzyslowy_i_kolejnosc_kluczy_bajt_w_bajt(
    swiat,
):
    _skille_testowe(swiat)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _zapisz(klient, "plan-i-recenzja", swiat.projekt_a, PLIK_NIEKANONICZNY)

    assert odp.status_code == 200, odp.text
    assert _plik_wf(swiat, "plan-i-recenzja").read_bytes() == PLIK_NIEKANONICZNY.encode("utf-8")


async def test_zapis_workflow_z_komentarzami_nadpisuje_istniejacy_plik_i_nie_gubi_komentarzy(swiat):
    _zapisz_wf_na_dysku(swiat, "plan-i-recenzja", "name: stary\nsteps:\n  a:\n    skill: x\n")
    klient = await _klient_z_katalogiem(swiat)

    odp = await _zapisz(klient, "plan-i-recenzja", swiat.projekt_a,
                        PLIK_Z_KOMENTARZAMI_I_CUDZYSLOWAMI)

    assert odp.status_code == 200, odp.text
    zapisane = _plik_wf(swiat, "plan-i-recenzja").read_text(encoding="utf-8")
    assert zapisane == PLIK_Z_KOMENTARZAMI_I_CUDZYSLOWAMI
    assert "# komentarz przy kroku review" in zapisane
    assert "name: 'plan-i-recenzja'" in zapisane


async def test_zapis_workflow_tworzy_katalog_workflows_gdy_projekt_jeszcze_go_nie_ma(swiat):
    assert not (swiat.korzen / ".cockpit" / "workflows").exists()
    klient = await _klient_z_katalogiem(swiat)

    odp = await _zapisz(klient, "nowy", swiat.projekt_a, "name: nowy\nsteps:\n  a:\n    skill: x\n")

    assert odp.status_code == 200, odp.text
    assert _plik_wf(swiat, "nowy").is_file()


async def test_zapis_workflow_idzie_do_runnera_projektu_z_bezwzgledna_sciezka_pliku(swiat):
    klient = await _klient_z_katalogiem(swiat)

    await _zapisz(klient, "nowy", swiat.projekt_a, PLIK_NIEKANONICZNY)

    (zapis,) = [w for w in swiat.runner.wywolania if w[1] == "write_file"]
    assert zapis[0] == swiat.runner_a
    assert zapis[2]["sciezka"] == f"{swiat.korzen}/.cockpit/workflows/nowy.yaml"
    assert zapis[2]["tresc"] == PLIK_NIEKANONICZNY


async def test_odpowiedz_na_zapis_workflow_to_walidacja_zapisanego_pliku(swiat):
    _skille_testowe(swiat)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _zapisz(klient, "dwa", swiat.projekt_a, WF_DWA_BLEDY)

    assert _problemy(odp) == [
        (BLAD, "a", "nieznany skill: ghost"),
        (BLAD, "b", "krok zalezy od nieznanego kroku: nie-ma"),
    ]
    assert (odp.json()["run_startuje_ze"], odp.json()["stan"]) == ("HEAD", "kopia robocza")
    assert _plik_wf(swiat, "dwa").read_bytes() == WF_DWA_BLEDY.encode()   # zapisany mimo bledow


async def test_zapisany_workflow_jest_od_razu_na_liscie_workflow(swiat):
    _skille_testowe(swiat)
    klient = await _klient_z_katalogiem(swiat)

    await _zapisz(klient, "ok", swiat.projekt_a, WF_POPRAWNY)

    assert (await _lista(klient, swiat.projekt_a)).json() == [
        {"name": "ok", "liczba_krokow": 2, "liczba_bledow": 0}]


ZLE_PLIKI_WORKFLOW = {
    "niepoprawny_yaml": (WF_NIEPOPRAWNY_YAML, None),
    "zduplikowany_krok": (
        "name: w\nsteps:\n  plan:\n    skill: a\n  plan:\n    skill: b\n", None),
    "brak_steps": ("name: w\n", "brak mapy steps"),
    "pusty_tekst": ("", "brak mapy steps"),
    "steps_to_lista": ("name: w\nsteps:\n  - plan\n", "brak mapy steps"),
}


@pytest.mark.parametrize(("tresc", "komunikat"), ZLE_PLIKI_WORKFLOW.values(),
                         ids=ZLE_PLIKI_WORKFLOW.keys())
async def test_zapis_tekstu_ktorego_nie_da_sie_wczytac_jako_workflow_to_422_a_stary_plik_zostaje(
    swiat, tresc, komunikat,
):
    plik = _zapisz_wf_na_dysku(swiat, "x", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _zapisz(klient, "x", swiat.projekt_a, tresc)

    assert odp.status_code == 422
    assert isinstance(odp.json()["detail"], str) and odp.json()["detail"]
    if komunikat is not None:
        assert odp.json() == {"detail": komunikat}
    assert "write_file" not in _typy_wywolan(swiat)
    assert plik.read_bytes() == WF_POPRAWNY.encode()


async def test_zapis_zduplikowanego_kroku_zwraca_komunikat_z_identyfikatorem_kroku(swiat):
    klient = await _klient_z_katalogiem(swiat)

    odp = await _zapisz(klient, "x", swiat.projekt_a,
                        "name: w\nsteps:\n  plan:\n    skill: a\n  plan:\n    skill: b\n")

    assert odp.status_code == 422
    assert odp.json()["detail"].startswith("zduplikowany klucz")
    assert not _plik_wf(swiat, "x").exists()


ZLE_CIALA_WORKFLOW = {
    "brak_tresci": {},
    "tresc_null": {"tresc": None},
    "tresc_liczba": {"tresc": 5},
    "tresc_lista": {"tresc": ["steps"]},
    "pole_dodatkowe": {"tresc": WF_POPRAWNY, "sciezka": "/etc/x"},
}


@pytest.mark.parametrize("cialo", ZLE_CIALA_WORKFLOW.values(), ids=ZLE_CIALA_WORKFLOW.keys())
async def test_zle_cialo_zapisu_workflow_to_422_bez_pytania_runnera_i_bez_zmiany_pliku(swiat, cialo):
    plik = _zapisz_wf_na_dysku(swiat, "x", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)

    odp = await klient.put("/api/workflows/x", params={"project": swiat.projekt_a}, json=cialo)

    assert odp.status_code == 422
    assert swiat.runner.wywolania == []
    assert plik.read_bytes() == WF_POPRAWNY.encode()


@pytest.mark.parametrize("nazwa", ["a b", "a:b", "x" * 300], ids=["spacja", "dwukropek", "dlugie"])
async def test_nazwa_workflow_niebedaca_bezpieczna_nazwa_pliku_to_422_na_zapisie_i_walidacji(
    swiat, nazwa,
):
    klient = await _klient_z_katalogiem(swiat)
    przed = _drzewo(swiat)

    zapis = await _zapisz(klient, nazwa, swiat.projekt_a, WF_POPRAWNY)
    walidacja = await _waliduj(klient, nazwa, swiat.projekt_a)

    assert (zapis.status_code, walidacja.status_code) == (422, 422)
    assert swiat.runner.wywolania == []
    assert _drzewo(swiat) == przed


async def test_czlonek_zespolu_moze_zapisac_workflow(swiat):
    klient = await _klient_z_katalogiem(swiat, swiat.czlonek_a)

    odp = await _zapisz(klient, "nowy", swiat.projekt_a, WF_POPRAWNY)

    assert odp.status_code == 200, odp.text
    assert _plik_wf(swiat, "nowy").read_bytes() == WF_POPRAWNY.encode()


# =================================================================================
# 4. Runner offline, odmowy i bledy zapisu
# =================================================================================

@pytest.mark.parametrize("rodzaj", ["lista", "walidacja", "zapis"])
async def test_runner_offline_daje_409_z_pelnym_komunikatem_na_trasach_workflow(swiat, rodzaj):
    _zapisz_wf_na_dysku(swiat, "x", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)
    for rid in (swiat.runner_a, swiat.runner_a2):
        bus.mark_runner_disconnected(rid)
    przed = _drzewo(swiat)

    odp = await _wywolaj(klient, rodzaj, swiat)

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_OFFLINE}
    assert swiat.runner.wywolania == []
    assert _drzewo(swiat) == przed


@pytest.mark.parametrize("rodzaj", ["lista", "walidacja", "zapis"])
async def test_runner_projektu_offline_to_409_na_trasach_workflow_choc_inny_runner_jest_online(
    swiat, rodzaj,
):
    _zapisz_wf_na_dysku(swiat, "x", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)
    bus.mark_runner_disconnected(swiat.runner_a)
    bus.mark_runner_connected(swiat.runner_a2)

    odp = await _wywolaj(klient, rodzaj, swiat)

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_OFFLINE}
    assert swiat.runner.wywolania == []


async def _wywolaj(klient, rodzaj, swiat):
    if rodzaj == "lista":
        return await _lista(klient, swiat.projekt_a)
    if rodzaj == "walidacja":
        return await _waliduj(klient, "x", swiat.projekt_a)
    return await _zapisz(klient, "x", swiat.projekt_a, WF_POPRAWNY)


async def test_odmowa_runnera_przy_zapisie_workflow_poza_zakresem_to_403(swiat):
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wymuszone["write_file"] = {
        "ok": False, "blad": KOMUNIKAT_POZA_ZAKRESEM, "kod": "poza_zakresem"}

    odp = await _zapisz(klient, "x", swiat.projekt_a, WF_POPRAWNY)

    assert odp.status_code == 403
    assert odp.json() == {"detail": KOMUNIKAT_POZA_ZAKRESEM}
    assert not _plik_wf(swiat, "x").exists()


async def test_porazka_zapisu_workflow_u_runnera_to_409_z_jego_komunikatem_bez_sladu_stosu(swiat):
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wymuszone["write_file"] = {
        "ok": False, "blad": "blad dysku przy operacji na pliku", "kod": "blad_dysku"}

    odp = await _zapisz(klient, "x", swiat.projekt_a, WF_POPRAWNY)

    assert odp.status_code == 409
    assert odp.json() == {"detail": "blad dysku przy operacji na pliku"}
    assert "Traceback" not in odp.text


async def test_brak_odpowiedzi_runnera_przy_zapisie_workflow_w_limicie_czasu_to_409_a_nie_500(swiat):
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wymuszone["write_file"] = TimeoutError()

    odp = await _zapisz(klient, "x", swiat.projekt_a, WF_POPRAWNY)

    assert odp.status_code == 409
    assert not _plik_wf(swiat, "x").exists()


async def test_projekt_poza_zakresem_runnera_daje_403_przy_zapisie_workflow_i_nic_nie_powstaje(swiat):
    obcy = swiat.korzen.parent / "obcy"
    obcy.mkdir()
    async with swiat.maker() as s:
        projekt = Project(tenant_id=swiat.tenant_a, runner_id=swiat.runner_a, path=str(obcy),
                          name="obcy")
        s.add(projekt)
        await s.commit()
    klient = await _klient_z_katalogiem(swiat)

    odp = await _zapisz(klient, "x", projekt.id, WF_POPRAWNY)

    assert odp.status_code == 403
    assert odp.json() == {"detail": KOMUNIKAT_POZA_ZAKRESEM}
    assert list(obcy.iterdir()) == []


# =================================================================================
# 5. Zespol i projekt: obcy zespol 404 na wszystkich nowych trasach
# =================================================================================

async def _wolaj_trase(klient, rodzaj, projekt):
    if rodzaj == "lista":
        return await _lista(klient, projekt)
    if rodzaj == "walidacja":
        return await _waliduj(klient, "x", projekt)
    return await _zapisz(klient, "x", projekt, WF_POPRAWNY)


@pytest.mark.parametrize("rodzaj", ["lista", "walidacja", "zapis"])
async def test_projekt_innego_zespolu_daje_404_na_trasach_workflow_a_jego_pliki_zostaja(swiat, rodzaj):
    cudzy_plik = swiat.korzen_b / ".cockpit" / "workflows" / "x.yaml"
    cudzy_plik.parent.mkdir(parents=True)
    cudzy_plik.write_bytes(WF_DWA_BLEDY.encode())
    klient = await swiat.zalogowany(swiat.wlasciciel_a)
    przed = _drzewo(swiat)

    odp = await _wolaj_trase(klient, rodzaj, swiat.projekt_b)

    assert odp.status_code == 404
    assert odp.json() == {"detail": KOMUNIKAT_PROJEKT_NIEZNANY}
    assert swiat.runner.wywolania == []
    assert cudzy_plik.read_bytes() == WF_DWA_BLEDY.encode()
    assert _drzewo(swiat) == przed


@pytest.mark.parametrize("rodzaj", ["lista", "walidacja", "zapis"])
async def test_nieistniejacy_projekt_daje_404_na_trasach_workflow(swiat, rodzaj):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _wolaj_trase(klient, rodzaj, "nie-ma-takiego")

    assert odp.status_code == 404
    assert odp.json() == {"detail": KOMUNIKAT_PROJEKT_NIEZNANY}
    assert swiat.runner.wywolania == []


@pytest.mark.parametrize("rodzaj", ["lista", "walidacja", "zapis"])
async def test_parametr_projektu_tras_workflow_jest_wymagany_i_ma_limit_36_znakow(swiat, rodzaj):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    if rodzaj == "lista":
        bez = await klient.get("/api/workflows")
    elif rodzaj == "walidacja":
        bez = await klient.get("/api/workflows/x/validate")
    else:
        bez = await klient.put("/api/workflows/x", json={"tresc": WF_POPRAWNY})
    za_dlugi = await _wolaj_trase(klient, rodzaj, "p" * 37)
    graniczny = await _wolaj_trase(klient, rodzaj, "p" * 36)

    assert (bez.status_code, za_dlugi.status_code) == (422, 422)
    assert graniczny.status_code == 404   # walidacja przeszla; takiego projektu nie ma
    assert swiat.runner.wywolania == []


# =================================================================================
# 6. Poprawka 1: zly ksztalt katalogu runnera, nazwy, rejestr z cache
# =================================================================================

KOMUNIKAT_NAZWY = (
    "nazwa workflow musi byc nazwa pliku: litery, cyfry, _ - . (do 64 znakow, "
    "pierwszy znak litera lub cyfra)")
ZLY_KSZTALT = "odpowiedz runnera ma zly ksztalt ({})"
BRAK_POLA = object()   # pole nieobecne w odpowiedzi runnera (inne niz null)


def _wymus_katalog(swiat, wynik) -> None:
    swiat.runner.wymuszone["katalog_definicji"] = {"ok": True, "wynik": wynik}


ZLE_ODPOWIEDZI_KATALOGU = {
    "wynik_lista": ([1], "wynik nie jest slownikiem"),
    "wynik_null": (None, "wynik nie jest slownikiem"),
    "wynik_napis": ("katalog", "wynik nie jest slownikiem"),
    "brak_dom": ({"projekty": []}, "pole dom nie jest niepustym napisem"),
    "dom_null": ({"dom": None, "projekty": []}, "pole dom nie jest niepustym napisem"),
    "dom_liczba": ({"dom": 5, "projekty": []}, "pole dom nie jest niepustym napisem"),
    "dom_pusty": ({"dom": "", "projekty": []}, "pole dom nie jest niepustym napisem"),
    "projekty_slownik": ({"dom": "/d", "projekty": {}}, "pole projekty nie jest lista slownikow"),
    "projekty_null": ({"dom": "/d", "projekty": None}, "pole projekty nie jest lista slownikow"),
    "brak_projektow": ({"dom": "/d"}, "pole projekty nie jest lista slownikow"),
    "projekty_z_liczba": ({"dom": "/d", "projekty": [1]}, "pole projekty nie jest lista slownikow"),
}


@pytest.mark.parametrize(("wynik", "powod"), ZLE_ODPOWIEDZI_KATALOGU.values(),
                         ids=ZLE_ODPOWIEDZI_KATALOGU.keys())
async def test_zly_ksztalt_katalogu_runnera_przy_walidacji_to_409_z_powodem_a_nie_500(
    swiat, wynik, powod,
):
    _zapisz_wf_na_dysku(swiat, "x", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)
    _wymus_katalog(swiat, wynik)

    odp = await _waliduj(klient, "x", swiat.projekt_a)

    assert odp.status_code == 409
    assert odp.json() == {"detail": ZLY_KSZTALT.format(powod)}


@pytest.mark.parametrize(("wynik", "powod"), ZLE_ODPOWIEDZI_KATALOGU.values(),
                         ids=ZLE_ODPOWIEDZI_KATALOGU.keys())
async def test_zly_ksztalt_katalogu_runnera_przy_zapisie_to_409_a_plik_nie_jest_zapisany(
    swiat, wynik, powod,
):
    plik = _zapisz_wf_na_dysku(swiat, "x", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)
    _wymus_katalog(swiat, wynik)

    odp = await _zapisz(klient, "x", swiat.projekt_a, WF_DWA_BLEDY)

    assert odp.status_code == 409
    assert odp.json() == {"detail": ZLY_KSZTALT.format(powod)}
    assert "write_file" not in _typy_wywolan(swiat)
    assert plik.read_bytes() == WF_POPRAWNY.encode()


ZLE_POLA_WORKFLOW = {
    "brak_pola": BRAK_POLA,
    "null": None,
    "slownik": {},
    "napis": "x.yaml",
    "liczba_zamiast_wpisu": [1],
    "wpis_bez_sciezki": [{}],
    "sciezka_liczba": [{"sciezka": 5}],
    "dobry_wpis_i_zly": [{"sciezka": ".cockpit/workflows/ok.yaml"}, 7],
}


@pytest.mark.parametrize("pole", ZLE_POLA_WORKFLOW.values(), ids=ZLE_POLA_WORKFLOW.keys())
async def test_zle_pole_workflow_w_katalogu_runnera_to_409_z_powodem_na_liscie_workflow(swiat, pole):
    _zapisz_wf_na_dysku(swiat, "ok", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)
    wpis = {"projekt": str(swiat.korzen), "skille": []}
    if pole is not BRAK_POLA:
        wpis["workflow"] = pole
    _wymus_katalog(swiat, {"dom": str(swiat.dom), "projekty": [wpis]})

    odp = await _lista(klient, swiat.projekt_a)

    assert odp.status_code == 409
    assert odp.json() == {"detail": ZLY_KSZTALT.format("pole workflow nie jest lista sciezek")}


async def test_projekt_ktorego_nie_ma_w_zakresie_runnera_daje_403_na_liscie_workflow(swiat):
    obcy = swiat.korzen.parent / "obcy"
    obcy.mkdir()
    async with swiat.maker() as s:
        projekt = Project(tenant_id=swiat.tenant_a, runner_id=swiat.runner_a, path=str(obcy),
                          name="obcy")
        s.add(projekt)
        await s.commit()
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, projekt.id)

    assert odp.status_code == 403
    assert odp.json() == {"detail": "sciezka poza zakresem runnera"}


async def test_lista_i_zapis_workflow_dzialaja_gdy_katalog_zawiera_skill_globalny(swiat):
    _skille_testowe(swiat)
    _skill(swiat.dom, PODKATALOG, "glob", role="gatherer")
    tresc = "name: x\nsteps:\n  plan:\n    skill: planner\n  zbierz:\n    skill: glob\n"
    _zapisz_wf_na_dysku(swiat, "x", tresc)
    klient = await _klient_z_katalogiem(swiat)

    lista = await _lista(klient, swiat.projekt_a)
    zapis = await _zapisz(klient, "x", swiat.projekt_a, tresc)

    assert lista.status_code == 200, lista.text
    assert lista.json() == [{"name": "x", "liczba_krokow": 2, "liczba_bledow": 0}]
    assert _problemy(zapis) == [
        (OSTRZEZENIE, "zbierz", "nieprzenośny — skill spoza repozytorium: glob")]


@pytest.mark.parametrize("nazwa", ["a b", ".ukryta", "-x", "_x", "x" * 65, "a:b", "ą"],
                         ids=["spacja_w_srodku", "kropka_na_poczatku", "myslnik_na_poczatku",
                              "podkreslnik_na_poczatku", "65_znakow", "dwukropek",
                              "litera_spoza_ascii"])
async def test_zla_nazwa_workflow_ma_na_zapisie_i_walidacji_opis_wzorca_nazwy(swiat, nazwa):
    klient = await _klient_z_katalogiem(swiat)

    zapis = await _zapisz(klient, nazwa, swiat.projekt_a, WF_POPRAWNY)
    walidacja = await _waliduj(klient, nazwa, swiat.projekt_a)

    assert (zapis.status_code, walidacja.status_code) == (422, 422)
    assert zapis.json() == {"detail": KOMUNIKAT_NAZWY}
    assert walidacja.json() == {"detail": KOMUNIKAT_NAZWY}
    assert swiat.runner.wywolania == []


@pytest.mark.parametrize("nazwa", ["x" * 64, "A", "9", "Wydanie.v2_a-b", "a.b", "wydanieV2"],
                         ids=["64_znaki", "wielka_litera", "cyfra", "kropka_myslnik_podkreslnik",
                              "kropka_w_srodku", "wielka_litera_w_srodku"])
async def test_bezpieczna_nazwa_workflow_jest_przyjeta_a_plik_powstaje_pod_ta_nazwa(swiat, nazwa):
    klient = await _klient_z_katalogiem(swiat)

    odp = await _zapisz(klient, nazwa, swiat.projekt_a, WF_POPRAWNY)

    assert odp.status_code == 200, odp.text
    assert _plik_wf(swiat, nazwa).read_bytes() == WF_POPRAWNY.encode()


async def test_rejestr_pomija_skill_z_problemem_i_skill_bez_roli_z_ich_powodami(swiat):
    _skille_testowe(swiat)
    _skill(swiat.korzen, PODKATALOG, "zepsuty", role="czarodziej")
    _zapisz_wf_na_dysku(swiat, "x", (
        "name: x\nsteps:\n  a:\n    skill: zepsuty\n  b:\n    skill: poltora\n"
        "  c:\n    skill: bezrolny\n"))
    klient = await _klient_z_katalogiem(swiat)
    projekt = str(swiat.korzen)
    # wiersz z problemem, ale z poprawna rola, i wiersz bez roli i bez problemu
    await _wstaw_definicje(swiat, swiat.tenant_a, swiat.runner_a, projekt, "poltora",
                           problem="reczny problem", rola="author")
    await _wstaw_definicje(swiat, swiat.tenant_a, swiat.runner_a, projekt, "bezrolny", rola=None)
    problem_zepsutego = _wpisy_skilli(await _lista_skilli(klient, swiat.projekt_a))["zepsuty"]["problem"]

    odp = await _waliduj(klient, "x", swiat.projekt_a)

    assert problem_zepsutego
    assert _problemy(odp) == [
        (BLAD, "a", f"skill pominiety przy wczytaniu: zepsuty ({problem_zepsutego})"),
        (BLAD, "b", "skill pominiety przy wczytaniu: poltora (reczny problem)"),
        (BLAD, "c", "skill pominiety przy wczytaniu: bezrolny (brak poprawnej roli w katalogu)"),
    ]


async def test_pominiety_skill_nie_przerywa_rejestru_wiec_skille_po_nim_sa_znane(swiat):
    # nazwy tak, by pominiety byl w srodku w kazdej kolejnosci wierszy
    _skill(swiat.korzen, PODKATALOG, "a-dobry", role="author", produces=("a.md",))
    _skill(swiat.korzen, PODKATALOG, "m-zepsuty", role="czarodziej")
    _skill(swiat.korzen, PODKATALOG, "z-dobry", role="author", produces=("z.md",))
    _zapisz_wf_na_dysku(swiat, "x", (
        "name: x\nsteps:\n  a:\n    skill: a-dobry\n  z:\n    skill: z-dobry\n"))
    klient = await _klient_z_katalogiem(swiat)

    odp = await _waliduj(klient, "x", swiat.projekt_a)

    assert _problemy(odp) == []


def test_rejestr_workflow_niesie_pola_skilla_z_katalogu_ale_bez_tresci_briefu():
    wiersz = SimpleNamespace(
        sciezka=".cockpit/skills/x/SKILL.md", rola="reviewer", problem=None, klucz="x",
        nazwa="Skill X", consumes=None, produces=["raport.md"])

    rejestr = workflowy_api._rejestr([wiersz], SimpleNamespace(path="/repo"), "/home/u")

    skill = rejestr.skille["x"]
    assert (skill.id, skill.name, skill.role) == ("x", "Skill X", Rola.reviewer)
    assert (skill.consumes, skill.produces) == ([], ["raport.md"])
    assert skill.brief == ""
    assert str(skill.katalog) == "/repo/.cockpit/skills/x"
    assert rejestr.pominiete == []


WARTOSCI_NIE_DO_SKONSTRUOWANIA = {
    "tag_bool_z_napisem": "name: w\nsteps:\n  a: !!bool x\n",
    "tag_zbioru_z_napisem": "name: w\nsteps:\n  a: !!set x\n",
    "liczba_bez_cyfr": "name: w\nsteps:\n  a: !!int 0b\n",
    "data_spoza_kalendarza": "name: w\nsteps:\n  a: 2024-02-30\n",
}


@pytest.mark.parametrize("tresc", WARTOSCI_NIE_DO_SKONSTRUOWANIA.values(),
                         ids=WARTOSCI_NIE_DO_SKONSTRUOWANIA.keys())
async def test_zapis_workflow_z_wartoscia_nie_do_skonstruowania_to_422_z_opisem_a_stary_plik_zostaje(
    swiat, tresc,
):
    plik = _zapisz_wf_na_dysku(swiat, "x", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _zapisz(klient, "x", swiat.projekt_a, tresc)

    assert odp.status_code == 422
    assert odp.json() == {"detail": "niepoprawny YAML: nieprawidlowa wartosc"}
    assert "Traceback" not in odp.text
    assert "write_file" not in _typy_wywolan(swiat)
    assert plik.read_bytes() == WF_POPRAWNY.encode()


@pytest.mark.parametrize("tresc", WARTOSCI_NIE_DO_SKONSTRUOWANIA.values(),
                         ids=WARTOSCI_NIE_DO_SKONSTRUOWANIA.keys())
async def test_lista_workflow_z_plikiem_z_wartoscia_nie_do_skonstruowania_ma_go_z_jednym_bledem_a_nie_500(
    swiat, tresc,
):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "ok", WF_POPRAWNY)
    _zapisz_wf_na_dysku(swiat, "zly", tresc)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _lista(klient, swiat.projekt_a)

    assert odp.status_code == 200, odp.text
    assert odp.json() == [
        {"name": "ok", "liczba_krokow": 2, "liczba_bledow": 0},
        {"name": "zly", "liczba_krokow": 0, "liczba_bledow": 1},
    ]


async def test_odpowiedz_na_zapis_workflow_ma_problemy_wczytania_przed_problemami_walidatora(swiat):
    _skille_testowe(swiat)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _zapisz(klient, "mieszany", swiat.projekt_a, WF_KROK_BEZ_SKILLA_I_NIEZNANY_SKILL)

    assert _problemy(odp) == [
        (BLAD, "bez-skilla", "krok nie ma pola skill ani system"),
        (BLAD, "dobry", "nieznany skill: ghost"),
    ]


LIMIT_TRESCI_WORKFLOWU = 131072   # niezalezna od kodu kopia: 512 KB / 4 bajty na znak


@pytest.mark.parametrize(("dlugosc", "status"), [
    (LIMIT_TRESCI_WORKFLOWU, 200), (LIMIT_TRESCI_WORKFLOWU + 1, 422)], ids=["rowno_limit", "limit_plus_1"])
async def test_tresc_workflow_do_131072_znakow_jest_zapisana_a_dluzsza_to_422_bez_zapisu(
    swiat, dlugosc, status,
):
    klient = await _klient_z_katalogiem(swiat)
    prefiks = "name: w\nsteps:\n  a:\n    skill: x\n# "
    tresc = prefiks + "z" * (dlugosc - len(prefiks))
    assert len(tresc) == dlugosc

    odp = await _zapisz(klient, "duzy", swiat.projekt_a, tresc)

    assert odp.status_code == status, odp.text[:200]
    assert _plik_wf(swiat, "duzy").exists() == (status == 200)
    if status == 200:
        assert _plik_wf(swiat, "duzy").read_bytes() == tresc.encode()


@pytest.mark.parametrize("tresc", WARTOSCI_NIE_DO_SKONSTRUOWANIA.values(),
                         ids=WARTOSCI_NIE_DO_SKONSTRUOWANIA.keys())
async def test_walidacja_pliku_z_wartoscia_nie_do_skonstruowania_to_jeden_blad_pliku_a_nie_500(
    swiat, tresc,
):
    _zapisz_wf_na_dysku(swiat, "x", tresc)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _waliduj(klient, "x", swiat.projekt_a)

    assert _problemy(odp) == [(BLAD, None, "niepoprawny YAML: nieprawidlowa wartosc")]


def test_problemy_pliku_to_problemy_wczytania_a_potem_walidacji_workflow():
    workflow, problemy = workflowy_api._problemy_pliku(
        "steps:\n  plan:\n    skill: planner\n", Rejestr(), "/home/u")

    assert workflow is not None
    assert [(p.krok, p.komunikat) for p in problemy] == [("plan", "nieznany skill: planner")]
