"""Zapytania kokpitu do runnera (HAR-49): jedyna droga kokpitu do plikow i integracji uzytkownika.

Wysylka jest wstrzykiwana (`wyslij`): serwis nie importuje `cockpit.api.*`.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Runner
from cockpit.settings import settings

from .errors import PlikPozaZakresem, RunnerOffline, StanNiemozliwy
from .runs import runner_online

Wyslij = Callable[[str, str, dict[str, Any], float], Awaitable[dict[str, Any]]]


async def zapytaj_runnera_zespolu(
    s: AsyncSession, tenant_id: str, typ: str, dane: dict[str, Any],
    polaczone: frozenset[str], wyslij: Wyslij,
) -> Any:
    runnery = (await s.execute(
        select(Runner).where(Runner.tenant_id == tenant_id).order_by(Runner.id)
    )).scalars().all()
    runner = next((r for r in runnery if runner_online(r, polaczone)), None)
    if runner is None:
        raise RunnerOffline("runner offline — tryb tylko do odczytu")
    try:
        cialo = await wyslij(runner.id, typ, dane, settings.runner_zapytanie_timeout_s)
    except TimeoutError:
        raise StanNiemozliwy("runner nie odpowiedzial w limicie czasu") from None
    if len(json.dumps(cialo).encode()) > settings.runner_odpowiedz_max_kb * 1024:
        raise StanNiemozliwy(
            f"odpowiedz runnera jest za duza (limit {settings.runner_odpowiedz_max_kb} KB)")
    if cialo.get("ok") is not True:
        if cialo.get("kod") == "poza_zakresem":
            raise PlikPozaZakresem(cialo.get("blad") or "sciezka poza zakresem runnera")
        raise StanNiemozliwy(cialo.get("blad") or "runner zglosil blad")
    return cialo.get("wynik")
