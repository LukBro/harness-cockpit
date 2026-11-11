"""Prompt kroku i polecenie agenta u runnera (HAR-33).

Prompt niesie sciezki, nigdy tresc, i nie trafia do argv (idzie plikiem jako stdin
agenta, HAR-174). Tier i prawo zapisu wynikaja wylacznie z profil_roli. Jedyny odczyt
pliku to `backendy.json` w `ProfilBackendu.z_konfiguracji`.
"""
from __future__ import annotations

import json
import math
import os
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from cockpit.services.errors import BladDziedziny
from cockpit.services.invariants import Rola, Tier, profil_roli

# Stala wlasna: services.skills ciagnie frontmatter/ruamel/pydantic, a runner ma byc lekki.
# MUSI byc zgodna z KATALOG_RUNU w cockpit/services/skills.py - inaczej agent nie znajdzie skilla.
SKILLE_RUNU = ".cockpit/_run/skills"

# Zl/Zp (separatory linii i akapitu) tez lamia linie promptu, a str.isprintable()
# odrzucilaby takze NBSP i znaki formatowania (Cf), ktore sa poprawne w nazwach.
KATEGORIE_STERUJACE = frozenset({"Cc", "Zl", "Zp"})

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


PROFIL_DOMYSLNY: Mapping[str, Mapping[Tier, str]] = MappingProxyType({
    "claude": DOMYSLNE_MODELE,
})
"""Profil domyslny per backend; jedyny znany backend to `claude`."""

ZDANIE_KONTEKSTU = "Przeczytaj te pliki; przy sprzeczności instrukcji wygrywa plik wyżej na liście."

NAZWA_PLIKU_PROFILU = "backendy.json"

FLAGI_STRUMIENIA: tuple[str, ...] = ("--output-format", "stream-json", "--verbose")
"""Strumien zdarzen NDJSON (HAR-174). Zweryfikowane na Claude Code 2.1.319: `--output-format
stream-json` wymaga `--verbose` w trybie `-p`. Bez argumentu promptu `claude -p` czyta prompt
ze stdin."""

FLAGA_BUDZETU = "--max-budget-usd"
FLAGA_TUR = "--max-turns"
"""`--max-budget-usd <amount>` jest w `claude --help` (2.1.319). `--max-turns <turns>` istnieje
w 2.1.319, ale jest UKRYTA w `--help` (tekst w binarce: "Maximum number of agentic turns in
non-interactive mode"); po zmianie wersji CLI sprawdz, czy flaga nadal dziala."""


class BrakModelu(BladDziedziny):
    """Profil backendu nie ma modelu dla tieru roli."""


class BladProfiluBackendu(BladDziedziny):
    """Nieznany backend albo uszkodzony/bledny plik `backendy.json`; tekst to powod."""


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
    budget_usd: float | None = None
    max_turns: int | None = None
    kontekst: tuple[str, ...] = ()     # sciezki warstw instrukcji w kolejnosci pierwszenstwa (HAR-169)


def sciezka_profilu(env: Mapping[str, str] | None = None) -> Path:
    """Plik profilu: COCKPIT_BACKENDY (niepusta) > obok COCKPIT_CONFIG > ~/.config/cockpit."""
    zrodlo = os.environ if env is None else env
    if wskazany := zrodlo.get("COCKPIT_BACKENDY"):
        return Path(wskazany)
    if konfiguracja := zrodlo.get("COCKPIT_CONFIG"):
        return Path(konfiguracja).parent / NAZWA_PLIKU_PROFILU
    return Path.home() / ".config" / "cockpit" / NAZWA_PLIKU_PROFILU


def _nadpisania_z_pliku(plik: Path, backend: str) -> dict[Tier, str]:
    """Modele tierow backendu z pliku; brak pliku to brak nadpisan, kazdy inny blad to
    `BladProfiluBackendu`."""
    try:
        tresc = plik.read_bytes().decode()
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as blad:
        raise BladProfiluBackendu(f"plik profilu backendu {plik}: {blad}") from blad
    try:
        dane = json.loads(tresc)
    except (ValueError, RecursionError) as blad:
        raise BladProfiluBackendu(f"plik profilu backendu {plik}: {blad}") from blad
    if not isinstance(dane, dict):
        raise BladProfiluBackendu(
            f"plik profilu backendu {plik}: oczekiwano obiektu z backendami"
        )
    if backend not in dane:
        return {}
    wpis = dane[backend]
    if not isinstance(wpis, dict):
        raise BladProfiluBackendu(
            f"plik profilu backendu {plik}: backend {backend!r} musi byc obiektem tierow"
        )
    tiery = {t.value: t for t in Tier}
    nadpisania: dict[Tier, str] = {}
    for nazwa, model in wpis.items():
        if nazwa not in tiery:
            raise BladProfiluBackendu(
                f"plik profilu backendu {plik}: nieznany tier {nazwa!r} backendu {backend!r}"
            )
        if not isinstance(model, str) or not model:
            raise BladProfiluBackendu(
                f"plik profilu backendu {plik}: model tieru {nazwa!r} backendu {backend!r} "
                "musi byc niepustym napisem"
            )
        nadpisania[tiery[nazwa]] = model
    return nadpisania


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

    @classmethod
    def z_konfiguracji(
        cls,
        backend: str = "claude",
        env: Mapping[str, str] | None = None,
        plik: Path | None = None,
    ) -> ProfilBackendu:
        """Modele: profil domyslny < plik `backendy.json` < COCKPIT_MODEL_<TIER> (niepusta).

        Plik nadpisuje pojedyncze tiery i jest czytany przy kazdym wywolaniu. Nieznany
        backend i bledny plik to `BladProfiluBackendu`; brak pliku to profil domyslny."""
        if backend not in PROFIL_DOMYSLNY:
            raise BladProfiluBackendu(f"nieznany backend: {backend}")
        zrodlo = os.environ if env is None else env
        modele = dict(PROFIL_DOMYSLNY[backend])
        modele.update(_nadpisania_z_pliku(
            plik if plik is not None else sciezka_profilu(zrodlo), backend))
        for tier in modele:
            wartosc = zrodlo.get(f"COCKPIT_MODEL_{tier.value.upper()}", "").strip()
            if wartosc:
                modele[tier] = wartosc
        return cls(modele=MappingProxyType(modele))


