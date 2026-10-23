#!/usr/bin/env python3
"""Zapadka baseline dla wynikow testow mutacyjnych (MON-151).

Czyta podsumowanie wygenerowane przez plugin/scripts/mutation_report.py
(kontrakt zamrozony w MON-149: score float|None, total, killed, survived
plus opcjonalne "survivors"/"_meta" ignorowane tutaj), porownuje je z
zapisanym baseline'em i wypisuje raport konczacy sie zamrozona linia
MUTATION-BASELINE. Krok CI oparty o ten skrypt jest z definicji
NIEBLOKUJACY: main() zwraca 0 dla kazdego statusu porownania (w tym
"down"), bo mutation score jest tu traktowany jako trend do obserwacji,
nie jako brama jakosci - jedyne kody bledu to 2 (zle wejscie) i 64 (zle
uzycie CLI).

Zapadka: baseline moze tylko rosnac. Status "down" (aktualny score nizszy
niz zapisany baseline) jest widoczny w raporcie (marker "UWAGA"), ale
NIGDY nie nadpisuje pliku baseline - odwrotna kolejnosc pozwolilaby
przypadkowemu spadkowi jakosci mutantow ciekle "resetowac" cel, co
zaprzeczaloby idei zapadki.

Baseline uszkodzony (zly JSON, brak "score"/"scope", zly typ) konczy
program bledem (kod 2) i NIGDY nie jest cicho nadpisywany pierwszym
pomiarem - plik baseline jest commitowany przez wlasciciela repo docelowego,
a ciche nadpisanie skasowaloby jego historie bez sladu w diffie.

Status "no-mutants" (total == 0, np. merge request dotyczacy wylacznie
dokumentacji) nie wykonuje porownania do baseline - scoru nie ma z czym
porownac, wiec sam status jest sygnalem informacyjnym, nie ocena trendu.

Status "scope-mismatch" (baseline zapisany dla innego zakresu, np. "full"
podczas gdy biezacy przebieg jest "diff") tez nie nadpisuje baseline - a
sam score z przebiegu diffowego (tylko zmienione pliki) nie jest
porownywalny z pelnym przebiegiem po calym repo, wiec delta w raporcie ma
charakter wylacznie orientacyjny.

CLI:
    python3 mutation_baseline.py --summary <plik|-> --baseline <plik>
        [--scope full|diff] [--commit <sha>] [--tool <nazwa>] [--dry-run]

    --summary - (myslnik) czyta podsumowanie ze stdin zamiast z pliku.
    --scope domyslnie "diff".
    --dry-run pokazuje raport, ale nigdy nie zapisuje baseline'u.

Kody wyjscia:
    0  - raport wypisany poprawnie na stdout (kazdy status porownania,
         takze "down"/"no-mutants"/"scope-mismatch" - krok jest nieblokujacy).
    2  - blad wejscia: niepoprawny JSON, brak pliku podsumowania, zly
         ksztalt podsumowania albo uszkodzony plik baseline.
    64 - blad uzycia CLI: brak wymaganego argumentu, nieznany argument,
         albo niepoprawna wartosc --scope.

Wejscie (plik podsumowania i plik baseline) jest granica systemu - zly
ksztalt konczy sie czytelnym bledem na stderr (_InputError), nigdy
tracebackiem; stdout przy bledzie zostaje pusty, zeby krok CI mogl
bezpiecznie przechwycic sam raport.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any


class _UsageError(Exception):
    """Blad parsowania argumentow CLI (mapowany na kod wyjscia 64)."""


class _InputError(Exception):
    """Blad tresci wejscia: zly JSON, brak pliku, zly ksztalt (kod wyjscia 2)."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        # argparse domyslnie wola sys.exit(2), co kolidowaloby z naszym
        # kodem 2 zarezerwowanym dla bledow tresci wejscia - zamieniamy na
        # wyjatek przechwytywany w main(), zeby zwrocic 64.
        raise _UsageError(message)


