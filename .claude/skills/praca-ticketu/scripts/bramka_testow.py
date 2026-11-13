#!/usr/bin/env python3
"""Bramka lint + testy (+ narzędzia mutacyjne projektu) z wiązaniem wyniku do stanu diffu. PRZETESTOWANY
lokalnie na komendach-atrapach; na prawdziwym toolchainie harness-cockpit: UNTESTED.

Zastępuje KROK 6.5 i 6.6c starego SKILL.md oraz ręczne odpytywanie logów: w 15 sesjach TM wykonał
ok. 284 wywołań API na testy i mutanty plus ok. 60 komend `tail`/`cat`/`grep` na plikach wyjścia.
Uruchamiasz RAZ, w tle, i czekasz na jedno powiadomienie; czytasz tylko linie `BRAMKA`.

Konfiguracja: <git-common-dir>/monolynx/<KEY>/bramki.json - lista bramek przepisana raz ze strony wiki
`toolchain` (Team Manager zapisuje ją przy starcie; Researcher podaje komendy):
  [{"nazwa": "lint", "argv": ["ruff", "check", "src", "tests"]},
   {"nazwa": "testy", "argv": ["sh", "-c", "tools/testy_pelne.sh"], "timeout": 5400,
    "kody": {"0": "zielony", "1": "czerwony", "75": "brak-dowodu"}},
   {"nazwa": "mutanty", "argv": ["python", "tools/mutanty_zmian.py"],
    "kody": {"0": "zielony", "1": "czerwony", "3": "reczne"}}]
`kody` mapuje kod wyjścia na wynik; domyślnie 0 = zielony, 1 = czerwony, każdy inny = brak-dowodu
(evidence_required: nieznany kod, timeout, niewystartowana komenda to NIGDY nie jest zielony).

Wywołanie: python3 bramka_testow.py <KEY> [--tylko nazwa,nazwa]

Wiązanie z diffem: przed i po przebiegu liczony jest skrót diffu (git diff --binary <baseline> + treść
plików nieśledzonych). Zmiana diffu w trakcie przebiegu = brak-dowodu (wynik dotyczyłby innego kodu).
Wynik każdej bramki trafia do stan.json jako bramki.<nazwa> = {wynik, kod, skrot_diffu, log, czas};
bramka_zamkniecia.py porównuje ten skrót z bieżącym diffem.

Wyjście: linia na bramkę `BRAMKA <nazwa>: wynik=<zielony|czerwony|reczne|brak-dowodu> kod=<n> log=<plik>`
i 20 ostatnich linii logu dla bramki nie-zielonej; ZAMROŻONA ostatnia linia `BRAMKI: kod=<n>`.
Kody wyjścia (priorytet 2 > 1 > 3 > 0):
  0  wszystkie zielone
  1  któraś czerwona -> poprawka agenta odpowiedzialnego (`stan.py poprawka`)
  2  brak dowodu (komenda nie wystartowała, timeout, kod spoza mapy, zmiana diffu w trakcie, brak bramki)
  3  zielone, ale narzędzie zgłosiło miejsca do ręcznego sprawdzenia (np. kod 3 mutanty_zmian.py)
  64 błąd użycia albo konfiguracji
"""
import hashlib
import json
import os
import subprocess
import sys
import time


def git(*a, binary=False):
    r = subprocess.run(["git", *a], capture_output=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(a)}: {r.stderr.decode(errors='replace')[:200]}")
    return r.stdout if binary else r.stdout.decode(errors="replace")


def skrot_diffu(sha):
    """Skrót treści plików zmienionych względem baseline (śledzone + nieśledzone), niezależny od indeksu."""
    root = git("rev-parse", "--show-toplevel").strip()
    sciezki = set(git("diff", "--name-only", "--no-renames", sha).split("\n"))
    sciezki |= set(git("ls-files", "--others", "--exclude-standard").split("\n"))
    h = hashlib.sha256()
    for p in sorted(x for x in sciezki if x):
        h.update(p.encode() + b"\0")
        try:
            with open(os.path.join(root, p), "rb") as f:
                h.update(f.read())
        except FileNotFoundError:
            h.update(b"<usuniety>")
        except OSError:
            h.update(b"<nieczytelny>")
    return h.hexdigest()[:16]


