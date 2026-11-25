import argparse
import asyncio
import getpass
import os
import sys

import uvicorn
from sqlalchemy.exc import DBAPIError

from cockpit.db.session import SessionLocal
from cockpit.services import konsola_hasla
from cockpit.services.errors import StanNiemozliwy
from cockpit.settings import settings
from runner.maskowanie import zainstaluj_maskowanie, zarejestruj_sekret

ZMIENNA_HASLA = "COCKPIT_NOWE_HASLO"


class _ParserBezEchaAgumentow(argparse.ArgumentParser):
    """Bledy skladni nie powtarzaja argumentow: ktos moglby wpisac haslo w poleceniu."""

    def error(self, message: str):
        # message pomijamy celowo: zawiera wpisane argumenty
        sys.stderr.write(
            "nieznane lub błędne argumenty; hasła nie podaje się w poleceniu: "
            f"terminal albo {ZMIENNA_HASLA}\n"
        )
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    parser = _ParserBezEchaAgumentow(prog="cockpit")
    podkomendy = parser.add_subparsers(dest="polecenie")
    ustaw = podkomendy.add_parser("ustaw-haslo", help="ustaw nowe haslo uzytkownika")
    # hasla nie bierzemy z argv: widac je w `ps`
    ustaw.add_argument("email")
    return parser


def _pobierz_haslo() -> str | int:
    """Haslo z terminala (dwa razy) albo ze zmiennej; kod wyjscia, gdy sie nie da."""
    if sys.stdin.isatty():
        try:
            pierwsze = getpass.getpass("Nowe hasło: ")
            drugie = getpass.getpass("Powtórz hasło: ")
        except EOFError:
            print("przerwano, nic nie zmieniono", file=sys.stderr)
            return 1
        zarejestruj_sekret(pierwsze)
        zarejestruj_sekret(drugie)
        if pierwsze != drugie:
            print("hasła się różnią; nic nie zmieniono", file=sys.stderr)
            return 1
        return pierwsze
    haslo = os.environ.get(ZMIENNA_HASLA)
    if not haslo:
        print(
            f"brak terminala: podaj nowe hasło w zmiennej {ZMIENNA_HASLA}",
            file=sys.stderr,
        )
        return 2
    zarejestruj_sekret(haslo)
    return haslo


async def _zapisz(maker, email: str, haslo: str) -> bool:
    async with maker() as s:
        return await konsola_hasla.ustaw_haslo_z_konsoli(s, email, haslo)


def _ustaw_haslo(email: str, maker) -> int:
    haslo = _pobierz_haslo()
    if isinstance(haslo, int):
        return haslo
    try:
        znaleziono = asyncio.run(_zapisz(maker, email, haslo))
    except StanNiemozliwy as blad:
        # komunikat domeny nie zawiera hasla
        print(f"hasło odrzucone: {blad}", file=sys.stderr)
        return 1
    except (DBAPIError, OSError):
        # stały komunikat: str(blad) niesie parametry zapytania, czyli hash i adres
        print(
            "nie można połączyć się z bazą albo zapytanie się nie powiodło; nic nie zmieniono",
            file=sys.stderr,
        )
        return 1
    if not znaleziono:
        print("nie ma użytkownika o takim adresie", file=sys.stderr)
        return 1
    print("hasło ustawione, wszystkie sesje użytkownika unieważnione")
    return 0


def main(argv: list[str] | None = None, *, maker=None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    zainstaluj_maskowanie()
    if args.polecenie == "ustaw-haslo":
        return _ustaw_haslo(args.email, maker or SessionLocal)
    uvicorn.run("cockpit.api.app:app", host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
