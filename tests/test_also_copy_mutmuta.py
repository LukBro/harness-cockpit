"""Pliki czytane przez testy sa kopiowane do mutants/ (audyt procesu 2026-11-19).

mutmut kopiuje do mutants/ tylko `source_paths` i `also_copy`. Test, ktory czyta plik spoza tej listy, pod
mutmutem pada i bramka mutacji konczy sie kodem 2 dopiero w CI albo u krytyka (HAR-169, HAR-242: brak
`.claude/skills/`). Ten test zapadkowo lapie NOWE odwolania: kazda sciezka repozytorium wymieniona w testach
jest pokryta albo jest na liscie znanych wyjatkow, ktore dzis dzialaja (testy dokumentacji, nie kodu).
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from procesy import uruchom_i_zbierz

KORZEN = Path(__file__).resolve().parents[1]
# dzis nieskopiowane, a czytane wylacznie przez testy, ktore nie pokrywaja mutowanego kodu
ZNANE_WYJATKI = {".claude/rules", ".claude/settings", ".claude/agent-memory", "docs", "README.md",
                 ".gitattributes"}


def _pokryte() -> list[str]:
    konf = tomllib.loads((KORZEN / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["mutmut"]
    return [p.rstrip("/") for p in konf["source_paths"] + konf["also_copy"]] + ["tests", "pyproject.toml"]


def _sledzone_korzenie() -> set[str]:
    # pod mutmutem KORZEN to mutants/ (w .gitignore) - liste plikow bierzemy z korzenia repozytorium gita
    gora = uruchom_i_zbierz(["git", "rev-parse", "--show-toplevel"], cwd=KORZEN, capture_output=True, text=True,
                            check=True, timeout=60).stdout.strip()
    pliki = uruchom_i_zbierz(["git", "ls-files"], cwd=gora, capture_output=True, text=True, check=True,
                             timeout=60).stdout.split()
    assert pliki, "git ls-files nic nie zwrocil - brak dowodu"
    return {p.split("/")[0] for p in pliki} | {"/".join(p.split("/")[:2]) for p in pliki if p.startswith(".claude/")}


def _odwolania_w_testach(korzenie: set[str]) -> set[str]:
    wzor = re.compile(r"""["']((?:\.claude/[A-Za-z_-]+)|[.A-Za-z_-]+(?:\.[a-z]+)?)[/"']""")
    znalezione = set()
    for plik in (KORZEN / "tests").rglob("*.py"):
        for m in wzor.finditer(plik.read_text(encoding="utf-8")):
            if m.group(1) in korzenie:
                znalezione.add(m.group(1))
    return znalezione


def test_kazda_sciezka_czytana_przez_testy_trafia_do_kopii_mutmuta():
    pokryte = _pokryte()
    odwolania = _odwolania_w_testach(_sledzone_korzenie())
    assert odwolania, "nie znaleziono zadnych odwolan - parser testu jest zepsuty"
    brak = sorted(o for o in odwolania
                  if o not in ZNANE_WYJATKI and not any(o == p or o.startswith(p + "/") or p.startswith(o + "/")
                                                        for p in pokryte))
    assert brak == [], f"dopisz do [tool.mutmut] also_copy w pyproject.toml: {brak}"


def test_znane_wyjatki_nie_sa_juz_pokryte():
    # wyjatek, ktory trafil do also_copy, trzeba usunac z listy - inaczej lista tylko rosnie
    pokryte = _pokryte()
    assert sorted(w for w in ZNANE_WYJATKI if w in pokryte) == []