def main(a):
    if not a:
        print("bramka_testow: brak KEY", file=sys.stderr)
        return 64
    key = a[0]
    tylko = None
    if len(a) == 3 and a[1] == "--tylko":
        tylko = set(a[2].split(","))
    elif len(a) != 1:
        print("bramka_testow: użycie: <KEY> [--tylko a,b]", file=sys.stderr)
        return 64
    try:
        common = git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        kat = os.path.join(common, "monolynx", key)
        with open(os.path.join(kat, "stan.json"), encoding="utf-8") as f:
            st = json.load(f)
        with open(os.path.join(kat, "bramki.json"), encoding="utf-8") as f:
            bramki = json.load(f)
        sha = st["baseline"]["sha"]
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError, RuntimeError) as e:
        print(f"bramka_testow: brak stanu albo bramki.json: {e}", file=sys.stderr)
        print("BRAMKI: kod=2")
        return 2
    if not isinstance(bramki, list) or not bramki or not all(isinstance(b, dict) and b.get("nazwa") and isinstance(b.get("argv"), list) and b["argv"] for b in bramki):
        print("bramka_testow: bramki.json musi być niepustą listą {nazwa, argv}", file=sys.stderr)
        return 64
    if tylko:
        bramki = [b for b in bramki if b["nazwa"] in tylko]
        if not bramki:
            print("BRAMKI: kod=2")
            return 2
    try:
        przed = skrot_diffu(sha)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        print(f"bramka_testow: {e}", file=sys.stderr)
        print("BRAMKI: kod=2")
        return 2
    wyniki = []
    for b in bramki:
        log = os.path.join(kat, f"bramka-{b['nazwa']}.log")
        mapa = {"0": "zielony", "1": "czerwony"}
        mapa.update({str(k): v for k, v in (b.get("kody") or {}).items()})
        t0 = time.time()
        try:
            with open(log, "w", encoding="utf-8") as f:
                r = subprocess.run(b["argv"], stdout=f, stderr=subprocess.STDOUT, timeout=int(b.get("timeout", 3600)),
                                   start_new_session=True)
            kod = r.returncode
            wynik = mapa.get(str(kod), "brak-dowodu")
        except subprocess.TimeoutExpired:
            kod, wynik = "timeout", "brak-dowodu"
        except OSError as e:
            kod, wynik = "start", "brak-dowodu"
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"\nnie wystartowała: {e}\n")
        if wynik not in ("zielony", "czerwony", "reczne"):
            wynik = "brak-dowodu"
        wyniki.append(wynik)
        print(f"BRAMKA {b['nazwa']}: wynik={wynik} kod={kod} log={log}")
        if wynik != "zielony":
            try:
                with open(log, encoding="utf-8", errors="replace") as f:
                    for line in f.readlines()[-20:]:
                        print("  | " + line.rstrip()[:200])
            except OSError:
                pass
        st.setdefault("bramki", {})[b["nazwa"]] = {"wynik": wynik, "kod": kod, "skrot_diffu": przed, "log": log,
                                                   "czas": int(time.time()), "sekundy": int(time.time() - t0)}
    try:
        po = skrot_diffu(sha)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        po = None
    if po != przed:
        print("bramka_testow: diff zmienił się w trakcie przebiegu - wynik nie dotyczy bieżącego kodu")
        for n in [b["nazwa"] for b in bramki]:
            st["bramki"][n]["wynik"] = "brak-dowodu"
        wyniki.append("brak-dowodu")
    with open(os.path.join(kat, "stan.json.tmp"), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(os.path.join(kat, "stan.json.tmp"), os.path.join(kat, "stan.json"))
    kod = 2 if "brak-dowodu" in wyniki else 1 if "czerwony" in wyniki else 3 if "reczne" in wyniki else 0
    print(f"BRAMKI: kod={kod}")
    return kod


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
