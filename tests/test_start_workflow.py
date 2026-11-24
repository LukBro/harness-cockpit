"""HAR-182: start runu workflow z API po walidacji - `POST /api/workflows/{name}/run?project=<id>`.

Kontrakt (przyjety przez test-writera, opisany w raporcie):
 * trasa waliduje plik workflow TYM SAMYM kodem co `GET /api/workflows/{name}/validate` (biezacy plik
   z kopii roboczej, rejestr skilli z katalogu projektu); przynajmniej jeden problem poziomu `blad`
   (takze plik, ktorego nie da sie wczytac) to 422 z napisem w `detail` i polem `problemy` - ta sama
   lista `{poziom, krok, komunikat}` co w `/validate`, z ostrzezeniami; run NIE powstaje i nic nie
   idzie do runnera;
 * same ostrzezenia nie blokuja startu;
 * runner projektu offline to 409 z podpowiedzia (`cockpit-runner`), bez rekordu runu i bez polecenia;
   brak pliku workflow to 409 z komunikatem runnera (jak przy walidacji), bez runu;
 * sukces: 200 `RunUtworzony {run_id}`; w bazie run zespolu z sesji z `project_id`, `workflow` =
   nazwa, runnerem PROJEKTU i statusem `running`; do tego runnera idzie dokladnie jedno polecenie
   `run_workflow {run_id, workflow, sciezka}`; start niczego nie zapisuje na dysku uzytkownika.
Runner robi PRAWDZIWE operacje na plikach w katalogu testu, a skutki czytamy z bazy
(rekordy `Run`) i z nagranych polecen `bus.send_to_runner` (asercje na danych, nie tylko na kodzie).
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from cockpit.api import bus
from cockpit.db.models import Run, RunStatus
from test_projekty_zmiana import KOMUNIKAT_PROJEKT_NIEZNANY, swiat
from test_skille_api import _klient_z_katalogiem, _skill
from test_workflowy_api import (
    BLAD,
    OSTRZEZENIE,
    PODKATALOG,
    WF_DWA_BLEDY,
    WF_JEDEN_BLAD_I_JEDNO_OSTRZEZENIE,
    WF_KROK_BEZ_SKILLA_I_NIEZNANY_SKILL,
    WF_NIEPOPRAWNY_YAML,
    WF_POPRAWNY,
    _skille_testowe,
    _waliduj,
    _zapisz_wf_na_dysku,
)

FIXTURY_Z_INNYCH_TESTOW = (swiat,)   # fixture pytest zzaimportowana z innego pliku testow

WF_SAMO_OSTRZEZENIE = (
    "name: ostrzezenie\nsteps:\n  plan:\n    skill: planner\n    optional: true\n")
WF_JEDEN_BLAD = "name: jeden\nsteps:\n  a:\n    skill: ghost\n"
WF_BLAD_W_JEDNYM_Z_WIELU_KROKOW = (
    "name: wiele\nsteps:\n"
    "  plan:\n    skill: planner\n"
    "  review:\n    skill: recenzent\n    needs: [plan]\n"
    "  koniec:\n    skill: ghost\n    needs: [review]\n")


@pytest.fixture
def wyslane(monkeypatch):
    """Nagrywa polecenia kokpitu dla runnera (`bus.send_to_runner`); nic nie trafia do kolejki."""
    nagrane: list[tuple[str, dict]] = []

    async def zapisz(runner_id, polecenie):
        nagrane.append((runner_id, polecenie))

    monkeypatch.setattr(bus, "send_to_runner", zapisz)
    return nagrane


async def _start(klient, nazwa, projekt):
    return await klient.post(f"/api/workflows/{nazwa}/run", params={"project": projekt})


async def _runy(swiat) -> list[Run]:
    async with swiat.maker() as s:
        return list((await s.execute(select(Run).order_by(Run.created_at, Run.id))).scalars())


def _polecenia_run_workflow(wyslane) -> list[tuple[str, dict]]:
    return [(r, p) for r, p in wyslane if p.get("type") == "run_workflow"]


# =================================================================================
# 1. Sukces: run w bazie, polecenie do runnera projektu
# =================================================================================

async def test_start_poprawnego_workflow_tworzy_run_projektu_z_nazwa_workflow_i_zwraca_jego_id(
    swiat, wyslane,
):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "ok", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _start(klient, "ok", swiat.projekt_a)

    assert odp.status_code == 200, odp.text
    assert set(odp.json()) == {"run_id"}
    (run,) = await _runy(swiat)
    assert odp.json()["run_id"] == run.id
    assert run.tenant_id == swiat.tenant_a
    assert run.project_id == swiat.projekt_a
    assert run.workflow == "ok"
    assert run.status == RunStatus.running


async def test_run_startuje_na_runnerze_projektu_a_nie_na_innym_runnerze_zespolu_z_ta_sama_sciezka(
    swiat, wyslane,
):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "ok", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)
    bus.mark_runner_connected(swiat.runner_a2)   # `projekt_a2` ma te sama sciezke, ale inny runner

    odp = await _start(klient, "ok", swiat.projekt_a)

    assert odp.status_code == 200, odp.text
    (run,) = await _runy(swiat)
    assert run.runner_id == swiat.runner_a
    assert [r for r, _ in wyslane] == [swiat.runner_a]


async def test_start_wysyla_runnerowi_jedno_polecenie_run_workflow_z_id_runu_nazwa_i_sciezka_projektu(
    swiat, wyslane,
):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "ok", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _start(klient, "ok", swiat.projekt_a)

    assert odp.status_code == 200, odp.text
    assert wyslane == [(swiat.runner_a, {
        "type": "run_workflow", "run_id": odp.json()["run_id"], "workflow": "ok",
        "sciezka": str(swiat.korzen)})]


async def test_start_runu_niczego_nie_zapisuje_na_dysku_uzytkownika(swiat, wyslane):
    _skille_testowe(swiat)
    plik = _zapisz_wf_na_dysku(swiat, "ok", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)
    przed = sorted(p for p in swiat.korzen.rglob("*"))

    odp = await _start(klient, "ok", swiat.projekt_a)

    assert odp.status_code == 200, odp.text
    assert sorted(p for p in swiat.korzen.rglob("*")) == przed
    assert plik.read_bytes() == WF_POPRAWNY.encode()
    assert not {"write_file", "delete_file"} & set(swiat.runner.typy())


async def test_start_waliduje_biezacy_plik_a_nie_stan_z_chwili_odswiezenia_katalogu(swiat, wyslane):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "x", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)
    _zapisz_wf_na_dysku(swiat, "x", WF_DWA_BLEDY)   # zmiana po odswiezeniu katalogu

    odp = await _start(klient, "x", swiat.projekt_a)

    assert odp.status_code == 422, odp.text
    assert await _runy(swiat) == []


async def test_czlonek_zespolu_uruchamia_workflow_projektu(swiat, wyslane):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "ok", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat, swiat.czlonek_a)

    odp = await _start(klient, "ok", swiat.projekt_a)

    assert odp.status_code == 200, odp.text
    assert len(await _runy(swiat)) == 1


async def test_kazdy_start_tworzy_osobny_run_a_drugi_start_nie_nadpisuje_pierwszego(swiat, wyslane):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "ok", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)

    pierwszy = await _start(klient, "ok", swiat.projekt_a)
    drugi = await _start(klient, "ok", swiat.projekt_a)

    assert (pierwszy.status_code, drugi.status_code) == (200, 200)
    assert pierwszy.json()["run_id"] != drugi.json()["run_id"]
    assert {r.id for r in await _runy(swiat)} == {pierwszy.json()["run_id"], drugi.json()["run_id"]}
    assert len(_polecenia_run_workflow(wyslane)) == 2


# =================================================================================
# 2. Walidacja: bledy blokuja, ostrzezenia nie
# =================================================================================

@pytest.mark.parametrize(("tresc", "oczekiwane_bledy"), [
    (WF_JEDEN_BLAD, [(BLAD, "a", "nieznany skill: ghost")]),
    (WF_DWA_BLEDY, [(BLAD, "a", "nieznany skill: ghost"),
                    (BLAD, "b", "krok zalezy od nieznanego kroku: nie-ma")]),
    (WF_BLAD_W_JEDNYM_Z_WIELU_KROKOW, [(BLAD, "koniec", "nieznany skill: ghost")]),
    (WF_KROK_BEZ_SKILLA_I_NIEZNANY_SKILL, [(BLAD, "bez-skilla", "krok nie ma pola skill ani system"),
                                           (BLAD, "dobry", "nieznany skill: ghost")]),
], ids=["jeden_blad", "dwa_bledy", "jeden_blad_wsrod_poprawnych_krokow", "blad_z_wczytania_i_z_walidacji"])
async def test_workflow_z_bledami_walidacji_daje_422_z_lista_problemow_a_run_nie_powstaje(
    swiat, wyslane, tresc, oczekiwane_bledy,
):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "x", tresc)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _start(klient, "x", swiat.projekt_a)

    assert odp.status_code == 422, odp.text
    problemy = [(p["poziom"], p["krok"], p["komunikat"]) for p in odp.json()["problemy"]]
    assert [p for p in problemy if p[0] == BLAD] == oczekiwane_bledy
    assert all(set(p) == {"poziom", "krok", "komunikat"} for p in odp.json()["problemy"])
    assert isinstance(odp.json()["detail"], str) and odp.json()["detail"]
    assert await _runy(swiat) == []
    assert _polecenia_run_workflow(wyslane) == []


async def test_lista_problemow_ze_startu_jest_ta_sama_co_w_walidacji_tego_pliku_z_ostrzezeniami(
    swiat, wyslane,
):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "zeta", WF_JEDEN_BLAD_I_JEDNO_OSTRZEZENIE)
    klient = await _klient_z_katalogiem(swiat)

    walidacja = await _waliduj(klient, "zeta", swiat.projekt_a)
    odp = await _start(klient, "zeta", swiat.projekt_a)

    assert odp.status_code == 422, odp.text
    assert odp.json()["problemy"] == walidacja.json()["problemy"]
    assert [(p["poziom"], p["krok"]) for p in odp.json()["problemy"]] == [
        (OSTRZEZENIE, "review"), (BLAD, "koniec")]
    assert await _runy(swiat) == []


async def test_workflow_z_samymi_ostrzezeniami_startuje_i_run_powstaje(swiat, wyslane):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "ostrzezenie", WF_SAMO_OSTRZEZENIE)
    klient = await _klient_z_katalogiem(swiat)
    walidacja = await _waliduj(klient, "ostrzezenie", swiat.projekt_a)

    odp = await _start(klient, "ostrzezenie", swiat.projekt_a)

    assert [p["poziom"] for p in walidacja.json()["problemy"]] == [OSTRZEZENIE]   # warunek testu
    assert odp.status_code == 200, odp.text
    (run,) = await _runy(swiat)
    assert (run.id, run.workflow, run.project_id) == (
        odp.json()["run_id"], "ostrzezenie", swiat.projekt_a)
    assert len(_polecenia_run_workflow(wyslane)) == 1


async def test_workflow_ze_skillem_z_katalogu_domowego_runnera_startuje_bo_dom_pochodzi_od_runnera(
    swiat, wyslane,
):
    # skill z `~/...` ma katalog liczony z `dom` runnera; start, ktory zgubilby `dom`, nie umialby
    # zbudowac rejestru i padl (500) zamiast uruchomic run z ostrzezeniem o nieprzenosnosci
    _skille_testowe(swiat)
    _skill(swiat.dom, PODKATALOG, "glob", role="gatherer")
    _zapisz_wf_na_dysku(swiat, "x", (
        "name: x\nsteps:\n  plan:\n    skill: planner\n  zbierz:\n    skill: glob\n"))
    klient = await _klient_z_katalogiem(swiat)
    walidacja = await _waliduj(klient, "x", swiat.projekt_a)

    odp = await _start(klient, "x", swiat.projekt_a)

    assert [(p["poziom"], p["krok"]) for p in walidacja.json()["problemy"]] == [
        (OSTRZEZENIE, "zbierz")]   # warunek testu: skill globalny jest w rejestrze
    assert odp.status_code == 200, odp.text
    (run,) = await _runy(swiat)
    assert run.id == odp.json()["run_id"]
    assert len(_polecenia_run_workflow(wyslane)) == 1


async def test_workflow_ktorego_pliku_nie_da_sie_wczytac_daje_422_z_jednym_problemem_calego_pliku(
    swiat, wyslane,
):
    _zapisz_wf_na_dysku(swiat, "zepsuty", WF_NIEPOPRAWNY_YAML)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _start(klient, "zepsuty", swiat.projekt_a)

    assert odp.status_code == 422, odp.text
    (problem,) = odp.json()["problemy"]
    assert (problem["poziom"], problem["krok"]) == (BLAD, None)
    assert problem["komunikat"].startswith("niepoprawny YAML")
    assert await _runy(swiat) == []
    assert _polecenia_run_workflow(wyslane) == []


@pytest.mark.parametrize("tresc", ["", "name: w\n", "name: w\nsteps:\n  - plan\n"],
                         ids=["pusty_plik", "brak_steps", "steps_jako_lista"])
async def test_pusty_albo_bez_krokow_plik_workflow_nie_startuje_runu(swiat, wyslane, tresc):
    _zapisz_wf_na_dysku(swiat, "x", tresc)
    klient = await _klient_z_katalogiem(swiat)

    odp = await _start(klient, "x", swiat.projekt_a)

    assert odp.status_code == 422, odp.text
    assert len(odp.json()["problemy"]) == 1
    assert await _runy(swiat) == []


# =================================================================================
# 3. Runner offline i bledy runnera: 409 bez runu
# =================================================================================

async def test_runner_projektu_offline_daje_409_z_podpowiedzia_a_run_nie_powstaje(swiat, wyslane):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "ok", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)
    bus.mark_runner_disconnected(swiat.runner_a)
    bus.mark_runner_connected(swiat.runner_a2)   # inny runner zespolu jest online, ale to nie jego projekt

    odp = await _start(klient, "ok", swiat.projekt_a)

    assert odp.status_code == 409, odp.text
    assert "cockpit-runner" in odp.json()["detail"]
    assert await _runy(swiat) == []
    assert wyslane == []


async def test_zaden_runner_zespolu_nie_jest_online_to_409_z_podpowiedzia_a_run_nie_powstaje(swiat, wyslane):
    _skille_testowe(swiat)
    _zapisz_wf_na_dysku(swiat, "ok", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)
    for rid in (swiat.runner_a, swiat.runner_a2):
        bus.mark_runner_disconnected(rid)

    odp = await _start(klient, "ok", swiat.projekt_a)

    assert odp.status_code == 409, odp.text
    assert "cockpit-runner" in odp.json()["detail"]
    assert await _runy(swiat) == []
    assert wyslane == []


async def test_brak_pliku_workflow_to_409_z_komunikatem_runnera_a_run_nie_powstaje(swiat, wyslane):
    klient = await _klient_z_katalogiem(swiat)

    odp = await _start(klient, "nie-ma", swiat.projekt_a)

    assert odp.status_code == 409, odp.text
    assert odp.json() == {"detail": "sciezka nie istnieje"}
    assert await _runy(swiat) == []
    assert wyslane == []


async def test_brak_odpowiedzi_runnera_przy_walidacji_w_limicie_czasu_to_409_a_run_nie_powstaje(
    swiat, wyslane,
):
    _zapisz_wf_na_dysku(swiat, "ok", WF_POPRAWNY)
    klient = await _klient_z_katalogiem(swiat)
    swiat.runner.wymuszone["read_file"] = TimeoutError()

    odp = await _start(klient, "ok", swiat.projekt_a)

    assert odp.status_code == 409, odp.text
    assert await _runy(swiat) == []
    assert wyslane == []


# =================================================================================
# 4. Zespol, projekt i nazwa: odmowy bez runu i bez pytania runnera o plik
# =================================================================================

async def test_projekt_innego_zespolu_daje_404_a_run_nie_powstaje_i_runner_nie_dostaje_polecenia(
    swiat, wyslane,
):
    _zapisz_wf_na_dysku(swiat, "x", WF_DWA_BLEDY)
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _start(klient, "x", swiat.projekt_b)

    assert odp.status_code == 404
    assert odp.json() == {"detail": KOMUNIKAT_PROJEKT_NIEZNANY}
    assert await _runy(swiat) == []
    assert swiat.runner.wywolania == []
    assert wyslane == []


async def test_nieistniejacy_projekt_daje_404_a_run_nie_powstaje(swiat, wyslane):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _start(klient, "x", "nie-ma-takiego")

    assert odp.status_code == 404
    assert odp.json() == {"detail": KOMUNIKAT_PROJEKT_NIEZNANY}
    assert await _runy(swiat) == []
    assert swiat.runner.wywolania == []


@pytest.mark.parametrize("nazwa", ["a b", "a:b", ".ukryta", "x" * 65],
                         ids=["spacja", "dwukropek", "kropka_na_poczatku", "65_znakow"])
async def test_nazwa_workflow_niebedaca_bezpieczna_nazwa_pliku_to_422_bez_pytania_runnera_i_bez_runu(
    swiat, wyslane, nazwa,
):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    odp = await _start(klient, nazwa, swiat.projekt_a)

    assert odp.status_code == 422
    assert isinstance(odp.json()["detail"], str)
    assert await _runy(swiat) == []
    assert swiat.runner.wywolania == []
    assert wyslane == []


async def test_parametr_projektu_startu_jest_wymagany_i_ma_limit_36_znakow(swiat, wyslane):
    klient = await swiat.zalogowany(swiat.wlasciciel_a)

    bez = await klient.post("/api/workflows/x/run")
    za_dlugi = await _start(klient, "x", "p" * 37)
    graniczny = await _start(klient, "x", "p" * 36)

    assert (bez.status_code, za_dlugi.status_code) == (422, 422)
    assert graniczny.status_code == 404   # walidacja przeszla; takiego projektu nie ma
    assert await _runy(swiat) == []
    assert swiat.runner.wywolania == []


def test_blad_walidacji_workflow_jest_szczegolnym_przypadkiem_niespojnych_danych_wejsciowych():
    # wolajacy lapiacy NiespojneDane (sprzeczne dane) dostaje tez start runu z bledami workflow
    from cockpit.services.errors import BledyWalidacjiWorkflow, NiespojneDane

    blad = BledyWalidacjiWorkflow([{"poziom": "blad", "krok": "a", "komunikat": "x"}])

    assert isinstance(blad, NiespojneDane)


def test_komunikat_bledu_walidacji_workflow_liczy_tylko_bledy_a_nie_ostrzezenia():
    from cockpit.services.errors import BledyWalidacjiWorkflow

    problemy = [
        {"poziom": "blad", "krok": "a", "komunikat": "x"},
        {"poziom": "ostrzezenie", "krok": "b", "komunikat": "y"},
        {"poziom": "blad", "krok": "c", "komunikat": "z"},
    ]

    blad = BledyWalidacjiWorkflow(problemy)

    assert str(blad) == "workflow ma błędy walidacji (2), run nie wystartował"
    assert blad.args == ("workflow ma błędy walidacji (2), run nie wystartował",)
    assert blad.problemy == problemy
    assert blad.problemy is problemy


def test_komunikat_bledu_walidacji_workflow_z_jednym_bledem_mowi_o_jednym():
    from cockpit.services.errors import BledyWalidacjiWorkflow

    blad = BledyWalidacjiWorkflow([{"poziom": "blad", "krok": "a", "komunikat": "x"}])

    assert str(blad) == "workflow ma błędy walidacji (1), run nie wystartował"
