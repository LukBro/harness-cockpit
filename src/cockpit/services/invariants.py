"""Niezmienniki jako byt w kodzie, nie zdania w dokumentacji (BRO-74).

Test liczy elementy tej listy i porownuje z liczba sprawdzen - dopisanie
niezmiennika bez implementacji sprawdzenia wywala test.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class Niezmiennik(StrEnum):
    role_binding = "role_binding"                 # rola ustawia tier i prawo zapisu razem
    reviewer_readonly = "reviewer_readonly"       # recenzent nigdy z prawem zapisu
    approval_required = "approval_required"       # krok z approval nie przechodzi bez decyzji
    # Wiaze silnik, nie czlowieka: commit po integrate jest dozwolony jako osobna,
    # jawna akcja czlowieka. Push nigdy. (BRO-128)
    no_git_write = "no_git_write"                 # silnik nie commituje ani nie pushuje
    run_dir_outside_repo = "run_dir_outside_repo" # artefakty runu poza repozytorium
    measured_or_null = "measured_or_null"         # brak pomiaru to brak, nigdy zero
    evidence_required = "evidence_required"       # brak dowodu to blad kroku, nie zielone przejscie


class Rola(StrEnum):
    """Rola skilla. Tylko nazwy: tier i prawo zapisu to niezmiennik role_binding (HAR-6)."""

    author = "author"
    reviewer = "reviewer"
    executor = "executor"
    gatherer = "gatherer"


class Tier(StrEnum):
    """Klasa modelu wymagana przez role. Nazwa modelu to profil backendu u runnera (BRO-98)."""

    judgment = "judgment"
    execution = "execution"
    retrieval = "retrieval"


@dataclass(frozen=True)
class ProfilRoli:
    """Tier i prawo zapisu razem - nigdy osobno (role_binding, HAR-6)."""

    tier: Tier
    zapis: bool


# Jedyne zrodlo tieru i prawa zapisu. Recenzent nigdy z zapisem (reviewer_readonly).
PROFILE_ROL: Mapping[Rola, ProfilRoli] = MappingProxyType({
    Rola.author: ProfilRoli(Tier.judgment, True),
    Rola.reviewer: ProfilRoli(Tier.judgment, False),
    Rola.executor: ProfilRoli(Tier.execution, True),
    Rola.gatherer: ProfilRoli(Tier.retrieval, False),
})


def profil_roli(rola: Rola) -> ProfilRoli:
    """Tier i prawo zapisu wynikaja wylacznie stad (HAR-6)."""
    return PROFILE_ROL[rola]


WSZYSTKIE = frozenset(Niezmiennik)
