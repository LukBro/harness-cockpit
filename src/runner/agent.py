"""Prompt kroku i polecenie agenta u runnera (HAR-33).

Czyste funkcje: bez procesow, bez sieci, bez czytania plikow. Prompt niesie
sciezki, nigdy tresc. Tier i prawo zapisu wynikaja wylacznie z profil_roli.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from cockpit.services.errors import BladDziedziny
from cockpit.services.invariants import Rola, Tier, profil_roli

# Stala wlasna: services.skills ciagnie frontmatter/ruamel/pydantic, a runner ma byc lekki.
# MUSI byc zgodna z KATALOG_RUNU w cockpit/services/skills.py - inaczej agent nie znajdzie skilla.
SKILLE_RUNU = ".cockpit/_run/skills"

DOMYSLNE_MODELE: Mapping[Tier, str] = MappingProxyType({
    Tier.judgment: "opus",
    Tier.execution: "sonnet",
    Tier.retrieval: "haiku",
})

FLAGI_BEZ_ZAPISU: tuple[str, ...] = (
    "--allowedTools", "Read,Grep,Glob",
    "--disallowedTools", "Write,Edit,MultiEdit,NotebookEdit,Bash",
    "--permission-mode", "dontAsk",
)
"""Flagi dla roli bez prawa zapisu (reviewer_readonly).

Zweryfikowane w `claude --help` (Claude Code 2.1.310):
- `--allowedTools` / `--disallowedTools` przyjmuja liste po przecinku jako jedna
  wartosc, bo opcja jest wariadyczna i polknelaby kolejne argumenty;
- `--permission-mode` ma wybory acceptEdits/auto/bypassPermissions/manual/dontAsk/plan;
  `dontAsk` odmawia wszystkiego, czego nie ma na liscie dozwolonych;
- `--disallowedTools` to pas bezpieczenstwa: odmowa ma pierwszenstwo nad zezwoleniem.
"""


class BrakModelu(BladDziedziny):
    """Profil backendu nie ma modelu dla tieru roli."""


@dataclass(frozen=True)
class SpecKroku:
    run_id: str
    step_id: str
    agent_id: str
    skill_id: str
    rola: Rola
    wejscia: list[str]
    wyjscia: list[str]
    komendy: Mapping[str, str]


@dataclass(frozen=True)
class ProfilBackendu:
    """Model per Tier. Pelny profil backendu to BRO-98; tu tylko mapa modeli."""

    modele: Mapping[Tier, str]

    def model(self, tier: Tier) -> str:
        nazwa = self.modele.get(tier)
        if not nazwa:
            raise BrakModelu(f"brak modelu dla tieru {tier.value}")
        return nazwa

    @classmethod
    def z_srodowiska(cls, env: Mapping[str, str] | None = None) -> ProfilBackendu:
        """Domyslne modele, nadpisywane przez COCKPIT_MODEL_<TIER>; pusta wartosc to brak nadpisania."""
        zrodlo = os.environ if env is None else env
        modele = dict(DOMYSLNE_MODELE)
        for tier in modele:
            wartosc = zrodlo.get(f"COCKPIT_MODEL_{tier.value.upper()}", "").strip()
            if wartosc:
                modele[tier] = wartosc
        return cls(modele=MappingProxyType(modele))


def _sekcja(naglowek: str, linie: list[str]) -> list[str]:
    return [naglowek, *(linie or ["- brak"])]


def zbuduj_prompt(spec: SpecKroku) -> str:
    linie = [f"przeczytaj {SKILLE_RUNU}/{spec.skill_id}/SKILL.md i wykonaj", ""]
    linie += _sekcja("wejście:", [f"- {p}" for p in spec.wejscia])
    linie.append("")
    linie += _sekcja("wyjście:", [f"- {p}" for p in spec.wyjscia])
    linie.append("")
    linie += _sekcja("komendy:", [f"{n} = {k}" for n, k in spec.komendy.items()])
    return "\n".join(linie)


def polecenie_agenta(spec: SpecKroku, prompt: str, profil: ProfilBackendu) -> list[str]:
    rola = profil_roli(spec.rola)
    argv = ["claude", "-p", prompt, "--model", profil.model(rola.tier)]
    if not rola.zapis:
        argv += FLAGI_BEZ_ZAPISU
    return argv
