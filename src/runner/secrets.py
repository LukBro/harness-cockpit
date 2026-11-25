"""Sekrety u runnera: zmienna srodowiskowa przed plikiem 0600 (BRO-98).

Zero dodatkowych bibliotek. Bez wlasnej kryptografii - szyfrowanie kluczem
lezacym obok pliku to teatr; ochrona to prawa 0600 i to, ze plik nie lezy w repo.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

CONFIG = Path(
    os.environ.get("COCKPIT_CONFIG", Path.home() / ".config" / "cockpit" / "credentials.json")
)


class BladSekretow(ValueError):
    """Plik sekretow runnera jest nieczytelny albo ma zla strukture (HAR-210). Komunikat
    wskazuje plik do naprawy; nigdy nie niesie tresci pliku ani `str()` wyjatku zrodlowego
    (moga niesc sekret).

    Dziedziczy po `ValueError`, bo adaptery ticketow (`runner.tickety.*`) lapia
    `(OSError, ValueError, AttributeError)` wokol `get_token` i tlumacza to na `brak_tokenu`."""


def blad_pliku(powod: str) -> BladSekretow:
    """`BladSekretow` z komunikatem wskazujacym plik do naprawy (tez dla wolajacych spoza modulu)."""
    return BladSekretow(f"plik sekretow {CONFIG} {powod}: napraw go albo usun")


_blad_pliku = blad_pliku


def _wczytaj() -> dict | None:
    """Slownik z pliku sekretow; `None`, gdy pliku nie ma. Kazda inna przyczyna to `BladSekretow`."""
    try:
        tryb = CONFIG.stat().st_mode
    except FileNotFoundError:
        return None
    except OSError as e:
        # takze ENOTDIR i ELOOP: `is_file()` zamienialby je w ciche "brak pliku"
        raise _blad_pliku(f"nie daje sie sprawdzic ({type(e).__name__})") from None
    if not stat.S_ISREG(tryb):      # katalog albo FIFO: open() moglby zawisnac
        raise _blad_pliku("nie jest zwyklym plikiem")
    try:
        tresc = CONFIG.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None                 # zniknal miedzy sprawdzeniem a odczytem
    except OSError as e:
        raise _blad_pliku(f"nie daje sie odczytac ({type(e).__name__})") from None
    except UnicodeDecodeError:
        raise _blad_pliku("nie jest tekstem UTF-8") from None
    try:
        dane = json.loads(tresc)
    except ValueError:      # JSONDecodeError, takze dla pustego pliku
        raise _blad_pliku("nie jest poprawnym JSON") from None
    except RecursionError:
        raise _blad_pliku("jest zagniezdzony ponad limit") from None
    if not isinstance(dane, dict):
        raise _blad_pliku("nie zawiera slownika JSON")
    return dane


def get_token(name: str) -> str | None:
    # import lokalny: maskowanie importuje mask() stad (cykl na poziomie modulu)
    from runner.maskowanie import zarejestruj_sekret

    env = os.environ.get(f"COCKPIT_{name.upper()}_TOKEN")
    if env:
        zarejestruj_sekret(env)
        return env
    dane = _wczytaj()
    if dane is None:
        return None
    wartosc = dane.get(name)
    # wartosc nie-napis wraca bez zmian (kontrakt adapterow ticketow: sami ja odrzucaja jako
    # brak tokenu); `zarejestruj_sekret` ignoruje nie-napisy
    zarejestruj_sekret(wartosc)
    return wartosc


def set_token(name: str, value: str) -> None:
    """Zapis atomowy: plik tymczasowy 0600 od utworzenia, potem os.replace. Plik, ktorego nie
    umiemy przeczytac, zostaje nietkniety (`BladSekretow`): nadpisanie zgubiloby inne wpisy."""
    cel = CONFIG
    try:
        cel.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as e:
        raise _blad_pliku(f"nie daje sie utworzyc katalogu ({type(e).__name__})") from None
    data = _wczytaj() or {}
    data[name] = value
    tmp = cel.with_name(f".{cel.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as e:
        raise _blad_pliku(f"nie daje sie zapisac ({type(e).__name__})") from None
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, cel)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise _blad_pliku(f"nie daje sie zapisac ({type(e).__name__})") from None
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def mask(token: str) -> str:
    return f"{token[:8]}{'*' * 8}" if len(token) > 8 else "*" * 8
