#!/usr/bin/env python3
"""Wrapper operacji MCP Monolynx przez CLI `monolynx` (MON-232).

Skille zapisuja operacje jako nazwa toola MCP + argumenty. Ten skrypt wykonuje
taka operacje przez CLI, gdy CLI jest dostepne i zalogowane, a w kazdym innym
przypadku mowi, ze trzeba uzyc toola MCP (kod 3 + linia `OP-FALLBACK`). Nie
robi `auth whoami` - jedno wywolanie CLI na operacje, bez ponowien.

Wywolanie:

    monolynx_op.py <op> --project_slug <slug> [--<param> <wartosc> ...]

    <op>        nazwa toola MCP (klucz `OPS`), np. get_ticket, add_comment
    --<param>   DOKLADNA nazwa parametru toola MCP z podkreslnikami
                (`--ticket_id`, `--content`, `--assignee_email`, ...); wrapper
                przekazuje tylko podane parametry, bez wartosci domyslnych
    wartosc     `-` = czytaj ze stdin wrappera, `@<sciezka>` = czytaj z pliku
                UTF-8 (dluga tresc: komentarz, opis, tresc wiki); listy
                (`label_ids`, `blocked_by_ids`, `acceptance_criteria`) jako
                tablica JSON stringow w jednej wartosci; wartosc bool jako
                `true` albo `false`

Parametry `compact` (`list_tickets`, -> `--compact`: lista bez opisow) i `brief`
(`create_ticket`, `update_ticket`, -> `--brief`: potwierdzenie tylko z id, key, title,
status, sprint_id, blocked_by i blocks) sa parametrami wrappera, nie tooli MCP; wrapper
nigdy nie wlacza ich sam, a przy fallbacku (kod 3) tool MCP wolasz bez nich.

CLI jest wolane jako `monolynx -o json --project <slug> <grupa> <komenda>
[pozycyjne] [flagi w kolejnosci podania parametrow]`. Parametry z dluga trescia
(`content` komentarza i wiki, `description` ticketu) ida do CLI przez stdin
(`--body -`, `--content -`, `--description -`), nie przez argv.

Kody wyjscia:

    0   stdout = stdout CLI bez zmian (JSON API v2)
    3   uzyj toola MCP: ostatnia linia stdout `OP-FALLBACK: mcp tool=<op>`,
        powod na stderr. Wrapper zwraca 3, gdy: op nie ma komendy CLI albo jest
        nieznana, `MONOLYNX_TRANSPORT=mcp`, brak `monolynx` w PATH, parametr bez
        odpowiednika w CLI, CLI zwrocilo 3 (brak logowania) albo 2 (blad uzycia,
        czyli dryf flag - nic nie zostalo zapisane)
    2   CLI zakonczylo sie 1 albo 4 (blad API / siec), innym nieznanym kodem albo
        przekroczylo czas: stderr CLI przekazany, stdout pusty. To NIE jest
        fallback - zapis mogl dotrzec do serwera, wiec przed ponowieniem przez
        MCP sprawdz, czy sie nie powiodl
    64  blad uzycia wrappera (brak op, brak/zly `--project_slug`, flaga bez
        wartosci, argument pozycyjny, zly JSON listy, zla wartosc bool, brak
        pliku `@sciezka`); CLI nie jest wolane

`create_sprint` wymaga `--name` i `--start_date`, a `find_graph_path` `--source_id` i `--target_id`
(brak -> 64, CLI nie jest wolane). Graf (MON-248) ma tylko odczyty: `list_graph_nodes`,
`get_graph_node` (`--node_id` pozycyjny), `query_graph`, `find_graph_path`, `get_graph_stats`;
narzedzia zapisu grafu nie maja komendy CLI i zawsze koncza sie kodem 3.
Blog (MON-261): `list_blog_posts` (`--status`), `get_blog_post` (`--page_id` pozycyjny) i `set_blog_post_meta`
(`--page_id` pozycyjny; `author_name` -> `--author`, `tags` jako tablica JSON -> powtarzane `--tag`, pusta
tablica -> `--no-tags`, pusty tekst w `translation_of_id`, `cover_attachment_id` i `last_verified_at` czysci
pole). Pierwsze dwa to odczyty (`READ_OPS`), trzeci to zapis.
Blog lint (MON-262): `lint_blog_post` (`--page_id` pozycyjny) to odczyt. `monolynx blog lint` konczy sie kodem 1,
gdy raport zawiera blad lintu; dla tej jednej operacji wrapper traktuje kod 1 ze stdout bedacym obiektem JSON
z lista `issues` jako wynik (wypisuje stdout, kod 0), a kazdy inny kod 1 zostaje bledem operacji (kod 2).
Blog publikacja (MON-263): `publish_blog_post` (`--page_id` pozycyjny, `--force true|false`: true -> flaga `--force`
CLI, false -> brak flagi) i `unpublish_blog_post` (`--page_id` pozycyjny) to zapisy, wiec nie wchodza do `READ_OPS`.
Odmowa publikacji przez lint to 422, czyli kod 1 CLI i kod 2 wrappera z powodem na stderr (brak wyjatku jak przy lint).
`goal` sprintu trafia do argv CLI jako zwykla opcja po rozwinieciu `-` / `@plik`
przez wrapper, bo CLI nie ma `--goal -` (literal `-` stalby sie celem sprintu).

Kody wyjscia CLI pochodza z `tests/fixtures/monolynx/` (CLI 0.1.0, `auth
whoami` bez tokenu = 3, nieistniejaca komenda = 2) i z `cli/src/monolynx_cli/
main.py`: 1 blad API/lokalny, 2 uzycie, 3 auth, 4 siec.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Tylko odczyty. Hook `plugin/hooks/bash_guard.py` trzyma kopie tej listy
# (`MONOLYNX_READ_OPS`), a `tests/unit/test_bash_guard.py` pilnuje rownosci.
READ_OPS = frozenset(
    {
        "get_ticket",
        "list_tickets",
        "search_tickets",
        "list_comments",
        "get_board",
        "list_sprints",
        "get_burndown",
        "get_sprint",
        "list_acceptance_criteria",
        "list_labels",
        "search_wiki",
        "get_wiki_page",
        "list_wiki_pages",
        "get_wiki_config",
        "list_graph_nodes",
        "get_graph_node",
        "query_graph",
        "find_graph_path",
        "get_graph_stats",
        "list_blog_posts",
        "get_blog_post",
        "lint_blog_post",
    }
)

_CLI = "monolynx"
# Gorny limit jednego wywolania CLI; po nim zapis mogl juz dotrzec do serwera, wiec to kod 2, nie fallback.
_CLI_TIMEOUT = 120
_OP_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_PARAM_FLAG = re.compile(r"^--([a-z][a-z0-9_]*)$")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Rodzaje mapowania parametru -> argv CLI.
_OPT = "opt"  # --flag <wartosc>
_STDIN = "stdin"  # --flag -, tresc na stdin CLI
_LIST = "list"  # powtarzane --flag <element>; pusta lista nic nie dodaje
_LIST_REPLACE = "list_replace"  # jak _LIST, ale pusta lista (zdjecie wszystkich) nie ma flagi w CLI
_BLOCKERS = "blockers"  # jak _LIST, pusta lista -> flaga zdejmujaca (--no-blockers, --no-tags)
_PRESENT = "present"  # true -> --flag, false -> nic
_PAIR = "pair"  # true -> pierwsza flaga, false -> druga

_STATUS = {"status": (_OPT, "--status")}
_DUE = {
    "due_date_before": (_OPT, "--due-before"),
    "due_before": (_OPT, "--due-before"),
    "due_date_after": (_OPT, "--due-after"),
    "due_after": (_OPT, "--due-after"),
}
_TICKET_COMMON = {
    "title": (_OPT, "--title"),
    "description": (_STDIN, "--description"),
    "priority": (_OPT, "--priority"),
    "story_points": (_OPT, "--sp"),
    "sprint_id": (_OPT, "--sprint"),
    "assignee_email": (_OPT, "--assignee"),
    "due_date": (_OPT, "--due"),
}

# op -> (komenda CLI, parametry pozycyjne [(nazwa, wymagany)], parametry-flagi).
# Flagi sprawdzone wobec `monolynx <grupa> <komenda> --help` (fixture'y w
# tests/fixtures/monolynx/: CLI 0.1.0 oraz `*.source.txt` z drzewa zrodel dla flag niewydanych) i wobec `cli/src/monolynx_cli/commands/`.
OPS = {
    "get_ticket": (("ticket", "get"), (("ticket_id", True),), {}),
    "list_tickets": (
        ("ticket", "list"),
        (),
        {
            **_STATUS,
            "priority": (_OPT, "--priority"),
            "search": (_OPT, "--search"),
            "sprint_id": (_OPT, "--sprint"),
            **_DUE,
            "overdue": (_PRESENT, "--overdue"),
            "label_id": (_OPT, "--label"),
            "page": (_OPT, "--page"),
            "compact": (_PRESENT, "--compact"),
        },
    ),
    "search_tickets": (
        ("ticket", "search"),
        (("query", False),),
        {
            **_STATUS,
            "priority": (_OPT, "--priority"),
            "assignee_email": (_OPT, "--assignee"),
            "sprint_id": (_OPT, "--sprint"),
            **_DUE,
            "page": (_OPT, "--page"),
        },
    ),
    "add_comment": (("ticket", "comment", "add"), (("ticket_id", True),), {"content": (_STDIN, "--body")}),
    "list_comments": (("ticket", "comment", "list"), (("ticket_id", True),), {}),
    "update_ticket": (
        ("ticket", "update"),
        (("ticket_id", True),),
        {
            **_STATUS,
            **_TICKET_COMMON,
            "label_ids": (_LIST_REPLACE, "--label"),
            "blocked_by_ids": (_BLOCKERS, "--blocked-by", "--no-blockers"),
            "brief": (_PRESENT, "--brief"),
        },
    ),
    "create_ticket": (
        ("ticket", "create"),
        (),
        {
            **_TICKET_COMMON,
            "label_ids": (_LIST, "--label"),
            "acceptance_criteria": (_LIST, "--ac"),
            "spec_page_id": (_OPT, "--spec-page"),
            "blocked_by_ids": (_LIST, "--blocked-by"),
            "brief": (_PRESENT, "--brief"),
        },
    ),
    "get_board": (("sprint", "board"), (), {}),
    "list_sprints": (("sprint", "list"), (), _STATUS),
    "complete_sprint": (("sprint", "complete"), (("sprint_id", True),), {}),
    "get_burndown": (("sprint", "burndown"), (("sprint_id", False),), {}),
    "get_sprint": (("sprint", "get"), (("sprint_id", True),), {}),
    "start_sprint": (("sprint", "start"), (("sprint_id", True),), {}),
    "create_sprint": (
        ("sprint", "create"),
        (),
        {"name": (_OPT, "--name"), "start_date": (_OPT, "--start"), "end_date": (_OPT, "--end"), "goal": (_OPT, "--goal")},
    ),
    "update_sprint": (
        ("sprint", "update"),
        (("sprint_id", True),),
        {"name": (_OPT, "--name"), "goal": (_OPT, "--goal"), "start_date": (_OPT, "--start"), "end_date": (_OPT, "--end")},
    ),
    "list_acceptance_criteria": (("ticket", "ac", "list"), (("ticket_id", True),), {}),
    "add_acceptance_criterion": (("ticket", "ac", "add"), (("ticket_id", True), ("description", True)), {}),
    "update_acceptance_criterion": (
        ("ticket", "ac", "update"),
        (("ticket_id", True), ("criterion_id", True)),
        {"description": (_OPT, "--description"), "is_completed": (_PAIR, "--done", "--not-done")},
    ),
    "list_labels": (("ticket", "label", "list"), (), {}),
    "search_wiki": (("wiki", "search"), (("query", True),), {"limit": (_OPT, "--limit")}),
    "get_wiki_page": (("wiki", "get"), (("page_id", True),), {}),
    "list_wiki_pages": (("wiki", "list"), (), {}),
    "create_wiki_page": (
        ("wiki", "create"),
        (),
        {
            "title": (_OPT, "--title"),
            "content": (_STDIN, "--content"),
            "parent_id": (_OPT, "--parent"),
            "position": (_OPT, "--position"),
            "is_public": (_PAIR, "--public", "--no-public"),
        },
    ),
    "update_wiki_page": (
        ("wiki", "update"),
        (("page_id", True),),
        {
            "title": (_OPT, "--title"),
            "content": (_STDIN, "--content"),
            "position": (_OPT, "--position"),
            "is_public": (_PAIR, "--public", "--no-public"),
        },
    ),
    "get_wiki_config": (("wiki", "config", "get"), (), {}),
    "list_graph_nodes": (
        ("graph", "nodes"),
        (),
        {"type": (_OPT, "--type"), "search": (_OPT, "--search"), "limit": (_OPT, "--limit")},
    ),
    "get_graph_node": (
        ("graph", "node"),
        (("node_id", True),),
        {"depth": (_OPT, "--depth"), "relation_types": (_LIST, "--relation-type"), "node_types": (_LIST, "--node-type")},
    ),
    "query_graph": (
        ("graph", "query"),
        (),
        {"node_type": (_OPT, "--node-type"), "search": (_OPT, "--search"), "limit": (_OPT, "--limit")},
    ),
    "find_graph_path": (
        ("graph", "path"),
        (),
        {"source_id": (_OPT, "--source-id"), "target_id": (_OPT, "--target-id")},
    ),
    "get_graph_stats": (("graph", "stats"), (), {}),
    "list_blog_posts": (("blog", "list"), (), _STATUS),
    "get_blog_post": (("blog", "get"), (("page_id", True),), {}),
    "lint_blog_post": (("blog", "lint"), (("page_id", True),), {}),
    "publish_blog_post": (("blog", "publish"), (("page_id", True),), {"force": (_PRESENT, "--force")}),
    "unpublish_blog_post": (("blog", "unpublish"), (("page_id", True),), {}),
    "set_blog_post_meta": (
        ("blog", "meta"),
        (("page_id", True),),
        {
            "slug": (_OPT, "--slug"),
            "description": (_OPT, "--description"),
            "author_name": (_OPT, "--author"),
            "tags": (_BLOCKERS, "--tag", "--no-tags"),
            "lang": (_OPT, "--lang"),
            "translation_of_id": (_OPT, "--translation-of"),
            "cover_attachment_id": (_OPT, "--cover"),
            "last_verified_at": (_OPT, "--verified"),
        },
    ),
}

# Wymagane parametry-flagi: krotka flag w OPS nie wyraza wymagalnosci (maja ja tylko pozycyjne).
# Brak -> kod 64 przed wywolaniem CLI, zamiast bledu uzycia CLI (kod 2 -> fallback na MCP).
REQUIRED_FLAG_PARAMS = {"create_sprint": ("name", "start_date"), "find_graph_path": ("source_id", "target_id")}


class _UsageError(Exception):
    """Blad uzycia wrappera -> kod 64."""


class _FallbackError(Exception):
    """Operacja nie da sie wykonac przez CLI -> kod 3 i tool MCP."""


def _err(message: str) -> None:
    print(f"monolynx_op: {message}", file=sys.stderr)


def _fallback(op: str, reason: str) -> int:
    _err(reason)
    print(f"OP-FALLBACK: mcp tool={op}")
    return 3


def _parse_args(argv: list[str]) -> tuple[str, list[tuple[str, str]]]:
    """(op, parametry w kolejnosci podania [(nazwa, wartosc)]) z surowego argv."""
    if not argv:
        raise _UsageError("brak nazwy operacji (pierwszy argument)")
    op, rest = argv[0], argv[1:]
    if not _OP_NAME.match(op):
        raise _UsageError(f"niepoprawna nazwa operacji: {op!r}")
    params: list[tuple[str, str]] = []
    index = 0
    while index < len(rest):
        match = _PARAM_FLAG.match(rest[index])
        if match is None:
            raise _UsageError(f"oczekiwano --<parametr>, jest: {rest[index]!r}")
        if index + 1 >= len(rest):
            raise _UsageError(f"parametr {rest[index]} bez wartosci")
        params.append((match.group(1), rest[index + 1]))
        index += 2
    return op, params


def _resolve_value(raw: str, stdin_cache: list[str]) -> str:
    """`-` = stdin wrappera (czytany raz), `@sciezka` = plik UTF-8, inaczej wartosc literalna."""
    if raw == "-":
        if not stdin_cache:
            try:
                stdin_cache.append(sys.stdin.read())
            except (OSError, UnicodeDecodeError) as exc:
                raise _UsageError(f"nie mozna odczytac stdin: {exc}") from exc
        return stdin_cache[0]
    if raw.startswith("@") and len(raw) > 1:
        try:
            return Path(raw[1:]).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise _UsageError(f"nie mozna odczytac pliku {raw[1:]}: {exc}") from exc
    return raw


def _parse_bool(name: str, value: str) -> bool:
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise _UsageError(f"parametr {name}: oczekiwano true albo false, jest {value!r}")


def _parse_list(name: str, value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except ValueError as exc:
        raise _UsageError(f"parametr {name}: oczekiwano tablicy JSON, {exc}") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise _UsageError(f"parametr {name}: oczekiwano tablicy JSON stringow")
    return parsed


def _build(op: str, params: list[tuple[str, str]]) -> tuple[list[str], str | None]:
    """(argv CLI bez nazwy binarki, stdin dla CLI albo None). Rzuca _UsageError / _FallbackError."""
    command, positional, flag_params = OPS[op]
    seen: set[str] = set()
    values: dict[str, str] = {}
    ordered: list[tuple[str, str]] = []
    stdin_cache: list[str] = []
    for name, raw in params:
        if name in seen:
            raise _UsageError(f"parametr {name} podany wiecej niz raz")
        seen.add(name)
        if name == "project_slug":
            values[name] = raw
            continue
        known = name in flag_params or any(name == pos for pos, _required in positional)
        if not known:
            raise _FallbackError(f"parametr {name} nie ma odpowiednika w CLI dla {op}")
        values[name] = _resolve_value(raw, stdin_cache)
        ordered.append((name, values[name]))

    slug = values.get("project_slug", "")
    if not _SLUG.match(slug):
        raise _UsageError("brak albo niepoprawny --project_slug (male litery, cyfry, myslniki)")

    for name in REQUIRED_FLAG_PARAMS.get(op, ()):
        if name not in values:
            raise _UsageError(f"brak wymaganego parametru {name}")

    argv = ["-o", "json", "--project", slug, *command]
    for name, required in positional:
        if name not in values:
            if required:
                raise _UsageError(f"brak wymaganego parametru {name}")
            continue
        if values[name].startswith("-"):
            # CLI przeczytaloby taka wartosc jako flage; MCP przyjmuje dowolny tekst.
            raise _FallbackError(f"wartosc pozycyjna {name} zaczyna sie od '-', CLI jej nie przyjmie")
        argv.append(values[name])

    cli_stdin: str | None = None
    positional_names = {name for name, _required in positional}
    for name, value in ordered:
        if name in positional_names:
            continue
        kind, *flags = flag_params[name]
        if kind == _OPT:
            argv.extend([flags[0], value])
        elif kind == _STDIN:
            argv.extend([flags[0], "-"])
            cli_stdin = value
        elif kind in (_LIST, _LIST_REPLACE, _BLOCKERS):
            items = _parse_list(name, value)
            if items:
                for item in items:
                    argv.extend([flags[0], item])
            elif kind == _BLOCKERS:
                argv.append(flags[1])
            elif kind == _LIST_REPLACE:
                raise _FallbackError(f"CLI nie ma flagi zdejmujacej wszystkie wartosci parametru {name}")
        elif kind == _PRESENT:
            if _parse_bool(name, value):
                argv.append(flags[0])
        elif kind == _PAIR:
            argv.append(flags[0] if _parse_bool(name, value) else flags[1])
    return argv, cli_stdin


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _is_lint_report(stdout: bytes) -> bool:
    try:
        payload = json.loads(_decode(stdout))
    except ValueError:
        return False
    return isinstance(payload, dict) and isinstance(payload.get("issues"), list)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        op, params = _parse_args(args)
    except _UsageError as exc:
        _err(str(exc))
        return 64
    if op not in OPS:
        return _fallback(op, f"operacja {op} nie ma komendy CLI")
    if os.environ.get("MONOLYNX_TRANSPORT", "").strip().lower() == "mcp":
        return _fallback(op, "MONOLYNX_TRANSPORT=mcp wymusza prace przez MCP")
    try:
        cli_argv, cli_stdin = _build(op, params)
    except _UsageError as exc:
        _err(str(exc))
        return 64
    except _FallbackError as exc:
        return _fallback(op, str(exc))

    binary = shutil.which(_CLI)
    if binary is None:
        return _fallback(op, "brak CLI monolynx w PATH")
    try:
        result = subprocess.run(
            [binary, *cli_argv],
            input=None if cli_stdin is None else cli_stdin.encode("utf-8"),
            stdin=subprocess.DEVNULL if cli_stdin is None else None,
            capture_output=True,
            timeout=_CLI_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _err(f"CLI nie odpowiedzialo w {_CLI_TIMEOUT} s; zapis mogl dotrzec do serwera - sprawdz stan przed ponowieniem")
        return 2
    except OSError as exc:
        # Proces nie wystartowal, wiec nic nie dotarlo do serwera.
        return _fallback(op, f"nie mozna uruchomic CLI: {exc}")

    if result.returncode == 0:
        sys.stdout.flush()
        sys.stdout.buffer.write(result.stdout)
        sys.stdout.buffer.flush()
        if result.stderr:
            sys.stderr.write(_decode(result.stderr))
        return 0
    if result.returncode == 1 and op == "lint_blog_post" and _is_lint_report(result.stdout):
        # Raport z bledami lintu to wynik odczytu, nie awaria: CLI sygnalizuje je kodem 1, ale stdout jest kompletny.
        sys.stdout.flush()
        sys.stdout.buffer.write(result.stdout)
        sys.stdout.buffer.flush()
        if result.stderr:
            sys.stderr.write(_decode(result.stderr))
        return 0
    detail = _decode(result.stderr).strip()
    if result.returncode in (2, 3):
        # 3 = brak logowania, 2 = dryf flag (komenda/flaga nie istnieje): w obu przypadkach nic nie zapisano.
        return _fallback(op, f"CLI zakonczylo sie kodem {result.returncode}: {detail or 'brak komunikatu'}")
    # 1 = blad API/lokalny, 4 = siec, kazdy inny nieznany kod: zapis mogl dotrzec, wiec bez fallbacku (odwracalny wariant).
    _err(f"CLI zakonczylo sie kodem {result.returncode}")
    if detail:
        print(detail, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
