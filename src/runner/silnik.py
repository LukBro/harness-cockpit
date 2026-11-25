"""Silnik runu u runnera: wykonanie workflow wedlug grafu `needs` (HAR-80, D11).

Petla wykonania zyje w runnerze, a kokpit wylicza stan runu ze zdarzen. Silnik waliduje
workflow, tworzy worktree, materializuje skille i uruchamia kroki rownolegle tam, gdzie
pozwala `needs`. Krok z `approval: true` zawiesza run na bramce BEZ procesu agenta
(HAR-172): silnik zapisuje checkpoint z `waiting_on` w chwili trafienia na bramke (bez
zdarzenia), kroki niezalezne od bramki dochodza do konca, potem drugi zapis, pauza testowa
i `step_waiting` jako ostatnie zdarzenie runu. Wszystkie odczyty (rejestr skilli,
workflow, project.yaml) ida z worktree (stan z commita), nie z repozytorium uzytkownika.
Silnik nie sprzata worktree: zostaje do odbioru. Konsensus i foreach to osobne tickety (fan-out zrobiony).

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
anulowanie (`CancelledError`, po anulowaniu zadan krokow; `step_aborted` zglasza klient po
`cancel_run`, a `zamknij_checkpoint` zamyka checkpoint z bramka) i bledy samego `emit`
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
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cockpit.services.errors import BladDziedziny
from cockpit.services.kroki_systemowe import (
    CAPABILITY_MUTATE_DIFF,
    PLIK_LINII,
    SYSTEM_CHECK,
    opis_kroku,
)
from cockpit.services.invariants import profil_roli
from cockpit.services.projekt import Projekt, wczytaj_projekt
from cockpit.services.skills import Rejestr, materializuj, rozstrzygnij_rejestr
from cockpit.services.workflow import Krok, Problem, Workflow, ma_bledy, wczytaj_workflow
from cockpit.services.workflow_walidacja import waliduj_workflow
from runner.agent import (
    ProfilBackendu,
    SpecKroku,
    etykieta_pliku,
    id_wariantu,
    podkatalog_wariantu,
    polecenie_agenta,
    sciezka_wyjscia_wariantu,
    zbuduj_prompt,
)
from runner.bufor import BladBufora
from runner.checkpoint import (
    BladCheckpointu,
    ZMIENNA_PAUZY,
    Checkpoint,
    Oczekiwanie,
    StanKrokuCheckpointu,
    wczytaj_checkpoint,
    zapisz_checkpoint,
)
from runner.dowody import wykonaj_check, wykonaj_mutate_diff
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
from runner.kontekst import Manifest, zloz_manifest
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


def _powod_wyjatku(id_kroku: str, e: Exception) -> str:
    """Powod porazki kroku (albo wariantu) z wyjatku zadania; `BladBufora` obsluguje wolajacy.

    `PorazkaKroku` i bledy granic niosa swoj tekst. Kazdy inny wyjatek (kod runnera, biblioteka)
    to porazka kroku, nie petli planu: inaczej run wisi w `running` bez `run_finished`. Jego powod
    niesie sama nazwe typu, bo tresc wyjatku moze niesc sekret."""
    if isinstance(e, PorazkaKroku):
        return str(e)
    if isinstance(e, BLEDY_GRANICY):
        print(f"krok {id_kroku[:36]!r} zakonczony bledem: {type(e).__name__}", file=sys.stderr)
        return _tekst_bledu(e)
    nazwa_typu = type(e).__name__
    print(f"krok {id_kroku[:36]!r} zakonczony bledem: {nazwa_typu}", file=sys.stderr)
    return f"nieoczekiwany wyjątek kroku: {nazwa_typu}"


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
    if capability == CAPABILITY_MUTATE_DIFF:
        # zmienione linie z kroku `diff` (walidator wymaga go wsrod przodkow przez `consumes`)
        producent = _krok_producenta(p, krok.id, PLIK_LINII)
        if producent is None:
            raise PorazkaKroku(f"brak kroku diff dla capability {capability}")
        return await wykonaj_mutate_diff(
            p.worktree.sciezka, komenda, p.worktree.katalog_runu / krok.id,
            p.worktree.katalog_runu / producent / PLIK_LINII,
            fail_on_survivors=krok.fail_on_survivors)
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


def _przodkowie(p: Przygotowany, id_kroku: str) -> Iterator[Krok]:
    """Przodkowie kroku po `needs`, wszerz: od najblizszych."""
    kroki = {k.id: k for k in p.workflow.kroki}
    stos = list(kroki[id_kroku].needs)
    odwiedzeni: set[str] = set()
    while stos:
        kandydat = stos.pop(0)
        if kandydat in odwiedzeni or kandydat not in kroki:
            continue
        odwiedzeni.add(kandydat)
        krok = kroki[kandydat]
        yield krok
        stos.extend(krok.needs)


def _krok_producenta(p: Przygotowany, id_kroku: str, artefakt: str) -> str | None:
    """Id kroku systemowego, ktory produkuje `artefakt` i jest przodkiem `id_kroku` (albo None).

    Producent skillowy nie jest tu potrzebny: jego artefakty to nazwy wzgledne, a krok
    systemowy zapisuje pod `<katalog_runu>/<id kroku>/`."""
    for krok in _przodkowie(p, id_kroku):
        if krok.skill is None and artefakt in opis_kroku(krok).produces:
            return krok.id
    return None


def _agenci_kroku(krok: Krok) -> list[str]:
    """Identyfikatory agentow kroku: id kroku (fanout 1, krok systemowy) albo `a1..aN` (HAR-83)."""
    if krok.skill is None or krok.fanout == 1:
        return [krok.id]
    return [id_wariantu(i) for i in range(1, krok.fanout + 1)]


def _wejscia_wariantow(p: Przygotowany, krok: Krok, artefakt: str) -> list[str]:
    """Sciezki `artefakt` wszystkich wariantow producenta z fanoutem: rola z zapisem pisze
    w worktree (sciezka wzgledna), rola bez zapisu w katalogu runu (sciezka bezwzgledna)."""
    assert krok.skill is not None
    zapis = profil_roli(p.rejestr.skille[krok.skill].role).zapis
    sciezki = []
    for agent_id in _agenci_kroku(krok):
        if zapis:
            sciezki.append(sciezka_wyjscia_wariantu(krok.id, agent_id, artefakt))
        else:
            sciezki.append(str(
                p.worktree.katalog_runu / krok.id / podkatalog_wariantu(agent_id) / artefakt))
    return sciezki


def _wejscie(p: Przygotowany, id_kroku: str, artefakt: str) -> list[str]:
    """Wejscia agenta dla artefaktu: sciezka bezwzgledna z kroku systemowego, sciezki wszystkich
    wariantow z producenta z `fanout > 1` (najblizszy przodek wygrywa), inaczej sama nazwa."""
    for krok in _przodkowie(p, id_kroku):
        if krok.skill is None:
            if artefakt in opis_kroku(krok).produces:
                return [str(p.worktree.katalog_runu / krok.id / artefakt)]
        elif artefakt in p.rejestr.skille[krok.skill].produces:
            # najblizszy producent konczy szukanie; bez fanoutu wejscie to sama nazwa
            return _wejscia_wariantow(p, krok, artefakt) if krok.fanout > 1 else [artefakt]
    return [artefakt]


async def _krok_systemowy(krok: Krok, p: Przygotowany) -> WynikKroku:
    """Krok bez agenta: wykonawca z rejestru, artefakty w katalogu runu pod id kroku."""
    wykonawca = WYKONAWCY_SYSTEMOWI.get(krok.system)
    if wykonawca is None:
        raise PorazkaKroku(f"nieznany krok systemowy: {krok.system}")
    return await wykonawca(krok, p)


async def _zloz_i_zapisz_manifest(
    p: Przygotowany, krok: Krok, sciezki_planu: Sequence[str], wariant: str | None = None,
) -> Manifest:
    """Manifest kroku ze skillem w `<katalog_runu>/manifest-<id>.json`; blad konczy krok.

    Wariant kroku z fanoutem (`wariant` = agent_id) ma wlasny plik `manifest-<id>-<agent_id>.json`."""
    assert krok.skill is not None
    if (blad := _blad_identyfikatora(krok.id)) is not None:
        raise PorazkaKroku(blad)
    try:
        etykieta = etykieta_pliku(krok.id, wariant or krok.id, wariant is not None)
    except ValueError as e:
        raise PorazkaKroku(str(e)) from e
    # BladManifestu (BladDziedziny) konczy krok w petli zadan (`except BLEDY_GRANICY`) z tym samym
    # tekstem - osobny except bylby martwy
    manifest = await asyncio.to_thread(
        zloz_manifest, p.worktree.sciezka, krok.id, sciezki_planu, skill_id=krok.skill)
    # mutant ensure_ascii=None jest rownowazny (json czyta go jako bool); wariant True chroni reczny mutant HAR-169
    tresc = json.dumps(manifest.jako_listy(), ensure_ascii=False).encode()  # pragma: no mutate
    try:
        await asyncio.to_thread(
            _zapisz_atomowo, p.worktree.katalog_runu / f"manifest-{etykieta}.json", tresc)
    except OSError as e:
        raise PorazkaKroku(f"nie można zapisać manifestu kroku {krok.id}: {e}") from e
    return manifest


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
    if krok.fanout > 1 and PLIK_NOTATKI in skill.produces:
        # fail-closed: kotwice notatki sprawdza `wykonaj_krok` po nazwie artefaktu, a wariant ma
        # sciezke w podkatalogu; bez tego krok przeszedlby bez sprawdzenia (evidence_required)
        raise PorazkaKroku("fanout z notatką zmiany nieobsługiwany")
    wejscia = [w for artefakt in skill.consumes for w in _wejscie(p, id_kroku, artefakt)]
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
    agenci = _agenci_kroku(krok)
    if len(agenci) == 1:
        return await _wariant(
            run_id, krok, skill, krok.id, False, wejscia, p, profil, uruchom_agenta, emit,
            proba, ogon)
    # fanout (HAR-83): warianty biegna rownolegle i wszystkie dobiegaja konca, takze po porazce
    # jednego; wyjatek wariantu to jego porazka, poza `BladBufora` (zepsuty zapis zdarzen)
    wyniki = await asyncio.gather(*(
        _wariant(run_id, krok, skill, agent_id, True, wejscia, p, profil, uruchom_agenta, emit,
                 proba, ogon)
        for agent_id in agenci), return_exceptions=True)
    return _zbierz_warianty(krok.id, agenci, wyniki)


def _zbierz_warianty(
    id_kroku: str, agenci: Sequence[str], wyniki: Sequence[WynikKroku | BaseException],
) -> WynikKroku:
    """Wynik zbiorczy kroku z wynikow wariantow w kolejnosci `agenci`.

    Krok zdany tylko, gdy zdane sa wszystkie warianty; inaczej niesie powod i dowody pierwszego
    nieudanego, z jego agent_id w powodzie. Pomiar zbiorczy jest pusty: kazdy wariant ma swoj
    pomiar w `warianty` (suma z brakami byla by zmierzona tylko z pozoru, measured_or_null)."""
    zebrane: list[tuple[str, WynikKroku]] = []
    for agent_id, wynik in zip(agenci, wyniki):
        if isinstance(wynik, BladBufora) or not isinstance(wynik, Exception | WynikKroku):
            raise wynik         # zapis zdarzen albo anulowanie: decyduje wolajacy
        if isinstance(wynik, Exception):
            wynik = WynikKroku(
                status="nieudany", powod=_powod_wyjatku(id_kroku, wynik), kod_wyjscia=None)
        zebrane.append((agent_id, wynik))
    pierwszy = next(((a, w) for a, w in zebrane if w.status != "zakonczony"), None)
    if pierwszy is None:
        return WynikKroku(
            status="zakonczony", powod=None, kod_wyjscia=zebrane[0][1].kod_wyjscia,
            warianty=tuple(zebrane))
    agent_id, nieudany = pierwszy
    return WynikKroku(
        status="nieudany", powod=f"wariant {agent_id}: {nieudany.powod}",
        kod_wyjscia=nieudany.kod_wyjscia, werdykt=nieudany.werdykt, dowod=nieudany.dowod,
        warianty=tuple(zebrane))


def _wyjscia_agenta(krok: Krok, skill: Any, agent_id: str, wariant: bool) -> list[str]:
    """`produces` skilla; wariant roli z zapisem pisze w swoim podkatalogu worktree, a rola bez
    zapisu zostaje przy nazwie (runner zapisze ja w `<katalog_runu>/<krok>/agent_<id>/`)."""
    if not wariant or not profil_roli(skill.role).zapis:
        return list(skill.produces)
    return [sciezka_wyjscia_wariantu(krok.id, agent_id, n) for n in skill.produces]


async def _wariant(
    run_id: str, krok: Krok, skill: Any, agent_id: str, wariant: bool, wejscia: list[str],
    p: Przygotowany, profil: ProfilBackendu, uruchom_agenta: UruchomAgenta, emit: Emit,
    proba: int, ogon: deque[str] | None,
) -> WynikKroku:
    """Jeden agent kroku (caly krok bez fanoutu albo jeden wariant): manifest, spec, prompt,
    polecenie i uruchomienie. Wyjatek wariantu zostaje wyjatkiem, tlumaczy go `_zbierz_warianty`."""
    assert krok.skill is not None
    manifest = await _zloz_i_zapisz_manifest(
        p, krok, skill.consumes + skill.produces, agent_id if wariant else None)
    spec = SpecKroku(
        run_id=run_id, step_id=krok.id, agent_id=agent_id, skill_id=krok.skill,
        rola=skill.role, wejscia=wejscia, wyjscia=_wyjscia_agenta(krok, skill, agent_id, wariant),
        komendy={n: p.projekt.capabilities[n] for n in skill.needs if n in p.projekt.capabilities},
        budget_usd=krok.budget_usd, max_turns=krok.max_turns,
        # lekcje wchodza do manifestu raz na wpis, a do promptu raz jako plik
        kontekst=tuple(dict.fromkeys(w.sciezka for w in manifest.wpisy if w.sciezka is not None)),
        wariant=wariant,
    )
    id_kroku = krok.id
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

    await emit(_zdarzenie(
        run_id, "context_manifest", krok.id, agent_id, "", attempt=proba,
        manifest=manifest.jako_listy()))

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


def _dodatkowe_wyniku(
    p: Przygotowany, id_kroku: str, wynik: WynikKroku, udany: bool,
) -> dict[str, object]:
    """Pola zdarzenia konca kroku (albo wariantu) z jego wyniku: kotwice, dowod, werdykt, artefakty."""
    dodatkowe: dict[str, object] = {}
    if udany and _produkuje_notatke(p, id_kroku):
        dodatkowe["kotwice"] = list(wynik.kotwice)
    if wynik.dowod is not None:
        dodatkowe["dowod"] = dict(wynik.dowod)
    if wynik.werdykt is not None:
        dodatkowe["verdict"] = wynik.werdykt
    artefakty = _artefakty_kroku(p, id_kroku)
    if udany and artefakty:
        dodatkowe["artefakty"] = artefakty
    return dodatkowe


def _agent_zdarzenia(id_kroku: str, wynik: WynikKroku | None, udany: bool) -> str:
    """agent_id zdarzenia konca kroku: pierwszy nieudany wariant fanoutu, inaczej id kroku."""
    if wynik is not None and not udany:
        for agent_id, wynik_wariantu in wynik.warianty:
            if wynik_wariantu.status != "zakonczony":
                return agent_id
    return id_kroku


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


async def _zapis_do_konca(katalog: Path, cp: Checkpoint) -> None:
    """Zapis checkpointu w watku, ktorego NIE przerywa anulowanie zadania: watek nie daje sie
    zatrzymac, wiec po `CancelledError` czekamy na jego koniec i dopiero wtedy anulowanie
    opuszcza zadanie (re-raise). Wolac pod `_BLOKADY` runu: blokada trzyma sie do konca zapisu,
    wiec zamkniecie checkpointu po `cancel_run` nigdy nie wyprzedzi spoznionego `os.replace`."""
    zapis = asyncio.ensure_future(asyncio.to_thread(zapisz_checkpoint, katalog, cp))
    try:
        await asyncio.shield(zapis)
    except asyncio.CancelledError:
        while not zapis.done():
            try:
                await asyncio.wait([zapis])
            except asyncio.CancelledError:
                pass            # kolejne anulowanie tez czeka na watek; re-raise nizej
        if not zapis.cancelled():
            zapis.exception()   # blad zapisu przy anulowaniu: odebrany, bez ostrzezenia asyncio
        raise


async def _zapisz(p: Przygotowany, cp: Checkpoint) -> BladCheckpointu | None:
    """Zapis checkpointu pod blokada runu; blad dysku wraca jako wartosc (silnik konczy run
    bledem). Nie jest wolany pod `_BLOKADY` (`_wykonaj_plan` dziala po `_przyjmij_decyzje`)."""
    try:
        async with _BLOKADY.setdefault(cp.run_id, asyncio.Lock()):
            await _zapis_do_konca(p.worktree.katalog_runu, cp)
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
        # rownolegly recenzent ma migawke worktree; ten plik to zapis silnika, a nie jego
        # (wzorzec RAPORTY_NAPRAWY_SILNIKA w krok.py musi obejmowac _sciezka_raportu)
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
                for agent_id in _agenci_kroku(krok_po_id[id_kroku]):
                    await emit(_zdarzenie(
                        run_id, "step_started", id_kroku, agent_id, "",
                        attempt=startowane[id_kroku]))
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
                except BladBufora:
                    # zapis zdarzenia sie nie udal: o tym decyduje wolajacy (przerwanie poza
                    # buforem); `step_failed` tez poszlo by przez zepsuty bufor.
                    # `CancelledError` jest `BaseException`, wiec ponizszy `except Exception` go nie lapie
                    raise
                except Exception as e:  # noqa: BLE001
                    # WHY szeroki: granica zadania asynchronicznego - kazdy wyjatek zadania kroku
                    # (PorazkaKroku, blad granicy, kod runnera, biblioteka) konczy krok, a nie petle
                    # planu; inaczej run wisi w `running` bez `step_failed` i `run_finished`
                    # (evidence_required). Powod sklada `_powod_wyjatku`.
                    udany, powod, pomiar = False, _powod_wyjatku(id_kroku, e), PomiarKroku()
                else:
                    udany, powod, pomiar = wynik.status == "zakonczony", wynik.powod, wynik.pomiar
                    dodatkowe = _dodatkowe_wyniku(p, id_kroku, wynik, udany)
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
                if wynik is not None and wynik.warianty:
                    # fanout: zdarzenie konca osobno dla kazdego wariantu (kokpit sklada kroki po
                    # (step_id, agent_id)), z jego pomiarem; udane przed nieudanymi, a powod kroku
                    # niesie pierwszy nieudany wariant
                    pierwszy = _agent_zdarzenia(id_kroku, wynik, udany)
                    kolejnosc = sorted(wynik.warianty, key=lambda a: a[1].status != "zakonczony")
                    for agent_id, wynik_wariantu in kolejnosc:
                        zdany = wynik_wariantu.status == "zakonczony"
                        tekst = "" if zdany else (
                            powod if agent_id == pierwszy
                            else f"wariant {agent_id}: {wynik_wariantu.powod}")
                        await emit(_zdarzenie(
                            run_id, "step_finished" if zdany else "step_failed", id_kroku,
                            agent_id, tekst, attempt=startowane[id_kroku], duration_ms=czas_ms,
                            **_pola_pomiaru(wynik_wariantu.pomiar),
                            **_dodatkowe_wyniku(p, id_kroku, wynik_wariantu, zdany)))
                else:
                    # wyjatek zadania (wynik None) konczy kazdy wiersz (step_id, agent_id), ktory
                    # dostal `step_started`; wynik bez wariantow ma jednego agenta
                    agenci_konca = (
                        _agenci_kroku(krok_po_id[id_kroku]) if wynik is None
                        else [_agent_zdarzenia(id_kroku, wynik, udany)])
                    for agent_id in agenci_konca:
                        await emit(_zdarzenie(
                            run_id, "step_finished" if udany else "step_failed", id_kroku,
                            agent_id, "" if udany else powod, attempt=startowane[id_kroku],
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
            for agent_id in _agenci_kroku(krok_po_id[oczekiwanie.step_id]):
                await emit(_zdarzenie(
                    run_id, "step_waiting", oczekiwanie.step_id, agent_id, TEKST_CZEKANIA,
                    attempt=startowane.get(oczekiwanie.step_id, 0) + 1))
            return
        for agent_id in _agenci_kroku(krok_po_id[oczekiwanie.step_id]):
            await emit(_zdarzenie(
                run_id, "step_failed", oczekiwanie.step_id, agent_id,
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


async def zamknij_checkpoint(run_id: str, root: Path | None = None) -> bool:
    """Zamyka checkpoint anulowanego runu: zapis bez `waiting_on` (HAR-126), pod ta sama
    blokada runu co `_przyjmij_decyzje` i `_zapisz`, w watku jak `zapisz_checkpoint`. Po tym
    `wznow_run` (takze po restarcie runnera) ignoruje `resume` z `_powod_ignorowania`.

    `True` gdy zapisano zamkniecie; `False` gdy nie bylo czego zamykac: brak checkpointu (run
    nieznany, zakonczony albo bez bramki) albo juz bez `waiting_on`. Inny blad odczytu albo
    zapisu to `BladCheckpointu`; `ValueError` odczytu (bajt NUL w `run_id`) tlumaczymy na
    `BladCheckpointu` (po nim zapis juz nie ruszy), zly identyfikator runu to `BladWorktree`."""
    katalog = katalog_runu(run_id, root)
    async with _BLOKADY.setdefault(run_id, asyncio.Lock()):
        try:
            cp = await asyncio.to_thread(wczytaj_checkpoint, katalog)
        except BladCheckpointu as e:
            if isinstance(e.__cause__, FileNotFoundError):
                return False
            raise
        except ValueError as e:     # bajt NUL w sciezce: `read_text` nie rzuca bledem dziedziny
            raise BladCheckpointu(f"odczyt checkpointu: {type(e).__name__}") from e
        if cp.waiting_on is None:
            return False
        await _zapis_do_konca(katalog, replace(cp, waiting_on=None))
    return True


def _agenci_bramki(katalog: Path, step_id: str) -> tuple[list[str], int | None]:
    """(agenci, numer proby) wierszy otwartych `step_waiting` bramki, z dysku runu.

    Agenci z kopii `workflow.yaml`, proba z checkpointu (`proby` + 1, jak w `step_waiting`).
    Czego nie da sie odczytac (brak checkpointu, kopii workflow, kroku), zostaje przy `step_id`
    i bez proby: tak jak przed fanoutem."""
    try:
        cp = wczytaj_checkpoint(katalog)
        workflow, _ = wczytaj_workflow(katalog / "workflow.yaml")
    except BLEDY_GRANICY:       # to samo wejscie moglo juz zawiesc w `_przyjmij_decyzje`
        return [step_id], None
    krok = next((k for k in workflow.kroki if k.id == step_id), None) if workflow else None
    if krok is None or step_id not in cp.kroki:
        return [step_id], None
    try:
        return _agenci_kroku(krok), cp.kroki[step_id].proby + 1
    except BLEDY_GRANICY:
        return [step_id], None


async def _agenci_bramki_z_dysku(
    run_id: str, step_id: str, root: Path | None,
) -> tuple[list[str], int | None]:
    """`_agenci_bramki` dla bledu wznowienia przed zbudowaniem `Przygotowany`."""
    try:
        katalog = katalog_runu(run_id, root)
    except BLEDY_GRANICY:       # zly identyfikator runu albo katalog: nie ma czego odczytac
        return [step_id], None
    return await asyncio.to_thread(_agenci_bramki, katalog, step_id)


async def _porazka_wznowienia(
    emit: Emit, run_id: str, step_id: str, tekst: str,
    agenci: Sequence[str] | None = None, proba: int | None = None,
) -> None:
    """`step_failed` kroku z polecenia i `run_finished`: bez pierwszego kokpit wyliczylby
    z samego `run_finished` falszywe "do odbioru" (evidence_required).

    `step_failed` idzie do kazdego wiersza otwartego przez `step_waiting` (agenci bramki z
    fanoutem) z ta sama proba; bez `agenci` jest jeden wiersz `step_id`. Krok bez fanoutu
    (agenci == [step_id]) dostaje zdarzenie jak dotad, bez `attempt`."""
    agenci = [step_id] if agenci is None else list(agenci)
    proba_zdarzenia = proba if agenci != [step_id] else None
    for agent_id in agenci:
        await emit(_zdarzenie(
            run_id, "step_failed", step_id, agent_id, tekst, attempt=proba_zdarzenia))
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
        await _zapis_do_konca(katalog, replace(cp, waiting_on=None))
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
    await _zapis_do_konca(katalog, _checkpoint(run_id, cp.workflow, p, stany, startowane, None))
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
        agenci, proba = await _agenci_bramki_z_dysku(run_id, step_id, root)
        await _porazka_wznowienia(emit, run_id, step_id, _tekst_bledu(e), agenci, proba)
        return
    if start is None:
        return
    nazwa, p, stany, startowane, przerwane = start
    krok_po_id = {k.id: k for k in p.workflow.kroki}
    # wiersze bramki otwarte przez `step_waiting` (numer proby sprzed startu kroku po decyzji)
    try:
        agenci_bramki = _agenci_kroku(krok_po_id[step_id]) if step_id in krok_po_id else [step_id]
        agenci_przerwanych = {i: _agenci_kroku(krok_po_id[i]) for i in przerwane}
    except BLEDY_GRANICY as e:
        # blad liczenia agentow konczy run (`step_failed` + `run_finished`), nie idzie dalej;
        # `[step_id]` to tylko zestaw wierszy dla zdarzen zamykajacych, gdy agentow nie da sie wyliczyc
        agenci, proba = await _agenci_bramki_z_dysku(run_id, step_id, root)
        await _porazka_wznowienia(emit, run_id, step_id, _tekst_bledu(e), agenci, proba)
        return
    proba_bramki = startowane.get(step_id, 0) + 1
    for id_kroku in przerwane:
        for agent_id in agenci_przerwanych[id_kroku]:
            await emit(_zdarzenie(
                run_id, "step_failed", id_kroku, agent_id, TEKST_PRZERWANIA,
                attempt=startowane[id_kroku]))
    try:
        await _wykonaj_plan(
            run_id, nazwa, p, stany, startowane, frozenset({step_id}), profil, uruchom_agenta,
            emit)
    except BLEDY_GRANICY as e:
        await _porazka_wznowienia(
            emit, run_id, step_id, _tekst_bledu(e), agenci_bramki, proba_bramki)
