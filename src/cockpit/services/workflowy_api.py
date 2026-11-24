"""Workflow projektu dla API (HAR-181): lista, walidacja i zapis plikow `.cockpit/workflows/*.yaml`.

Pliki czyta i zapisuje wylacznie runner PROJEKTU. Walidacje robi `waliduj_workflow` (bez osobnej
logiki w API): rejestr skilli pochodzi z tabeli `definicja` projektu, a katalog domowy
uzytkownika (`dom`, do ostrzezenia o skillu spoza repozytorium) z odpowiedzi runnera - serwer
nie zna dysku uzytkownika, wiec `Path.home()` serwera tu nie wchodzi.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Definicja, Project

from . import projekty, runs, skille_api, zapytania_runnera
from .errors import (
    BledyWalidacjiWorkflow,
    NiespojneDane,
    PlikPozaZakresem,
    RunnerOffline,
    StanNiemozliwy,
)
from .invariants import Rola
from .skills import NAZWA_PLIKU, Pominiety, Rejestr, Skill
from .workflow import Poziom, Problem, Workflow, wczytaj_workflow_z_tekstu
from .workflow_walidacja import waliduj_workflow
from .zapytania_runnera import Wyslij

WyslijPolecenie = Callable[[str, dict[str, Any]], Awaitable[None]]   # `bus.send_to_runner`
KATALOG_WORKFLOW = ".cockpit/workflows"
ROZSZERZENIE = ".yaml"
RUN_STARTUJE_ZE = "HEAD"   # run startuje z HEAD, nie z kopii roboczej
STAN_PLIKU = "kopia robocza"   # a walidowany jest plik z kopii roboczej


def _zly_ksztalt(powod: str) -> StanNiemozliwy:
    return StanNiemozliwy(f"odpowiedz runnera ma zly ksztalt ({powod})")


def _sprawdz_nazwe(nazwa: str) -> None:
    """Nazwa pliku workflow trafia do sciezki na dysku uzytkownika: wzorzec, nie czarna lista."""
    if not skille_api.WZORZEC_BEZPIECZNEJ_NAZWY.fullmatch(nazwa):
        raise NiespojneDane(
            "nazwa workflow musi byc nazwa pliku: litery, cyfry, _ - . (do 64 znakow, "
            "pierwszy znak litera lub cyfra)")


def _sciezka_workflow(nazwa: str) -> str:
    return f"{KATALOG_WORKFLOW}/{nazwa}{ROZSZERZENIE}"


async def _katalog_runnera(
    s: AsyncSession, tenant_id: str, projekt: Project, polaczone: frozenset[str], wyslij: Wyslij,
) -> tuple[str, dict[str, Any] | None]:
    """(dom, wpis projektu) z odpowiedzi `katalog_definicji` runnera PROJEKTU. Wpis jest None,
    gdy runner nie ma tego projektu w swoim zakresie. Zly ksztalt to StanNiemozliwy."""
    _, wynik = await zapytania_runnera.zapytaj_runnera_online(
        s, tenant_id, "katalog_definicji", {}, polaczone, wyslij, runner_id=projekt.runner_id)
    if not isinstance(wynik, dict):
        raise _zly_ksztalt("wynik nie jest slownikiem")
    dom = wynik.get("dom")
    if not isinstance(dom, str) or not dom:
        raise _zly_ksztalt("pole dom nie jest niepustym napisem")
    wpisy = wynik.get("projekty")
    if not isinstance(wpisy, list) or not all(isinstance(w, dict) for w in wpisy):
        raise _zly_ksztalt("pole projekty nie jest lista slownikow")
    wpis = next((w for w in wpisy if w.get("projekt") == projekt.path), None)
    return dom, wpis


def _katalog_skilla(wiersz: Definicja, projekt: Project, dom: str) -> Path:
    """Katalog skilla z jego sciezki w cache (`~/...` to dom runnera, reszta repo projektu);
    po nim walidator odroznia skill spoza repozytorium."""
    sciezka = wiersz.sciezka
    if sciezka.startswith("~/"):
        return (Path(dom) / sciezka.removeprefix("~/")).parent
    return (Path(projekt.path) / sciezka).parent


def _rejestr(wiersze: list[Definicja], projekt: Project, dom: str) -> Rejestr:
    """Rejestr skilli z cache katalogu projektu. Skill z problemem albo bez roli jest
    `pominiety` (walidator zglasza go jako skill pominiety przy wczytaniu), nie poprawny."""
    rejestr = Rejestr()
    for w in wiersze:
        katalog = _katalog_skilla(w, projekt, dom)
        rola = _rola(w.rola)
        if w.problem is not None or rola is None:
            powod = w.problem if w.problem is not None else "brak poprawnej roli w katalogu"
            rejestr.pominiete.append(Pominiety(plik=katalog / NAZWA_PLIKU, powod=powod))
            continue
        rejestr.skille[w.klucz] = Skill(
            id=w.klucz, name=w.nazwa, role=rola, consumes=list(w.consumes or []),
            produces=list(w.produces or []), brief="", katalog=katalog)
    return rejestr


def _rola(wartosc: str | None) -> Rola | None:
    try:
        return Rola(wartosc)
    except ValueError:   # brak (None) albo wartosc spoza listy: wiersz nie jest poprawnym skillem
        return None


def _problemy_pliku(
    tekst: str, rejestr: Rejestr, dom: str,
) -> tuple[Workflow | None, list[Problem]]:
    """Problemy z wczytania, potem wynik `waliduj_workflow`; plik nie do wczytania ma tylko te
    pierwsze (jeden problem calego pliku)."""
    workflow, problemy = wczytaj_workflow_z_tekstu(tekst)
    if workflow is None:
        return None, problemy
    return workflow, problemy + waliduj_workflow(workflow, rejestr, Path(dom))


def _odpowiedz(problemy: list[Problem]) -> dict:
    return {
        "problemy": [
            {"poziom": p.poziom.value, "krok": p.krok, "komunikat": p.komunikat}
            for p in problemy],
        "run_startuje_ze": RUN_STARTUJE_ZE,
        "stan": STAN_PLIKU,
    }


def _liczba_bledow(problemy: list[Problem]) -> int:
    return sum(1 for p in problemy if p.poziom == Poziom.blad)


async def lista_workflow(
    s: AsyncSession, tenant_id: str, project_id: str, polaczone: frozenset[str], wyslij: Wyslij,
) -> list[dict]:
    """Workflow projektu posortowane po nazwie; kazdy plik czytany biezaco przez runnera
    i walidowany tym samym kodem co `/validate`. Projekt poza zakresem runnera to
    PlikPozaZakresem (brak dowodu, ze lista jest pelna, nie jest pusta lista)."""
    projekt = await projekty.projekt_zespolu(s, tenant_id, project_id)
    dom, wpis = await _katalog_runnera(s, tenant_id, projekt, polaczone, wyslij)
    if wpis is None:
        raise PlikPozaZakresem("sciezka poza zakresem runnera")
    sciezki = wpis.get("workflow")
    if not isinstance(sciezki, list) or not all(
            isinstance(w, dict) and isinstance(w.get("sciezka"), str) for w in sciezki):
        raise _zly_ksztalt("pole workflow nie jest lista sciezek")
    nazwy = sorted({PurePosixPath(w["sciezka"]).name.removesuffix(ROZSZERZENIE) for w in sciezki})
    rejestr = _rejestr(await skille_api.wiersze_skilli(s, projekt), projekt, dom)
    lista = []
    for nazwa in nazwy:
        tekst = await projekty.czytaj_plik_projektu(
            s, tenant_id, projekt, _sciezka_workflow(nazwa), polaczone, wyslij)
        workflow, problemy = _problemy_pliku(tekst, rejestr, dom)
        liczba_krokow = 0 if workflow is None else len(workflow.kroki) + len(workflow.niepoprawne)
        lista.append({
            "name": nazwa, "liczba_krokow": liczba_krokow,
            "liczba_bledow": _liczba_bledow(problemy)})
    return lista


async def waliduj(
    s: AsyncSession, tenant_id: str, project_id: str, nazwa: str,
    polaczone: frozenset[str], wyslij: Wyslij,
) -> dict:
    """Walidacja biezacego pliku workflow z dysku (kopia robocza); nic nie zapisuje."""
    _sprawdz_nazwe(nazwa)
    projekt = await projekty.projekt_zespolu(s, tenant_id, project_id)
    tekst = await projekty.czytaj_plik_projektu(
        s, tenant_id, projekt, _sciezka_workflow(nazwa), polaczone, wyslij)
    dom, _ = await _katalog_runnera(s, tenant_id, projekt, polaczone, wyslij)
    rejestr = _rejestr(await skille_api.wiersze_skilli(s, projekt), projekt, dom)
    _, problemy = _problemy_pliku(tekst, rejestr, dom)
    return _odpowiedz(problemy)


async def uruchom_workflow(
    s: AsyncSession, tenant_id: str, project_id: str, nazwa: str,
    polaczone: frozenset[str], wyslij: Wyslij, wyslij_polecenie: WyslijPolecenie,
) -> str:
    """Start runu workflow projektu po walidacji (HAR-182); zwraca id runu.

    Waliduje BIEZACY plik z kopii roboczej (ten sam kod co `waliduj`, wiec ta sama lista
    problemow); jeden problem poziomu `blad` to BledyWalidacjiWorkflow z cala lista i run nie
    powstaje. Same ostrzezenia nie blokuja. Runner projektu offline to RunnerOffline z podpowiedzia
    startu runnera. Silnik startuje run z HEAD, wiec walidacja kopii roboczej nie dowodzi, ze
    HEAD jest poprawny (`run_startuje_ze` w odpowiedzi walidacji mowi to samo)."""
    _sprawdz_nazwe(nazwa)
    projekt = await projekty.projekt_zespolu(s, tenant_id, project_id)
    try:
        tekst = await projekty.czytaj_plik_projektu(
            s, tenant_id, projekt, _sciezka_workflow(nazwa), polaczone, wyslij)
        dom, _ = await _katalog_runnera(s, tenant_id, projekt, polaczone, wyslij)
    except RunnerOffline:
        raise RunnerOffline(projekty.KOMUNIKAT_OFFLINE) from None
    rejestr = _rejestr(await skille_api.wiersze_skilli(s, projekt), projekt, dom)
    _, problemy = _problemy_pliku(tekst, rejestr, dom)
    if _liczba_bledow(problemy) > 0:
        raise BledyWalidacjiWorkflow(_odpowiedz(problemy)["problemy"])
    run = await runs.utworz_run(s, tenant_id, nazwa, polaczone, project_id=project_id)
    await wyslij_polecenie(run.runner_id, {
        "type": "run_workflow", "run_id": run.id, "workflow": nazwa, "sciezka": projekt.path})
    return run.id


async def zapisz(
    s: AsyncSession, tenant_id: str, project_id: str, nazwa: str, tresc: str,
    polaczone: frozenset[str], wyslij: Wyslij,
) -> dict:
    """Zapisuje tresc workflow bajt w bajt (komentarze, kolejnosc kluczy i cudzyslowy zostaja)
    i odpowiada walidacja zapisanego pliku. Plik, ktorego nie da sie wczytac, to NiespojneDane
    z komunikatem pierwszego problemu i brak zapisu; bledy walidatora nie blokuja zapisu."""
    _sprawdz_nazwe(nazwa)
    workflow, wczytanie = wczytaj_workflow_z_tekstu(tresc)
    if workflow is None:
        raise NiespojneDane(wczytanie[0].komunikat)
    projekt = await projekty.projekt_zespolu(s, tenant_id, project_id)
    # dom i rejestr przed zapisem: blad runnera po zapisie udawalby, ze zapis sie nie udal
    dom, _ = await _katalog_runnera(s, tenant_id, projekt, polaczone, wyslij)
    rejestr = _rejestr(await skille_api.wiersze_skilli(s, projekt), projekt, dom)
    await projekty.zapisz_plik_projektu(
        s, tenant_id, projekt, _sciezka_workflow(nazwa), tresc, polaczone, wyslij)
    return _odpowiedz(wczytanie + waliduj_workflow(workflow, rejestr, Path(dom)))
