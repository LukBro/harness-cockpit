"""Skille projektu dla API (HAR-180): lista z cache katalogu i szczegoly z pliku przez runnera.

Lista czyta wylacznie tabele `definicja` (BRO-93, HAR-67), wiec dziala takze offline, z data
ostatniego odswiezenia. Szczegoly czytaja biezacy SKILL.md przez runnera PROJEKTU i oddaja
frontmatter i brief bajt w bajt; parsowanie robi `skills.py` (D23), runner oddaje surowy plik.
"""
from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Definicja, Project

from . import projekty
from .errors import BlednySkill, NieZnaleziono
from .katalog import _iso
from .skills import ZRODLA, podziel_skill, skill_z_tekstu
from .zapytania_runnera import Wyslij


def _zapytanie_skilli(projekt: Project) -> Select:
    """Wiersze skilli tego projektu: tego zespolu, jego runnera i jego katalogu.

    Wiersz bez zakresu pochodzi sprzed migracji 0015 (przed odswiezeniem katalogu) - nie jest
    jeszcze skillem w nowym formacie, wiec go tu nie ma."""
    return select(Definicja).where(
        Definicja.tenant_id == projekt.tenant_id, Definicja.runner_id == projekt.runner_id,
        Definicja.projekt == projekt.path, Definicja.rodzaj == "skill",
        Definicja.zakres.is_not(None))


def _zrodlo_scisle(sciezka: str) -> bool:
    """Czy sciezka z cache (repo wzgledna, dom z `~/`) lezy w zrodle w modelu scislym kokpitu."""
    wzgledna = sciezka.removeprefix("~/")
    return any(scisle and wzgledna.startswith(f"{podkatalog}/") for _, podkatalog, scisle in ZRODLA)


async def rejestr_projektu(s: AsyncSession, tenant_id: str, project_id: str) -> dict:
    """Rozstrzygniety rejestr skilli projektu z cache, posortowany po id; tylko baza."""
    projekt = await projekty.projekt_zespolu(s, tenant_id, project_id)
    wiersze = sorted(
        (await s.execute(_zapytanie_skilli(projekt))).scalars().all(), key=lambda w: w.klucz)
    najstarszy = min((w.odswiezono_at for w in wiersze), default=None)
    return {
        "odswiezono_at": None if najstarszy is None else _iso(najstarszy),
        "skille": [{
            "id": w.klucz, "name": w.nazwa, "role": w.rola, "consumes": w.consumes,
            "produces": w.produces, "zakres": w.zakres, "problem": w.problem,
        } for w in wiersze],
    }


async def skill_projektu(
    s: AsyncSession, tenant_id: str, project_id: str, skill_id: str,
    polaczone: frozenset[str], wyslij: Wyslij,
) -> dict:
    """Szczegoly skilla: pola z biezacego pliku, zakres i sciezka z cache.

    Skill spoza cache projektu to NieZnaleziono (bez pytania runnera); plik, ktory po odswiezeniu
    przestal byc poprawnym skillem, to BlednySkill z komunikatem parsera."""
    projekt = await projekty.projekt_zespolu(s, tenant_id, project_id)
    wiersz = (await s.execute(
        _zapytanie_skilli(projekt).where(Definicja.klucz == skill_id))).scalar_one_or_none()
    if wiersz is None:
        raise NieZnaleziono("skill nieznany")
    tresc = await projekty.czytaj_plik_projektu(
        s, tenant_id, projekt, wiersz.sciezka, polaczone, wyslij)
    skill = skill_z_tekstu(tresc, wiersz.klucz, scisle=_zrodlo_scisle(wiersz.sciezka))
    if skill is None:
        raise BlednySkill(
            f"plik skilla '{wiersz.klucz}' nie jest skillem kokpitu: "
            "brak frontmattera albo pola role")
    # skill_z_tekstu wyzej juz podzielil ten tekst bez bledu, wiec nazwa katalogu (tylko do
    # komunikatu BlednySkill) nie ma tu skutku: mutant argumentu rownowazny
    frontmatter, brief = podziel_skill(tresc, wiersz.klucz)  # pragma: no mutate
    return {
        "id": skill.id, "name": skill.name, "role": skill.role.value,
        "consumes": list(skill.consumes), "produces": list(skill.produces),
        "zakres": wiersz.zakres, "sciezka": wiersz.sciezka,
        "frontmatter": frontmatter, "brief": brief,
    }
