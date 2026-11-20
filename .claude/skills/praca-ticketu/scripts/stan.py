#!/usr/bin/env python3
"""Stan epizodu ticketu: jeden plik JSON poza drzewem roboczym. Tylko stdlib. PRZETESTOWANY lokalnie (testy w raport.md).

Plik: <absolutny git-common-dir>/monolynx/<KEY>/stan.json. Wspólny dla wszystkich worktree repo,
niewidoczny w `git status`, przeżywa /clear i kompakcję - z niego korzysta wznowienie.

Komendy:
  stan.py pokaz <KEY>                      wypisuje stan (JSON)
  stan.py ustaw <KEY> klucz=wartosc ...    wartosc to JSON albo napis; klucz z kropką = pole zagnieżdżone
  stan.py znacznik <KEY> <nazwa>           zapisuje czas (epoch s) pod znaczniki.<nazwa>; wypisuje go
  stan.py poprawka <KEY> <agent>           zwiększa wspólny licznik poprawek agenta (czerwone testy,
                                           NEEDS WORK, niedostarczona praca); limit 3
  stan.py czas <KEY>                       liczy minuty TM: (koniec - start_tm) - (koniec_zespolu - start_zespolu),
                                           minimum 1; brak znacznika -> wall-clock z adnotacją
  stan.py bramki <KEY> <plik>              kopiuje listę bramek {nazwa, argv} z pliku JSON do bramki.json
                                           katalogu stanu (sesja w worktree nie zapisze tam narzędziem pliku)
  stan.py hook                             stan ochrony hooka z markerów ~/.cache/monolynx/hook-active (60 min)

Kody wyjścia:
  0   wykonane
  1   reguła odmówiła: limit poprawek przekroczony (4. poprawka) albo hook bez potwierdzenia
      (stan b/c) - na stdout linia z powodem
  2   błąd stanu albo wejścia: brak repo git, plik stanu nieczytelny lub uszkodzony, brak klucza
  64  błąd użycia
Ostatnia linia stdout przy `hook`: `HOOK: stan=a|b|c` (a = potwierdzona, b = niepełna, c = brak).
"""
import json
import os
import subprocess
import sys
import time

LIMIT_POPRAWEK = 3


def blad(kod, tekst):
    print(f"stan: {tekst}", file=sys.stderr)
    sys.exit(kod)


def katalog(key):
    try:
        r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        blad(2, f"git niedostępny: {e}")
    if r.returncode != 0 or not r.stdout.strip():
        blad(2, "katalog poza repozytorium git")
    k = os.path.join(r.stdout.strip(), "monolynx", key)
    try:
        os.makedirs(k, exist_ok=True)
    except OSError as e:
        blad(2, f"nie da się utworzyć {k}: {e}")
    return k


def wczytaj(key):
    p = os.path.join(katalog(key), "stan.json")
    if not os.path.exists(p):
        return p, {"key": key}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        blad(2, f"plik stanu nieczytelny ({p}): {e}")
    if not isinstance(d, dict):
        blad(2, f"plik stanu nie jest obiektem JSON ({p})")
    return p, d


SZABLON_BRAMEK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bramki-szablon.json")


