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

# Artefakty kroku `diff` w katalogu runu (`<katalog_runu>/<id kroku>/<nazwa>`).
PLIK_DIFFU = "zmiana.diff"
PLIK_LINII = "zmienione-linie.json"


@dataclass(frozen=True)
class OpisKrokuSystemowego:
    """Kontrakt kroku systemowego; `needs` to capabilities projektu, jak `Skill.needs`."""

    produces: tuple[str, ...]
    consumes: tuple[str, ...] = ()
    needs: tuple[str, ...] = ()


KROKI_SYSTEMOWE: Mapping[str, OpisKrokuSystemowego] = MappingProxyType({
    "diff": OpisKrokuSystemowego(produces=(PLIK_DIFFU, PLIK_LINII)),
})

# Nieznany krok systemowy nic nie produkuje i nic nie konsumuje (zglasza go walidator).
NIEZNANY_OPIS = OpisKrokuSystemowego(produces=())


def opis_kroku_systemowego(system: str | None) -> OpisKrokuSystemowego:
    """Opis kroku o danej nazwie systemowej; nieznana nazwa (albo brak) to opis pusty."""
    if system is None:
        return NIEZNANY_OPIS
    return KROKI_SYSTEMOWE.get(system, NIEZNANY_OPIS)
