"""Heartbeat: runner co kilka sekund melduje kokpitowi, ze zyje (HAR-35, D12).

Strumien SSE bywa po stronie serwera "otwarty" po `kill -9` runnera albo zerwaniu
sieci; heartbeat jest dowodem zycia, ktorego kokpit wymaga obok strumienia.
"""
from __future__ import annotations

import asyncio
import sys

import httpx

LIMIT_ZADANIA_S = 10     # gorna granica limitu pojedynczego zadania


class Heartbeat:
    def __init__(
        self, client: httpx.AsyncClient, url: str, naglowki: dict[str, str], co_s: float,
    ) -> None:
        self._client = client
        self._url = url
        self._naglowki = naglowki
        self.co_s = co_s
        self.timeout_s = min(co_s, LIMIT_ZADANIA_S)
        self._zadanie: asyncio.Task | None = None

    def start(self) -> None:
        if self._zadanie is None:
            self._zadanie = asyncio.create_task(self._petla())

    async def stop(self) -> None:
        zadanie, self._zadanie = self._zadanie, None
        if zadanie is None:
            return
        zadanie.cancel()          # nie czekamy na zawieszone zadanie HTTP
        await asyncio.wait({zadanie})

    async def _petla(self) -> None:
        """Zadanie nie ma wolajacego, ktory odebralby wyjatek; cichy zgon petli to runner
        uznany za offline bez sladu w logu, wiec kazdy wyjatek (poza anulowaniem) trwa dalej."""
        while True:
            await asyncio.sleep(self.co_s)
            try:
                # wait_for: limit dziala tez wtedy, gdy transport ignoruje timeout httpx
                r = await asyncio.wait_for(
                    self._client.post(self._url, headers=self._naglowki, timeout=self.timeout_s),
                    self.timeout_s,
                )
            except Exception as e:
                # tylko nazwa klasy: tresc wyjatku moze niesc adres albo naglowki
                print(f"heartbeat nieudany: {type(e).__name__}", file=sys.stderr)
                continue
            if not r.is_success:
                print(f"heartbeat odrzucony: http={r.status_code}", file=sys.stderr)