def ostrzezenia_bramek(bramki):
    """Tryb ostrzezen (audyt 2026-11-19, krok 4b): zapis bez zmian, uwagi na stderr."""
    wynik = []
    try:
        with open(SZABLON_BRAMEK, encoding="utf-8") as f:
            wzor = {b["nazwa"] for b in json.load(f)}
    except (OSError, ValueError, KeyError, TypeError) as e:
        wynik.append(f"nie sprawdzono bramek z szablonu: {e}")
        wzor = set()
    wynik += [f"brak bramki z szablonu: {n}" for n in sorted(wzor - {b["nazwa"] for b in bramki})]
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=30)
        repo = os.path.realpath(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        repo = None
    if repo is None:
        wynik.append("nie sprawdzono skryptow spoza repozytorium: brak korzenia git")
    for b in bramki:
        n = b["nazwa"]
        kody = b.get("kody", {})
        if not isinstance(kody, dict):
            wynik.append(f"bramka {n}: kody nie sa obiektem - nie sprawdzono mapowania kodow")
            kody = {}
        wynik += [f"bramka {n}: kod {k} mapowany na zielony" for k, v in kody.items()
                  if str(k) != "0" and v == "zielony"]
        if len(b["argv"]) > 1 and os.path.basename(b["argv"][0]) in ("sh", "bash", "zsh") and b["argv"][1] == "-c":
            wynik.append(f"bramka {n}: {b['argv'][0]} -c ukrywa komende przed sprawdzeniem")
        for x in b["argv"] if repo is not None else []:
            if not (os.path.isabs(x) and x.endswith((".py", ".sh"))):
                continue
            try:
                poza = not os.path.realpath(x).startswith(repo + os.sep)
            except ValueError as e:  # bajt zerowy w sciezce - ostrzezenie, nie wywrocony zapis
                wynik.append(f"bramka {n}: nie sprawdzono skryptu {x!r}: {e}")
                continue
            if poza:
                wynik.append(f"bramka {n}: skrypt spoza repozytorium: {x}")
    return wynik


def zapisz(p, d):
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except OSError as e:
        blad(2, f"zapis stanu nieudany: {e}")


def ustaw_pole(d, klucz, wartosc):
    cel = d
    czesci = klucz.split(".")
    for c in czesci[:-1]:
        nast = cel.setdefault(c, {})
        if not isinstance(nast, dict):
            blad(2, f"pole {c} nie jest obiektem")
        cel = nast
    cel[czesci[-1]] = wartosc


def hook():
    kat = os.path.expanduser("~/.cache/monolynx/hook-active")
    wpisy = []
    teraz = time.time()
    try:
        nazwy = os.listdir(kat)
    except FileNotFoundError:
        nazwy = []
    except OSError as e:
        blad(2, f"katalog markerów nieczytelny: {e}")
    for n in nazwy:
        p = os.path.join(kat, n)
        try:
            if teraz - os.path.getmtime(p) > 3600:
                continue
            with open(p, encoding="utf-8") as f:
                wpisy.append(json.load(f))
        except (OSError, UnicodeDecodeError, ValueError):
            continue  # marker uszkodzony liczy się jak brak - raportujemy stan słabszy, nigdy mocniejszy
    if any(isinstance(w, dict) and w.get("subagent_identification") is True for w in wpisy):
        print("HOOK: stan=a")
        return 0
    if wpisy:
        print("stan: hook żyje, ale klient nie przekazuje identyfikacji subagenta")
        print("HOOK: stan=b")
        return 1
    print("stan: brak markerów z ostatniej godziny")
    print("HOOK: stan=c")
    return 1


def main(a):
    if not a:
        blad(64, "brak komendy")
    cmd = a[0]
    if cmd == "hook":
        return hook()
    if len(a) < 2:
        blad(64, "brak klucza ticketu")
    key = a[1]
    p, d = wczytaj(key)
    if cmd == "pokaz":
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return 0
    if cmd == "ustaw":
        if len(a) < 3:
            blad(64, "brak par klucz=wartosc")
        for para in a[2:]:
            if "=" not in para:
                blad(64, f"para bez '=': {para}")
            k, v = para.split("=", 1)
            try:
                v = json.loads(v)
            except ValueError:
                if v[:1] in ("{", "["):
                    # typowy objaw: powłoka zjadła cudzysłowy - cała para musi być w apostrofach
                    blad(64, f"wartość {k} wygląda na JSON, ale nie parsuje się: {v[:80]}")
            ustaw_pole(d, k, v)
        zapisz(p, d)
        return 0
    if cmd == "znacznik":
        if len(a) != 3:
            blad(64, "znacznik <KEY> <nazwa>")
        t = int(time.time())
        ustaw_pole(d, "znaczniki." + a[2], t)
        zapisz(p, d)
        print(t)
        return 0
    if cmd == "poprawka":
        if len(a) != 3:
            blad(64, "poprawka <KEY> <agent>")
        lic = d.setdefault("poprawki", {})
        lic[a[2]] = int(lic.get(a[2], 0)) + 1
        zapisz(p, d)
        if lic[a[2]] > LIMIT_POPRAWEK:
            print(f"POPRAWKA: agent={a[2]} n={lic[a[2]]} limit={LIMIT_POPRAWEK} -> decyzja człowieka")
            return 1
        print(f"POPRAWKA: agent={a[2]} n={lic[a[2]]} limit={LIMIT_POPRAWEK}")
        return 0
    if cmd == "czas":
        z = d.get("znaczniki", {})
        teraz = int(time.time())
        if "start_tm" not in z:
            blad(2, "brak znacznika start_tm")
        wall = teraz - z["start_tm"]
        if "start_zespolu" in z and "koniec_zespolu" in z:
            minuty = max(1, round((wall - (z["koniec_zespolu"] - z["start_zespolu"])) / 60))
            print(f"CZAS-TM: minuty={minuty} metoda=formula")
        else:
            print(f"CZAS-TM: minuty={max(1, round(wall / 60))} metoda=wall-clock")
        return 0
    if cmd == "bramki":
        if len(a) != 3:
            blad(64, "bramki <KEY> <plik>")
        try:
            with open(a[2], encoding="utf-8") as f:
                bramki = json.load(f)
        except (OSError, ValueError, RecursionError) as e:  # UnicodeDecodeError dziedziczy po ValueError
            blad(2, f"plik bramek nieczytelny ({a[2]}): {e}")
        # ten sam kształt, którego wymaga bramka_testow.py - zły plik odrzucony przy zapisie, nie przy bramce
        if not isinstance(bramki, list) or not bramki or not all(
                isinstance(b, dict) and b.get("nazwa") and isinstance(b["nazwa"], str)
                and "/" not in b["nazwa"]                   # nazwa trafia do ścieżki logu bramki
                and isinstance(b.get("argv"), list) and b["argv"]
                and all(isinstance(x, str) for x in b["argv"])     # subprocess.run przyjmuje tylko napisy
                for b in bramki):
            blad(2, "bramki muszą być niepustą listą {nazwa, argv}")
        for o in ostrzezenia_bramek(bramki):
            print(f"OSTRZEZENIE: {o}", file=sys.stderr)
        zapisz(os.path.join(os.path.dirname(p), "bramki.json"), bramki)
        print(f"BRAMKI: zapisane={len(bramki)}")
        return 0
    blad(64, f"nieznana komenda {cmd}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
