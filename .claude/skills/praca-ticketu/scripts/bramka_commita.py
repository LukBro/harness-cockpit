#!/usr/bin/env python3
"""Bramka commita: sprawdza tożsamość, treść komunikatu i zawartość indeksu PRZED `git commit`.
PRZETESTOWANY lokalnie (repozytorium tymczasowe). Sam niczego nie commituje ani nie pushuje.

Zastępuje: ręczne `git config user.email` (8 z 15 sesji), regułę "bez Co-Authored-By" z CLAUDE.md projektu,
zasadę "imienny git add przy brudnym drzewie" z commit-mr.md i warunek auto-commita z 7d.

Wywołanie: python3 bramka_commita.py <KEY> --komunikat <plik> [--autor-email <adres>] [--bez-wspolautora]
  --autor-email      oczekiwany `git config user.email` (projekt harness-cockpit: adres z CLAUDE.md)
  --bez-wspolautora  żadna linia `Co-Authored-By:` w komunikacie (reguła projektu ma pierwszeństwo
                     przed domyślną atrybucją narzędzia)

Warunki:
  1. bramka_zamkniecia.py <KEY> zwraca 0 (kod 5 = decyzja człowieka -> auto-commit NIE, kod 1)
  2. indeks niepusty; każdy plik w indeksie należy do przydziału albo pliki_tm ze stan.json
     i nie należy do baseline.pliki_sprzed_startu (cudza albo wcześniejsza praca)
  3. pierwsza linia komunikatu zaczyna się od <KEY>
  4. tożsamość i współautor jak w opcjach
  5. bieżący branch != branch bazowy ze stanu
Wyjście: `BRAK: <warunek>` na każdy niespełniony; ZAMROŻONA ostatnia linia `COMMIT-GATE: kod=<n>`.
Kody wyjścia: 0 wolno commitować; 1 warunek niespełniony; 2 brak dowodu (stan, git, komunikat); 64 użycie.
"""
import json
import os
import re
import subprocess
import sys


def git(*a):
    r = subprocess.run(["git", *a], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(a)}: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def main(a):
    if not a or a[0].startswith("-"):
        print("bramka_commita: użycie: <KEY> --komunikat <plik> [--autor-email A] [--bez-wspolautora]", file=sys.stderr)
        return 64
    key, opcje, i = a[0], {}, 1
    while i < len(a):
        if a[i] == "--bez-wspolautora":
            opcje["bez"] = True; i += 1
        elif a[i] in ("--komunikat", "--autor-email") and i + 1 < len(a):
            opcje[a[i]] = a[i + 1]; i += 2
        else:
            print(f"bramka_commita: nieznany argument {a[i]}", file=sys.stderr)
            return 64
    if "--komunikat" not in opcje:
        return 64
    braki = []
    try:
        common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
        with open(os.path.join(common, "monolynx", key, "stan.json"), encoding="utf-8") as f:
            st = json.load(f)
        with open(opcje["--komunikat"], encoding="utf-8") as f:
            msg = f.read()
        indeks = [x for x in git("diff", "--cached", "--name-only", "--no-renames").split("\n") if x]
        branch = git("branch", "--show-current")
        try:
            email = git("config", "user.email")
        except RuntimeError:
            email = ""
    except (OSError, UnicodeDecodeError, ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"bramka_commita: brak dowodu: {e}", file=sys.stderr)
        print("COMMIT-GATE: kod=2")
        return 2
    zam = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bramka_zamkniecia.py")
    r = subprocess.run([sys.executable, zam, key], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        braki.append(f"bramka_zamkniecia kod {r.returncode} (5 = zamknięcie na decyzję człowieka, bez auto-commita)")
    dozwolone = set(st.get("pliki_tm") or [])
    for pl in (st.get("przydzial") or {}).values():
        dozwolone |= set(pl)
    przed = set((st.get("baseline") or {}).get("pliki_sprzed_startu") or [])
    if not indeks:
        braki.append("indeks pusty")
    for x in indeks:
        if x in przed:
            braki.append(f"w indeksie plik sprzed startu: {x}")
        elif x not in dozwolone:
            braki.append(f"w indeksie plik spoza przydziału: {x}")
    if not msg.strip().splitlines() or not msg.strip().splitlines()[0].startswith(key):
        braki.append(f"pierwsza linia komunikatu nie zaczyna się od {key}")
    if opcje.get("bez") and re.search(r"(?im)^co-authored-by:", msg):
        braki.append("komunikat zawiera Co-Authored-By (zakazane przez projekt)")
    if "--autor-email" in opcje and email != opcje["--autor-email"]:
        braki.append(f"git config user.email = '{email}', oczekiwano '{opcje['--autor-email']}'")
    if branch == st.get("baza"):
        braki.append(f"commit na branchu bazowym {branch}")
    for b in braki:
        print("BRAK: " + b)
    kod = 1 if braki else 0
    print(f"COMMIT-GATE: kod={kod}")
    return kod


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
