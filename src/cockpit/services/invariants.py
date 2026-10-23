"""Niezmienniki jako byt w kodzie, nie zdania w dokumentacji (BRO-74).

Test liczy elementy tej listy i porownuje z liczba sprawdzen - dopisanie
niezmiennika bez implementacji sprawdzenia wywala test.
"""
from __future__ import annotations

from enum import StrEnum


class Niezmiennik(StrEnum):
    role_binding = "role_binding"                 # rola ustawia tier i prawo zapisu razem
    reviewer_readonly = "reviewer_readonly"       # recenzent nigdy z prawem zapisu
    approval_required = "approval_required"       # krok z approval nie przechodzi bez decyzji
    # Wiaze silnik, nie czlowieka: commit po integrate jest dozwolony jako osobna,
    # jawna akcja czlowieka. Push nigdy. (BRO-128)
    no_git_write = "no_git_write"                 # silnik nie commituje ani nie pushuje
    run_dir_outside_repo = "run_dir_outside_repo" # artefakty runu poza repozytorium
    measured_or_null = "measured_or_null"         # brak pomiaru to brak, nigdy zero


WSZYSTKIE = frozenset(Niezmiennik)
