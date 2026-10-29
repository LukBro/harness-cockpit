"""Wspolny helper procesow w testach (HAR-142).

Retro M1.3 (HAR-35): sprzatanie `killpg(getpgid(pid))` pod mutantem bez
`start_new_session` trafialo w grupe pytesta/mutmuta/agenta CI i zabijalo runner.
Zasady: sygnal do grupy dostaje WYLACZNIE grupa utworzona przez helper (nigdy
`os.getpgid(0)`), a kazde oczekiwanie ma twardy limit i nazwe operacji w bledzie.

Poza tym plikiem testy nie wolaja `os.killpg` ani `os.kill` (wyjatki: sonda sygnalem 0
i `os.kill(os.getpid(), ...)` do samego siebie) - pilnuje tego
`test_procesy_testowe.py`. Import z testow: `from procesy import ...`.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

SIGTERM = signal.SIGTERM
SIGKILL = signal.SIGKILL

# pid -> pgid z chwili uruchomienia; pgid sluzy do wykrycia recyklingu numeru pid.
_PROCESY: dict[int, int] = {}


class LimitCzasuTestu(AssertionError):
    """Oczekiwanie w tescie przekroczylo twardy limit; komunikat niesie nazwe operacji."""


class WlasnaGrupaProcesow(AssertionError):
    """Proba wyslania sygnalu do grupy procesow samego testu (pytest/mutmut/agent CI)."""


def _komunikat_limitu(nazwa: str, limit: float) -> str:
    return f"{nazwa}: przekroczono limit {limit} s"


# --- sygnaly i sondy ------------------------------------------------------------


def zabij_grupe(pgid: int, sygnal: int = SIGKILL) -> None:
    """Sygnal do grupy `pgid`; grupa testu -> `WlasnaGrupaProcesow` bez wysylania."""
    if pgid == os.getpgid(0):
        raise WlasnaGrupaProcesow(f"grupa {pgid} jest grupa testu, nie wysylam sygnalu {sygnal}")
    try:
        os.killpg(pgid, sygnal)
    except ProcessLookupError:
        pass


def zabij_proces(pid: int, sygnal: int = SIGKILL) -> None:
    """Sygnal do jednego procesu (np. potomka, ktory sam zmienil grupe); brak procesu jest ok."""
    try:
        os.kill(pid, sygnal)
    except ProcessLookupError:
        pass


def czy_zyje(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:       # istnieje, tylko nie nasz
        return True
    return True


def grupa_istnieje(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- oczekiwanie z limitem ------------------------------------------------------


async def czekaj(awaitable: Awaitable, limit: float, nazwa: str) -> Any:
    """`asyncio.wait_for`; po limicie `LimitCzasuTestu` z nazwa operacji.
    Uwaga: po limicie `wait_for` anuluje awaitable - dla zadan, ktore moga polknac
    anulowanie, uzyj `czekaj_bez_anulowania`."""
    try:
        return await asyncio.wait_for(awaitable, limit)
    except TimeoutError:
        raise LimitCzasuTestu(_komunikat_limitu(nazwa, limit)) from None


async def czekaj_bez_anulowania(zadanie: asyncio.Future, limit: float, nazwa: str) -> Any:
    """Czeka na `zadanie` BEZ anulowania go po limicie (`wait_for` maskowalby zadanie
    polykajace anulowanie - HAR-35). Zwraca wynik zadania, jego wyjatek przepuszcza,
    po limicie `LimitCzasuTestu` (zadanie zostaje zywe - sprzata je wolajacy)."""
    zakonczone, _ = await asyncio.wait({zadanie}, timeout=limit)
    if zadanie not in zakonczone:
        raise LimitCzasuTestu(_komunikat_limitu(nazwa, limit))
    return zadanie.result()


async def czekaj_na_warunek(
    warunek: Callable[[], object], limit: float, nazwa: str, krok: float = 0.01
) -> None:
    koniec = time.monotonic() + limit
    while not warunek():
        if time.monotonic() >= koniec:
            raise LimitCzasuTestu(_komunikat_limitu(nazwa, limit))
        await asyncio.sleep(krok)


async def grupa_umarla(pgid: int, limit: float = 5.0) -> bool:
    """True, gdy grupa zniknela w limicie; False (bez wyjatku), gdy nadal zyje."""
    try:
        await czekaj_na_warunek(
            lambda: not grupa_istnieje(pgid), limit, f"koniec grupy {pgid}", krok=0.05
        )
    except LimitCzasuTestu:
        return False
    return True


@contextlib.asynccontextmanager
async def zadanie_tla(
    korutyna: Awaitable, nazwa: str, limit_konca: float = 5.0
) -> AsyncIterator[asyncio.Task]:
    """Zadanie tla na czas bloku; na wyjsciu anulowane i doczekane w limicie.
    Zadanie, ktore nie skonczylo sie po anulowaniu, daje `LimitCzasuTestu`."""
    zadanie = asyncio.ensure_future(korutyna)
    try:
        yield zadanie
    finally:
        zadanie.cancel()
        zakonczone, _ = await asyncio.wait({zadanie}, timeout=limit_konca)
        if zadanie not in zakonczone:
            raise LimitCzasuTestu(_komunikat_limitu(f"koniec zadania {nazwa}", limit_konca))


# --- uruchamianie i konczenie procesow -------------------------------------------


def zapamietaj_proces(pid: int) -> None:
    """Rejestruje proces do sprzatania po tescie (`sprzatnij_zapamietane`)."""
    try:
        _PROCESY[pid] = os.getpgid(pid)
    except ProcessLookupError:
        pass


def sprzatnij_zapamietane() -> None:
    """Po tescie: grupa zapamietanego procesu dostaje SIGKILL, ale tylko gdy pid nadal
    nalezy do zapamietanej grupy (anty-recykling). Proces z grupy testu dostaje sygnal
    sam, nigdy cala grupa."""
    zapamietane = dict(_PROCESY)
    _PROCESY.clear()
    for pid, pgid in zapamietane.items():
        try:
            if os.getpgid(pid) != pgid:
                continue
        except ProcessLookupError:
            continue
        if pgid == os.getpgid(0):
            zabij_proces(pid)
        else:
            zabij_grupe(pgid)


async def uruchom_proces(*argv: str, **kw: Any) -> asyncio.subprocess.Process:
    """Proces w NOWEJ sesji (wlasna grupa), zapamietany do sprzatania po tescie."""
    proc = await asyncio.create_subprocess_exec(*argv, start_new_session=True, **kw)
    zapamietaj_proces(proc.pid)
    return proc


def uruchom_proces_sync(argv: Sequence[str], **kw: Any) -> subprocess.Popen:
    """Wariant `Popen` (e2e) z nowa sesja i zapamietaniem."""
    proc = subprocess.Popen(list(argv), start_new_session=True, **kw)
    zapamietaj_proces(proc.pid)
    return proc


def _grupa_procesu(pid: int, nazwa: str) -> int | None:
    """pgid procesu do zabicia; None gdy proces zniknal; grupa testu -> proces jest
    dobijany sam (bez grupy, zeby nie zostal sierota), potem `WlasnaGrupaProcesow`."""
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return None
    if pgid == os.getpgid(0):
        zabij_proces(pid)
        raise WlasnaGrupaProcesow(
            f"{nazwa}: proces {pid} nie ma wlasnej sesji (dzieli grupe z testem); "
            "uruchom go przez uruchom_proces / uruchom_proces_sync"
        )
    return pgid


async def zakoncz_proces(
    proc: asyncio.subprocess.Process,
    *,
    nazwa: str,
    limit_term: float = 5.0,
    limit_wait: float = 5.0,
) -> None:
    """SIGTERM do grupy -> po `limit_term` SIGKILL -> `wait()` z `limit_wait`; na koncu
    SIGKILL do reszty grupy (potomek, ktory przezyl lidera). Zakonczony proces: no-op."""
    if proc.returncode is not None:
        return
    pgid = _grupa_procesu(proc.pid, nazwa)
    if pgid is None:
        await czekaj(proc.wait(), limit_wait, f"koniec procesu {nazwa}")
        return
    zabij_grupe(pgid, SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), limit_term)
    except TimeoutError:
        zabij_grupe(pgid, SIGKILL)
        await czekaj(proc.wait(), limit_wait, f"koniec procesu {nazwa} po SIGKILL")
    zabij_grupe(pgid, SIGKILL)


def zakoncz_proces_sync(
    proc: subprocess.Popen,
    *,
    nazwa: str,
    limit_term: float = 5.0,
    limit_wait: float = 5.0,
) -> None:
    """Jak `zakoncz_proces` dla `Popen`."""
    if proc.poll() is not None:
        return
    pgid = _grupa_procesu(proc.pid, nazwa)
    if pgid is None:
        _czekaj_sync(proc, limit_wait, f"koniec procesu {nazwa}")
        return
    zabij_grupe(pgid, SIGTERM)
    try:
        proc.wait(limit_term)
    except subprocess.TimeoutExpired:
        zabij_grupe(pgid, SIGKILL)
        _czekaj_sync(proc, limit_wait, f"koniec procesu {nazwa} po SIGKILL")
    zabij_grupe(pgid, SIGKILL)


def _czekaj_sync(proc: subprocess.Popen, limit: float, nazwa: str) -> None:
    try:
        proc.wait(limit)
    except subprocess.TimeoutExpired:
        raise LimitCzasuTestu(_komunikat_limitu(nazwa, limit)) from None