def _require_int(data: dict[str, Any], key: str, label: str) -> int:
    if key not in data:
        raise _InputError(f"{label}: brak wymaganego klucza '{key}'")
    value = data[key]
    # bool jest podklasa int w Pythonie - isinstance(True, int) to True,
    # wiec trzeba go jawnie wykluczyc, inaczej {"total": true} przeszloby
    # walidacje jako poprawna liczba calkowita.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _InputError(f"{label}: klucz '{key}' musi byc liczba calkowita")
    return value


def _load_json_file(path: Path, label: str) -> Any:
    if not path.is_file():
        raise _InputError(f"{label}: nie znaleziono pliku '{path}'")
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        raise _InputError(f"{label}: nie mozna odczytac pliku '{path}': {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise _InputError(f"{label}: niepoprawny JSON w '{path}': {exc}") from exc


def _validate_summary(data: Any, label: str = "summary") -> dict[str, Any]:
    if not isinstance(data, dict):
        raise _InputError(f"{label}: oczekiwano obiektu JSON")
    if "score" not in data:
        raise _InputError(f"{label}: brak wymaganego klucza 'score'")
    score = data["score"]
    if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float))):
        raise _InputError(f"{label}: klucz 'score' musi byc liczba albo null")
    total = _require_int(data, "total", label)
    killed = _require_int(data, "killed", label)
    survived = _require_int(data, "survived", label)
    return {
        "score": float(score) if score is not None else None,
        "total": total,
        "killed": killed,
        "survived": survived,
    }


def load_summary(path: str | Path) -> dict[str, Any]:
    """Wczytaj podsumowanie mutation_report.py z pliku i zwaliduj ksztalt.

    Wymaga kluczy score (float|None), total (int), killed (int), survived
    (int); inne klucze ("survivors", "_meta") sa ignorowane. Brak pliku,
    niepoprawny JSON, top-level nie-dict, brak wymaganego klucza albo zly
    typ -> _InputError.
    """
    data = _load_json_file(Path(path), "summary")
    return _validate_summary(data, "summary")


def load_baseline(path: str | Path) -> dict[str, Any] | None:
    """Wczytaj plik baseline (commitowany przez wlasciciela repo docelowego).

    Brak pliku -> None (pierwszy przebieg, nie blad). Plik obecny ale
    uszkodzony (zly JSON, brak "score"/"scope", zly typ) -> _InputError -
    uszkodzony baseline nigdy nie jest cicho traktowany jak jego brak,
    bo to ukrylo by utrate historii zapisanej przez wlasciciela repo.
    """
    baseline_path = Path(path)
    if not baseline_path.is_file():
        return None
    data = _load_json_file(baseline_path, "baseline")
    if not isinstance(data, dict):
        raise _InputError("baseline: oczekiwano obiektu JSON")
    if "score" not in data:
        raise _InputError("baseline: brak wymaganego klucza 'score'")
    score = data["score"]
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise _InputError("baseline: klucz 'score' musi byc liczba")
    if "scope" not in data:
        raise _InputError("baseline: brak wymaganego klucza 'scope'")
    if not isinstance(data["scope"], str):
        raise _InputError("baseline: klucz 'scope' musi byc stringiem")
    return data


