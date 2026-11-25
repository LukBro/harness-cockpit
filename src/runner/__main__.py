import asyncio
import os
import signal
import sys

from runner.bufor import BladBufora
from runner.client import KokpitNiezgodny, Runner, TokenOdrzucony
from runner.maskowanie import zainstaluj_maskowanie
from runner.secrets import BladSekretow, blad_pliku, get_token


async def _uruchom(base: str, token: str) -> None:
    runner = Runner(base, token)
    loop = asyncio.get_running_loop()
    zatrzymania: set[asyncio.Task] = set()

    def zatrzymaj() -> None:
        zatrzymania.add(loop.create_task(runner.stop()))

    for numer in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(numer, zatrzymaj)
    try:
        await runner.listen()
    finally:
        for numer in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(numer)
        await asyncio.gather(*zatrzymania)      # stop() konczy sie przed wyjsciem procesu


def _poprawny_token(token: str) -> bool:
    """Token trafia do naglowka HTTP: drukowalne ASCII, bez bialych znakow."""
    return token.isascii() and token.isprintable() and not any(c.isspace() for c in token)


def main() -> None:
    zainstaluj_maskowanie()
    try:
        token = get_token("runner")
        if token is not None and not isinstance(token, str):
            raise blad_pliku('ma wartosc klucza "runner" inna niz napis')
        if token and not _poprawny_token(token):
            # httpx rzuca UnicodeEncodeError przy budowie naglowka Authorization; tresci tokenu
            # nie ma w komunikacie
            raise blad_pliku(
                "albo zmienna COCKPIT_RUNNER_TOKEN ma token z niedozwolonymi znakami "
                "(dozwolone: drukowalne ASCII bez bialych znakow)")
    except BladSekretow as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(2) from None
    if not token:
        print("Brak tokenu. Wygeneruj go w Ustawieniach i zapisz:", file=sys.stderr)
        print("  COCKPIT_RUNNER_TOKEN=... cockpit-runner", file=sys.stderr)
        raise SystemExit(2)
    base = os.environ.get("COCKPIT_URL", "http://127.0.0.1:8765")
    try:
        asyncio.run(_uruchom(base, token))
    except BladBufora as e:
        print(f"bufor zdarzen: {e}", file=sys.stderr)
        raise SystemExit(2)
    except KokpitNiezgodny as e:
        print(f"kokpit odpowiedzial niezgodnie z kontraktem: {e}", file=sys.stderr)
        raise SystemExit(2)
    except TokenOdrzucony:
        print(
            "token odrzucony przez kokpit - wygeneruj nowy w Ustawieniach "
            "i zapisz go w COCKPIT_RUNNER_TOKEN albo w ~/.config/cockpit/credentials.json",
            file=sys.stderr,
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
