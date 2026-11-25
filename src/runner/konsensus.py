"""Konsensus K z N wariantow kroku z `fanout` (HAR-95): glosuje pierwsza linia pierwszego artefaktu.

Czyste funkcje bez zdarzen i bez kopiowania; silnik (`silnik._konsensus`) sklada z nich wynik kroku."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ZDANY = "zakonczony"
BRAK_WERDYKTU = "brak werdyktu"


@dataclass(frozen=True)
class WynikWariantu:
    status: str
    artefakty: tuple[Path, ...]


@dataclass(frozen=True)
class Rozstrzygniecie:
    zdany: bool
    zwyciezca: str | None
    opis: str


def werdykt(pliki: Sequence[Path]) -> str:
    """Pierwsza niepusta linia pierwszego pliku (casefold); brak, pusty albo nieczytelny plik to ""."""
    if not pliki:
        return ""
    try:
        tresc = pliki[0].read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):   # brak dowodu to brak glosu, nie wyjatek (evidence_required)
        return ""
    return next((linia.strip().casefold() for linia in tresc.splitlines() if linia.strip()), "")


def rozstrzygnij(warianty: Mapping[str, WynikWariantu], k: int) -> Rozstrzygniecie:
    """Krok zdany, gdy najwieksza grupa zgodnych werdyktow zdanych wariantow ma >= k glosow.

    Zwyciezca to pierwszy (w kolejnosci `warianty`) glosujacy z najwieksza grupa; remis grup
    rozstrzyga wiec kolejnosc agentow, nie tresc werdyktu."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError(f"prog konsensusu musi być liczbą całkowitą >= 1, jest {k!r}")
    glosy: dict[str, str] = {}
    opisy = []
    for agent_id, w in warianty.items():
        zdany = w.status == ZDANY
        wd = werdykt(w.artefakty) if zdany else ""
        if wd:
            glosy[agent_id] = wd
        opisy.append(f"{agent_id}: {wd or (BRAK_WERDYKTU if zdany else 'nieudany')}")
    grupy = Counter(glosy.values())
    najwieksza = max(grupy.values(), default=0)
    remis = sum(1 for n in grupy.values() if n == najwieksza) > 1
    opis = "; ".join(opisy) + ("; remis grup" if remis else "")
    if najwieksza < k:
        return Rozstrzygniecie(False, None, f"konsensus {k} niespełniony: {opis}")
    zwyciezca = next(a for a, wd in glosy.items() if grupy[wd] == najwieksza)
    return Rozstrzygniecie(True, zwyciezca, opis)
