"""Szyna w pamieci: strumien polecen do runnera i strumien zdarzen do przegladarki.

Transport runnera: SSE w dol, POST w gore (BRO-86). Runner laczy sie wychodzaco,
wiec kokpit nie moze go zawolac - polecenie jedzie tym samym kanalem co zdarzenia
do przegladarki, a odpowiedz wraca POST-em skorelowana po `req_id`.

W szkielecie trzymane w pamieci. Docelowo: tabela zadan w bazie, zeby restart
kokpitu nie gubil polecenia (BRO-75).
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

_runner_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
_run_subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)


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
