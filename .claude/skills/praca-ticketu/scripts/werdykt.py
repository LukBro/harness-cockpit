#!/usr/bin/env python3
"""Bramka krytyka: parsuje werdykt w formacie z review-rubric.md i rozstrzyga próg deterministycznie.
PRZETESTOWANY lokalnie na przykładach z rubryki i na transkrypcie krytyka HAR-170.

Zastępuje rozstrzyganie APPROVED / NEEDS WORK "na oko" w 6.6b oraz ręczne przepisywanie linii REGULA.
Werdykt bierzesz z jednego źródła:
  --plik <ścieżka>      plik werdyktu (krytyk Codex: plik --out wrappera)
  --agent-id <id>       id subagenta krytyka z wyniku narzędzia Agent; skrypt czyta jego transkrypt
                        ~/.claude/projects/*/*/subagents/agent-<id>.jsonl i bierze OSTATNI blok **Code Review**
                        (Team Manager nie przepisuje werdyktu - nie może go zmienić ani zgubić)

Wywołanie: python3 werdykt.py <KEY> (--plik P | --agent-id ID) [--prog 82]
Lista ocenianych agentów pochodzi z stan.json (klucze `przydzial`); każdy musi mieć linię `<agent>: ... = X/100`.
Ponowna ocena po poprawce obejmuje zwykle jednego agenta: brakujące oceny są przenoszone z poprzedniej rundy
(linia `PRZENIESIONA:`); agent bez oceny w żadnej rundzie = kod 2.

Zapis: stan.json `werdykty` += {runda, oceny, status, skrot_diffu}; skrót diffu wiąże werdykt z kodem,
który krytyk widział - bramka_zamkniecia.py odrzuci werdykt sprzed późniejszej zmiany.

Wyjście: linie `OCENA: <agent>=<X>`, `REGULA: <agent>: <zdanie>`, ZAMROŻONA ostatnia linia
  `WERDYKT: kod=<n> status=APPROVED|NEEDS-WORK|BLOKADA|BRAK minimum=<X>`
Kody wyjścia:
  0  każdy agent >= progu (APPROVED)
  1  ktoś poniżej progu i ma odjęcie bez znacznika BLOKADA-ZEWNĘTRZNA -> iteracja tylko tego agenta
  4  poniżej progu wyłącznie przez odjęcia z BLOKADA-ZEWNĘTRZNA -> bez iteracji; komentarz BLOKADA + decyzja człowieka
  2  brak werdyktu: brak bloku **Code Review**, brak oceny któregoś agenta, plik/transkrypt nieczytelny
     (review NIGDY nie jest liczone jako zaliczone bez werdyktu)
  64 błąd użycia
"""
import glob
import hashlib
import json
import os
import re
import subprocess
import sys

LINIA = re.compile(r"^\s*(?P<agent>[\w.\-/ ]+?):\s*100\b(?P<odj>.*?)=\s*(?P<score>-?\d{1,3})/100\s*$")


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


NAGLOWEK = re.compile(r"\*\*Code Review[^*\n]*\*\*")


