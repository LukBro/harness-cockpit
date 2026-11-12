#!/usr/bin/env python3
"""Bramka zamknięcia: czy wolno ustawić `in_review` i zrobić auto-commit. PRZETESTOWANY lokalnie.

Zastępuje rozproszone akapity starego SKILL.md (6.6c "pełny przebieg po OSTATNIEJ zmianie", 6.7b,
7d "warunek auto-commita", 7e) jednym sprawdzeniem na danych ze stan.json.

Warunki (wszystkie liczone względem BIEŻĄCEGO skrótu diffu - ten sam algorytm co bramka_testow.py):
  1. każda bramka z bramki.json ma wynik `zielony` i skrót diffu równy bieżącemu
     (bramka `reczne` przechodzi tylko z polem stanu reczne_potwierdzone = <bieżący skrót>)
  2. ostatni werdykt krytyka ma kod 0 i skrót diffu równy bieżącemu
     (zmiana kodu po werdykcie wymaga nowej oceny - nawet "kosmetyczna")
  3. każde kryterium akceptacji w stanie `kryteria` ([{id, zrobione, dowod}]) ma decyzję,
     a zrobione=true ma niepusty `dowod` (plik:linia, test, komentarz) - brak dowodu = niezrobione
Wyjątek decyzji człowieka: pole `zamkniecie_wyjatek` (powód, wpisany po jawnej decyzji człowieka) -> kod 5.

Wywołanie: python3 bramka_zamkniecia.py <KEY>
Wyjście: linia `BRAK: <warunek>` na każdy niespełniony warunek, `NIEZREALIZOWANE: <id>` dla kryteriów
z zrobione=false, ZAMROŻONA ostatnia linia `ZAMKNIECIE: kod=<n> skrot=<skrót diffu>`.
Kody wyjścia:
  0  wolno: status in_review, commit wg flag AUTO*
  1  warunek niespełniony -> wróć do kroku, który go daje (bramki, krytyk, kryteria)
  5  zamknięcie na decyzję człowieka mimo niespełnionych warunków: in_review TAK, auto-commit NIE
  2  brak dowodu: brak stanu, bramek albo werdyktu, błąd gita
  64 błąd użycia
"""
import hashlib
import json
import os
import subprocess
import sys


def git(*a, binary=False):
    r = subprocess.run(["git", *a], capture_output=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace")[:200])
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
    if len(a) != 1:
        print("bramka_zamkniecia: użycie: <KEY>", file=sys.stderr)
        return 64
    try:
        common = git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        kat = os.path.join(common, "monolynx", a[0])
        with open(os.path.join(kat, "stan.json"), encoding="utf-8") as f:
            st = json.load(f)
        with open(os.path.join(kat, "bramki.json"), encoding="utf-8") as f:
            wymagane = [b["nazwa"] for b in json.load(f)]
        teraz = skrot_diffu(st["baseline"]["sha"])
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"bramka_zamkniecia: brak dowodu: {e}", file=sys.stderr)
        print("ZAMKNIECIE: kod=2 skrot=-")
        return 2
    braki = []
    if not wymagane:
        print("ZAMKNIECIE: kod=2 skrot=" + teraz)
        return 2
    for n in wymagane:
        b = (st.get("bramki") or {}).get(n)
        if not b:
            braki.append(f"bramka {n}: nie uruchomiona")
        elif b.get("skrot_diffu") != teraz:
            braki.append(f"bramka {n}: wynik dotyczy innego diffu ({b.get('skrot_diffu')} != {teraz})")
        elif b.get("wynik") == "reczne" and st.get("reczne_potwierdzone") == teraz:
            pass
        elif b.get("wynik") != "zielony":
            braki.append(f"bramka {n}: wynik {b.get('wynik')}")
    w = (st.get("werdykty") or [None])[-1]
    if not w:
        braki.append("krytyk: brak werdyktu")
    elif w.get("kod") != 0:
        braki.append(f"krytyk: ostatni werdykt {w.get('status')}")
    elif w.get("skrot_diffu") != teraz:
        braki.append("krytyk: werdykt dotyczy innego diffu - kod zmieniono po ocenie")
    kryteria = st.get("kryteria")
    if not isinstance(kryteria, list):
        braki.append("kryteria: brak listy w stanie (pusta lista [] = ticket bez kryteriów)")
    else:
        for k in kryteria:
            if not isinstance(k, dict) or not isinstance(k.get("zrobione"), bool):
                braki.append(f"kryterium {k.get('id') if isinstance(k, dict) else k}: brak decyzji")
            elif k["zrobione"] and not str(k.get("dowod") or "").strip():
                braki.append(f"kryterium {k.get('id')}: zrobione bez dowodu")
            elif not k["zrobione"]:
                print(f"NIEZREALIZOWANE: {k.get('id')}")
    for b in braki:
        print("BRAK: " + b)
    if braki and str(st.get("zamkniecie_wyjatek") or "").strip():
        print(f"ZAMKNIECIE: kod=5 skrot={teraz}")
        return 5
    kod = 1 if braki else 0
    print(f"ZAMKNIECIE: kod={kod} skrot={teraz}")
    return kod


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
