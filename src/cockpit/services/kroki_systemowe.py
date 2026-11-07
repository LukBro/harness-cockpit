"""Kroki systemowe workflow: krok bez agenta, wykonywany przez silnik (HAR-176).

Rejestr opisuje kontrakt kroku (co produkuje, co konsumuje, jakich capabilities wymaga),
tak jak frontmatter opisuje skill. Walidator workflow i graf krokow czytaja go zamiast
rozpoznawac nazwy kroków po literale; wykonawcow rejestruje silnik u runnera
(`runner.silnik.WYKONAWCY_SYSTEMOWI`). Nowy krok systemowy (np. `check`, HAR-177) to wpis
tutaj i wykonawca tam. Modul nie dotyka bazy ani dysku.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cockpit.services.workflow import Krok

# Artefakty kroku `diff` w katalogu runu (`<katalog_runu>/<id kroku>/<nazwa>`).
PLIK_DIFFU = "zmiana.diff"
PLIK_LINII = "zmienione-linie.json"
# Krok `check` (HAR-177) uruchamia komende capability i zapisuje dowod silnika
# `dowod-<capability>.json` w katalogu kroku.
SYSTEM_CHECK = "check"
SZABLON_DOWODU = "dowod-{}.json"


@dataclass(frozen=True)
class OpisKrokuSystemowego:
    """Kontrakt kroku systemowego; `needs` to capabilities projektu, jak `Skill.needs`."""

    produces: tuple[str, ...]
    consumes: tuple[str, ...] = ()
    needs: tuple[str, ...] = ()


KROKI_SYSTEMOWE: Mapping[str, OpisKrokuSystemowego] = MappingProxyType({
    "diff": OpisKrokuSystemowego(produces=(PLIK_DIFFU, PLIK_LINII)),
    # produkcja i capability zaleza od pola `capability` kroku (`opis_kroku`)
    SYSTEM_CHECK: OpisKrokuSystemowego(produces=()),
})

# Nieznany krok systemowy nic nie produkuje i nic nie konsumuje (zglasza go walidator).
NIEZNANY_OPIS = OpisKrokuSystemowego(produces=())


def opis_kroku_systemowego(system: str | None) -> OpisKrokuSystemowego:
    """Opis kroku o danej nazwie systemowej; nieznana nazwa (albo brak) to opis pusty."""
    if system is None:
        return NIEZNANY_OPIS
    return KROKI_SYSTEMOWE.get(system, NIEZNANY_OPIS)


def nazwa_dowodu(capability: str) -> str:
    """Nazwa pliku dowodu silnika dla capability: `dowod-typecheck.json`."""
    return SZABLON_DOWODU.format(capability)


def opis_kroku(krok: Krok) -> OpisKrokuSystemowego:
    """Opis kontraktu kroku systemowego z uwzglednieniem jego pol (`check`: capability).

    Krok `check` z capability produkuje swoj dowod i wymaga tej capability w projekcie;
    bez capability jest nieznany (zgłasza go walidator)."""
    if krok.system == SYSTEM_CHECK:
        if not krok.capability:
            return NIEZNANY_OPIS
        return OpisKrokuSystemowego(
            produces=(nazwa_dowodu(krok.capability),), needs=(krok.capability,))
    return opis_kroku_systemowego(krok.system)
