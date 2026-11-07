"""Silnik runu u runnera: wykonanie workflow wedlug grafu `needs` (HAR-80, D11).

Petla wykonania zyje w runnerze, a kokpit wylicza stan runu ze zdarzen. Silnik waliduje
workflow, tworzy worktree, materializuje skille i uruchamia kroki rownolegle tam, gdzie
pozwala `needs`. Krok z `approval: true` zawiesza run na bramce BEZ procesu agenta
(HAR-172): silnik zapisuje checkpoint z `waiting_on` w chwili trafienia na bramke (bez
zdarzenia), kroki niezalezne od bramki dochodza do konca, potem drugi zapis, pauza testowa
i `step_waiting` jako ostatnie zdarzenie runu. Wszystkie odczyty (rejestr skilli,
workflow, project.yaml) ida z worktree (stan z commita), nie z repozytorium uzytkownika.
Silnik nie sprzata worktree: zostaje do odbioru. Fan-out, konsensus, foreach i petla
naprawcza to osobne tickety.

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
    BladProfiluBackendu,
    BrakModelu,
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
from runner.graf import StanKroku, nastepne
from runner.kotwice import PLIK_NOTATKI
from runner.krok import KATALOG_RUNU, PomiarKroku, WynikKroku, _zapisz_atomowo, wykonaj_krok
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
        materializuj({k.skill for k in workflow.kroki if k.skill is not None}, rejestr, wt.sciezka)
    except (BladDziedziny, ZlaKonfiguracjaZakresu, OSError) as e:
        raise BladStartu(str(e)) from e
    return Przygotowany(wt, workflow, rejestr, projekt, repo)


def _sciezka_decyzji(step_id: str) -> Path:
    """Sciezka artefaktu decyzji wzgledem worktree."""
    return KATALOG_ARTEFAKTOW / f"decyzja_{step_id}.json"


def _zdarzenie(run_id: str, kind: str, step_id: str, agent_id: str, text: str, **reszta) -> dict:
    return {
        "kind": kind, "run_id": run_id, "step_id": step_id, "agent_id": agent_id,
        "text": text, **reszta,
    }


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
) -> WynikKroku:
    """Jeden krok: specyfikacja agenta, prompt, polecenie; blad budowy to `PorazkaKroku`.

    Krok systemowy idzie do wykonawcy z rejestru, bez agenta. Krok zatwierdzony na bramce
    dostaje artefakt decyzji jako ostatnie wejscie."""
    krok = next(k for k in p.workflow.kroki if k.id == id_kroku)
    if krok.skill is None:
        return await _krok_systemowy(krok, p)
    skill = p.rejestr.skille[krok.skill]
    wejscia = [_wejscie(p, id_kroku, artefakt) for artefakt in skill.consumes]
    if id_kroku in zatwierdzone:
        wejscia.append(_sciezka_decyzji(id_kroku).as_posix())
    spec = SpecKroku(
        run_id=run_id, step_id=krok.id, agent_id=krok.id, skill_id=krok.skill,
        rola=skill.role, wejscia=wejscia, wyjscia=list(skill.produces),
        komendy={n: p.projekt.capabilities[n] for n in skill.needs if n in p.projekt.capabilities},
        budget_usd=krok.budget_usd, max_turns=krok.max_turns,
    )
    try:
        prompt = zbuduj_prompt(spec)
    except ValueError as e:
        raise PorazkaKroku(str(e)) from e
    try:
        argv = polecenie_agenta(spec, profil)
    except (BrakModelu, ValueError) as e:      # brak modelu tieru, zly budzet albo limit tur
        raise PorazkaKroku(str(e)) from e
    dodatkowe: dict[str, object] = {}
    if PLIK_NOTATKI in skill.produces:
        producent = _krok_producenta(p, id_kroku, PLIK_LINII)
        if producent is not None:
            dodatkowe["zmienione_linie"] = p.worktree.katalog_runu / producent / PLIK_LINII
    if skill.verdict is not None:
        dodatkowe["werdykt"] = skill.verdict
    return await uruchom_agenta(
        spec, argv, p.worktree.sciezka, emit, katalog_runu=p.worktree.katalog_runu,
        prompt=prompt, **dodatkowe)


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
    startowane: set[str], czeka: Oczekiwanie | None,
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


def _pola_pomiaru(pomiar: PomiarKroku) -> dict:
    """Pola pomiaru zdarzenia konca kroku; brak kosztu to `unavailable`, nigdy zero."""
    return {
        "cost_usd": pomiar.cost_usd,
        "cost_source": "measured" if pomiar.cost_usd is not None else "unavailable",
        "tokens_in": pomiar.tokens_in, "tokens_out": pomiar.tokens_out,
        "num_turns": pomiar.num_turns, "session_id": pomiar.session_id,
    }


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
    except BladProfiluBackendu as e:
        await _blad_przed_startem(emit, run_id, str(e))
        return
    dom = dom if dom is not None else Path.home()
    try:
        p = await asyncio.to_thread(_przygotuj, polecenie, dom, root, zakres, run_id)
    except BladStartu as e:
        await _blad_przed_startem(emit, run_id, str(e))
        return

    await _wykonaj_plan(
        run_id, polecenie["workflow"],     # nazwa sprawdzona w `_przygotuj`
        p, {k.id: StanKroku.OCZEKUJE for k in p.workflow.kroki}, set(), frozenset(),
        profil, uruchom_agenta, emit)


async def _wykonaj_plan(
    run_id: str, nazwa: str, p: Przygotowany, stany: dict[str, StanKroku],
    startowane: set[str], zatwierdzone: frozenset[str], profil: ProfilBackendu,
    uruchom_agenta: UruchomAgenta, emit: Emit,
) -> None:
    """Petla planu wspolna dla `wykonaj_run` i `wznow_run`: rusza od podanych stanow krokow.

    `zatwierdzone` to kroki z bramka, ktorych decyzja juz zapadla: nie zatrzymuja sie drugi
    raz, a ich agent dostaje artefakt decyzji wsrod wejsc."""
    capabilities = p.projekt.capabilities
    w_toku: dict[asyncio.Task, tuple[str, float]] = {}
    ostatni = "run"
    bramki = {k.id for k in p.workflow.kroki if k.approval and k.id not in zatwierdzone}
    oczekiwanie: Oczekiwanie | None = None     # najwyzej jedna zawieszona bramka naraz
    blad_zapisu: BladCheckpointu | None = None
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
                    _krok(run_id, id_kroku, p, profil, uruchom_agenta, emit, zatwierdzone))
                w_toku[zadanie] = (id_kroku, time.monotonic())
            if not w_toku:
                break
            gotowe, _ = await asyncio.wait(w_toku, return_when=asyncio.FIRST_COMPLETED)
            for zadanie in [z for z in w_toku if z in gotowe]:
                id_kroku, start = w_toku.pop(zadanie)
                dodatkowe: dict[str, object] = {}
                try:
                    wynik = zadanie.result()
                except PorazkaKroku as e:
                    udany, powod, pomiar = False, str(e), PomiarKroku()
                else:
                    udany, powod, pomiar = wynik.status == "zakonczony", wynik.powod, wynik.pomiar
                    if udany and _produkuje_notatke(p, id_kroku):
                        dodatkowe["kotwice"] = list(wynik.kotwice)
                    if wynik.dowod is not None:
                        dodatkowe["dowod"] = dict(wynik.dowod)
                    if wynik.werdykt is not None:
                        dodatkowe["verdict"] = wynik.werdykt
                czas_ms = int((time.monotonic() - start) * 1000)
                stany[id_kroku] = StanKroku.ZAKONCZONY if udany else StanKroku.NIEUDANY
                ostatni = id_kroku
                # nieudany krok ma powod (krok.py); text zakonczonego to pusty napis
                await emit(_zdarzenie(
                    run_id, "step_finished" if udany else "step_failed", id_kroku, id_kroku,
                    "" if udany else powod,
                    duration_ms=czas_ms, **_pola_pomiaru(pomiar), **dodatkowe))
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
) -> tuple[str, Przygotowany, dict[str, StanKroku], set[str], list[str]] | None:
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
    startowane = {i for i, k in cp.kroki.items() if k.proby > 0}
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
    dom = dom if dom is not None else Path.home()
    try:
        # przed decyzja: zly profil nie zjada oczekiwania, wiec po poprawce pliku da sie wznowic
        profil = profil if profil is not None else ProfilBackendu.z_konfiguracji()
        async with _BLOKADY.setdefault(run_id, asyncio.Lock()):
            start = await _przyjmij_decyzje(run_id, step_id, decyzja, powod, dom, root, emit)
    except (BladDziedziny, OSError) as e:      # BladCheckpointu, BladWznowienia, katalog runu
        await _porazka_wznowienia(emit, run_id, step_id, str(e))
        return
    if start is None:
        return
    nazwa, p, stany, startowane, przerwane = start
    for id_kroku in przerwane:
        await emit(_zdarzenie(run_id, "step_failed", id_kroku, id_kroku, TEKST_PRZERWANIA))
    await _wykonaj_plan(
        run_id, nazwa, p, stany, startowane, frozenset({step_id}), profil, uruchom_agenta,
        emit)
