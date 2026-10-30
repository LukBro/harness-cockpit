"""Szyna w pamieci: strumien polecen do runnera i strumien zdarzen do przegladarki.

Transport runnera: SSE w dol, POST w gore (BRO-86). Runner laczy sie wychodzaco,
wiec kokpit nie moze go zawolac - polecenie jedzie tym samym kanalem co zdarzenia
do przegladarki, a odpowiedz wraca POST-em skorelowana po `req_id`.

W szkielecie trzymane w pamieci. Docelowo: tabela zadan w bazie, zeby restart
kokpitu nie gubil polecenia (BRO-75).
"""
from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

_runner_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
_run_subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)
# Ewidencja aktywnych strumieni runnera (HAR-2). Licznik, bo runner moze miec dwa.
_polaczone: dict[str, int] = {}
# Chwila, w ktorej ostatni strumien runnera sie zamknal (rozlaczenie wygrywa ze swiezym last_seen_at).
_rozlaczone: dict[str, datetime] = {}


def mark_runner_connected(runner_id: str) -> None:
    _polaczone[runner_id] = _polaczone.get(runner_id, 0) + 1


def mark_runner_disconnected(runner_id: str) -> None:
    n = _polaczone.get(runner_id, 0) - 1
    if n > 0:
        _polaczone[runner_id] = n
    else:
        if _polaczone.pop(runner_id, None) is not None:
            _rozlaczone[runner_id] = datetime.now(timezone.utc)


def connected_runner_ids() -> frozenset[str]:
    return frozenset(_polaczone)


def disconnected_runners() -> dict[str, datetime]:
    return dict(_rozlaczone)


def runner_queue(runner_id: str) -> asyncio.Queue[dict[str, Any]]:
    return _runner_queues.setdefault(runner_id, asyncio.Queue())


async def send_to_runner(runner_id: str, command: dict[str, Any]) -> None:
    await runner_queue(runner_id).put(command)


def subscribe_run(run_id: str) -> asyncio.Queue[dict[str, Any]]:
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    _run_subscribers[run_id].append(q)
    return q


def unsubscribe_run(run_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
    if q in _run_subscribers.get(run_id, []):
        _run_subscribers[run_id].remove(q)


async def publish_event(run_id: str, event: dict[str, Any]) -> None:
    for q in list(_run_subscribers.get(run_id, [])):
        await q.put(event)


# req_id -> (runner_id, future). Odpowiedz rozwiazuje tylko zapytanie wyslane do tego runnera (HAR-49).
_oczekujace: dict[str, tuple[str, asyncio.Future[dict[str, Any]]]] = {}


async def zapytaj(
    runner_id: str, typ: str, dane: dict[str, Any], timeout_s: float,
) -> dict[str, Any]:
    req_id = uuid.uuid4().hex
    fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    # wpis przed wysylka: szybki runner moze odpowiedziec, zanim send_to_runner wroci
    _oczekujace[req_id] = (runner_id, fut)
    try:
        # type i req_id na koncu: dane nie moga ich nadpisac
        await send_to_runner(runner_id, {**dane, "type": typ, "req_id": req_id})
        return await asyncio.wait_for(fut, timeout_s)
    finally:
        _oczekujace.pop(req_id, None)


def odpowiedz(runner_id: str, req_id: str, cialo: dict[str, Any]) -> bool:
    wpis = _oczekujace.get(req_id)
    if wpis is None or wpis[0] != runner_id or wpis[1].done():
        return False
    wpis[1].set_result(cialo)
    return True