def compare(summary: dict[str, Any], baseline: dict[str, Any] | None, scope: str) -> dict[str, Any]:
    """Wyznacz jeden z szesciu statusow porownania, w zamrozonej kolejnosci:

        1. summary["score"] is None -> "no-mutants" (pierwszenstwo przed
           "first" i "scope-mismatch" - nie ma czego porownywac, np. MR
           dotyczacy wylacznie dokumentacji)
        2. baseline is None -> "first" (pierwszy przebieg dla tego pliku)
        3. baseline["scope"] != scope -> "scope-mismatch" (score z innego
           zakresu pomiaru jest nieporownywalny wprost)
        4. score > baseline["score"] -> "up"
        5. score == baseline["score"] -> "same"
        6. score < baseline["score"] -> "down"

    write=True tylko dla "first" i "up" - to jest zapadka: baseline moze
    tylko rosnac, spadek (status "down") jest widoczny w raporcie ale
    nigdy nie nadpisuje pliku.
    """
    score = summary["score"]
    survived = summary["survived"]
    total = summary["total"]

    if score is None:
        baseline_score = baseline["score"] if baseline is not None else None
        return {
            "status": "no-mutants",
            "score": None,
            "baseline": baseline_score,
            "delta": None,
            "survived": survived,
            "total": total,
            "scope": scope,
            "write": False,
        }

    if baseline is None:
        return {
            "status": "first",
            "score": score,
            "baseline": None,
            "delta": None,
            "survived": survived,
            "total": total,
            "scope": scope,
            "write": True,
        }

    baseline_score = baseline["score"]

    if baseline["scope"] != scope:
        return {
            "status": "scope-mismatch",
            "score": score,
            "baseline": baseline_score,
            "delta": round(score - baseline_score, 2),
            "survived": survived,
            "total": total,
            "scope": scope,
            "write": False,
        }

    delta = round(score - baseline_score, 2)
    if score > baseline_score:
        status, write = "up", True
    elif score == baseline_score:
        status, write = "same", False
    else:
        status, write = "down", False

    return {
        "status": status,
        "score": score,
        "baseline": baseline_score,
        "delta": delta,
        "survived": survived,
        "total": total,
        "scope": scope,
        "write": write,
    }


def build_baseline(
    summary: dict[str, Any],
    scope: str,
    commit: str | None,
    tool: str | None,
    measured_at: str,
) -> dict[str, Any]:
    """Zbuduj nowy rekord baseline - dokladnie 7 kluczy, bez przeliczen."""
    return {
        "score": summary["score"],
        "survived": summary["survived"],
        "total": summary["total"],
        "scope": scope,
        "measured_at": measured_at,
        "commit": commit,
        "tool": tool,
    }


def write_baseline(path: str | Path, data: dict[str, Any]) -> None:
    """Zapisz baseline jako JSON czytelny w diffie (indent=2, newline na koncu)."""
    baseline_path = Path(path)
    try:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise _InputError(f"baseline: nie mozna zapisac pliku '{baseline_path}': {exc}") from exc


def _fmt(value: Any) -> str:
    return "none" if value is None else str(value)


