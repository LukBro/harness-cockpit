"""Pierwszenstwo warstw instrukcji jako byt w kodzie (HAR-87, BRO-96).

Kolejnosc definicji to pierwszenstwo od najwyzszego. Test porownuje ja z lista
w sekcji "Pierwszenstwo warstw" w CLAUDE.md, wiec rozjazd tekstu i kodu oblewa test.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class WarstwaInstrukcji(StrEnum):
    niezmienniki_silnika = "niezmienniki_silnika"
    project_yaml = "project_yaml"
    claude_md_katalogu = "claude_md_katalogu"
    claude_md_korzenia = "claude_md_korzenia"
    skill_md = "skill_md"
    lekcje = "lekcje"


OD_NAJWYZSZEJ: tuple[WarstwaInstrukcji, ...] = tuple(WarstwaInstrukcji)

# Poczatek pozycji listy w CLAUDE.md; tekst po opisie (uzasadnienie) jest dowolny.
OPIS_W_CLAUDE_MD: Mapping[WarstwaInstrukcji, str] = MappingProxyType(
    {
        WarstwaInstrukcji.niezmienniki_silnika: "niezmienniki silnika",
        WarstwaInstrukcji.project_yaml: "`.cockpit/project.yaml`",
        WarstwaInstrukcji.claude_md_katalogu: "`CLAUDE.md` katalogu",
        WarstwaInstrukcji.claude_md_korzenia: "`CLAUDE.md` korzenia",
        WarstwaInstrukcji.skill_md: "`SKILL.md` kroku",
        WarstwaInstrukcji.lekcje: "`.ai/lessons.md`",
    }
)

_POZYCJA = re.compile(r"^(\d+)\. (.*)$")


def _warstwa(wartosc: object) -> WarstwaInstrukcji:
    # StrEnum == str, wiec sam napis o wartosci czlonka przeszedlby porownania.
    if not isinstance(wartosc, WarstwaInstrukcji):
        raise TypeError(f"oczekiwano WarstwaInstrukcji, jest {type(wartosc).__name__}")
    return wartosc


def pierwszenstwo(warstwa: WarstwaInstrukcji) -> int:
    """Numer od 1 (najwyzsza) do 6; zgodny z numeracja listy w CLAUDE.md."""
    return OD_NAJWYZSZEJ.index(_warstwa(warstwa)) + 1


def rozstrzygnij(a: WarstwaInstrukcji, b: WarstwaInstrukcji) -> WarstwaInstrukcji:
    """Warstwa, ktora wygrywa przy sprzecznosci: ta o mniejszym numerze."""
    return a if pierwszenstwo(a) <= pierwszenstwo(b) else b


def lekcja_moze_nadpisac(warstwa: WarstwaInstrukcji) -> bool:
    return rozstrzygnij(WarstwaInstrukcji.lekcje, warstwa) is WarstwaInstrukcji.lekcje


def warstwy_z_claude_md(tekst: str) -> list[WarstwaInstrukcji]:
    """Warstwy z listy numerowanej sekcji "Pierwszenstwo warstw", w kolejnosci tekstu."""
    sekcja: list[str] = []
    w_sekcji = False
    for linia in tekst.splitlines():
        if linia.startswith("## Pierwszeństwo warstw"):
            w_sekcji = True
        elif linia.startswith("## "):
            if w_sekcji:
                break
        elif w_sekcji:
            sekcja.append(linia)
    if not w_sekcji:
        raise ValueError("CLAUDE.md nie ma sekcji „Pierwszeństwo warstw”")

    wynik: list[WarstwaInstrukcji] = []
    for linia in sekcja:
        trafienie = _POZYCJA.match(linia)
        if trafienie is None:
            continue
        numer, tresc = int(trafienie.group(1)), trafienie.group(2)
        if numer != len(wynik) + 1:
            raise ValueError(f"numeracja listy warstw nie jest kolejna od 1: {linia!r}")
        for warstwa, opis in OPIS_W_CLAUDE_MD.items():
            if tresc.startswith(opis):
                wynik.append(warstwa)
                break
        else:
            raise ValueError(f"pozycja listy nie pasuje do zadnej warstwy: {linia!r}")
    return wynik
