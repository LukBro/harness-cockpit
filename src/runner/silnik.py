"""Silnik runu u runnera: wykonanie workflow wedlug grafu `needs` (HAR-80, D11).

Petla wykonania zyje w runnerze, a kokpit wylicza stan runu ze zdarzen. Silnik waliduje
workflow, tworzy worktree, materializuje skille i uruchamia kroki rownolegle tam, gdzie
pozwala `needs`. Krok z `approval: true` zawiesza run na bramce BEZ procesu agenta
(HAR-172): silnik zapisuje checkpoint z `waiting_on` w chwili trafienia na bramke (bez
zdarzenia), kroki niezalezne od bramki dochodza do konca, potem drugi zapis, pauza testowa
i `step_waiting` jako ostatnie zdarzenie runu. Wszystkie odczyty (rejestr skilli,
workflow, project.yaml) ida z worktree (stan z commita), nie z repozytorium uzytkownika.
Silnik nie sprzata worktree: zostaje do odbioru. Fan-out, konsensus, foreach, wznowienie
po decyzji i petla naprawcza to osobne tickety.

Kazda sciezka konczy sie zdarzeniem `run_finished`. Wyjatki: zawieszenie na bramce (ostatnie
zdarzenie to `step_waiting`; `run_finished` zmienilby w kokpicie `waiting` na "do odbioru"),
anulowanie (`CancelledError`, po anulowaniu zadan krokow) i bledy samego `emit`
(np. `BrakMiejsca`) - decyduje o nich wolajacy. Nieudany zapis checkpointu to `step_failed`
kroku bramki (bez `step_waiting`) i `run_finished`.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cockpit.services.errors import BladDziedziny
from cockpit.services.projekt import Projekt, wczytaj_projekt
from cockpit.services.skills import Rejestr, materializuj, rozstrzygnij_rejestr
from cockpit.services.workflow import Problem, Workflow, ma_bledy, wczytaj_workflow
from cockpit.services.workflow_walidacja import waliduj_workflow
from runner.agent import BrakModelu, ProfilBackendu, SpecKroku, polecenie_agenta, zbuduj_prompt
from runner.checkpoint import (
    BladCheckpointu,
    ZMIENNA_PAUZY,
    Checkpoint,
    Oczekiwanie,
    StanKrokuCheckpointu,
    zapisz_checkpoint,
)
from runner.graf import StanKroku, nastepne
from runner.krok import WynikKroku, wykonaj_krok
from runner.worktree import WorktreeRunu, utworz_worktree
from runner.zakres import Zakres, ZlaKonfiguracjaZakresu

Emit = Callable[[dict], Awaitable[None]]
UruchomAgenta = Callable[..., Awaitable[WynikKroku]]

NAZWA_WORKFLOW = re.compile(r"[A-Za-z0-9_.-]{1,64}")
# Katalog workflow w repozytorium: odczyt idzie z worktree (stan z commita). Stala, bo literal
# w funkcji przezywa mutacje wielkosci liter na systemach plikow bez jej rozrozniania (macOS).
KATALOG_WORKFLOW = Path(".cockpit") / "workflows"
KROK_WALIDACJI = "walidacja"   # krok, na ktorym kokpit widzi blad przed startem krokow
TEKST_CZEKANIA = "czeka na decyzję człowieka (approval)"


class BladStartu(Exception):
    """Run nie moze ruszyc: tekst to lista problemow, jeden w linii (D4)."""


class PorazkaKroku(Exception):
    """Krok zbudowany przez silnik nie ruszyl (prompt, model); tekst to powod."""


@dataclass(frozen=True)
class Przygotowany:
    worktree: WorktreeRunu
    workflow: Workflow
    rejestr: Rejestr
    projekt: Projekt
    repozytorium: Path


def _linia(problem: Problem) -> str:
    krok = problem.krok if problem.krok is not None else "-"
    return f"{krok}: {problem.kod}: {problem.komunikat}"


def _bledy(problemy: list[Problem]) -> str:
    return "\n".join(_linia(p) for p in problemy)


def _przygotuj(
    polecenie: Mapping[str, Any], dom: Path, root: Path | None, zakres: Zakres | None,
    run_id: str,
) -> Przygotowany:
    """Walidacja polecenia, worktree, rejestr, workflow, project.yaml, kopia, materializacja.

    Blad wejscia (dysk, git, zakres, skill) staje sie `BladStartu`; nic nie powstaje na
    dysku przed sprawdzeniem nazwy workflow i zakresu."""
    nazwa = polecenie.get("workflow")
    if (
        not isinstance(nazwa, str)
        or not NAZWA_WORKFLOW.fullmatch(nazwa)
        or nazwa in (".", "..")
    ):
        raise BladStartu(f"niedozwolona nazwa workflow: {nazwa!r}")
    try:
        if zakres is None:
            zakres = Zakres.z_konfiguracji(scisle=True)
        repo = zakres.rozwiaz(polecenie.get("sciezka"))
        wt = utworz_worktree(repo, run_id, root)
        rejestr = rozstrzygnij_rejestr(wt.sciezka, dom)
        plik = wt.sciezka / KATALOG_WORKFLOW / f"{nazwa}.yaml"
        workflow, problemy = wczytaj_workflow(plik)
        if workflow is not None:
            problemy += waliduj_workflow(workflow, rejestr, dom)
        if workflow is None or ma_bledy(problemy):
            raise BladStartu(_bledy([p for p in problemy if ma_bledy([p])]))
        projekt, problemy_projektu = wczytaj_projekt(wt.sciezka)
        if projekt is None:
            raise BladStartu(_bledy(problemy_projektu))
        shutil.copyfile(plik, wt.katalog_runu / "workflow.yaml")
        materializuj({k.skill for k in workflow.kroki}, rejestr, wt.sciezka)
    except (BladDziedziny, ZlaKonfiguracjaZakresu, OSError) as e:
        raise BladStartu(str(e)) from e
    return Przygotowany(wt, workflow, rejestr, projekt, repo)


def _zdarzenie(run_id: str, kind: str, step_id: str, agent_id: str, text: str, **reszta) -> dict:
    return {
        "kind": kind, "run_id": run_id, "step_id": step_id, "agent_id": agent_id,
        "text": text, **reszta,
    }


async def _krok(
    run_id: str, id_kroku: str, p: Przygotowany, profil: ProfilBackendu,
    uruchom_agenta: UruchomAgenta, emit: Emit,
) -> WynikKroku:
    """Jeden krok: specyfikacja agenta, prompt, polecenie; blad budowy to `PorazkaKroku`."""
    krok = next(k for k in p.workflow.kroki if k.id == id_kroku)
    skill = p.rejestr.skille[krok.skill]
    spec = SpecKroku(
        run_id=run_id, step_id=krok.id, agent_id=krok.id, skill_id=krok.skill,
        rola=skill.role, wejscia=list(skill.consumes), wyjscia=list(skill.produces),
        komendy={n: p.projekt.capabilities[n] for n in skill.needs if n in p.projekt.capabilities},
    )
    try:
        prompt = zbuduj_prompt(spec)
    except ValueError as e:
        raise PorazkaKroku(str(e)) from e
    try:
        argv = polecenie_agenta(spec, prompt, profil)
    except BrakModelu as e:
        raise PorazkaKroku(str(e)) from e
    return await uruchom_agenta(
        spec, argv, p.worktree.sciezka, emit, katalog_runu=p.worktree.katalog_runu)


async def _pauza_testowa() -> None:
    """Pauza miedzy zapisem checkpointu a `step_waiting` (test zabicia silnika w oknie).

    Tylko dla dodatniej liczby sekund; wartosc nieliczbowa zostawia komunikat i nie czeka."""
    surowa = os.environ.get(ZMIENNA_PAUZY)
    if surowa is None:
        return
    try:
        sekundy = float(surowa)
    except ValueError:
        print(f"{ZMIENNA_PAUZY}={surowa!r} nie jest liczba, pauza pominieta", file=sys.stderr)
        return
    if sekundy > 0:
        await asyncio.sleep(sekundy)


async def _zapisz(p: Przygotowany, cp: Checkpoint) -> BladCheckpointu | None:
    """Zapis checkpointu w watku; blad dysku wraca jako wartosc (silnik konczy run bledem)."""
    try:
        await asyncio.to_thread(zapisz_checkpoint, p.worktree.katalog_runu, cp)
    except BladCheckpointu as e:
        return e
    return None


def _checkpoint(
    run_id: str, nazwa: str, p: Przygotowany, stany: Mapping[str, StanKroku],
    startowane: set[str], czeka: Oczekiwanie,
) -> Checkpoint:
    return Checkpoint(
        run_id=run_id, repozytorium=str(p.repozytorium), workflow=nazwa,
        base_commit=p.worktree.base_commit, worktree=str(p.worktree.sciezka),
        kroki={
            i: StanKrokuCheckpointu(stan.value, 1 if i in startowane else 0)
            for i, stan in stany.items()
        },
        waiting_on=czeka,
    )


async def _blad_przed_startem(emit: Emit, run_id: str, tekst: str) -> None:
    """Dokladnie `step_failed` i `run_finished` kroku walidacji: bez pierwszego kokpit
    wyliczylby z samego `run_finished` falszywe "do odbioru" (evidence_required)."""
    await emit(_zdarzenie(run_id, "step_failed", KROK_WALIDACJI, "", tekst))
    await emit(_zdarzenie(
        run_id, "run_finished", KROK_WALIDACJI, "", tekst,
        cost_usd=None, cost_source="unavailable"))


async def wykonaj_run(
    polecenie: Mapping[str, Any],
    emit: Emit,
    uruchom_agenta: UruchomAgenta = wykonaj_krok,
    *,
    profil: ProfilBackendu | None = None,
    dom: Path | None = None,
    root: Path | None = None,
    zakres: Zakres | None = None,
) -> None:
    """Wykonuje workflow z polecenia `run_workflow`; zdarzenia idzie przez `emit`."""
    run_id = polecenie.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        print("polecenie run_workflow bez poprawnego run_id odrzucone", file=sys.stderr)
        return
    profil = profil if profil is not None else ProfilBackendu.z_srodowiska()
    dom = dom if dom is not None else Path.home()
    try:
        p = await asyncio.to_thread(_przygotuj, polecenie, dom, root, zakres, run_id)
    except BladStartu as e:
        await _blad_przed_startem(emit, run_id, str(e))
        return

    capabilities = p.projekt.capabilities
    stany = {k.id: StanKroku.OCZEKUJE for k in p.workflow.kroki}
    w_toku: dict[asyncio.Task, tuple[str, float]] = {}
    ostatni = "run"
    bramki = {k.id for k in p.workflow.kroki if k.approval}
    startowane: set[str] = set()
    oczekiwanie: Oczekiwanie | None = None     # najwyzej jedna zawieszona bramka naraz
    blad_zapisu: BladCheckpointu | None = None
    nazwa = polecenie["workflow"]              # sprawdzone w `_przygotuj`
    try:
        while True:
            plan = nastepne(p.workflow, p.rejestr, capabilities, stany)
            for id_kroku, powod in plan.do_pominiecia.items():
                stany[id_kroku] = StanKroku.POMINIETY
                await emit(_zdarzenie(
                    run_id, "step_skipped", id_kroku, id_kroku,
                    f"{plan.powody[id_kroku].value}: {powod}"))
            for id_kroku in plan.do_startu:
                if blad_zapisu is not None:
                    break      # bez dalszych startow; kroki w toku dobiegaja konca
                if id_kroku in bramki:
                    # bramka stoi PRZED krokiem: bez step_started, zadania i procesu agenta;
                    # krok zostaje OCZEKUJE, wiec `nastepne` zwraca go w kazdej iteracji.
                    # Zapis nr 1 (bezpieczenstwo przy smierci procesu), bez zdarzenia: gdyby
                    # `step_waiting` poszlo przed krokami niezaleznymi, ich `step_started`
                    # przestawilby run z `waiting` na `running`.
                    if oczekiwanie is None:
                        oczekiwanie = Oczekiwanie(
                            id_kroku, datetime.now(timezone.utc).isoformat())
                        blad_zapisu = await _zapisz(
                            p, _checkpoint(run_id, nazwa, p, stany, startowane, oczekiwanie))
                    continue
                startowane.add(id_kroku)
                # mutant None jest rownowazny dla `nastepne` (planuje tylko OCZEKUJE, a `needs`
                # spelnia tylko ZAKONCZONY/POMINIETY); chroni go reczny mutant W_TOKU -> OCZEKUJE
                stany[id_kroku] = StanKroku.W_TOKU  # pragma: no mutate
                await emit(_zdarzenie(run_id, "step_started", id_kroku, id_kroku, ""))
                zadanie = asyncio.create_task(
                    _krok(run_id, id_kroku, p, profil, uruchom_agenta, emit))
                w_toku[zadanie] = (id_kroku, time.monotonic())
            if not w_toku:
                break
            gotowe, _ = await asyncio.wait(w_toku, return_when=asyncio.FIRST_COMPLETED)
            for zadanie in [z for z in w_toku if z in gotowe]:
                id_kroku, start = w_toku.pop(zadanie)
                try:
                    wynik = zadanie.result()
                except PorazkaKroku as e:
                    udany, powod = False, str(e)
                else:
                    udany, powod = wynik.status == "zakonczony", wynik.powod
                czas_ms = int((time.monotonic() - start) * 1000)
                stany[id_kroku] = StanKroku.ZAKONCZONY if udany else StanKroku.NIEUDANY
                ostatni = id_kroku
                # nieudany krok ma powod (krok.py); text zakonczonego to pusty napis
                await emit(_zdarzenie(
                    run_id, "step_finished" if udany else "step_failed", id_kroku, id_kroku,
                    "" if udany else powod,
                    duration_ms=czas_ms, cost_usd=None, cost_source="unavailable"))
    finally:
        zadania = list(w_toku)
        for zadanie in zadania:
            zadanie.cancel()
        await asyncio.gather(*zadania, return_exceptions=True)
    if oczekiwanie is not None:
        if blad_zapisu is None:
            # zapis nr 2: stany krokow poszly dalej od pierwszego zapisu
            blad_zapisu = await _zapisz(
                p, _checkpoint(run_id, nazwa, p, stany, startowane, oczekiwanie))
        if blad_zapisu is None:
            # run czeka na czlowieka: `step_waiting` jest ostatnim zdarzeniem, bez `run_finished`
            await _pauza_testowa()
            await emit(_zdarzenie(
                run_id, "step_waiting", oczekiwanie.step_id, oczekiwanie.step_id, TEKST_CZEKANIA))
            return
        await emit(_zdarzenie(
            run_id, "step_failed", oczekiwanie.step_id, oczekiwanie.step_id,
            f"zapis checkpointu nieudany: {blad_zapisu}"))
    await emit(_zdarzenie(
        run_id, "run_finished", ostatni, "", "", cost_usd=None, cost_source="unavailable"))
