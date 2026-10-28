import asyncio
import os
import sys

from runner.client import Runner, StrumienZamkniety, TokenOdrzucony
from runner.secrets import get_token

# EX_TEMPFAIL: stan przejsciowy - ponowne laczenie dopiero w BRO-75, wiec
# skrypt nadrzedny moze po tym kodzie sprobowac ponownie (kod 0 tego nie wyraza).
EX_TEMPFAIL = 75


def main() -> None:
    token = get_token("runner")
    if not token:
        print("Brak tokenu. Wygeneruj go w Ustawieniach i zapisz:", file=sys.stderr)
        print("  COCKPIT_RUNNER_TOKEN=... cockpit-runner", file=sys.stderr)
        raise SystemExit(2)
    base = os.environ.get("COCKPIT_URL", "http://127.0.0.1:8765")
    try:
        asyncio.run(Runner(base, token).listen())
    except StrumienZamkniety:
        print(
            "strumien zamkniety przez kokpit - koncze prace (kod 75); "
            "ponowne laczenie: BRO-75",
            file=sys.stderr,
        )
        raise SystemExit(EX_TEMPFAIL)
    except TokenOdrzucony:
        print(
            "token odrzucony przez kokpit - wygeneruj nowy w Ustawieniach "
            "i zapisz go w COCKPIT_RUNNER_TOKEN albo w ~/.config/cockpit/credentials.json",
            file=sys.stderr,
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
