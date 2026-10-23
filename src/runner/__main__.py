import asyncio
import os
import sys

from runner.client import Runner
from runner.secrets import get_token


def main() -> None:
    token = get_token("runner")
    if not token:
        print("Brak tokenu. Wygeneruj go w Ustawieniach i zapisz:", file=sys.stderr)
        print("  COCKPIT_RUNNER_TOKEN=... cockpit-runner", file=sys.stderr)
        raise SystemExit(2)
    base = os.environ.get("COCKPIT_URL", "http://127.0.0.1:8765")
    asyncio.run(Runner(base, token).listen())


if __name__ == "__main__":
    main()
