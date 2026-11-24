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

class StrumienRunnera:
    """Jedno otwarte polaczenie SSE runnera (HAR-200).

    `zamkniety` to token generacji: nowszy strumien tego runnera ustawia go starszym,
    a stary generator konczy sie nawet wtedy, gdy wisi na oczekiwaniu na polecenie.
    `liczy` mowi, czy strumien jest jeszcze w ewidencji `_polaczone`.
    """

    def __init__(self, runner_id: str) -> None:
        self.runner_id = runner_id
        self.zamkniety = asyncio.Event()
        self.liczy = True
        self.oczekuje: asyncio.Future[dict[str, Any]] | None = None   # biezace `get` z kolejki


class KolejkaPolecen(asyncio.Queue[dict[str, Any]]):
    """Kolejka polecen runnera: jak `asyncio.Queue`, plus zwrot polecenia na poczatek.

    Strumienie runnera trzymamy na kolejce, a nie w osobnym slowniku modulu: kolejka jest
    juz sprzatana tam, gdzie sprzata sie stan szyny, wiec nie przybywa nowy stan globalny.
    """

    def __init__(self) -> None:
        super().__init__()
        self.strumienie: list[StrumienRunnera] = []
        self._na_poczatek = False

    def _put(self, item: dict[str, Any]) -> None:
        # hak `_put` jest przeznaczony do podklas (LifoQueue, PriorityQueue); kolejka bez limitu,
        # a bez `await` miedzy ustawieniem flagi i jej zdjeciem nikt jej nie zobaczy
        if self._na_poczatek:
            self._queue.appendleft(item)
        else:
            self._queue.append(item)

    def zwroc_na_poczatek(self, polecenie: dict[str, Any]) -> None:
        self._na_poczatek = True
        try:
            self.put_nowait(polecenie)
        finally:
            self._na_poczatek = False


_runner_queues: dict[str, KolejkaPolecen] = {}
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


def runner_queue(runner_id: str) -> KolejkaPolecen:
    q = _runner_queues.get(runner_id)
    if q is None:
        q = _runner_queues[runner_id] = KolejkaPolecen()
    return q


def otworz_strumien_runnera(runner_id: str) -> StrumienRunnera:
    """Rejestruje nowy strumien runnera i zamyka poprzednie strumienie tego runnera.

    Stare strumienie wypadaja z ewidencji od razu (bez wpisu o rozlaczeniu - runner jest
    polaczony nowym), a ich pozniejsze `zamknij_strumien` niczego juz nie zdejmuje.
    """
    q = runner_queue(runner_id)
    nowy = StrumienRunnera(runner_id)
    mark_runner_connected(runner_id)     # najpierw nowy: licznik nie schodzi do zera
    for stary in q.strumienie:
        stary.zamkniety.set()
        # Synchronicznie: getter starego strumienia znika z kolejki, zanim ktokolwiek
        # zdazy wyslac polecenie, wiec nie przechwyci go przed nowym (kolejnosc zostaje).
        if stary.oczekuje is not None:
            stary.oczekuje.cancel()
        _wyrejestruj(stary)
    q.strumienie[:] = [nowy]
    return nowy


def _wyrejestruj(strumien: StrumienRunnera) -> None:
    if strumien.liczy:
        strumien.liczy = False
        mark_runner_disconnected(strumien.runner_id)


def zamknij_strumien(strumien: StrumienRunnera) -> None:
    """Zdejmuje strumien; zamkniecie starego strumienia nie rusza nowszego ani runnera."""
    _wyrejestruj(strumien)


def zwroc_polecenie(runner_id: str, polecenie: dict[str, Any]) -> None:
    """Polecenie pobrane, ale niewyslane, wraca na POCZATEK kolejki."""
    runner_queue(runner_id).zwroc_na_poczatek(polecenie)


def _odebrane(zadanie: asyncio.Future[dict[str, Any]]) -> dict[str, Any] | None:
    # `get` nie rzuca; anulowane bywa przez `otworz_strumien_runnera` albo nasz finally
    if zadanie.done() and not zadanie.cancelled():
        return zadanie.result()
    return None


async def nastepne_polecenie(strumien: StrumienRunnera) -> dict[str, Any] | None:
    """Czeka na polecenie dla strumienia albo na jego zamkniecie; `None` = strumien zamkniety.

    `Queue.get` blokuje, wiec scigamy je z sygnalem zamkniecia. Polecenie pobrane w chwili
    zamkniecia (albo anulowania) wraca na poczatek kolejki, a nie ginie.
    """
    q = runner_queue(strumien.runner_id)
    pobierz = asyncio.ensure_future(q.get())
    zamkniecie = asyncio.ensure_future(strumien.zamkniety.wait())
    strumien.oczekuje = pobierz
    zakonczone = False
    try:
        await asyncio.wait({pobierz, zamkniecie}, return_when=asyncio.FIRST_COMPLETED)
        zakonczone = True
    finally:
        # Bez `await` w finally: anulowanie z zakresu anyio (rozlaczenie klienta SSE) ponawia
        # sie przy kazdym await i zwrot polecenia nigdy by sie nie wykonal.
        odebrane = _odebrane(pobierz)
        zamkniecie.cancel()
        pobierz.cancel()
        if odebrane is not None and (not zakonczone or strumien.zamkniety.is_set()):
            q.zwroc_na_poczatek(odebrane)
            odebrane = None
    return odebrane


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
