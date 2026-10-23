#!/usr/bin/env python3
"""Parser raportow narzedzi mutacyjnych do wspolnego modelu survivora (MON-149).

Czyta raport jednego z czterech wspieranych narzedzi i zwraca znormalizowany
wynik: liczbe zabitych/przezylych mutantow, procentowy score i liste
survivorow w jednym wspolnym ksztalcie, niezaleznie od jezyka/narzedzia. To
jedyny kawalek kodu, ktory plugin wnosi do tej funkcji - dzieki niemu skille
korzystajace z wyniku sa jezyk-agnostyczne.

Wejscie jest granica systemu (raport wygenerowany przez cudze narzedzie w
cudzym repo) - niepoprawny JSON/XML, brakujacy plik i ksztalt niezgodny ze
schematem formatu nigdy nie konczy sie tracebackiem ani cicha pusta lista,
tylko czytelnym bledem po polsku na stderr i niezerowym kodem wyjscia. Pusta
lista survivorow oznacza WYLACZNIE "zero mutantow przezylo", nigdy "nie
umialem tego przeczytac".

Modul eksportuje:
    FORMATS = ("mte-json", "pit-xml", "mutmut", "cargo-mutants")
    parse_report(path, fmt) -> dict
        Rzuca _InputError (blad tresci wejscia) albo _UsageError (nieznany
        format przekazany bezposrednio, z pominieciem CLI/argparse).
    main(argv) -> int
        CLI: --report <plik> --format <fmt>. Wynik JSON w jednej linii na
        stdout (json.dumps(..., ensure_ascii=False)). Bledy na stderr, wtedy
        stdout jest pusty.

Kody wyjscia main(): 0 OK; 2 blad wejscia (plik nie istnieje, niepoprawny
JSON/XML, raport pusty, raport niezgodny ze schematem formatu); 64 blad
uzycia (brak --report/--format, nieznany --format, nieznany argument).

Wynik parse_report(): klucze zawsze obecne: score (float 0-100 zaokraglony
do 2 miejsc albo None gdy total == 0), total, killed, survived, survivors.
Niezmienniki: total == killed + survived; len(survivors) == survived;
score == round(100 * killed / total, 2) gdy total > 0, inaczej None.

Survivor: dokladnie 7 kluczy - file, line (int|None), mutator, original
(str|None), replacement (str|None), status (literal narzedzia, BEZ
tlumaczenia), description (str|None). Kolejnosc survivorow = kolejnosc w
raporcie zrodlowym.

Kazdy zbior literalow statusow ponizej jest wyprowadzony z realnego
fixture'u przechwyconego zgodnie z .claude/rules/external-tool-output.md
(patrz komentarze WHY przy kazdej stalej) - nigdy z wyobrazenia autora.
Literal spoza znanej listy dla danego formatu zawsze mapuje sie na survivor
(wariant odwracalny): falszywy survivor kosztuje jedno odrzucenie przez
czlowieka, falszywy "killed" cicho ukrywa dziure w pokryciu testami.

Potwierdzone na: Stryker 10.0.0, PIT 1.30.0, mutmut 3.8.0, cargo-mutants
27.1.0 (patrz naglowki proweniencji w tests/fixtures/{stryker,pitest,mutmut,
cargo-mutants}/).

Zero zaleznosci spoza biblioteki standardowej - skrypt dziala w cudzych
repo, gdzie nic poza systemowym Pythonem nie jest zagwarantowane. Skladnia
i API ograniczone do Python 3.9 (z wyjatkiem adnotacji typow dzieki
`from __future__ import annotations`), z tego samego powodu.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

FORMATS = ("mte-json", "pit-xml", "mutmut", "cargo-mutants")


class _UsageError(Exception):
    """Blad parsowania argumentow CLI albo nieznany format (kod wyjscia 64)."""


class _InputError(Exception):
    """Blad tresci wejscia: brak pliku, zly JSON/XML, zly ksztalt (kod wyjscia 2)."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        # argparse domyslnie wola sys.exit(2), co kolidowaloby z naszym kodem 2
        # zarezerwowanym dla bledow tresci wejscia - zamieniamy na wyjatek do
        # przechwycenia w main() i zamapowania na kod 64 (blad uzycia CLI).
        raise _UsageError(message)


# ---------------------------------------------------------------------------
# Wspolny model wyniku
# ---------------------------------------------------------------------------


def _survivor(
    *,
    file: str,
    line: int | None,
    mutator: str,
    original: str | None,
    replacement: str | None,
    status: str,
    description: str | None,
) -> dict[str, Any]:
    return {
        "file": file,
        "line": line,
        "mutator": mutator,
        "original": original,
        "replacement": replacement,
        "status": status,
        "description": description,
    }


