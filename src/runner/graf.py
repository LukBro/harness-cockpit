"""Plan wykonania grafu krokow: needs, pominiecia, porazka galezi (HAR-43).

Czysta funkcja, bez I/O: z workflow, rejestru, capabilities runnera i stanow krokow
mowi, ktore kroki wystartowac, ktore pominac (i dlaczego) oraz czy run jest skonczony.
Pominiecie propaguje sie w jednym wywolaniu do punktu stalego.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum

from cockpit.services.kroki_systemowe import opis_kroku_systemowego
from cockpit.services.skills import Rejestr
from cockpit.services.workflow import Krok, Workflow


class StanKroku(StrEnum):
    OCZEKUJE = "oczekuje"
    W_TOKU = "w_toku"
    ZAKONCZONY = "zakonczony"
    NIEUDANY = "nieudany"
    POMINIETY = "pominiety"


class PowodPominiecia(StrEnum):
    """Zamknieta lista D4; `PUSTY_FOREACH` zarezerwowany dla foreach, graf go nie zwraca."""

    BRAK_CAPABILITY = "brak_capability"
    PUSTY_FOREACH = "pusty_foreach"
    BRAK_WEJSCIA = "brak_wejscia"
    ZALEZNOSC_NIEUDANA = "zaleznosc_nieudana"


@dataclass(frozen=True)
class Plan:
    do_startu: list[str]
    do_pominiecia: dict[str, str]
    powody: dict[str, PowodPominiecia]


@dataclass(frozen=True)
class Podsumowanie:
    skonczony: bool
    nieudane: list[str]


def _przodkowie(start: str, needs: Mapping[str, list[str]]) -> set[str]:
    """Domkniecie przechodnie `needs` od `start`, bez `start`."""
    odwiedzeni: set[str] = set()
    stos = list(needs[start])
    while stos:
        id_kroku = stos.pop()
        if id_kroku in odwiedzeni:
            continue
        odwiedzeni.add(id_kroku)
        stos.extend(needs[id_kroku])
    return odwiedzeni


@dataclass(frozen=True)
class _Kontrakt:
    """Artefakty i capabilities kroku: ze skilla albo z opisu kroku systemowego."""

    consumes: tuple[str, ...]
    produces: tuple[str, ...]
    needs: tuple[str, ...]


def _kontrakt(krok: Krok, rejestr: Rejestr) -> _Kontrakt:
    if krok.skill is None:
        opis = opis_kroku_systemowego(krok.system)
        return _Kontrakt(opis.consumes, opis.produces, opis.needs)
    skill = rejestr.skille[krok.skill]
    return _Kontrakt(tuple(skill.consumes), tuple(skill.produces), tuple(skill.needs))


def nastepne(
    workflow: Workflow,
    rejestr: Rejestr,
    capabilities: Collection[str],
    stany: Mapping[str, StanKroku],
) -> Plan:
    """Plan: kroki do startu i do pominiecia (z powodem). Planuje tylko kroki `oczekuje`."""
    ids = [krok.id for krok in workflow.kroki]
    needs = {krok.id: list(krok.needs) for krok in workflow.kroki}
    kontrakty = {krok.id: _kontrakt(krok, rejestr) for krok in workflow.kroki}
    przodkowie = {id_kroku: _przodkowie(id_kroku, needs) for id_kroku in ids}
    lokalne = {id_kroku: stany.get(id_kroku, StanKroku.OCZEKUJE) for id_kroku in ids}
    pominiecia: dict[str, tuple[PowodPominiecia, str]] = {}

    def powod(id_kroku: str) -> tuple[PowodPominiecia, str] | None:
        moi = [p for p in ids if p in przodkowie[id_kroku]]
        kontrakt = kontrakty[id_kroku]
        # utracone wejscie (kazdy producent artefaktu pominiety albo nieudany) jest dokladniejsza
        # przyczyna niz nieudany przodek w ogole, wiec idzie przed `zaleznosc_nieudana`
        stracone = (StanKroku.POMINIETY, StanKroku.NIEUDANY)
        for artefakt in kontrakt.consumes:
            producenci = [p for p in moi if artefakt in kontrakty[p].produces]
            if producenci and all(lokalne[p] in stracone for p in producenci):
                return (
                    PowodPominiecia.BRAK_WEJSCIA,
                    f"brak wejścia: {artefakt} z kroku {producenci[0]}",
                )
        for przodek in moi:
            if lokalne[przodek] == StanKroku.NIEUDANY:
                return PowodPominiecia.ZALEZNOSC_NIEUDANA, f"zależność nieudana: {przodek}"
        for capability in kontrakt.needs:
            if capability not in capabilities:
                return PowodPominiecia.BRAK_CAPABILITY, f"brak capability {capability}"
        return None

    while True:
        liczba = len(pominiecia)
        for id_kroku in ids:
            if lokalne[id_kroku] != StanKroku.OCZEKUJE:
                continue
            znaleziony = powod(id_kroku)
            if znaleziony is not None:
                lokalne[id_kroku] = StanKroku.POMINIETY
                pominiecia[id_kroku] = znaleziony
        if len(pominiecia) == liczba:
            break

    gotowe = (StanKroku.ZAKONCZONY, StanKroku.POMINIETY)
    do_startu = [
        id_kroku
        for id_kroku in ids
        if lokalne[id_kroku] == StanKroku.OCZEKUJE
        and all(lokalne[n] in gotowe for n in needs[id_kroku])
    ]
    uporzadkowane = [id_kroku for id_kroku in ids if id_kroku in pominiecia]
    return Plan(
        do_startu=do_startu,
        do_pominiecia={i: pominiecia[i][1] for i in uporzadkowane},
        powody={i: pominiecia[i][0] for i in uporzadkowane},
    )


def podsumowanie(stany: Mapping[str, StanKroku]) -> Podsumowanie:
    """Run skonczony, gdy nic nie oczekuje ani nie jest w toku; lista nieudanych w kolejnosci `stany`."""
    trwa = (StanKroku.OCZEKUJE, StanKroku.W_TOKU)
    return Podsumowanie(
        skonczony=not any(stan in trwa for stan in stany.values()),
        nieudane=[i for i, stan in stany.items() if stan == StanKroku.NIEUDANY],
    )
