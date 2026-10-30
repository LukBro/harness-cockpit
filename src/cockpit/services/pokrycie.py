"""Pokrycie bramkami: zaplanowane, wykonane, pominiete (HAR-64).

Run, w ktorym wszystkie bramki pominieto, ma byc odroznialny od sprawdzonego
(audyt G9; analogia BRO-87: niezmierzona skladowa = niepelna). Funkcja jest czysta:
nie zmienia RunStatus i niczego nie blokuje.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class WynikBramki(StrEnum):
    wykonana = "wykonana"
    nieudana = "nieudana"
    pominieta = "pominieta"


class StanPokrycia(StrEnum):
    sprawdzony = "sprawdzony"
    czesciowo = "czesciowo"
    niesprawdzony = "niesprawdzony"


class PowodPominiecia(StrEnum):
    """Lista D4. `runner.graf.PowodPominiecia` ma te same wartosci, ale import do
    services dalby cykl - ujednolicenie przy HAR-92."""

    brak_capability = "brak_capability"
    pusty_foreach = "pusty_foreach"
    brak_wejscia = "brak_wejscia"
    zaleznosc_nieudana = "zaleznosc_nieudana"


@dataclass(frozen=True)
class WynikKrokuWeryfikujacego:
    krok_id: str
    wynik: WynikBramki
    powod: PowodPominiecia | None = None

    def __post_init__(self) -> None:
        try:
            wynik = WynikBramki(self.wynik)
        except ValueError:
            raise ValueError(f"wynik spoza listy: {self.wynik!r}") from None
        object.__setattr__(self, "wynik", wynik)
        if self.wynik == WynikBramki.pominieta:
            if self.powod is None:
                raise ValueError("pominieta wymaga powodu")
            try:
                powod = PowodPominiecia(self.powod)
            except ValueError:
                raise ValueError(f"powod spoza listy D4: {self.powod!r}") from None
            object.__setattr__(self, "powod", powod)
        elif self.powod is not None:
            raise ValueError("wykonana ani nieudana nie moze miec powodu")


@dataclass(frozen=True)
class PokrycieBramek:
    zaplanowane: int
    wykonane: int
    pominiete: int
    powody: Mapping[str, PowodPominiecia]
    stan: StanPokrycia


def pokrycie_bramek(kroki: Iterable[WynikKrokuWeryfikujacego]) -> PokrycieBramek:
    """Liczy pokrycie bramkami; `nieudana` liczy sie jako wykonana.

    Ktory krok jest weryfikujacy, klasyfikuje wolajacy (HAR-92);
    `zaleznosc_nieudana` wystepuje tylko w runie failed.
    """
    powody: dict[str, PowodPominiecia] = {}
    widziane: set[str] = set()
    wykonane = 0
    for krok in kroki:
        if krok.krok_id in widziane:
            raise ValueError(f"powtorzony krok_id: {krok.krok_id}")
        widziane.add(krok.krok_id)
        if krok.wynik != WynikBramki.pominieta:
            wykonane += 1
        elif krok.powod != PowodPominiecia.zaleznosc_nieudana:
            powody[krok.krok_id] = krok.powod
        # zaleznosc_nieudana to nie nazwane pominiecie bramki: krok nie ruszyl, bo
        # zaleznosc padla (run i tak jest failed). Liczy sie do zaplanowanych, nie do
        # wykonanych ani pominietych, wiec stan nie wyjdzie sprawdzony.

    zaplanowane = len(widziane)
    if wykonane == 0:
        stan = StanPokrycia.niesprawdzony
    elif wykonane == zaplanowane:
        stan = StanPokrycia.sprawdzony
    else:
        stan = StanPokrycia.czesciowo
    return PokrycieBramek(
        zaplanowane=zaplanowane,
        wykonane=wykonane,
        pominiete=len(powody),
        powody=MappingProxyType(powody),
        stan=stan,
    )