def _result(killed: int, survivors: list[dict[str, Any]]) -> dict[str, Any]:
    survived = len(survivors)
    total = killed + survived
    score = None if total == 0 else round(100 * killed / total, 2)
    return {
        "score": score,
        "total": total,
        "killed": killed,
        "survived": survived,
        "survivors": survivors,
    }


def _load_json(path: Path, label: str) -> Any:
    if not path.is_file():
        raise _InputError(f"{label}: nie znaleziono pliku '{path}'")
    text = path.read_text(errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise _InputError(f"{label}: niepoprawny JSON w '{path}': {exc}") from exc


def _load_xml_root(path: Path, label: str) -> ET.Element:
    if not path.is_file():
        raise _InputError(f"{label}: nie znaleziono pliku '{path}'")
    text = path.read_text(errors="replace")
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise _InputError(f"{label}: niepoprawny XML w '{path}': {exc}") from exc


def _load_text(path: Path, label: str) -> str:
    if not path.is_file():
        raise _InputError(f"{label}: nie znaleziono pliku '{path}'")
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# mte-json (Stryker / Stryker.NET / Infection) - Mutation Testing Elements
# ---------------------------------------------------------------------------

# Zaobserwowane w tests/fixtures/stryker/mutation-report.10.0.0.json.
_MTE_KILLED = {"Killed", "Timeout"}
_MTE_SURVIVED = {"Survived", "NoCoverage"}
# NIE zaobserwowane w fixture (ktory ma tylko Killed/Survived) - czesc
# formalnego schematu statusow Mutation Testing Elements
# (https://mutation-testing-elements.netlify.app), odnotowane uczciwie jako
# "poza total" bo nie sa ani zabite, ani przezywajace, tylko niewazne dla
# wyniku (blad kompilacji/runtime, mutant zignorowany albo jeszcze nie
# uruchomiony).
_MTE_EXCLUDED = {"CompileError", "RuntimeError", "Ignored", "Pending"}


def _mte_original(source_lines: list[str] | None, start: dict[str, Any], end: dict[str, Any]) -> str | None:
    if source_lines is None:
        return None
    start_line, end_line = start.get("line"), end.get("line")
    if start_line != end_line or not isinstance(start_line, int):
        return None
    start_col, end_col = start.get("column"), end.get("column")
    if not isinstance(start_col, int) or not isinstance(end_col, int):
        return None
    if not (1 <= start_line <= len(source_lines)):
        return None
    line_text = source_lines[start_line - 1]
    return line_text[start_col - 1 : end_col - 1]


def _parse_mte_json(path: Path) -> dict[str, Any]:
    data = _load_json(path, "mte-json")
    if not isinstance(data, dict):
        raise _InputError("mte-json: oczekiwano obiektu JSON na najwyzszym poziomie")
    files = data.get("files")
    if not isinstance(files, dict):
        raise _InputError("mte-json: brak klucza 'files' (obiekt) na najwyzszym poziomie")

    killed = 0
    survivors: list[dict[str, Any]] = []
    for file_path, file_data in files.items():
        if not isinstance(file_data, dict):
            raise _InputError(f"mte-json: wpis 'files[{file_path!r}]' nie jest obiektem JSON")
        source = file_data.get("source")
        source_lines = source.split("\n") if isinstance(source, str) else None
        for mutant in file_data.get("mutants", []):
            status = mutant.get("status") or ""
            if status in _MTE_KILLED:
                killed += 1
                continue
            if status in _MTE_EXCLUDED:
                continue
            # status in _MTE_SURVIVED, albo literal nieznany -> survivor
            # (wariant odwracalny, patrz WHY na poczatku pliku).
            location = mutant.get("location") or {}
            start = location.get("start") or {}
            end = location.get("end") or {}
            survivors.append(
                _survivor(
                    file=file_path,
                    line=start.get("line"),
                    mutator=mutant.get("mutatorName") or "",
                    original=_mte_original(source_lines, start, end),
                    replacement=mutant.get("replacement"),
                    status=status,
                    description=mutant.get("description"),
                )
            )
    return _result(killed, survivors)


# ---------------------------------------------------------------------------
# pit-xml (PIT)
# ---------------------------------------------------------------------------

# Zaobserwowane w tests/fixtures/pitest/mutations.1.30.0.xml.
_PIT_NON_VIABLE = "NON_VIABLE"  # nie zaobserwowany w fixture, czesc formalnego schematu statusow PIT


def _pit_child_text(mutation: ET.Element, tag: str) -> str | None:
    child = mutation.find(tag)
    if child is None or child.text is None:
        return None
    return child.text


def _pit_file(mutated_class: str | None, source_file: str | None) -> str:
    source_file = source_file or ""
    if mutated_class and "." in mutated_class:
        package = mutated_class.rsplit(".", 1)[0].replace(".", "/")
        return f"{package}/{source_file}" if source_file else package
    return source_file


def _parse_pit_xml(path: Path) -> dict[str, Any]:
    root = _load_xml_root(path, "pit-xml")
    if root.tag != "mutations":
        raise _InputError(f"pit-xml: oczekiwano elementu glownego 'mutations', otrzymano '{root.tag}'")

    killed = 0
    survivors: list[dict[str, Any]] = []
    for mutation in root.findall("mutation"):
        detected = mutation.get("detected")
        status = mutation.get("status") or ""
        if detected == "true":
            killed += 1
            continue
        if detected == "false" and status == _PIT_NON_VIABLE:
            continue
        # detected brakuje, detected == "false" (bez NON_VIABLE), albo
        # wartosc detected spoza "true"/"false" -> survivor (wariant
        # odwracalny, patrz WHY na poczatku pliku).
        line_text = _pit_child_text(mutation, "lineNumber")
        mutator_text = _pit_child_text(mutation, "mutator") or ""
        survivors.append(
            _survivor(
                file=_pit_file(_pit_child_text(mutation, "mutatedClass"), _pit_child_text(mutation, "sourceFile")),
                line=int(line_text) if line_text and line_text.strip().lstrip("-").isdigit() else None,
                mutator=mutator_text.rsplit(".", 1)[-1],
                original=None,
                replacement=None,
                status=status,
                description=_pit_child_text(mutation, "description"),
            )
        )
    return _result(killed, survivors)


# ---------------------------------------------------------------------------
# mutmut (mutmut 3)
# ---------------------------------------------------------------------------

# Linia wyniku `mutmut results --all true`: "<wciecie><id>: <status>". Nie
# zawezamy do "linia wyniku" na samym tym wzorcu, bo ostrzezenia narzedzia
# (np. "/usr/local/.../configuration.py:143: UserWarning: ...") tez pasuja
# skladniowo do "<token>: <reszta>" - dopiero struktura id w _MUTMUT_ID_RE
# odsiewa prawdziwe wyniki (patrz tests/fixtures/mutmut/results.3.8.0.txt).
_MUTMUT_LINE_RE = re.compile(r"^\s*(\S+): (.+?)\s*$")
_MUTMUT_ID_RE = re.compile(r"^(.*)\.x_(\w+)__mutmut_(\d+)$")

# Zaobserwowane jako linie wyniku w tests/fixtures/mutmut/results-all.3.8.0.txt.
_MUTMUT_SURVIVED = {"survived", "no tests"}
# "timeout" zaobserwowany wylacznie jako licznik w
# tests/fixtures/mutmut/export-cicd-stats.3.8.0.json (dokumentacja formatu,
# NIE wejscie parsera) - nie ma pojedynczej linii wyniku z tym statusem w
# zadnym fixture, ale licznik dowodzi ze mutmut liczy timeout jako zabity.
_MUTMUT_KILLED = {"killed", "timeout"}
# jw. - "skipped" zaobserwowany tylko jako licznik dokumentacyjny.
_MUTMUT_EXCLUDED = {"skipped"}


def _parse_mutmut(path: Path) -> dict[str, Any]:
    text = _load_text(path, "mutmut")

    recognized = 0
    killed = 0
    survivors: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line_match = _MUTMUT_LINE_RE.match(raw_line)
        if line_match is None:
            continue
        candidate_id, status = line_match.group(1), line_match.group(2)
        id_match = _MUTMUT_ID_RE.match(candidate_id)
        if id_match is None:
            continue

        recognized += 1
        module_part, function_name, _mutant_number = id_match.groups()
        if status in _MUTMUT_KILLED:
            killed += 1
            continue
        if status in _MUTMUT_EXCLUDED:
            continue
        # status in _MUTMUT_SURVIVED, albo literal nieznany (np.
        # "suspicious") -> survivor (wariant odwracalny, patrz WHY na
        # poczatku pliku).
        survivors.append(
            _survivor(
                file=f"{module_part.replace('.', '/')}.py",
                line=None,
                mutator=candidate_id,
                original=None,
                replacement=None,
                status=status,
                description=function_name,
            )
        )

    if recognized == 0:
        raise _InputError(
            "mutmut: brak linii wyniku w raporcie - uzyj 'mutmut results --all true' "
            "(pusty output bez flagi --all oznacza 'wszystko zabite', nieodrozniane od zlego pliku)"
        )
    return _result(killed, survivors)


# ---------------------------------------------------------------------------
# cargo-mutants
# ---------------------------------------------------------------------------

# Zaobserwowane w tests/fixtures/cargo-mutants/outcomes.27.1.0.json.
_CARGO_SURVIVED = {"MissedMutant"}
# "Timeout" zaobserwowany wylacznie jako licznik top-level "timeout"=0 w obu
# fixture'ach - zaden pojedynczy mutant nie ma tego statusu, ale schemat
# licznikow (caught/missed/timeout/unviable) dowodzi ze timeout liczy sie
# jako zabity.
_CARGO_KILLED = {"CaughtMutant", "Timeout"}
# jw. - "unviable"=0 w obu fixture'ach, analogicznie poza total.
_CARGO_EXCLUDED = {"Unviable"}


def _parse_cargo_mutants(path: Path) -> dict[str, Any]:
    data = _load_json(path, "cargo-mutants")
    if not isinstance(data, dict):
        raise _InputError("cargo-mutants: oczekiwano obiektu JSON na najwyzszym poziomie")
    outcomes = data.get("outcomes")
    if not isinstance(outcomes, list):
        raise _InputError("cargo-mutants: brak klucza 'outcomes' (lista) na najwyzszym poziomie")

    killed = 0
    survivors: list[dict[str, Any]] = []
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            raise _InputError("cargo-mutants: element 'outcomes' nie jest obiektem JSON")
        scenario = outcome.get("scenario")
        if scenario == "Baseline":
            continue
        if not (isinstance(scenario, dict) and "Mutant" in scenario):
            raise _InputError("cargo-mutants: nieoczekiwany ksztalt pola 'scenario' (ani 'Baseline', ani obiekt z 'Mutant')")

        mutant = scenario["Mutant"]
        status = outcome.get("summary") or ""
        if status in _CARGO_KILLED:
            killed += 1
            continue
        if status in _CARGO_EXCLUDED:
            continue
        # status in _CARGO_SURVIVED, albo literal nieznany -> survivor
        # (wariant odwracalny, patrz WHY na poczatku pliku).
        span = mutant.get("span") or {}
        start = span.get("start") or {}
        survivors.append(
            _survivor(
                file=mutant.get("file") or "",
                line=start.get("line"),
                mutator=mutant.get("genre") or "",
                original=None,
                replacement=mutant.get("replacement"),
                status=status,
                description=mutant.get("name"),
            )
        )
    return _result(killed, survivors)


_PARSERS = {
    "mte-json": _parse_mte_json,
    "pit-xml": _parse_pit_xml,
    "mutmut": _parse_mutmut,
    "cargo-mutants": _parse_cargo_mutants,
}


def parse_report(path: str | Path, fmt: str) -> dict[str, Any]:
    """Sparsuj raport narzedzia mutacyjnego do wspolnego modelu wyniku.

    Rzuca _UsageError gdy fmt spoza FORMATS (wywolanie z pominieciem CLI,
    ktore normalnie ogranicza wybor przez argparse choices), albo
    _InputError gdy tresc raportu jest niepoprawna albo niezgodna ze
    schematem danego formatu.
    """
    if fmt not in FORMATS:
        raise _UsageError(f"nieznany format '{fmt}' (oczekiwano jednego z: {', '.join(FORMATS)})")
    return _PARSERS[fmt](Path(path))


def main(argv: list[str]) -> int:
    parser = _ArgumentParser(
        prog="mutation_report.py",
        description="Sparsuj raport narzedzia mutacyjnego do wspolnego modelu survivora.",
    )
    parser.add_argument("--report", required=True, help="sciezka do pliku raportu")
    parser.add_argument("--format", required=True, choices=FORMATS, help="format raportu")

    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        print(f"mutation_report: {exc}", file=sys.stderr)
        return 64

    try:
        result = parse_report(args.report, args.format)
    except _UsageError as exc:
        print(f"mutation_report: {exc}", file=sys.stderr)
        return 64
    except Exception as exc:
        # Granica CLI: raport zewnetrznego narzedzia nigdy nie moze skonczyc
        # sie tracebackiem - kazdy niespodziewany blad tresci wejscia mapuje
        # sie na czytelny komunikat i kod 2.
        print(f"mutation_report: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