def z_transkryptu(aid):
    """Ostatni werdykt subagenta: jego tekst, jego SubagentHandback albo <agent-message from=ID> u TM."""
    if not re.fullmatch(r"[0-9a-zA-Z]+", aid):
        return None
    pliki = glob.glob(os.path.expanduser(f"~/.claude/projects/*/*/subagents/agent-{aid}.jsonl"))
    if len(pliki) != 1:
        return None
    kandydaci = []
    glowny = os.path.dirname(os.path.dirname(pliki[0])) + ".jsonl"
    for plik, czy_glowny in ((pliki[0], False), (glowny, True)):
        try:
            f = open(plik, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                if czy_glowny and f'from="{aid}"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                for x in (d.get("message") or {}).get("content") or []:
                    if not isinstance(x, dict):
                        continue
                    t = x.get("text") or ""
                    if x.get("type") == "tool_use":
                        t = json.dumps(x.get("input"), ensure_ascii=False) if x.get("name") == "SubagentHandback" else ""
                        t = json.loads(t).get("message", "") if t else ""
                    if NAGLOWEK.search(t):
                        kandydaci.append(t)
                c = (d.get("message") or {}).get("content")
                if czy_glowny and isinstance(c, str) and NAGLOWEK.search(c):
                    kandydaci.append(c)
    return kandydaci[-1] if kandydaci else None


def parsuj(tekst, agenci, prog):
    trafienia = list(NAGLOWEK.finditer(tekst or ""))
    if not trafienia:
        return 2, {}, [], "brak bloku **Code Review**"
    blok = tekst[trafienia[-1].start():]
    oceny, blokady_tylko, reguly, akt = {}, {}, [], None
    for line in blok.splitlines():
        m = LINIA.match(line)
        if m:
            akt = m.group("agent").strip()
            oceny[akt] = int(m.group("score"))
            odj = re.findall(r"-\d+\s*\(([^)]*)\)", m.group("odj"))
            blokady_tylko[akt] = bool(odj) and all("BLOKADA-ZEWN" in o for o in odj)
            continue
        r = re.match(r"^\s*REGULA:\s*(.+)$", line)
        if r and akt:
            reguly.append((akt, r.group(1).strip()))
    brak = [a for a in agenci if a not in oceny]
    if brak:
        return 2, oceny, reguly, "brak oceny agentów: " + ",".join(brak)
    ponizej = [a for a in agenci if oceny[a] < prog]
    if not ponizej:
        return 0, oceny, reguly, "APPROVED"
    if all(blokady_tylko.get(a) for a in ponizej):
        return 4, oceny, reguly, "BLOKADA"
    return 1, oceny, reguly, "NEEDS-WORK"


def main(a):
    if len(a) < 3:
        print("werdykt: użycie: <KEY> (--plik P | --agent-id ID) [--prog 82]", file=sys.stderr)
        return 64
    key, zrodlo, wart = a[0], a[1], a[2]
    prog = 82
    if len(a) == 5 and a[3] == "--prog" and a[4].isdigit():
        prog = int(a[4])
    elif len(a) not in (3,):
        print("werdykt: zły argument", file=sys.stderr)
        return 64
    try:
        common = git("rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        p = os.path.join(common, "monolynx", key, "stan.json")
        with open(p, encoding="utf-8") as f:
            st = json.load(f)
        agenci = list(st["przydzial"].keys())
        sha = st["baseline"]["sha"]
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError) as e:
        print(f"werdykt: stan nieczytelny: {e}", file=sys.stderr)
        print("WERDYKT: kod=2 status=BRAK minimum=-")
        return 2
    if zrodlo == "--plik":
        try:
            with open(wart, encoding="utf-8") as f:
                tekst = f.read()
        except (OSError, UnicodeDecodeError):
            tekst = None
    elif zrodlo == "--agent-id":
        tekst = z_transkryptu(wart)
    else:
        return 64
    # Ponowna ocena obejmuje tylko poprawianego agenta: brakujące oceny przenosimy z poprzedniej rundy
    # (jawnie oznaczone). Brak poprzedniej oceny = brak werdyktu.
    poprzednie = ((st.get("werdykty") or [{}])[-1] or {}).get("oceny") or {}
    _, wstepne, _, _ = parsuj(tekst, [], prog)
    przeniesione = {a_: poprzednie[a_] for a_ in agenci if a_ not in wstepne and a_ in poprzednie}
    if przeniesione and tekst:
        tekst = tekst + "\n" + "\n".join(f"{a_}: 100 = {s_}/100" for a_, s_ in przeniesione.items())
    kod, oceny, reguly, opis = parsuj(tekst, agenci, prog)
    for a_, s_ in przeniesione.items():
        print(f"PRZENIESIONA: {a_}={s_} (z poprzedniej rundy)")
    for ag, sc in oceny.items():
        print(f"OCENA: {ag}={sc}")
    for ag, r in reguly:
        print(f"REGULA: {ag}: {r}")
    if kod == 2:
        print(f"werdykt: {opis}")
    try:
        skrot = skrot_diffu(sha)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        skrot = None
    st.setdefault("werdykty", []).append({"runda": len(st.get("werdykty", [])) + 1, "oceny": oceny,
                                          "status": opis if kod != 2 else "BRAK", "kod": kod,
                                          "skrot_diffu": skrot, "zrodlo": f"{zrodlo} {wart}"})
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(p + ".tmp", p)
    status = {0: "APPROVED", 1: "NEEDS-WORK", 4: "BLOKADA", 2: "BRAK"}[kod]
    mini = min(oceny.values()) if oceny else "-"
    print(f"WERDYKT: kod={kod} status={status} minimum={mini}")
    return kod


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