def format_report(result: dict[str, Any]) -> str:
    """Zbuduj raport czytelny dla czlowieka, konczacy sie zamrozona linia:

        MUTATION-BASELINE: status=<status> score=<x|none> baseline=<y|none>
        delta=<z|none> survived=<n> total=<t> scope=<scope>

    Status "down" dostaje widoczne ostrzezenie "UWAGA" (spadek score jest
    sygnalem do przejrzenia, mimo ze krok pozostaje nieblokujacy). Status
    "first" wspomina wprost o zapisaniu pierwszego pomiaru. survived/total
    sa wypisywane zawsze, bo w trybie diffowym liczba przetrwalych
    mutantow jest glownym sygnalem, nie tylko sam procent.

    Dla statusow "first"/"up" (jedyne z write=True) opcjonalny klucz
    "written" rozstrzyga, czy zapis FAKTYCZNIE nastapil - main() ustawia
    go na False przy --dry-run mimo write=True, inaczej raport klamalby,
    ze baseline zostal zapisany, choc plik nie zostal tkniety. Brak klucza
    (np. w testach jednostkowych budujacych wynik recznie, bez przejscia
    przez main()) domyslnie przyjmuje written == write - zachowuje stare
    zachowanie sprzed tego rozroznienia. Opcjonalny klucz "baseline_path"
    (ustawiany przez main() z --baseline) trafia do komunikatu; jego brak
    nie jest bledem, komunikat wtedy pomija sciezke.
    """
    status = result["status"]
    score = result["score"]
    baseline = result["baseline"]
    delta = result["delta"]
    survived = result["survived"]
    total = result["total"]
    scope = result["scope"]
    baseline_path = result.get("baseline_path")

    def _save_line(written: bool) -> str:
        if written:
            return f"Nowy baseline zapisany: {baseline_path}" if baseline_path else "Nowy baseline zapisany."
        suffix = f" (bylby: {baseline_path})" if baseline_path else ""
        return f"Tryb dry-run: baseline NIE zostal zapisany{suffix}."

    lines: list[str] = []
    if status == "first":
        written = result.get("written", result["write"])
        lines.append(f"Brak wczesniejszego baseline dla zakresu '{scope}' - wykryto pierwszy pomiar mutation score ({_fmt(score)}).")
        lines.append(_save_line(written))
    elif status == "up":
        written = result.get("written", result["write"])
        lines.append(f"Mutation score wzrosl: {_fmt(baseline)} -> {_fmt(score)} (delta {_fmt(delta)}).")
        lines.append(_save_line(written))
    elif status == "same":
        lines.append(f"Mutation score bez zmian: {_fmt(score)}.")
    elif status == "down":
        lines.append(
            f"UWAGA: mutation score spadl z {_fmt(baseline)} do {_fmt(score)} "
            f"(delta {_fmt(delta)}). Baseline NIE zostal nadpisany - "
            "krok pozostaje nieblokujacy."
        )
    elif status == "no-mutants":
        lines.append(f"Brak mutantow w zakresie '{scope}' (total=0) - porownanie do baseline pominiete.")
    elif status == "scope-mismatch":
        lines.append(
            f"Zakres pomiaru ('{scope}') rozni sie od zakresu zapisanego baseline - delta jest wylacznie orientacyjna, baseline nie zostal nadpisany."
        )

    lines.append(f"Przetrwale mutanty: {survived} z {total}.")
    lines.append(
        f"MUTATION-BASELINE: status={status} score={_fmt(score)} "
        f"baseline={_fmt(baseline)} delta={_fmt(delta)} survived={survived} "
        f"total={total} scope={scope}"
    )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = _ArgumentParser(
        prog="mutation_baseline.py",
        description=("Zapadka baseline dla mutation score: porownuje przebieg z zapisanym baseline i raportuje trend bez blokowania builda."),
    )
    parser.add_argument("--summary", required=True, help="plik JSON z mutation_report.py, albo '-' dla stdin")
    parser.add_argument("--baseline", required=True, help="plik baseline (JSON), commitowany przez wlasciciela repo")
    parser.add_argument("--scope", default="diff", choices=["full", "diff"], help="zakres pomiaru (domyslnie diff)")
    parser.add_argument("--commit", default=None, help="sha commita mierzonego przebiegu")
    parser.add_argument("--tool", default=None, help="nazwa narzedzia mutacyjnego")
    parser.add_argument("--dry-run", action="store_true", help="pokaz raport, nigdy nie zapisuj baseline")

    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        print(f"mutation_baseline: {exc}", file=sys.stderr)
        return 64

    try:
        if args.summary == "-":
            text = sys.stdin.read()
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                raise _InputError(f"summary: niepoprawny JSON na stdin: {exc}") from exc
            summary = _validate_summary(raw, "summary")
        else:
            summary = load_summary(args.summary)

        baseline = load_baseline(args.baseline)
        result = compare(summary, baseline, args.scope)

        # "written" jest niezalezny od "write": write=True mowi ze WYPADALOBY
        # zapisac wg zapadki, written mowi czy zapis FAKTYCZNIE nastapil w tym
        # przebiegu (blokuje go --dry-run) - format_report uzywa written, zeby
        # nie oglaszac zapisu, ktorego --dry-run celowo nie wykonal.
        result["baseline_path"] = args.baseline
        result["written"] = False
        if result["write"] and not args.dry_run:
            new_baseline = build_baseline(
                summary,
                scope=args.scope,
                commit=args.commit,
                tool=args.tool,
                measured_at=date.today().isoformat(),
            )
            write_baseline(args.baseline, new_baseline)
            result["written"] = True
    except _InputError as exc:
        print(f"mutation_baseline: {exc}", file=sys.stderr)
        return 2

    print(format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
