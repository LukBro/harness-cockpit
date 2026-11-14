"""Silnik runu u runnera: wykonanie workflow wedlug grafu `needs` (HAR-80, D11).

Petla wykonania zyje w runnerze, a kokpit wylicza stan runu ze zdarzen. Silnik waliduje
workflow, tworzy worktree, materializuje skille i uruchamia kroki rownolegle tam, gdzie
pozwala `needs`. Krok z `approval: true` zawiesza run na bramce BEZ procesu agenta
(HAR-172): silnik zapisuje checkpoint z `waiting_on` w chwili trafienia na bramke (bez
zdarzenia), kroki niezalezne od bramki dochodza do konca, potem drugi zapis, pauza testowa
i `step_waiting` jako ostatnie zdarzenie runu. Wszystkie odczyty (rejestr skilli,
workflow, project.yaml) ida z worktree (stan z commita), nie z repozytorium uzytkownika.
Silnik nie sprzata worktree: zostaje do odbioru. Fan-out, konsensus i foreach to osobne tickety.

Petla naprawcza (HAR-109, D28): krok z `on_fail: {retry, max}` po porazce wraca do celu `retry`
(wszystkie kroki na sciezce `needs` od celu do kroku nieudanego) z raportem porazki jako
wejsciem celu, co najwyzej `max` razy; potem `step_failed` z powodem wyczerpania i koniec runu.
Kazdy start kroku zwieksza jego licznik prob (checkpoint: `proby`), a zdarzenia kroku niosa `attempt`.

Wznowienie po decyzji (HAR-173): `wznow_run` czyta WYLACZNIE checkpoint z dysku i kopie
workflow w katalogu runu, wiec dziala w nowej instancji runnera. `approve` zapisuje decyzje
jako artefakt wejsciowy kroku za bramka, zeruje `waiting_on` w checkpoincie i dopiero potem
rusza petla planu (ta sama co w `wykonaj_run`) od stanow z checkpointu; `reject` zamyka
checkpoint i konczy run bez startu kroku. Polecenie, ktore nie pasuje do oczekiwania
(powtorzone, dla innego kroku, nieznana decyzja), jest ignorowane zdarzeniem `resume_ignored`.
Kroki zabite przy smierci runnera (stan `w_toku`) nie sa wznawiane: dostaja `step_failed`.

Kazda sciezka konczy sie zdarzeniem `run_finished`. Wyjatki: zawieszenie na bramce (ostatnie
zdarzenie to `step_waiting`; `run_finished` zmienilby w kokpicie `waiting` na "do odbioru"),
anulowanie (`CancelledError`, po anulowaniu zadan krokow) i bledy samego `emit`
(np. `BrakMiejsca`) - decyduje o nich wolajacy. Nieudany zapis checkpointu to `step_failed`
kroku bramki (bez `step_waiting`) i `run_finished`. Wyjatek wznowienia: polecenie ignorowane
konczy sie samym `resume_ignored`.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
import weakref
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cockpit.services.errors import BladDziedziny
from cockpit.services.kroki_systemowe import PLIK_LINII, SYSTEM_CHECK, opis_kroku
from cockpit.services.projekt import Projekt, wczytaj_projekt
from cockpit.services.skills import Rejestr, materializuj, rozstrzygnij_rejestr
from cockpit.services.workflow import Krok, Problem, Workflow, ma_bledy, wczytaj_workflow
from cockpit.services.workflow_walidacja import waliduj_workflow
from runner.agent import (
    ProfilBackendu,
    SpecKroku,
    polecenie_agenta,
    zbuduj_prompt,
)
from runner.checkpoint import (
    BladCheckpointu,
    ZMIENNA_PAUZY,
    Checkpoint,
    Oczekiwanie,
    StanKrokuCheckpointu,
    wczytaj_checkpoint,
    zapisz_checkpoint,
)
from runner.dowody import wykonaj_check
from runner.graf import StanKroku, kroki_do_powtorzenia, nastepne
from runner.kotwice import PLIK_NOTATKI
from runner.krok import (
    KATALOG_RUNU,
    PomiarKroku,
    WynikKroku,
    _blad_identyfikatora,
    _zapisz_atomowo,
    wykonaj_krok,
)
from runner.krok_diff import wykonaj_diff
from runner.weryfikacja import kroki_weryfikujace
from runner.worktree import WorktreeRunu, katalog_runu, utworz_worktree
from runner.zakres import Zakres, ZlaKonfiguracjaZakresu

Emit = Callable[[dict], Awaitable[None]]
UruchomAgenta = Callable[..., Awaitable[WynikKroku]]

NAZWA_WORKFLOW = re.compile(r"[A-Za-z0-9_.-]{1,64}")
# Katalog workflow w repozytorium: odczyt idzie z worktree (stan z commita). Stala, bo literal
# w funkcji przezywa mutacje wielkosci liter na systemach plikow bez jej rozrozniania (macOS).
KATALOG_WORKFLOW = Path(".cockpit") / "workflows"
KROK_WALIDACJI = "walidacja"   # krok, na ktorym kokpit widzi blad przed startem krokow
TEKST_CZEKANIA = "czeka na decyzję człowieka (approval)"
TEKST_PRZERWANIA = "przerwany przy śmierci runnera — nie wznawiamy"
DECYZJE = ("approve", "reject")
LINIE_OGONA = 20   # ostatnie linie `output` kroku nieudanego w raporcie porazki (HAR-109)
KATALOG_ARTEFAKTOW = KATALOG_RUNU / "artifacts"   # decyzje czlowieka jako wejscia krokow
# blokada per run: sprawdzenie oczekiwania i zapis checkpointu bez `waiting_on` sa atomowe
# wzgledem drugiego polecenia `resume`; slaba mapa nie trzyma blokad zakonczonych runow
_BLOKADY: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


class BladStartu(Exception):
    """Run nie moze ruszyc: tekst to lista problemow, jeden w linii (D4)."""


class BladWznowienia(BladDziedziny):
    """Run nie da sie wznowic z checkpointu (worktree, workflow, stan); tekst to powod."""


class PorazkaKroku(Exception):
    """Krok zbudowany przez silnik nie ruszyl (prompt, model); tekst to powod."""


# Bledy granic wywolan, ktore zamieniamy na `step_failed` + `run_finished` (HAR-242);
# `CancelledError` nie jest ich podklasa, wiec anulowanie leci dalej.
BLEDY_GRANICY = (BladDziedziny, OSError, KeyError, ValueError)


def _tekst_bledu(e: Exception) -> str:
    """`str(KeyError('x'))` to samo `'x'` i nic nie mowi; reszta zostaje bez zmian."""
    return f"brak klucza {e}" if isinstance(e, KeyError) else str(e)


@dataclass(frozen=True)
class Przygotowany:
    worktree: WorktreeRunu
    workflow: Workflow
    rejestr: Rejestr
    projekt: Projekt
    repozytorium: Path


# Wykonawca kroku systemowego: (krok, przygotowany run) -> wynik. Kazdy krok systemowy ma
# katalog `<katalog_runu>/<id kroku>`. Rejestr jest jedynym miejscem, ktore zna nazwy krokow
# systemowych po stronie runnera; opis kontraktu (produces/consumes/needs) lezy w
# `cockpit.services.kroki_systemowe`.
WykonawcaSystemowy = Callable[[Krok, Przygotowany], Awaitable[WynikKroku]]


async def _wykonaj_diff(krok: Krok, p: Przygotowany) -> WynikKroku:
    return await wykonaj_diff(
        p.worktree.sciezka, p.worktree.base_commit, p.worktree.katalog_runu / krok.id)


async def _wykonaj_check(krok: Krok, p: Przygotowany) -> WynikKroku:
    """Komenda capability kroku z project.yaml; jej brak tu (graf pomija krok wczesniej) to porazka."""
    capability = krok.capability
    if capability is None or capability not in p.projekt.capabilities:
        raise PorazkaKroku(f"brak capability {capability}")
    komenda = p.projekt.capabilities[capability]
    return await wykonaj_check(
        p.worktree.sciezka, capability, komenda, p.worktree.katalog_runu / krok.id)


WYKONAWCY_SYSTEMOWI: Mapping[str, WykonawcaSystemowy] = {
    "diff": _wykonaj_diff,
    SYSTEM_CHECK: _wykonaj_check,
}


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

    Blad wejscia (dysk, git, zakres, skill) konczy run bledem startu; nic nie powstaje na
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
        materializuj({k.skill for k in workflow.kroki if k.skill is not None}, rejestr, wt.sciezka)
    # BladDziedziny i OSError koncza run w `wykonaj_run` (`except BLEDY_GRANICY`) z tym samym
    # tekstem - tu tylko ZlaKonfiguracjaZakresu, ktora nie jest bledem granicy
    except ZlaKonfiguracjaZakresu as e:
        raise BladStartu(str(e)) from e
    return Przygotowany(wt, workflow, rejestr, projekt, repo)


def _sciezka_decyzji(step_id: str) -> Path:
    """Sciezka artefaktu decyzji wzgledem worktree."""
    return KATALOG_ARTEFAKTOW / f"decyzja_{step_id}.json"


def _zdarzenie(
    run_id: str, kind: str, step_id: str, agent_id: str, text: str, *,
    attempt: int | None = None, **reszta,
) -> dict:
    """Zdarzenie runu; `attempt` (numer proby kroku) tylko dla zdarzen kroku."""
    zdarzenie = {
        "kind": kind, "run_id": run_id, "step_id": step_id, "agent_id": agent_id,
        "text": text, **reszta,
    }
    if attempt is not None:
        zdarzenie["attempt"] = attempt
    return zdarzenie


def _sciezka_raportu(cel: str, nr: int) -> Path:
    """Sciezka raportu porazki wzgledem worktree: artefakt celu petli naprawczej (HAR-109)."""
    return KATALOG_ARTEFAKTOW / cel / "_naprawa" / f"proba_{nr}.md"


def _krok_producenta(p: Przygotowany, id_kroku: str, artefakt: str) -> str | None:
    """Id kroku systemowego, ktory produkuje `artefakt` i jest przodkiem `id_kroku` (albo None).

    Producent skillowy nie jest tu potrzebny: jego artefakty to nazwy wzgledne, a krok
    systemowy zapisuje pod `<katalog_runu>/<id kroku>/`."""
    kroki = {k.id: k for k in p.workflow.kroki}
    stos = list(kroki[id_kroku].needs)
    odwiedzeni: set[str] = set()
    while stos:
        kandydat = stos.pop(0)
        if kandydat in odwiedzeni or kandydat not in kroki:
            continue
        odwiedzeni.add(kandydat)
        krok = kroki[kandydat]
        if krok.skill is None and artefakt in opis_kroku(krok).produces:
            return kandydat
        stos.extend(krok.needs)
    return None


def _wejscie(p: Przygotowany, id_kroku: str, artefakt: str) -> str:
    """Wejscie agenta: sciezka bezwzgledna dla artefaktu kroku systemowego, inaczej nazwa."""
    producent = _krok_producenta(p, id_kroku, artefakt)
    if producent is None:
        return artefakt
    return str(p.worktree.katalog_runu / producent / artefakt)


async def _krok_systemowy(krok: Krok, p: Przygotowany) -> WynikKroku:
    """Krok bez agenta: wykonawca z rejestru, artefakty w katalogu runu pod id kroku."""
    wykonawca = WYKONAWCY_SYSTEMOWI.get(krok.system)
    if wykonawca is None:
        raise PorazkaKroku(f"nieznany krok systemowy: {krok.system}")
    return await wykonawca(krok, p)


async def _krok(
    run_id: str, id_kroku: str, p: Przygotowany, profil: ProfilBackendu,
    uruchom_agenta: UruchomAgenta, emit: Emit, zatwierdzone: frozenset[str] = frozenset(),
    *, proba: int, ogon: deque[str] | None = None,
) -> WynikKroku:
    """Jeden krok: specyfikacja agenta, prompt, polecenie; blad budowy to `PorazkaKroku`.

    Krok systemowy idzie do wykonawcy z rejestru, bez agenta. Krok zatwierdzony na bramce
    dostaje artefakt decyzji jako ostatnie wejscie. Cel petli naprawczej w probie > 1 dostaje
    sciezke raportu porazki poprzedniej proby, jesli plik istnieje (HAR-109). Zdarzenia `output`
    niosa numer proby, a ich tekst trafia do `ogon` (raport porazki)."""
    krok = next(k for k in p.workflow.kroki if k.id == id_kroku)
    if krok.skill is None:
        return await _krok_systemowy(krok, p)
    skill = p.rejestr.skille[krok.skill]
    wejscia = [_wejscie(p, id_kroku, artefakt) for artefakt in skill.consumes]
    if proba > 1:
        raport = _sciezka_raportu(id_kroku, proba - 1)
        try:
            (p.worktree.sciezka / raport).stat()
        except FileNotFoundError:
            pass
        except OSError as e:     # nie umiemy stwierdzic, czy raport jest: nie przepuszczamy
            raise PorazkaKroku(f"nie sprawdzono raportu porażki ({type(e).__name__})") from e
        else:
            wejscia.append(raport.as_posix())
    if id_kroku in zatwierdzone:
        wejscia.append(_sciezka_decyzji(id_kroku).as_posix())
    spec = SpecKroku(
        run_id=run_id, step_id=krok.id, agent_id=krok.id, skill_id=krok.skill,
        rola=skill.role, wejscia=wejscia, wyjscia=list(skill.produces),
        komendy={n: p.projekt.capabilities[n] for n in skill.needs if n in p.projekt.capabilities},
        budget_usd=krok.budget_usd, max_turns=krok.max_turns,
    )
    # zly prompt (ValueError), brak modelu tieru (BrakModelu), zly budzet albo limit tur konczy
    # krok w petli zadan (`except BLEDY_GRANICY`) z tym samym tekstem - osobny except bylby martwy
    prompt = zbuduj_prompt(spec)
    argv = polecenie_agenta(spec, profil)
    dodatkowe: dict[str, object] = {}
    if PLIK_NOTATKI in skill.produces:
        producent = _krok_producenta(p, id_kroku, PLIK_LINII)
        if producent is not None:
            dodatkowe["zmienione_linie"] = p.worktree.katalog_runu / producent / PLIK_LINII
    if skill.verdict is not None:
        dodatkowe["werdykt"] = skill.verdict

    async def emit_proby(zdarzenie: dict) -> None:
        if zdarzenie.get("kind") == "output":
            zdarzenie = {**zdarzenie, "attempt": proba}
            if ogon is not None:
                ogon.append(str(zdarzenie.get("text", "")))
        await emit(zdarzenie)

    return await uruchom_agenta(
        spec, argv, p.worktree.sciezka, emit_proby, katalog_runu=p.worktree.katalog_runu,
        prompt=prompt, **dodatkowe)


def _artefakty_kroku(p: Przygotowany, id_kroku: str) -> list[str]:
    """Artefakty (`produces`) kroku: z opisu kroku systemowego albo ze skilla (HAR-170)."""
    krok = next(k for k in p.workflow.kroki if k.id == id_kroku)
    if krok.skill is None:
        return list(opis_kroku(krok).produces)
    return list(p.rejestr.skille[krok.skill].produces)


def _produkuje_notatke(p: Przygotowany, id_kroku: str) -> bool:
    """Czy skill kroku produkuje `notatka-zmiany.md` (krok systemowy nie ma skilla)."""
    krok = next(k for k in p.workflow.kroki if k.id == id_kroku)
    return krok.skill is not None and PLIK_NOTATKI in p.rejestr.skille[krok.skill].produces


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
    startowane: Mapping[str, int], czeka: Oczekiwanie | None,
) -> Checkpoint:
    return Checkpoint(
        run_id=run_id, repozytorium=str(p.repozytorium), workflow=nazwa,
        base_commit=p.worktree.base_commit, worktree=str(p.worktree.sciezka),
        kroki={
            i: StanKrokuCheckpointu(stan.value, startowane.get(i, 0))
            for i, stan in stany.items()
        },
        waiting_on=czeka,
    )


def _pola_pomiaru(pomiar: PomiarKroku) -> dict:
    """Pola pomiaru zdarzenia konca kroku; brak kosztu to `unavailable`, nigdy zero."""
    return {
        "cost_usd": pomiar.cost_usd,
        "cost_source": "measured" if pomiar.cost_usd is not None else "unavailable",
        "tokens_in": pomiar.tokens_in, "tokens_out": pomiar.tokens_out,
        "num_turns": pomiar.num_turns, "session_id": pomiar.session_id,
    }


def _ogon_dowodu(p: Przygotowany, id_kroku: str, dowod: Mapping[str, object]) -> str:
    """`stdout_tail` z pliku dowodu kroku `check`; nieczytelny plik to linia, nie wyjatek."""
    try:
        plik = p.worktree.katalog_runu / id_kroku / str(dowod["plik"])
        # mutanty UTF-8 i None sa rownowazne (nazwa kodeka bez rozroznienia wielkosci liter,
        # domyslne kodowanie procesu to UTF-8); inne kodowanie chroni reczny mutant
        dane = json.loads(plik.read_text(encoding="utf-8"))  # pragma: no mutate
        return str(dane["stdout_tail"])
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as e:
        return f"ogon niedostępny ({type(e).__name__})"


def _zapisz_raport_porazki(
    p: Przygotowany, id_kroku: str, proba: int, cel: str, proba_celu: int,
    wynik: WynikKroku | None, powod: str | None, ogon: list[str],
) -> str | None:
    """Raport porazki jako artefakt celu petli naprawczej; `None` albo opis bledu zapisu."""
    blad = _blad_identyfikatora(cel)
    if blad is not None:
        return blad
    kod = wynik.kod_wyjscia if wynik is not None else None
    werdykt = wynik.werdykt if wynik is not None else None
    linie = [
        f"# Raport porażki kroku {id_kroku}", "",
        f"- krok: {id_kroku}",
        f"- próba kroku: {proba}",
        f"- kod wyjścia: {'brak' if kod is None else kod}",
        f"- powód: {'brak' if powod is None else powod}",
        f"- werdykt: {'brak' if werdykt is None else werdykt}",
        "", "## Ogon logu", "", *ogon,
    ]
    if wynik is not None and wynik.dowod is not None and "plik" in wynik.dowod:
        linie += ["", "## Ogon wyjścia komendy", "", _ogon_dowodu(p, id_kroku, wynik.dowod)]
    # mutant UTF-8 jest rownowazny (nazwa kodeka bez rozroznienia wielkosci liter);
    # inne kodowanie chroni reczny mutant
    tresc = ("\n".join(linie) + "\n").encode("utf-8")  # pragma: no mutate
    try:
        _zapisz_atomowo(p.worktree.sciezka / _sciezka_raportu(cel, proba_celu), tresc)
    except OSError as e:
        return type(e).__name__
    return None


async def _obsluz_porazke(
    p: Przygotowany, krok: Krok, startowane: Mapping[str, int], wynik: WynikKroku | None,
    powod: str | None, ogon: list[str],
) -> tuple[str | None, list[str]]:
    """(tekst `step_failed`, kroki do powtorzenia) dla kroku z `on_fail` (HAR-109).

    `max` to liczba PONOWIEN: krok w probie `p` wraca, gdy `p - 1 < max`. Raport porazki
    powstaje tez przy wyczerpaniu. Brak mozliwosci ponowienia (blad zapisu raportu, zly cel)
    to porazka bez powtorzenia, nigdy cicha."""
    on_fail = krok.on_fail
    if on_fail is None:
        return powod, []
    # cel mogl zostac pominiety (np. brak capability) i nigdy nie wystartowac, a krok nieudany
    # i tak ruszyl (pominiety przodek liczy sie jak gotowy); bez startu celu nie ma do czego
    # wracac ani numeru raportu, wiec to porazka bez ponowienia i bez raportu
    if startowane.get(on_fail.retry, 0) == 0:
        return f"nie można ponowić kroku {krok.id}: cel {on_fail.retry} nie wystartował", []
    try:
        powtarzane = kroki_do_powtorzenia(p.workflow, on_fail.retry, krok.id)
    except ValueError as e:
        return f"nie można ponowić kroku {krok.id}: {e}", []
    blad = await asyncio.to_thread(
        _zapisz_raport_porazki, p, krok.id, startowane[krok.id], on_fail.retry,
        startowane[on_fail.retry], wynik, powod, ogon)
    if blad is not None:
        return f"nie zapisano raportu porażki ({blad})", []
    if startowane[krok.id] - 1 < on_fail.max:
        return powod, powtarzane
    return f"wyczerpany limit {on_fail.max} ponowień kroku {krok.id}", []


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
    try:
        profil = profil if profil is not None else ProfilBackendu.z_konfiguracji()
        dom = dom if dom is not None else Path.home()
    except BLEDY_GRANICY as e:      # `BladProfiluBackendu` jest błędem dziedziny
        await _blad_przed_startem(emit, run_id, _tekst_bledu(e))
        return
    try:
        p = await asyncio.to_thread(_przygotuj, polecenie, dom, root, zakres, run_id)
    except BladStartu as e:
        await _blad_przed_startem(emit, run_id, str(e))
        return
    except BLEDY_GRANICY as e:      # wyjatek spoza `BladStartu` nie ucieka bez konca runu
        await _blad_przed_startem(emit, run_id, _tekst_bledu(e))
        return

    try:
        await _wykonaj_plan(
            run_id, polecenie["workflow"],     # nazwa sprawdzona w `_przygotuj`
            p, {k.id: StanKroku.OCZEKUJE for k in p.workflow.kroki}, {}, frozenset(),
            profil, uruchom_agenta, emit)
    except BLEDY_GRANICY as e:
        await _blad_przed_startem(emit, run_id, _tekst_bledu(e))


async def _wykonaj_plan(
    run_id: str, nazwa: str, p: Przygotowany, stany: dict[str, StanKroku],
    startowane: dict[str, int], zatwierdzone: frozenset[str], profil: ProfilBackendu,
    uruchom_agenta: UruchomAgenta, emit: Emit,
) -> None:
    """Petla planu wspolna dla `wykonaj_run` i `wznow_run`: rusza od podanych stanow krokow.

    `zatwierdzone` to kroki z bramka, ktorych decyzja juz zapadla: nie zatrzymuja sie drugi
    raz, a ich agent dostaje artefakt decyzji wsrod wejsc. `startowane` to licznik startow
    per krok (numer proby). Ponowienie kroku z `on_fail` cofa kroki sciezki do `OCZEKUJE`, a te
    z bramka wracaja do bramek: nowa decyzja czlowieka (HAR-109)."""
    capabilities = p.projekt.capabilities
    w_toku: dict[asyncio.Task, tuple[str, float]] = {}
    ostatni = "run"
    bramki = {k.id for k in p.workflow.kroki if k.approval and k.id not in zatwierdzone}
    krok_po_id = {k.id: k for k in p.workflow.kroki}
    ogony: dict[str, deque[str]] = {}
    oczekiwanie: Oczekiwanie | None = None     # najwyzej jedna zawieszona bramka naraz
    blad_zapisu: BladCheckpointu | None = None
    try:
        while True:
            plan = nastepne(p.workflow, p.rejestr, capabilities, stany)
            for id_kroku, powod in plan.do_pominiecia.items():
                stany[id_kroku] = StanKroku.POMINIETY
                await emit(_zdarzenie(
                    run_id, "step_skipped", id_kroku, id_kroku,
                    f"{plan.powody[id_kroku].value}: {powod}",
                    attempt=startowane.get(id_kroku, 0) + 1))
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
                startowane[id_kroku] = startowane.get(id_kroku, 0) + 1
                ogony[id_kroku] = deque(maxlen=LINIE_OGONA)
                # mutant None jest rownowazny dla `nastepne` (planuje tylko OCZEKUJE, a `needs`
                # spelnia tylko ZAKONCZONY/POMINIETY); chroni go reczny mutant W_TOKU -> OCZEKUJE
                stany[id_kroku] = StanKroku.W_TOKU  # pragma: no mutate
                await emit(_zdarzenie(
                    run_id, "step_started", id_kroku, id_kroku, "", attempt=startowane[id_kroku]))
                zadanie = asyncio.create_task(
                    _krok(
                        run_id, id_kroku, p, profil, uruchom_agenta, emit, zatwierdzone,
                        proba=startowane[id_kroku], ogon=ogony[id_kroku]))
                w_toku[zadanie] = (id_kroku, time.monotonic())
            if not w_toku:
                break
            gotowe, _ = await asyncio.wait(w_toku, return_when=asyncio.FIRST_COMPLETED)
            for zadanie in [z for z in w_toku if z in gotowe]:
                id_kroku, start = w_toku.pop(zadanie)
                dodatkowe: dict[str, object] = {}
                wynik: WynikKroku | None = None
                try:
                    wynik = zadanie.result()
                except PorazkaKroku as e:
                    udany, powod, pomiar = False, str(e), PomiarKroku()
                except BLEDY_GRANICY as e:      # wyjatek zadania kroku to koniec kroku, nie runu
                    udany, powod, pomiar = False, _tekst_bledu(e), PomiarKroku()
                else:
                    udany, powod, pomiar = wynik.status == "zakonczony", wynik.powod, wynik.pomiar
                    if udany and _produkuje_notatke(p, id_kroku):
                        dodatkowe["kotwice"] = list(wynik.kotwice)
                    if wynik.dowod is not None:
                        dodatkowe["dowod"] = dict(wynik.dowod)
                    if wynik.werdykt is not None:
                        dodatkowe["verdict"] = wynik.werdykt
                    artefakty = _artefakty_kroku(p, id_kroku)
                    if udany and artefakty:
                        dodatkowe["artefakty"] = artefakty
                czas_ms = int((time.monotonic() - start) * 1000)
                stany[id_kroku] = StanKroku.ZAKONCZONY if udany else StanKroku.NIEUDANY
                ostatni = id_kroku
                powtarzane: list[str] = []
                if not udany:
                    # porazka kroku z `on_fail`: ponowienie albo wyczerpanie (przed propagacja
                    # porazki do potomkow w `nastepne`); bez `on_fail` powod zostaje jak byl
                    powod, powtarzane = await _obsluz_porazke(
                        p, krok_po_id[id_kroku], startowane, wynik, powod, list(ogony[id_kroku]))
                # nieudany krok ma powod (krok.py); text zakonczonego to pusty napis
                await emit(_zdarzenie(
                    run_id, "step_finished" if udany else "step_failed", id_kroku, id_kroku,
                    "" if udany else powod, attempt=startowane[id_kroku],
                    duration_ms=czas_ms, **_pola_pomiaru(pomiar), **dodatkowe))
                for powtarzany in powtarzane:
                    stany[powtarzany] = StanKroku.OCZEKUJE
                    if krok_po_id[powtarzany].approval:
                        # wystarczy `bramki`: krok cofniety na bramke nie startuje w tym wywolaniu
                        # (zatrzymanie na `step_waiting`), a nowa decyzja przychodzi przez
                        # `wznow_run` z nowym `zatwierdzone`
                        bramki.add(powtarzany)
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
                run_id, "step_waiting", oczekiwanie.step_id, oczekiwanie.step_id, TEKST_CZEKANIA,
                attempt=startowane.get(oczekiwanie.step_id, 0) + 1))
            return
        await emit(_zdarzenie(
            run_id, "step_failed", oczekiwanie.step_id, oczekiwanie.step_id,
            f"zapis checkpointu nieudany: {blad_zapisu}",
            attempt=startowane.get(oczekiwanie.step_id, 0) + 1))
    await emit(_zdarzenie(
        run_id, "run_finished", ostatni, "", "", cost_usd=None, cost_source="unavailable",
        kroki_weryfikujace=kroki_weryfikujace(p.workflow, p.rejestr)))


def _zapisz_decyzje(
    p: Przygotowany, run_id: str, step_id: str, decyzja: str, powod: str | None,
) -> None:
    """Artefakt decyzji w worktree (atomowo); blad dysku to `BladWznowienia`."""
    tresc = json.dumps({
        "run_id": run_id, "step_id": step_id, "decyzja": decyzja, "powod": powod,
        "czas": datetime.now(timezone.utc).isoformat(),
    # mutant None jest rownowazny (json czyta ensure_ascii jako bool); wariant True chroni reczny mutant
    }, ensure_ascii=False)  # pragma: no mutate
    # mutant UTF-8 jest rownowazny (codecs.lookup nie rozroznia wielkosci liter w nazwie kodeka);
    # inne kodowanie chroni reczny mutant
    bajty = tresc.encode("utf-8")  # pragma: no mutate
    try:
        _zapisz_atomowo(p.worktree.sciezka / _sciezka_decyzji(step_id), bajty)
    except OSError as e:
        raise BladWznowienia(f"zapis artefaktu decyzji dla kroku {step_id}: {e}") from e


def _przygotuj_wznowienie(cp: Checkpoint, katalog: Path, dom: Path) -> Przygotowany:
    """Workflow z kopii w katalogu runu, rejestr i projekt z worktree z checkpointu."""
    worktree = Path(cp.worktree)
    if not worktree.is_dir():
        raise BladWznowienia(f"brak worktree runu: {worktree}")
    workflow, problemy = wczytaj_workflow(katalog / "workflow.yaml")
    rejestr = rozstrzygnij_rejestr(worktree, dom)
    if workflow is not None:
        # ta sama walidacja co przy starcie: skill kroku mogl zniknac miedzy bramka a decyzja
        problemy += waliduj_workflow(workflow, rejestr, dom)
    if workflow is None or ma_bledy(problemy):
        raise BladWznowienia(_bledy([p for p in problemy if ma_bledy([p])]))
    projekt, problemy_projektu = wczytaj_projekt(worktree)
    if projekt is None:
        raise BladWznowienia(_bledy(problemy_projektu))
    return Przygotowany(
        WorktreeRunu(worktree, cp.base_commit, katalog), workflow, rejestr, projekt,
        Path(cp.repozytorium))


def _stany_z_checkpointu(cp: Checkpoint) -> dict[str, StanKroku]:
    try:
        return {i: StanKroku(k.stan) for i, k in cp.kroki.items()}
    except ValueError as e:
        raise BladWznowienia(f"checkpoint: nieznany stan kroku: {e}") from e


def _powod_ignorowania(cp: Checkpoint, step_id: str, decyzja: str) -> str | None:
    if cp.waiting_on is None:
        return "run nie czeka na decyzję (polecenie powtórzone albo run już zakończony)"
    if cp.waiting_on.step_id != step_id:
        return f"run czeka na decyzję dla kroku {cp.waiting_on.step_id}, nie {step_id}"
    if decyzja not in DECYZJE:
        return f"nieznana decyzja: {decyzja!r}"
    return None


async def _porazka_wznowienia(emit: Emit, run_id: str, step_id: str, tekst: str) -> None:
    """`step_failed` kroku z polecenia i `run_finished`: bez pierwszego kokpit wyliczylby
    z samego `run_finished` falszywe "do odbioru" (evidence_required)."""
    await emit(_zdarzenie(run_id, "step_failed", step_id, step_id, tekst))
    await emit(_zdarzenie(
        run_id, "run_finished", step_id, "", tekst, cost_usd=None, cost_source="unavailable"))


async def _przyjmij_decyzje(
    run_id: str, step_id: str, decyzja: str, powod: str | None, dom: Path, root: Path | None,
    emit: Emit,
) -> tuple[str, Przygotowany, dict[str, StanKroku], dict[str, int], list[str]] | None:
    """Pod blokadą runu: odczyt checkpointu, decyzja i zapis checkpointu bez `waiting_on`.

    `None` gdy polecenie ignorowane albo run odrzucony (zdarzenia juz poszly); inaczej
    stan do dalszego wykonania i kroki przerwane przy smierci runnera (juz `nieudany`)."""
    katalog = katalog_runu(run_id, root)
    cp = await asyncio.to_thread(wczytaj_checkpoint, katalog)
    ignorowane = _powod_ignorowania(cp, step_id, decyzja)
    if ignorowane is not None:
        await emit(_zdarzenie(run_id, "resume_ignored", step_id, step_id, ignorowane))
        return None
    if decyzja == "reject":
        await asyncio.to_thread(zapisz_checkpoint, katalog, replace(cp, waiting_on=None))
        await emit(_zdarzenie(
            run_id, "run_finished", step_id, "", powod or "",
            cost_usd=None, cost_source="unavailable"))
        return None
    p = await asyncio.to_thread(_przygotuj_wznowienie, cp, katalog, dom)
    stany = _stany_z_checkpointu(cp)
    if set(stany) != {k.id for k in p.workflow.kroki}:
        raise BladWznowienia("checkpoint nie pasuje do kopii workflow: inne kroki")
    startowane = {i: k.proby for i, k in cp.kroki.items()}
    przerwane = [i for i, stan in stany.items() if stan is StanKroku.W_TOKU]
    for id_kroku in przerwane:
        stany[id_kroku] = StanKroku.NIEUDANY
    await asyncio.to_thread(_zapisz_decyzje, p, run_id, step_id, decyzja, powod)
    await asyncio.to_thread(
        zapisz_checkpoint, katalog, _checkpoint(run_id, cp.workflow, p, stany, startowane, None))
    return cp.workflow, p, stany, startowane, przerwane


async def wznow_run(
    run_id: str,
    step_id: str,
    decyzja: str,
    emit: Emit,
    uruchom_agenta: UruchomAgenta = wykonaj_krok,
    *,
    powod: str | None = None,
    profil: ProfilBackendu | None = None,
    dom: Path | None = None,
    root: Path | None = None,
) -> None:
    """Wznawia run zawieszony na bramce `step_id` decyzja czlowieka, wylacznie z checkpointu."""
    try:
        dom = dom if dom is not None else Path.home()
        # przed decyzja: zly profil nie zjada oczekiwania, wiec po poprawce pliku da sie wznowic
        profil = profil if profil is not None else ProfilBackendu.z_konfiguracji()
        async with _BLOKADY.setdefault(run_id, asyncio.Lock()):
            start = await _przyjmij_decyzje(run_id, step_id, decyzja, powod, dom, root, emit)
    except BLEDY_GRANICY as e:      # BladCheckpointu, BladWznowienia, katalog runu, struktura
        await _porazka_wznowienia(emit, run_id, step_id, _tekst_bledu(e))
        return
    if start is None:
        return
    nazwa, p, stany, startowane, przerwane = start
    for id_kroku in przerwane:
        await emit(_zdarzenie(
            run_id, "step_failed", id_kroku, id_kroku, TEKST_PRZERWANIA,
            attempt=startowane[id_kroku]))
    try:
        await _wykonaj_plan(
            run_id, nazwa, p, stany, startowane, frozenset({step_id}), profil, uruchom_agenta,
            emit)
    except BLEDY_GRANICY as e:
        await _porazka_wznowienia(emit, run_id, step_id, _tekst_bledu(e))