def _sekcja(naglowek: str, linie: list[str]) -> list[str]:
    return [naglowek, *(linie or ["- brak"])]


def _bez_znakow_sterujacych(pole: str, wartosc: str) -> None:
    if any(unicodedata.category(znak) in KATEGORIE_STERUJACE for znak in wartosc):
        raise ValueError(f"pole {pole} zawiera znak sterujący: {wartosc!r}")


def _sprawdz_pola(spec: SpecKroku) -> None:
    _bez_znakow_sterujacych("skill_id", spec.skill_id)
    for i, sciezka in enumerate(spec.wejscia):
        _bez_znakow_sterujacych(f"wejscia[{i}]", sciezka)
    for i, sciezka in enumerate(spec.wyjscia):
        _bez_znakow_sterujacych(f"wyjscia[{i}]", sciezka)
    for i, sciezka in enumerate(spec.kontekst):
        _bez_znakow_sterujacych(f"kontekst[{i}]", sciezka)
    for nazwa, komenda in spec.komendy.items():
        _bez_znakow_sterujacych("komendy", nazwa)
        _bez_znakow_sterujacych(f"komendy[{nazwa}]", komenda)


def zbuduj_prompt(spec: SpecKroku) -> str:
    _sprawdz_pola(spec)
    linie = [f"przeczytaj {SKILLE_RUNU}/{spec.skill_id}/SKILL.md i wykonaj", ""]
    linie += _sekcja("wejście:", [f"- {p}" for p in spec.wejscia])
    linie.append("")
    linie += _sekcja("wyjście:", [f"- {p}" for p in spec.wyjscia])
    linie.append("")
    linie += _sekcja("komendy:", [f"{n} = {k}" for n, k in spec.komendy.items()])
    if spec.kontekst:
        linie += ["", "kontekst (w kolejności pierwszeństwa):", *(f"- {p}" for p in spec.kontekst)]
        linie.append(ZDANIE_KONTEKSTU)
    return "\n".join(linie)


def _budzet_do_argv(budzet: object) -> str:
    # bool jest podklasa int, ale nie jest liczba budzetu
    if isinstance(budzet, bool) or not isinstance(budzet, (int, float)):
        raise ValueError(f"budget_usd musi być liczbą, jest {budzet!r}")
    if not math.isfinite(budzet) or budzet <= 0:
        raise ValueError(f"budget_usd musi być skończony i dodatni, jest {budzet!r}")
    return str(float(budzet))


def _tury_do_argv(tury: object) -> str:
    if isinstance(tury, bool) or not isinstance(tury, int):
        raise ValueError(f"max_turns musi być liczbą całkowitą, jest {tury!r}")
    if tury < 1:
        raise ValueError(f"max_turns musi być dodatni, jest {tury!r}")
    return str(tury)


def polecenie_agenta(
    spec: SpecKroku,
    profil: ProfilBackendu,
    ustawienia: Path | None = None,
) -> list[str]:
    """Polecenie `claude` bez promptu (ten idzie plikiem jako stdin agenta, HAR-174).

    `ustawienia` to plik hookow kroku (HAR-175), podawany przez --settings. Niepoprawny
    budzet albo limit tur kroku to `ValueError`."""
    rola = profil_roli(spec.rola)
    argv = ["claude", "-p", "--model", profil.model(rola.tier), *FLAGI_STRUMIENIA]
    if spec.budget_usd is not None:
        argv += [FLAGA_BUDZETU, _budzet_do_argv(spec.budget_usd)]
    if spec.max_turns is not None:
        argv += [FLAGA_TUR, _tury_do_argv(spec.max_turns)]
    if ustawienia is not None:
        argv += ["--settings", str(ustawienia)]
    if not rola.zapis:
        argv += FLAGI_BEZ_ZAPISU
    return argv
