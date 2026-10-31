"""Twardy zakres sciezek runnera: katalogi projektow i rejestr skilli (HAR-29, BRO-93, BRO-98).

Zakres pochodzi z pliku na maszynie uzytkownika, nigdy z kokpitu - inaczej kokpit
moglby go poszerzyc. Kazda odmowa idzie przez `_odmow`: linia na stderr, potem wyjatek.
Modul nie czyta tresci plikow docelowych, wiec nie ma jak jej ujawnic.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path

from cockpit.services.errors import BladDziedziny
from cockpit.services.skills import ZRODLA

# Wzorce sekretow, porownywane na malych literach (D14).
_SEKRETY = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*")
_NIE_PLIK = {"", ".", ".."}


def _ostrzez(tekst: str) -> None:
    # sys.stderr czytany w chwili zapisu (nie przy imporcie) - podmiana w testach i w runnerze.
    czas = datetime.now(timezone.utc).isoformat()
    sys.stderr.write(f"{czas} runner zakres: {tekst}\n")


class PozaZakresem(BladDziedziny):
    """Sciezka poza zakresem albo objeta zakazem (.git, sekrety). Bez tresci pliku."""

    def __init__(self, sciezka, powod: str) -> None:
        self.sciezka = sciezka
        self.powod = powod
        super().__init__(f"sciezka poza zakresem ({powod}): {str(sciezka)!r}")


def _odmow(sciezka, powod: str) -> PozaZakresem:
    """Jedyne miejsce, w ktorym powstaje odmowa: wpis w logu PRZED wyjatkiem."""
    _ostrzez(f"odmowa powod={powod} sciezka={str(sciezka)!r}")
    return PozaZakresem(sciezka, powod)


def _plik_konfiguracji() -> Path:
    # Liczony przy wywolaniu: zmiana HOME / COCKPIT_RUNNER_CONFIG dziala bez reimportu.
    env = os.environ.get("COCKPIT_RUNNER_CONFIG")
    if env:
        return Path(env)
    return Path.home() / ".config" / "cockpit" / "runner.json"


def _jest_sekretem(nazwa: str) -> bool:
    n = nazwa.lower()
    return any(fnmatchcase(n, wzorzec) for wzorzec in _SEKRETY)


class Zakres:
    def __init__(self, korzenie, projekty=()) -> None:
        self._korzenie = tuple(Path(k).resolve() for k in korzenie)
        self._projekty = tuple(Path(k).resolve() for k in projekty)

    @property
    def korzenie(self) -> tuple[Path, ...]:
        return self._korzenie

    @property
    def projekty(self) -> tuple[Path, ...]:
        """Korzenie projektow z `runner.json`, bez rejestru domowego (HAR-67)."""
        return self._projekty

    @classmethod
    def z_konfiguracji(cls, plik: Path | str | None = None) -> Zakres:
        sciezka = Path(plik) if plik is not None else _plik_konfiguracji()
        try:
            dane = json.loads(sciezka.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls([])  # brak pliku = brak zakresu, to nie awaria
        except (OSError, ValueError, RecursionError) as e:  # UnicodeDecodeError to ValueError
            _ostrzez(f"konfiguracja {str(sciezka)!r} nieczytelna ({type(e).__name__}), zakres pusty")
            return cls([])
        projekty = dane.get("projekty") if isinstance(dane, dict) else None
        if not isinstance(projekty, list):
            _ostrzez(f"konfiguracja {str(sciezka)!r} ma zla strukture, zakres pusty")
            return cls([])
        korzenie: list[Path] = []
        for wpis in projekty:
            korzen = cls._korzen_projektu(wpis)
            if korzen is not None:
                korzenie.append(korzen)
        projekty = list(korzenie)
        dom = Path.home()
        korzenie += [dom / podkatalog for baza, podkatalog, _ in ZRODLA if baza == "dom"]
        return cls(korzenie, projekty)

    @staticmethod
    def _korzen_projektu(wpis) -> Path | None:
        """Wpis do korzenia albo None (z linia na stderr) - zly wpis nie psuje pozostalych."""
        if not isinstance(wpis, str) or "\0" in wpis or not os.path.isabs(wpis):
            _ostrzez(f"pominieto wpis projektow {wpis!r}: nie jest bezwzgledna sciezka")
            return None
        try:
            korzen = Path(wpis).resolve()
        except (OSError, RuntimeError):
            _ostrzez(f"pominieto wpis projektow {wpis!r}: nie da sie rozwiazac")
            return None
        if korzen.parent == korzen:  # korzen systemu plikow otwieralby caly dysk
            _ostrzez(f"pominieto wpis projektow {wpis!r}: to korzen systemu plikow")
            return None
        return korzen

    def _w_zakresie(self, cel: Path) -> bool:
        return any(cel.is_relative_to(k) for k in self._korzenie)

    def _rozwiaz(self, sciezka, p: Path) -> Path:
        # resolve() moze rzucic (petla dowiazan, OSError) - fail-closed.
        try:
            return p.resolve()
        except (OSError, RuntimeError, ValueError):
            raise _odmow(sciezka, "poza_zakresem") from None

    def rozwiaz(self, sciezka, *, do_zapisu: bool = False) -> Path:
        tekst = str(sciezka)
        if "\0" in tekst:
            raise _odmow(sciezka, "nul")
        zadana = Path(tekst)
        if not zadana.is_absolute():
            raise _odmow(sciezka, "wzgledna")

        if not do_zapisu:
            cel = self._rozwiaz(sciezka, zadana)
            if not self._w_zakresie(cel):
                raise _odmow(sciezka, "poza_zakresem")
        else:
            nazwa = zadana.name
            if nazwa in _NIE_PLIK:
                raise _odmow(sciezka, "nie_plik")
            rodzic = self._rozwiaz(sciezka, zadana.parent)
            if not self._w_zakresie(rodzic):
                raise _odmow(sciezka, "poza_zakresem")
            cel = rodzic / nazwa
            if cel.is_symlink():  # tez wiszace: zapis poszedlby tam, gdzie wskazuje
                raise _odmow(sciezka, "dowiazanie")
            if not self._w_zakresie(self._rozwiaz(sciezka, cel)):
                raise _odmow(sciezka, "poza_zakresem")
            # no_git_write ma pierwszenstwo przed sekretem; odczyt .git/ jest dozwolony.
            for k in self._korzenie:
                if cel.is_relative_to(k) and any(
                    c.lower() == ".git" for c in cel.relative_to(k).parts
                ):
                    raise _odmow(sciezka, "no_git_write")

        if _jest_sekretem(zadana.name) or _jest_sekretem(cel.name):
            raise _odmow(sciezka, "sekret")
        return cel
