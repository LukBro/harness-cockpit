"""Bufor zdarzen runnera na dysku (HAR-45).

Zdarzenie trafia do bufora PRZED wysylka i znika dopiero po potwierdzeniu przez kokpit,
wiec znikniecie kokpitu albo restart runnera niczego nie gubi. Bufor lezy w katalogu
stanu uzytkownika, poza repozytorium (`run_dir_outside_repo`). Zapisujemy wylacznie
cialo zdarzenia: token i naglowki nigdy nie trafiaja na dysk.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ODCZEKANIE_MAX_S = 30.0
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")      # nazwa pliku: bez "/", ".." i kropek


class BladBufora(Exception):
    """Bufor nie daje sie odczytac ani zapisac (dysk, prawa, uszkodzona struktura)."""


def odczekanie(proba: int) -> float:
    return min(2.0**proba, ODCZEKANIE_MAX_S)


def katalog_stanu() -> Path:
    z_srodowiska = os.environ.get("COCKPIT_STATE_DIR")
    if z_srodowiska:
        return Path(z_srodowiska)
    return Path.home() / ".local" / "state" / "cockpit"


def _wpis(linia: str) -> dict | None:
    """Obiekt JSON z `run_id` i `event_id`; wszystko inne (np. linia obcieta po awarii) to None."""
    try:
        wpis = json.loads(linia)
    except (ValueError, RecursionError):
        return None
    if (
        isinstance(wpis, dict)
        and isinstance(wpis.get("run_id"), str)
        and isinstance(wpis.get("event_id"), str)
    ):
        return wpis
    return None


class Bufor:
    def __init__(self, katalog: Path | None = None) -> None:
        self.katalog = katalog if katalog is not None else katalog_stanu() / "bufor"

    def _plik(self, run_id: object) -> Path:
        # run_id pochodzi z kokpitu: bez sprawdzenia "../x" wyszloby poza katalog bufora
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise BladBufora("niedozwolony run_id zdarzenia")
        return self.katalog / f"{run_id}.ndjson"

    def dopisz(self, zdarzenie: dict) -> None:
        plik = self._plik(zdarzenie.get("run_id"))
        if not isinstance(zdarzenie.get("event_id"), str) or not zdarzenie["event_id"]:
            raise BladBufora("zdarzenie bez event_id")
        dane = (json.dumps(zdarzenie, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.katalog.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(plik, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "ab") as f:
                os.fchmod(fd, 0o600)               # umask nie moze poszerzyc praw
                rozmiar = os.fstat(fd).st_size
                # obcieta po awarii ostatnia linia skleilaby sie z nowym wpisem
                if rozmiar and os.pread(fd, 1, rozmiar - 1) != b"\n":
                    dane = b"\n" + dane
                f.write(dane)
                f.flush()
                os.fsync(fd)
        except OSError as e:
            raise BladBufora(f"zapis {plik.name}: {type(e).__name__}") from e

    def potwierdz(self, run_id: str, event_id: str) -> None:
        plik = self._plik(run_id)
        try:
            tekst = plik.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError) as e:
            raise BladBufora(f"odczyt {plik.name}: {type(e).__name__}") from e
        linie = _linie(tekst)
        pozostale = [
            linia for linia in linie
            if (wpis := _wpis(linia)) is None or wpis["event_id"] != event_id
        ]
        if len(pozostale) == len(linie):
            return
        tmp = plik.with_name(f".{plik.name}.{os.getpid()}.tmp")
        try:
            if not pozostale:
                plik.unlink(missing_ok=True)
                return
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                os.fchmod(fd, 0o600)
                f.write(("\n".join(pozostale) + "\n").encode("utf-8"))
                f.flush()
                os.fsync(fd)
            os.replace(tmp, plik)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise BladBufora(f"zapis {plik.name}: {type(e).__name__}") from e

    def niepotwierdzone(self) -> list[dict]:
        try:
            nazwy = sorted(n for n in os.listdir(self.katalog) if n.endswith(".ndjson"))
        except FileNotFoundError:
            return []
        except OSError as e:
            raise BladBufora(f"katalog bufora: {type(e).__name__}") from e
        wynik: list[dict] = []
        for nazwa in nazwy:
            try:
                tekst = (self.katalog / nazwa).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                raise BladBufora(f"odczyt {nazwa}: {type(e).__name__}") from e
            for numer, linia in enumerate(_linie(tekst), start=1):
                wpis = _wpis(linia)
                if wpis is None:
                    # bez tresci linii: moze niesc dane zdarzenia
                    print(f"bufor: pominiety uszkodzony wpis {nazwa}:{numer}", file=sys.stderr)
                else:
                    wynik.append(wpis)
        return wynik


def _linie(tekst: str) -> list[str]:
    """Dzielimy po "\\n", nie `splitlines()`: tekst zdarzenia moze zawierac np. U+2028."""
    linie = tekst.split("\n")
    if linie[-1] == "":
        linie.pop()
    return linie
