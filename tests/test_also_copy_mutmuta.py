"""Pliki czytane przez testy sa kopiowane do mutants/ (audyt procesu 2026-11-19).

mutmut kopiuje do mutants/ tylko `source_paths` i `also_copy`. Test, ktory czyta plik spoza tej listy, pod
mutmutem pada i bramka mutacji konczy sie kodem 2 dopiero w CI albo u krytyka (HAR-169, HAR-242: brak
`.claude/skills/`). Ten test zapadkowo lapie NOWE odwolania: kazda sciezka repozytorium wymieniona w testach
(takze skladana z literalow operatorem `/`) jest pokryta albo jest na liscie znanych wyjatkow, ktore dzis
dzialaja (testy dokumentacji, nie kodu).
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from procesy import uruchom_i_zbierz

KORZEN = Path(__file__).resolve().parents[1]
# dzis nieskopiowane, a czytane wylacznie przez testy, ktore nie pokrywaja mutowanego kodu (z podkatalogami)
ZNANE_WYJATKI = {".claude/rules", ".claude/settings.json", ".claude/agent-memory", "docs", "README.md",
                 ".gitattributes"}
# katalogi kopiowane czesciowo, ktore testy czytaja w calosci (rglob po src/) - wyjatek tylko dla tej
# dokladnej sciezki, nie dla jej podkatalogow
ZNANE_CZESCIOWE = {".claude", "src", "src/cockpit"}
# literal napisu, po nim kolejne literaly doklejane operatorem `/` (`KORZEN / ".claude" / "skills"`)
SCIEZKA = re.compile(r"""["']([.\w/-]+)["']((?:\s*/\s*["'][.\w-]+["'])*)""")


def _pokryte() -> list[str]:
    konf = tomllib.loads((KORZEN / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["mutmut"]
    return [p.rstrip("/") for p in konf["source_paths"] + konf["also_copy"]] + ["tests", "pyproject.toml"]


def _sledzone() -> set[str]:
    # pod mutmutem KORZEN to mutants/ (w .gitignore) - liste plikow bierzemy z korzenia repozytorium gita
    gora = uruchom_i_zbierz(["git", "rev-parse", "--show-toplevel"], cwd=KORZEN, capture_output=True, text=True,
                            check=True, timeout=60).stdout.strip()
    pliki = uruchom_i_zbierz(["git", "ls-files"], cwd=gora, capture_output=True, text=True, check=True,
                             timeout=60).stdout.split()
    assert pliki, "git ls-files nic nie zwrocil - brak dowodu"
    return {"/".join(p.split("/")[:i]) for p in pliki for i in range(1, p.count("/") + 2)}


def odwolania(tekst: str, sledzone: set[str]) -> set[str]:
    """Sciezki z tekstu testu (ze zlozonymi segmentami), ktore sa plikiem albo katalogiem repozytorium."""
    znalezione = set()
    for m in SCIEZKA.finditer(tekst):
        segmenty = [m.group(1)] + re.findall(r"""["']([.\w-]+)["']""", m.group(2))
        sciezka = "/".join(s.strip("/") for s in segmenty)
        if sciezka in sledzone:
            znalezione.add(sciezka)
    return znalezione


def niepokryte(sciezki: set[str], pokryte: list[str], wyjatki: set[str], czesciowe: frozenset = frozenset()
               ) -> list[str]:
    """Sciezka jest pokryta, gdy lezy w kopiowanej sciezce; sam prefiks kopiowanej (`.claude`) nie wystarcza."""
    def ok(s: str) -> bool:
        return s in czesciowe or any(s == w or s.startswith(w + "/") for w in wyjatki) or any(
            s == p or s.startswith(p + "/") for p in pokryte)
    return sorted(s for s in sciezki if not ok(s))


def test_kazda_sciezka_czytana_przez_testy_trafia_do_kopii_mutmuta():
    sledzone = _sledzone()
    znalezione = set()
    for plik in (KORZEN / "tests").rglob("*.py"):
        if plik.name != Path(__file__).name:  # wlasne literaly nie sa dowodem, ze parser cos znajduje
            znalezione |= odwolania(plik.read_text(encoding="utf-8"), sledzone)
    assert znalezione, "nie znaleziono zadnych odwolan - parser testu jest zepsuty"
    brak = niepokryte(znalezione, _pokryte(), ZNANE_WYJATKI, frozenset(ZNANE_CZESCIOWE))
    assert brak == [], f"dopisz do [tool.mutmut] also_copy w pyproject.toml albo do ZNANE_WYJATKI: {brak}"


def test_sciezka_skladana_z_literalow_jest_sprawdzana_w_calosci():
    # postac z incydentu HAR-169/HAR-242: przy kopii samego .claude/hooks brak .claude/skills
    znalezione = odwolania('RUBRYKA = KORZEN / ".claude" / "skills" / "praca-ticketu"',
                           {".claude", ".claude/skills", ".claude/skills/praca-ticketu"})
    assert znalezione == {".claude/skills/praca-ticketu"}
    assert niepokryte(znalezione, [".claude/hooks"], set()) == [".claude/skills/praca-ticketu"]
    assert niepokryte(znalezione, [".claude/hooks", ".claude/skills"], set()) == []


def test_sam_prefiks_kopiowanego_katalogu_nie_jest_pokryciem():
    assert niepokryte({".claude"}, [".claude/hooks"], set()) == [".claude"]
    assert niepokryte({".claude"}, [".claude/hooks"], set(), frozenset({".claude"})) == []
    # wyjatek czesciowy nie obejmuje podkatalogow
    assert niepokryte({".claude/skills"}, [".claude/hooks"], set(), frozenset({".claude"})) == [".claude/skills"]


def test_napis_ktory_nie_jest_sciezka_repozytorium_nie_jest_odwolaniem():
    # sciezki fikcyjnych repozytoriow z tmp_path (`src/x.py`) nie sa odwolaniem do tego repo
    assert odwolania('x = "src" / "x.py"', {"src", "src/cockpit"}) == set()


def test_znane_wyjatki_nie_sa_juz_pokryte():
    # wyjatek, ktory trafil do also_copy, trzeba usunac z listy - inaczej lista tylko rosnie
    pokryte = _pokryte()
    assert sorted(w for w in ZNANE_WYJATKI if w in pokryte) == []
