"""Model pliku projektu (.cockpit/project.yaml): stack, tickets i capabilities.

Plik opisuje repozytorium uzytkownika, wiec wczytanie nigdy nie rzuca wyjatku na
bledne dane: zwraca `Projekt` (albo `None`) i liste `Problem` z workflow.py.
Capability to mapa `nazwa: komenda` (D15); komendy nie dzielimy ani nie wykonujemy,
a ich tresci nie wpisujemy do komunikatow (moga zawierac sekret). Tylko ruamel.yaml.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import YAMLError

from cockpit.services.workflow import Poziom, Problem

PLIK_PROJEKTU = Path(".cockpit") / "project.yaml"

# Kody problemow. Testy dopasowuja sie po nich, nie po tresci komunikatow.
BRAK_PROJEKTU = "brak_projektu"
BLAD_ODCZYTU = "blad_odczytu"
NIEPOPRAWNY_YAML = "niepoprawny_yaml"
ZDUPLIKOWANY_KLUCZ = "zduplikowany_klucz"
NIE_MAPA = "nie_mapa"
BLAD_POLA = "blad_pola"
BLEDNA_CAPABILITY = "bledna_capability"

_ZA_GLEBOKO = "niepoprawny YAML: zbyt gleboko zagniezdzona struktura"


class Projekt(BaseModel):
    stack: list[str] = []
    # surowa konfiguracja zrodla ticketow; jej kszalt ustala BRO-111
    tickets: dict[str, Any] | None = None
    capabilities: dict[str, str] = {}


def _lokalizacja(exc: YAMLError) -> str:
    znacznik = getattr(exc, "problem_mark", None)
    return f" (linia {znacznik.line + 1})" if znacznik is not None else ""


def _zwykle(wartosc: Any) -> Any:
    """Zamienia mapy i listy ruamel na zwykle `dict` i `list` (klucze jako napisy)."""
    if isinstance(wartosc, dict):
        return {str(k): _zwykle(v) for k, v in wartosc.items()}
    if isinstance(wartosc, list):
        return [_zwykle(v) for v in wartosc]
    return wartosc


def _zbuduj(mapa: CommentedMap) -> tuple[Projekt, list[Problem]]:
    problemy: list[Problem] = []

    stack_surowy = mapa.get("stack", [])
    stack: list[str] = []
    if isinstance(stack_surowy, list) and all(isinstance(s, str) for s in stack_surowy):
        stack = list(stack_surowy)
    else:
        problemy.append(Problem("stack", BLAD_POLA, "stack musi byc lista napisow"))

    tickets_surowe = mapa.get("tickets")
    tickets = None
    if isinstance(tickets_surowe, CommentedMap):
        tickets = _zwykle(tickets_surowe)
    elif "tickets" in mapa:
        problemy.append(Problem("tickets", BLAD_POLA, "tickets musi byc mapa"))

    capabilities_surowe = mapa.get("capabilities")
    capabilities: dict[str, str] = {}
    if isinstance(capabilities_surowe, CommentedMap):
        for nazwa, komenda in capabilities_surowe.items():
            if not isinstance(nazwa, str):
                problemy.append(Problem(str(nazwa), BLEDNA_CAPABILITY, "nazwa capability musi byc napisem"))
            elif not isinstance(komenda, str) or not komenda.strip():
                opis = "pusta" if isinstance(komenda, str) else f"typu {type(komenda).__name__}"
                problemy.append(
                    Problem(nazwa, BLEDNA_CAPABILITY, f"komenda capability {nazwa} musi byc niepustym napisem ({opis})")
                )
            else:
                capabilities[nazwa] = komenda
    elif "capabilities" in mapa:
        problemy.append(Problem("capabilities", BLAD_POLA, "capabilities musi byc mapa nazwa: komenda"))

    return Projekt(stack=stack, tickets=tickets, capabilities=capabilities), problemy


def wczytaj_projekt(repo: Path) -> tuple[Projekt | None, list[Problem]]:
    """Wczytuje projekt. Brak pliku to `Projekt()` z ostrzezeniem; `None` tylko przy bledzie calego pliku."""
    try:
        tekst = (repo / PLIK_PROJEKTU).read_bytes().decode("utf-8")
    except FileNotFoundError:
        return Projekt(), [
            Problem(None, BRAK_PROJEKTU, f"brak pliku {PLIK_PROJEKTU.as_posix()}", Poziom.ostrzezenie)
        ]
    except (OSError, UnicodeDecodeError) as exc:
        return None, [Problem(None, BLAD_ODCZYTU, f"nie mozna odczytac pliku projektu: {exc}")]

    try:
        mapa = YAML().load(tekst)
        if not isinstance(mapa, CommentedMap):
            return None, [Problem(None, NIE_MAPA, "project.yaml musi byc mapa")]
        return _zbuduj(mapa)
    except RecursionError:
        return None, [Problem(None, NIEPOPRAWNY_YAML, _ZA_GLEBOKO)]
    except DuplicateKeyError as exc:
        return None, [Problem(None, ZDUPLIKOWANY_KLUCZ, f"zduplikowany klucz{_lokalizacja(exc)}")]
    except YAMLError as exc:
        return None, [Problem(None, NIEPOPRAWNY_YAML, f"niepoprawny YAML{_lokalizacja(exc)}")]
