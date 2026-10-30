"""Transport ASGI dla testow, ktory przepuszcza nieskonczony strumien SSE (wspolny helper).

`httpx.ASGITransport` buforuje cale ciala odpowiedzi, wiec `GET /api/runners/stream` by zawisl.
Wyniesione z `test_zapytania_runnera.py` bez zmiany zachowania (HAR-55).
"""
from __future__ import annotations

import asyncio

import httpx


class StrumienOdpowiedziASGI(httpx.AsyncByteStream):
    """Cialo odpowiedzi karmione z kolejki komunikatow `http.response.body` aplikacji."""

    def __init__(self, odpowiedzi: asyncio.Queue, rozlaczenie: asyncio.Event, zadanie: asyncio.Task):
        self._odpowiedzi = odpowiedzi
        self._rozlaczenie = rozlaczenie
        self._zadanie = zadanie

    async def __aiter__(self):
        while True:
            komunikat = await self._odpowiedzi.get()
            if komunikat is None:
                return
            if isinstance(komunikat, BaseException):
                raise komunikat
            if komunikat["type"] != "http.response.body":
                continue
            if komunikat.get("body"):
                yield komunikat["body"]
            if not komunikat.get("more_body", False):
                return

    async def aclose(self) -> None:
        """Klient zamknal odpowiedz = rozlaczyl sie: aplikacja dostaje `http.disconnect`, a gdy
        generator SSE tkwi w oczekiwaniu na polecenie, zadanie jest anulowane (zwalnia zasoby)."""
        self._rozlaczenie.set()
        gotowe, _ = await asyncio.wait({self._zadanie}, timeout=1)
        if not gotowe:
            self._zadanie.cancel()
            await asyncio.wait({self._zadanie}, timeout=5)


class TransportASGIStrumieniujacy(httpx.AsyncBaseTransport):
    """Jak `httpx.ASGITransport`, ale oddaje odpowiedz po `http.response.start` i dalej karmi
    jej cialo na biezaco. Dzieki temu nieskonczony `GET /api/runners/stream` nie wiesza
    klienta, a wszystkie zadania runnera przechodza przez prawdziwa aplikacje FastAPI."""

    def __init__(self, aplikacja) -> None:
        self._aplikacja = aplikacja

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        tresc = b"".join([kawalek async for kawalek in request.stream])
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": request.method,
            "headers": [(k.lower(), v) for (k, v) in request.headers.raw],
            "scheme": request.url.scheme, "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "server": (request.url.host, request.url.port),
            "client": ("127.0.0.1", 123), "root_path": "",
        }
        odpowiedzi: asyncio.Queue = asyncio.Queue()
        rozlaczenie = asyncio.Event()
        tresc_wyslana = False

        async def receive():
            nonlocal tresc_wyslana
            if not tresc_wyslana:
                tresc_wyslana = True
                return {"type": "http.request", "body": tresc, "more_body": False}
            await rozlaczenie.wait()
            return {"type": "http.disconnect"}

        async def send(komunikat):
            await odpowiedzi.put(komunikat)

        async def uruchom():
            try:
                await self._aplikacja(scope, receive, send)
            except Exception as e:
                await odpowiedzi.put(e)
            finally:
                odpowiedzi.put_nowait(None)

        zadanie = asyncio.create_task(uruchom())
        try:
            pierwszy = await odpowiedzi.get()
        except asyncio.CancelledError:
            zadanie.cancel()
            raise
        if isinstance(pierwszy, BaseException):
            raise pierwszy
        if pierwszy is None:
            raise RuntimeError("aplikacja zakonczyla sie bez odpowiedzi")
        assert pierwszy["type"] == "http.response.start"
        return httpx.Response(
            pierwszy["status"], headers=pierwszy.get("headers", []),
            stream=StrumienOdpowiedziASGI(odpowiedzi, rozlaczenie, zadanie))
