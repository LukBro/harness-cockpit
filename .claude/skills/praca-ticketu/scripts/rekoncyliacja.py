#!/usr/bin/env python3
"""Bramka rekoncyliacji: pliki zmienione wg gita kontra przydział i raporty agentów. PRZETESTOWANY lokalnie.

Zastępuje krok 6e (tabela rekoncyliacji budowana ręcznie z `git status` i `git diff --stat`) oraz
wykrywanie "pliku spoza przydziału", które dziś robi krytyk ręcznie.

Wejście: stan.json epizodu (baseline.sha, baseline.pliki_sprzed_startu, przydzial, pliki_tm).
  przydzial: {"<agent>": ["ścieżka", ...]}   - konkretne pliki, bez globów (glob = kod 64)
  pliki_tm:  ["ścieżka", ...]                 - pliki wspólne utworzone przez Team Managera (legalne)
Opcjonalnie raporty agentów: --raport <agent>=<plik1>,<plik2> (lista z raportu końcowego agenta).

Wywołanie: python3 rekoncyliacja.py <KEY> [--raport agent=a.py,b.py ...]

Wyjście: po jednej linii na rozbieżność, potem ZAMROŻONA linia
  `REKONCYLIACJA: kod=<n> zmienione=<N> poza_przydzialem=<M> niezgodne_raporty=<R>`
Kody wyjścia:
  0  każdy zmieniony plik ma właściciela w przydziale albo w pliki_tm, raporty zgodne z gitem
  1  plik poza przydziałem (linia `POZA: <plik>`) albo raport niezgodny z gitem
     (`RAPORT: <agent> zgłosił <plik>, git go nie zmienił` / `RAPORT: <agent> nie zgłosił <plik>`)
     -> decyzja TM: re-run agenta (zużywa limit, `stan.py poprawka`) albo jawne przyjęcie z powodem
  2  brak dowodu: brak stanu/baseline/przydziału, błąd gita, ZERO zmienionych plików
  64 błąd użycia (glob w przydziale, zły format --raport)
"""
import json
import os
import subprocess
import sys


def git(*a):
    try:
        r = subprocess.run(["git", *a], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"rekoncyliacja: git: {e}", file=sys.stderr)
        sys.exit(2)
    if r.returncode != 0:
        print(f"rekoncyliacja: git {' '.join(a)}: {r.stderr.strip()[:200]}", file=sys.stderr)
        sys.exit(2)
    return r.stdout


def main(a):
    if not a:
        print("rekoncyliacja: brak KEY", file=sys.stderr)
        return 64
    key = a[0]
    raporty = {}
    i = 1
    while i < len(a):
        if a[i] != "--raport" or i + 1 >= len(a) or "=" not in a[i + 1]:
            print("rekoncyliacja: oczekiwano --raport agent=plik,plik", file=sys.stderr)
            return 64
        ag, pl = a[i + 1].split("=", 1)
        raporty[ag] = {x.strip() for x in pl.split(",") if x.strip()}
        i += 2
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    p = os.path.join(common, "monolynx", key, "stan.json")
    try:
        with open(p, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        print(f"rekoncyliacja: stan nieczytelny {p}: {e}", file=sys.stderr)
        return 2
    sha = (st.get("baseline") or {}).get("sha")
    przydzial = st.get("przydzial")
    if not sha or not isinstance(przydzial, dict) or not przydzial:
        print("rekoncyliacja: w stanie brak baseline.sha albo przydzial", file=sys.stderr)
        return 2
    for ag, pliki in przydzial.items():
        for x in pliki:
            if any(c in x for c in "*?["):
                print(f"rekoncyliacja: glob w przydziale {ag}: {x}", file=sys.stderr)
                return 64
    przed = set((st.get("baseline") or {}).get("pliki_sprzed_startu") or [])
    tm = set(st.get("pliki_tm") or [])
    zmienione = set(git("diff", "--name-only", "--no-renames", sha).split("\n"))
    zmienione |= set(git("ls-files", "--others", "--exclude-standard").split("\n"))
    zmienione = {x for x in zmienione if x} - przed
    if not zmienione:
        print("REKONCYLIACJA: kod=2 zmienione=0 poza_przydzialem=0 niezgodne_raporty=0")
        return 2
    wlasciciel = {}
    for ag, pliki in przydzial.items():
        for x in pliki:
            wlasciciel.setdefault(x, []).append(ag)
    poza = sorted(x for x in zmienione if x not in wlasciciel and x not in tm)
    for x in poza:
        print(f"POZA: {x}")
    niezg = 0
    for ag, zgl in raporty.items():
        moje = {x for x in zmienione if ag in wlasciciel.get(x, [])}
        for x in sorted(zgl - zmienione):
            print(f"RAPORT: {ag} zgłosił {x}, git go nie zmienił"); niezg += 1
        for x in sorted(moje - zgl):
            print(f"RAPORT: {ag} nie zgłosił {x}"); niezg += 1
    kod = 1 if (poza or niezg) else 0
    print(f"REKONCYLIACJA: kod={kod} zmienione={len(zmienione)} poza_przydzialem={len(poza)} niezgodne_raporty={niezg}")
    return kod


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
