"""Ktore kroki workflow sa weryfikujace (HAR-178, D27).

Kokpit nie zna skilli ani workflow, wiec klasyfikacje robi runner i dosyla ja w koncowym
`run_finished`. Funkcja jest czysta: bez I/O.
"""
from __future__ import annotations

from cockpit.services.invariants import Rola
from cockpit.services.skills import Rejestr
from cockpit.services.workflow import Workflow

CAPABILITY_BRAMKI = frozenset({"test_narrow", "test_full", "lint", "typecheck", "build"})


def kroki_weryfikujace(workflow: Workflow, rejestr: Rejestr) -> list[str]:
    """Id krokow w kolejnosci workflow, ktorych skill ma role `reviewer` albo potrzebuje
    capability-bramki. Workflow jest zwalidowany przy starcie (jak w `graf.nastepne`)."""
    wynik: list[str] = []
    for krok in workflow.kroki:
        if krok.skill is None:
            # jedyny krok systemowy, `diff` (HAR-176), daje dowod, nie werdykt; `check` z HAR-177
            # weryfikuje i sam zmieni te galaz

            continue
        skill = rejestr.skille[krok.skill]
        if skill.role == Rola.reviewer or CAPABILITY_BRAMKI & set(skill.needs):
            wynik.append(krok.id)
    return wynik
