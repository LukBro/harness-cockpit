"""Odbior runu (HAR-89): diff runu zbiera runner, kokpit tylko pyta i pilnuje regul.

Odbior jest tylko do odczytu: serwis nic nie zapisuje w bazie. Pytamy runnera TEGO runu
(`Run.runner_id`), nie pierwszego online - worktree lezy na jego dysku.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Run

from .errors import StanNiemozliwy
from .runs import STATUSY_AKTYWNE, run_dla_najemcy
from .zapytania_runnera import Wyslij, zapytaj_runnera


async def run_do_odbioru(s: AsyncSession, run_id: str, tenant_id: str) -> Run:
    run = await run_dla_najemcy(s, run_id, tenant_id)
    if run.status in STATUSY_AKTYWNE:
        raise StanNiemozliwy("run jeszcze trwa")
    if run.base_commit is None:
        raise StanNiemozliwy("run nie ma base_commit")
    if run.runner_id is None:
        raise StanNiemozliwy("run nie ma runnera")
    return run


async def diff_runu(
    s: AsyncSession, run_id: str, tenant_id: str, polaczone: frozenset[str], wyslij: Wyslij,
) -> str:
    run = await run_do_odbioru(s, run_id, tenant_id)
    wynik = await zapytaj_runnera(
        s, tenant_id, run.runner_id, "collect",
        {"run_id": run.id, "base_commit": run.base_commit}, polaczone, wyslij)
    if not isinstance(wynik, dict) or not isinstance(wynik.get("diff"), str):
        raise StanNiemozliwy("runner zwrocil zly wynik collect")
    return wynik["diff"]
