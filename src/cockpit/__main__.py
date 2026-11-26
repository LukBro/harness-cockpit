import argparse
import asyncio
import getpass
import os
import sys
import threading
import time

import uvicorn
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from cockpit.services import konsola_hasla
from cockpit.services.errors import StanNiemozliwy
from cockpit.settings import settings
from runner.maskowanie import zainstaluj_maskowanie, zarejestruj_sekret

# bez importu cockpit.db.session: tworzy silnik przy imporcie, więc zły adres bazy
# kończyłby polecenie tracebackiem, zanim cokolwiek zdąży go obsłużyć

ZMIENNA_HASLA = "COCKPIT_NOWE_HASLO"

# zamknięta lista: sterowniki, dla których sprawdzono, jakie błędy dają złe adresy
_DOZWOLONE_STEROWNIKI = frozenset({"postgresql+asyncpg", "sqlite+aiosqlite"})

# wspólny limit na zakończenie wszystkich wątków sterownika bazy
_LIMIT_CZEKANIA_NA_STEROWNIK_S = 5.0


class _NiepoprawnyAdresBazy(Exception):
    """COCKPIT_DATABASE_URL nie pozwala zbudować silnika albo otworzyć połączenia."""


class _SterownikNieZakonczyl(Exception):
    """Wątek sterownika bazy żyje po limicie; wynik zapisu hasła jest niepewny."""


# błędy, które polecenie samo tłumaczy na komunikat: przy żywym wątku sterownika tylko one
# ustępują wynikowi niepewnemu, każdy inny wyjątek przechodzi bez zmian
_BLEDY_ZASTEPOWANE_WYNIKIEM_NIEPEWNYM = (
    StanNiemozliwy,
    _NiepoprawnyAdresBazy,
    DBAPIError,
    OSError,
)


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
    # zamknięte wejście (`0<&-`) daje sys.stdin == None
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            pierwsze = getpass.getpass("Nowe hasło: ")
            drugie = getpass.getpass("Powtórz hasło: ")
        except EOFError:
            print("przerwano, nic nie zmieniono", file=sys.stderr)
            return 1
        except UnicodeDecodeError:
            # stały komunikat: str(blad) zawiera bajt wpisanego hasła
            sys.stderr.write(
                "hasło zawiera znaki spoza kodowania terminala; nic nie zmieniono\n"
            )
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


def _utworz_silnik() -> AsyncEngine:
    # from None: treść wyjątku może powtarzać adres razem z hasłem do bazy
    try:
        adres = make_url(settings.database_url)
    except (ArgumentError, ValueError):
        raise _NiepoprawnyAdresBazy from None
    # `loop` z adresu trafia do asyncpg jako napis i kończy się AttributeError
    if adres.drivername not in _DOZWOLONE_STEROWNIKI or "loop" in adres.query:
        raise _NiepoprawnyAdresBazy
    try:
        # int() jak w dialekcie, więc obejmuje każdy zapis liczby, który ten przyjmie;
        # ujemny rozmiar zapętla LRUCache w SQLAlchemy: proces wisi bez wyjątku
        if int(adres.query.get("prepared_statement_cache_size", 0)) < 0:
            raise _NiepoprawnyAdresBazy
        return create_async_engine(settings.database_url, future=True)
    except (ArgumentError, TypeError, ValueError):
        # TypeError to powtórzony parametr liczbowy
        raise _NiepoprawnyAdresBazy from None


async def _zapisz_w_sesji(maker, email: str, haslo: str) -> bool:
    async with maker() as s:
        try:
            # parametry z adresu sterownik sprawdza dopiero przy połączeniu;
            # OverflowError to m.in. port spoza 0-65535
            await s.connection()
        except (TypeError, ValueError, OverflowError):
            raise _NiepoprawnyAdresBazy from None
        return await konsola_hasla.ustaw_haslo_z_konsoli(s, email, haslo)


def _pomijaj_wyjatki_watkow(porzucone: set[threading.Thread]) -> None:
    # wątek porzucony po limicie trafi potem na zamkniętą pętlę, a jego traceback
    # nakładałby się na stały komunikat albo przerywał zamykanie interpretera
    poprzedni = threading.excepthook

    def filtr(args: threading.ExceptHookArgs) -> None:
        # tożsamość, nie nazwa: wyjątek każdego innego wątku ma być nadal widoczny
        if not any(args.thread is watek for watek in porzucone):
            poprzedni(args)

    threading.excepthook = filtr


def _dolacz_watki_sterownika(przed: set[threading.Thread]) -> set[threading.Thread]:
    """Czeka na nowe wątki sterownika; zwraca te, które żyją po limicie."""
    # tylko nowe wątki demoniczne: tak SQLAlchemy oznacza wątek aiosqlite, a wątki
    # executora asyncio nie są demoniczne i żyją aż do końca asyncio.run
    nowe = [w for w in threading.enumerate() if w not in przed and w.daemon]
    koniec = time.monotonic() + _LIMIT_CZEKANIA_NA_STEROWNIK_S
    for watek in nowe:
        # ujemny czas po terminie join traktuje jak zero
        watek.join(koniec - time.monotonic())
    zywe = {watek for watek in nowe if watek.is_alive()}
    if zywe:
        # już tutaj, przy otwartej pętli: po jej zamknięciu wątek może rzucić od razu
        _pomijaj_wyjatki_watkow(zywe)
    return zywe


async def _zapisz(maker, email: str, haslo: str) -> bool:
    if maker is not None:
        return await _zapisz_w_sesji(maker, email, haslo)
    watki_przed = set(threading.enumerate())
    zywe: set[threading.Thread] = set()
    silnik = None
    try:
        try:
            silnik = _utworz_silnik()
            wlasny = async_sessionmaker(silnik, expire_on_commit=False)
            znaleziono = await _zapisz_w_sesji(wlasny, email, haslo)
        finally:
            try:
                if silnik is not None:
                    await silnik.dispose()
            finally:
                # przed końcem asyncio.run: po nieudanym połączeniu wątek aiosqlite jeszcze
                # sprząta i na zamkniętej pętli kończy się tracebackiem albo przerwaniem
                # procesu
                zywe = await asyncio.to_thread(_dolacz_watki_sterownika, watki_przed)
    except _BLEDY_ZASTEPOWANE_WYNIKIEM_NIEPEWNYM as blad:
        if zywe:
            raise _SterownikNieZakonczyl from blad
        raise
    if zywe:
        raise _SterownikNieZakonczyl
    return znaleziono


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
    except _NiepoprawnyAdresBazy:
        sys.stderr.write(
            "niepoprawny adres bazy w COCKPIT_DATABASE_URL; nic nie zmieniono\n"
        )
        return 1
    except _SterownikNieZakonczyl:
        # bez „nic nie zmieniono”: hasło mogło zostać już zatwierdzone
        sys.stderr.write(
            "sterownik bazy nie zakończył pracy w czasie; "
            "sprawdź, czy hasło zostało zmienione\n"
        )
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
        return _ustaw_haslo(args.email, maker)
    uvicorn.run("cockpit.api.app:app", host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
