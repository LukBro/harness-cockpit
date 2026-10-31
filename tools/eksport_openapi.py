"""Schemat OpenAPI aplikacji w repozytorium: zapis i sprawdzenie zgodności (HAR-69).

`web/openapi.json` jest kontraktem API: wejściem do generowania typów frontu (HAR-84),
a CI (`--sprawdz`) oblewa się, gdy plik rozjedzie się z kodem. Od tego ticketu każdy
ticket zmieniający API (trasę, model odpowiedzi, kod błędu) regeneruje plik:

    python tools/eksport_openapi.py            # zapisuje web/openapi.json
    python tools/eksport_openapi.py --sprawdz  # kod 1, gdy plik różni się od kodu

Schemat powstaje z samej aplikacji: bez startu serwera, bez bazy i bez zmiennych środowiska.
Wynik jest deterministyczny (posortowane klucze, stałe wcięcie), więc dwa uruchomienia dają
identyczny plik. Brak pliku albo błąd odczytu przy `--sprawdz` to błąd, nie zielone przejście.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

KORZEN = Path(__file__).resolve().parent.parent
# src/ na początku: import ma wziąć kod z tego repozytorium, a nie z innego checkoutu
# zainstalowanego w trybie editable.
sys.path.insert(0, str(KORZEN / "src"))

from fastapi import FastAPI  # noqa: E402

PLIK_SCHEMATU = KORZEN / "web" / "openapi.json"
POLECENIE_NAPRAWY = "python tools/eksport_openapi.py"


def generuj(app: FastAPI | None = None) -> str:
    """Schemat aplikacji jako napis JSON; zawsze świeży, nigdy z cache `app.openapi_schema`."""
    if app is None:
        from cockpit.api.app import app as aplikacja_kokpitu
        app = aplikacja_kokpitu
    app.openapi_schema = None
    schemat = app.openapi()
    return json.dumps(schemat, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _sprawdz(plik: Path, oczekiwany: str) -> int:
    try:
        zapisany = plik.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"Nie można odczytać {plik}: {type(e).__name__}: {e}. "
              f"Wygeneruj plik: {POLECENIE_NAPRAWY}", file=sys.stderr)
        return 1
    if zapisany != oczekiwany:
        print(f"{plik} różni się od schematu aplikacji. "
              f"Zregeneruj plik: {POLECENIE_NAPRAWY}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None, *, plik: Path = PLIK_SCHEMATU,
         app: FastAPI | None = None) -> int:
    parser = argparse.ArgumentParser(description="Eksport schematu OpenAPI do web/openapi.json")
    parser.add_argument("--sprawdz", action="store_true",
                        help="nie zapisuje; kod 1, gdy plik różni się od schematu aplikacji")
    args = parser.parse_args(argv)
    napis = generuj(app)
    if args.sprawdz:
        return _sprawdz(plik, napis)
    plik.write_text(napis, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
