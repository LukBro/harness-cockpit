"""Operacje runnera na zapytania kokpitu (HAR-49). Nowa operacja = nowy wpis w `OPERACJE`."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


async def ping(dane: dict) -> dict:
    return {"pong": True}


OPERACJE: dict[str, Callable[[dict], Awaitable[Any]]] = {"ping": ping}


async def wykonaj(polecenie: dict) -> dict:
    """Zawsze zwraca odpowiedz z `req_id`; blad operacji nie wychodzi wyjatkiem."""
    req_id = polecenie.get("req_id")
    typ = polecenie.get("type")
    operacja = OPERACJE.get(typ)
    if operacja is None:
        return {"req_id": req_id, "ok": False, "blad": f"nieznana operacja: {typ}",
                "kod": "nieznana_operacja"}
    dane = {k: v for k, v in polecenie.items() if k not in ("type", "req_id")}
    try:
        wynik = await operacja(dane)
    except Exception as e:
        # bez str(e) i sladu stosu: tresc wyjatku moze niesc sekret
        return {"req_id": req_id, "ok": False,
                "blad": f"operacja {typ} nie powiodla sie ({type(e).__name__})",
                "kod": "blad_operacji"}
    return {"req_id": req_id, "ok": True, "wynik": wynik}
