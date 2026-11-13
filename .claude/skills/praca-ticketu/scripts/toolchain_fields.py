#!/usr/bin/env python3
"""Normalizacja pola `pełny przebieg` strony wiki `toolchain` (MON-269).

Pole `pełny przebieg: lokalnie|ci` stoi w sekcji `## Test` strony `toolchain`. Skille `work`,
`work-simple` i `setup` pytają tym skryptem, czy pełny zestaw testów idzie lokalnie (zachowanie
sprzed MON-269, wartość domyślna), czy weryfikuje go pipeline MR (`ci`).

Wartość nieznana albo pusta to ZAWSZE `lokalnie` z ostrzeżeniem, nigdy `ci`: literówka na stronie
nie może po cichu wyłączyć testów lokalnych. Pole poza sekcją `## Test` (np. w `## Worktree`)
jest ignorowane, bo runtime czyta tylko `## Test`.

CLI (dokładnie jedna z dwóch opcji):
    python3 toolchain_fields.py --value <surowa wartość pola>
    python3 toolchain_fields.py --file <plik z treścią strony toolchain>

Wyjście: stdout to jedna linia JSON z kluczami w tej kolejności:
    {"pelny_przebieg": "lokalnie|ci", "zrodlo": "pole|domyslna|nieznana", "ostrzezenia": [...]}
`zrodlo`: `pole` (poprawna wartość z pola), `domyslna` (brak sekcji `## Test` albo brak pola),
`nieznana` (wartość pusta lub spoza `lokalnie`/`ci`). Każde ostrzeżenie trafia też na stderr jako
`toolchain_fields: ostrzezenie: <treść>`. Kilka linii pola w `## Test`: liczy się pierwsza,
reszta to ostrzeżenie o duplikacie.

Kody wyjścia:
    0  - wynik policzony (także wartość nieznana: to ostrzeżenie, nie błąd).
    2  - błąd wejścia: `--file` wskazuje brak pliku, katalog, plik nieczytelny, nie-UTF-8 albo
         pusty po strip(); komunikat `toolchain_fields: <powód>` na stderr, stdout pusty.
    64 - błąd użycia: brak opcji, obie naraz, nieznana flaga; stdout pusty.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import NoReturn

MODES = ("lokalnie", "ci")
DEFAULT_MODE = "lokalnie"

_TEST_HEADING = "## test"
_H2_PREFIX = "## "
_FIELD_LINE = re.compile(r"^\s*(?:-\s+)?(?:pełny|pelny)\s+przebieg\s*:(.*)$", re.IGNORECASE)


class _UsageError(Exception):
    pass


class _InputError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    # argparse kończy proces kodem 2, a kontrakt skryptu rezerwuje 2 dla błędu wejścia.
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def normalize_full_run(raw: str) -> tuple[str, str, list[str]]:
    value = raw.strip()
    key = value.casefold()
    if key in MODES:
        return key, "pole", []
    if not value:
        return DEFAULT_MODE, "nieznana", [f"pole 'pełny przebieg' jest puste, używam '{DEFAULT_MODE}'"]
    return (
        DEFAULT_MODE,
        "nieznana",
        [f"nieznana wartość pola 'pełny przebieg': '{value}' (dozwolone: lokalnie, ci), używam '{DEFAULT_MODE}'"],
    )


def parse_full_run(text: str) -> tuple[str, str, list[str]]:
    values: list[str] = []
    in_test = False
    for line in text.splitlines():
        if line.startswith(_H2_PREFIX):
            in_test = line.rstrip().casefold() == _TEST_HEADING
            continue
        if not in_test:
            continue
        match = _FIELD_LINE.match(line)
        if match:
            values.append(match.group(1))
    if not values:
        return DEFAULT_MODE, "domyslna", []
    mode, source, warnings = normalize_full_run(values[0])
    if len(values) > 1:
        warnings.append(f"duplikat pola 'pełny przebieg' w sekcji '## Test' ({len(values)} linii), liczy się pierwsza")
    return mode, source, warnings


def _read_page(path: str) -> str:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise _InputError(f"nie można odczytać pliku {path}: {exc.strerror or exc}") from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _InputError(f"plik {path} nie jest poprawnym UTF-8") from exc
    if not text.strip():
        raise _InputError(f"plik {path} jest pusty")
    return text


def _build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False: skrót flagi (`--val`) ma być błędem użycia (64), jak każda nieznana flaga.
    parser = _Parser(
        prog="toolchain_fields.py",
        description="Normalizuje pole 'pełny przebieg' strony toolchain.",
        allow_abbrev=False,
    )
    parser.add_argument("--value", help="surowa wartość pola")
    parser.add_argument("--file", help="plik z treścią strony toolchain")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        if (args.value is None) == (args.file is None):
            raise _UsageError("podaj dokładnie jedną z opcji: --value albo --file")
    except _UsageError as exc:
        print(f"toolchain_fields: błąd użycia: {exc}", file=sys.stderr)
        return 64
    except SystemExit as exc:
        # --help kończy się w argparse SystemExit(0); nic innego tu nie dociera, bo error() jest nadpisane.
        return int(exc.code or 0)

    try:
        if args.value is not None:
            mode, source, warnings = normalize_full_run(args.value)
        else:
            mode, source, warnings = parse_full_run(_read_page(args.file))
    except _InputError as exc:
        print(f"toolchain_fields: {exc}", file=sys.stderr)
        return 2

    for warning in warnings:
        print(f"toolchain_fields: ostrzezenie: {warning}", file=sys.stderr)
    print(json.dumps({"pelny_przebieg": mode, "zrodlo": source, "ostrzezenia": warnings}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
