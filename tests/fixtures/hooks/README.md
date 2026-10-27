# Fixture'y hookow Claude Code (HAR-40)

Prawdziwe payloady hookow przechwycone z Claude Code **2.1.311**, 2026-10-27.

## Jak przechwycono

Headless `claude -p`, hooki typu `command` zapisujace stdin do pliku (jedna linia
NDJSON = jeden payload, w kolejnosci przyjscia).

## Zastrzezenie

Komendy `phase_timer.py` w `fazy-atrapa.ndjson` pochodza od **ATRAPY** skryptu,
bo realnego skryptu z serwera palserwis nie bylo. **Format znacznika fazy
(`phase_timer.py <RUN_DIR> mark <faza> start|end`) potwierdzono tylko na atrapie**,
nie na realnych wywolaniach z palserwis - to osobny ticket. Payloady subagentow
nie zalezą od atrapy.

## Anonimizacja

- `session_id` zastapiony stalymi wartosciami (`session-fazy-1`,
  `session-subagenty-1`, `session-subagenty-odmowa-1`);
- sciezki: katalog domowy -> `/home/user`, katalog roboczy -> `/tmp/har40`;
- `agent_id` -> `agent-1` / `agent-2`, z zachowaniem rozroznienia agentow.

## Pliki

- `fazy-atrapa.ndjson` - znaczniki faz (`mark B.2 start|end`, `C start`, `C.1 end`) wywolane przez Bash, komendy z atrapy `phase_timer.py`.
- `subagenty.ndjson` - dwa rownolegle subagenty (`general-purpose` pisze i edytuje plik, `Explore` czyta i listuje) zakonczone sukcesem.
- `subagenty-odmowa.ndjson` - ten sam uklad, ale zapis pliku przez subagenta zostal odmowiony (Write bez PostToolUse).

## Znane obserwacje (poza zakresem testow, naleza do HAR-22)

- `PostToolUseFailure` dla `... mark B.2 end && false` jest zamieniany na zwykle `phase_end`.
- `RUN_DIR` ze spacja (`"/tmp/har40/run dir"`) nie pasuje do wzorca znacznika.

Wyniki pomiaru: strona wiki `sprawdzenie-hooki-fazy-subagenty`.
