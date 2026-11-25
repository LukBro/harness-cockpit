"""Pilnuje granicy wejścia: limity pól modeli wejścia = długości kolumn (HAR-242).

Pole modelu pydantic o nazwie kolumny `String(n)` ma `max_length <= n`; inaczej PostgreSQL
rzuca `DataError`, a klient dostaje 500 zamiast 422. Runner waliduje zdarzenia tym samym
`EventIn` przed buforem i POST (`src/runner/client.py`) — to sprawdzamy statycznie.
Brak dowodu (puste pary, zero sprawdzonych modeli) to błąd narzędzia, nie zielony wynik.
Uruchamiane w CI i w testach.
"""
from __future__ import annotations

import ast
import sys
import types
import typing
from collections.abc import Sequence
from pathlib import Path

KORZEN = Path(__file__).resolve().parent.parent
KLIENT = KORZEN / "src" / "runner" / "client.py"
NAZWA_MODELU_ZDARZENIA = "EventIn"


class BladNarzedzia(Exception):
    """Narzędzie nie ma czego sprawdzić albo nie umie odczytać wejścia (kod wyjścia 2)."""


def _limity_z_metadanych(metadane) -> list[int]:
    """`max_length` z elementów metadanych; `Field(...)` w `Annotated` niesie je w `.metadata`."""
    limity: list[int] = []
    for m in metadane:
        if getattr(m, "max_length", None) is not None:
            limity.append(m.max_length)
        limity.extend(_limity_z_metadanych(getattr(m, "metadata", None) or ()))
    return limity


def _limit_typu(typ) -> int | None:
    """Największa długość napisu, jaką przepuszcza typ; None = bez limitu.

    `Literal` to najdłuższa wartość, unia to maksimum po gałęziach (gałąź bez limitu daje None),
    napis ma limit z `Annotated`. Typ, którego narzędzie nie umie ocenić, to `BladNarzedzia`."""
    ogon = typing.get_origin(typ)
    if ogon is typing.Annotated:
        baza, *metadane = typing.get_args(typ)
        limity = _limity_z_metadanych(metadane)
        z_bazy = _limit_typu(baza)
        if z_bazy is not None:
            limity.append(z_bazy)
        return min(limity) if limity else None
    if ogon is typing.Literal:
        return max(len(str(w)) for w in typing.get_args(typ))
    if ogon is typing.Union or ogon is types.UnionType:
        galezie = [g for g in typing.get_args(typ) if g is not type(None)]
        limity = [_limit_typu(g) for g in galezie]
        if any(limit is None for limit in limity):
            return None
        return max(limity)
    if typ is str:
        return None
    raise BladNarzedzia(f"nie umiem ocenić limitu typu {typ!r}")


def _max_length(pole) -> int | None:
    """Limit długości pola pydantic: `max_length` z metadanych pola i z typu; None, gdy go nie ma."""
    z_pola = _limity_z_metadanych(pole.metadata)
    z_typu = _limit_typu(pole.annotation)    # typ nieoceniany to BladNarzedzia, bez zastepstwa
    limity = [*z_pola, *([z_typu] if z_typu is not None else [])]
    return min(limity) if limity else None


def _wysylka_bez_walidacji(plik: Path) -> bool:
    """True, gdy plik klienta runnera nie odwołuje się do `EventIn` (statycznie, AST)."""
    try:
        drzewo = ast.parse(plik.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError) as e:      # ValueError: bajt NUL w Pythonie 3.11; obejmuje też UnicodeDecodeError
        raise BladNarzedzia(f"nie można odczytać {plik}: {e}") from e
    return not any(
        isinstance(w, ast.Name) and w.id == NAZWA_MODELU_ZDARZENIA for w in ast.walk(drzewo)
    )


def sprawdz(pary: Sequence[tuple[type, type]], klient: Path = KLIENT) -> list[str]:
    """Naruszenia dla par (model pydantic, model SQLAlchemy); pola mapowane po nazwie."""
    from sqlalchemy import String

    if not pary:
        raise BladNarzedzia("brak par modeli do sprawdzenia")
    naruszenia: list[str] = []
    sprawdzone = 0
    for model, tabela in pary:
        kolumny = tabela.__table__.columns
        pokryte = False
        for nazwa, pole in model.model_fields.items():
            if nazwa not in kolumny or not isinstance(kolumny[nazwa].type, String):
                continue
            dlugosc = kolumny[nazwa].type.length
            if dlugosc is None:
                continue
            pokryte = True
            limit = _max_length(pole)
            if limit is None:
                naruszenia.append(f"{model.__name__}.{nazwa}: String({dlugosc}) bez max_length")
            elif limit > dlugosc:
                naruszenia.append(
                    f"{model.__name__}.{nazwa}: String({dlugosc}) max_length={limit} > {dlugosc}")
        sprawdzone += pokryte
    if sprawdzone == 0:
        raise BladNarzedzia("żaden model nie miał pola odpowiadającego kolumnie String(n)")
    if _wysylka_bez_walidacji(klient):
        naruszenia.append("src/runner/client.py: wysyłka bez walidacji EventIn")
    return naruszenia


def domyslne_pary() -> list[tuple[type, type]]:
    """Modele wejścia i ich tabele; import z tego checkoutu (`src/` na początku)."""
    sys.path.insert(0, str(KORZEN / "src"))
    from cockpit.api.app import RunCreate, RunnerRegister
    from cockpit.api.schematy import EventIn
    from cockpit.db.models import Event, Run, Runner

    return [(EventIn, Event), (RunnerRegister, Runner), (RunCreate, Run)]


def main(argv: list[str] | None = None) -> int:
    try:
        naruszenia = sprawdz(domyslne_pary())
    except (ImportError, BladNarzedzia) as e:
        print(e, file=sys.stderr)
        return 2
    if naruszenia:
        print("Granica wejścia bez limitów:")
        for x in naruszenia:
            print(x)
        return 1
    print("limity pól wejścia zgodne z kolumnami - w porządku")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
