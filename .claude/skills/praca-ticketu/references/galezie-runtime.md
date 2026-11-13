# Gałęzie, które w harness-cockpit nie wystąpiły

Czytasz TYLKO, gdy wynik `start.py` albo środowisko wskazuje daną gałąź. W 15 zmierzonych sesjach
(HAR-104…HAR-240) żadna z nich nie wystąpiła: transport `cli`, krytyk `claude` (`codex=none`),
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=0`, GitHub, `MONOLYNX_SPRINT_RUN=0`, `CONTRACT=auto`, AUTO*=true.
Pełny opis każdej gałęzi jest w SKILL.md wersji 1.26.0 (numery linii w nawiasach) - ten plik
streszcza zachowanie i mówi, gdzie szukać szczegółów.

## Krytyk przez Codex CLI (start.py: `CRITIC: backend=codex`) - v1.26.0 l. 119-197, 1226-1255, 1600-1680, 1779-1811

- Prompt z `prompt_krytyka.py` podajesz plikiem: `critic_backend.py run --prompt-file <plik> --out <werdykt>
  --cwd <korzeń> --timeout 540` (timeout poniżej limitu narzędzia terminala).
- Kod 0 -> `werdykt.py <KEY> --plik <werdykt>`; kod 2/64/odmowa -> fallback: krytyk Claude (`opus`) do końca
  ticketu, komentarz `**Krytyk: fallback na Claude Opus**` z powodem. Nieudane wywołanie nie jest rundą
  i nie zużywa limitu; review nigdy nie jest pomijane.
- Czas krytyka = stoper TM wokół wywołań (`stan.py znacznik`).

## Agent Teams (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) - v1.26.0 l. 923-955, 1336-1381, 1574-1586

Krytyk spawnowany pierwszy z `name="krytyk"`, developerzy wysyłają mu listę plików, review startuje na
sygnał TM po zielonych bramkach. Bramki (`prompt_krytyka.py`, `werdykt.py`) działają tak samo - werdykt
z wiadomości krytyka zapisz plikiem i podaj `--plik`.

## Transport MCP (`TRANSPORT: mode=mcp`) - v1.26.0 l. 74-117, 199-243

Operacje toolami MCP (prefiks z katalogu narzędzi, ładowany przez ToolSearch). Ticket pobrany toolem
zapisz jako JSON i podaj `start.py --ticket-json`.

## Brak delegowania (fallback solo) - v1.26.0 l. 957-978

Tylko gdy w katalogu narzędzi nie ma żadnego mechanizmu powołania agenta. Review w osobnym przebiegu wg
rubryki; zdanie "Review wykonane w trybie solo przez tego samego agenta co implementacja; to nie jest
niezależne review" w planie, podsumowaniu i odpowiedzi.

## GitLab / glab, tryb `pełny przebieg: ci` - v1.26.0 l. 870-892, 1813-1831, 1981-2017; lint-test.md, commit-mr.md

`in_review` tylko po wystawionym MR; lokalnie testy zawężone; pełny zestaw weryfikuje pipeline MR.

## Interaktywne decyzje o branchu (start.py kod 11) - v1.26.0 l. 486-520

(a) zostań na branchu, (b) nowy branch `feature-<nr>-<slug>` z bazy (najpierw czyste drzewo),
(c) tryb `sprint` do końca sesji.

## Instrumentacja Pipelines (MONOLYNX_PIPELINE=on) - pipeline.md

Obserwowalność, nie bramka. W zmierzonych sesjach kosztowała 109 wywołań API TM (7,8% kontekstu).
Proponowany domyślny stan: wyłączona; po włączeniu protokół z `pipeline.md` bez zmian.

## Propozycja przebiegu mutation-check - v1.26.0 l. 1486-1565

W harness-cockpit mutacje są bramką projektu (`tools/mutanty_zmian.py`, `tools/mutanty_reczne.py`)
w `bramki.json`, więc propozycja skilla `monolynx:mutation-check` się nie pojawia.
