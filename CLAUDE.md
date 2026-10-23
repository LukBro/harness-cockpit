# Harness Cockpit — zasady dla agentów

Czytaj to przed pierwszą zmianą w tym repozytorium.

## Czym to jest

Kokpit do składania, uruchamiania i obserwowania workflow agentowych.
Workflow składa się wizualnie ze skilli. Działa na dowolnym repozytorium.

Pozycjonowanie: **bez terminala, z człowiekiem w pętli.** Terminal nie jest
warunkiem korzystania — nie jest też zakazany.

## Niezmienniki — NEVER

Lista jest bytem w kodzie, nie zbiorem zdań. Walidator sprawdza każdy punkt.

- `role_binding` — rola ustawia tier i prawo zapisu razem; nigdy osobno
- `reviewer_readonly` — recenzent NIGDY nie dostaje prawa zapisu, w żadnym backendzie
- `approval_required` — krok z `approval` nie przechodzi bez decyzji człowieka
- `no_git_write` — **silnik** NIGDY nie commituje ani nie pushuje. Niezmiennik wiąże silnik,
  nie człowieka: commit po `integrate` jest dozwolony wyłącznie jako osobna, jawna akcja
  człowieka (własny przycisk, nie część `integrate`). Push NIGDY, w żadnym scenariuszu.
  Decyzja BRO-128, szczegóły w BRO-108.
- `run_dir_outside_repo` — artefakty runu nie lądują w repozytorium użytkownika
- `measured_or_null` — brak pomiaru zapisujemy jako brak, NIGDY jako zero

## Granice — MUST / NEVER

- MUST: prompt agenta niesie **ścieżkę** do pliku skilla, nigdy jego treść.
  WHY: treść wklejona rozjeżdża się z plikiem przy pierwszej edycji w UI
  i psuje audyt — nie wiadomo wtedy, co agent naprawdę dostał.
- MUST: kokpit sięga do plików użytkownika **wyłącznie przez runnera**.
  WHY: serwer nie widzi dysku użytkownika; to nie jest wygoda, tylko architektura.
- MUST: runner łączy się **wychodząco**. NEVER: nie otwieraj portu u użytkownika.
- MUST: każde zapytanie do danych niesie `tenant_id`. NEVER: zapytanie bez niego.
- MUST: uwierzytelnianie działa także na localhoście.
  WHY: „lokalny znaczy zaufany" oznacza dorabianie autoryzacji przed publikacją.
- NEVER: sekret w `argv` (widoczny w `ps`) ani w logach bez maskowania.
- NEVER: własna kryptografia na pliku z sekretami — prawa `0600` i `.gitignore`.

## Decyzje implementacyjne

| Rzecz | Wybór | Dlaczego |
| -- | -- | -- |
| baza | PostgreSQL, SQLAlchemy **async** + asyncpg | silnik i tak jest asynchroniczny |
| migracje | Alembic | `create_all` tylko w testach szkieletu |
| YAML | **ruamel.yaml**, nigdy pyyaml | pyyaml niszczy komentarze i kolejność kluczy przy zapisie z kanwy |
| frontmatter | python-frontmatter | zapis `SKILL.md` bez utraty formatowania briefu |
| transport runnera | **SSE w dół, POST w górę**, korelacja po `req_id` | runner łączy się wychodząco; ten sam mechanizm co dla przeglądarki |
| procesy | `asyncio.create_subprocess_exec` + `start_new_session=True` | zabicie grupy procesów, zero sierot |
| git | `subprocess`, nie GitPython | mniej magii, czytelne komunikaty błędów |
| typy frontu | generowane z OpenAPI (`openapi-typescript`) | rozjazd kontraktu = błąd kompilacji |
| hasła | argon2id (`argon2-cffi`) | nie bcrypt, nie SHA |
| sesja | identyfikator w bazie + ciasteczko `httpOnly` | JWT nie daje się unieważnić przed wygaśnięciem |

## Testy

- **Nazwy w języku dziedziny, nie technologii.** Próba: osoba nieznająca
  technologii czyta listę testów i rozumie, co jest sprawdzane.
  Dobrze: `test_run_zakonczony_sukcesem_ma_stan_do_odbioru_nie_gotowe`
  Źle: `test_sse_reconnect_dedup`
- Test przed implementacją.
- `pytest` dla backendu, Playwright dla scenariuszy end-to-end.

## Warstwy — reguła, nie konwencja

| Warstwa | Odpowiedzialność |
| -- | -- |
| `api/` | HTTP: wejście, wyjście, kody odpowiedzi. **Zero zapytań do bazy** |
| `services/` | reguły: uprawnienia, `tenant_id`, niezmienniki, transakcje |
| `db/` | model i zapytania |

MUST: w `api/` nie pojawia się `select`, `session.add` ani import z `cockpit.db.models`
(poza typami wejścia). WHY: niezmienniki muszą być wymuszone w jednym miejscu —
logika w trasach oznacza, że każdy nowy endpoint jest kolejną szansą na obejście,
a kod pisze agent, który naśladuje wzorzec z sąsiednich tras.

Pilnuje tego `tools/check_layers.py` — uruchamiany w CI i przez
`test_warstwa_api_nie_dotyka_bazy`. **Reguła bez sprawdzenia to życzenie.**

NEVER: nie dokładaj repozytoriów ani wzorca jednostki pracy nad SQLAlchemy.
WHY: on już jest abstrakcją nad bazą; druga warstwa nad nim niczego nie wymusza.

## Układ repozytorium

```
src/cockpit/     API, model danych, szyna zdarzeń
src/cockpit/services/  reguly i jedyny dostep do bazy
src/cockpit/kernel/    normalizacja zdarzeń hooków (M0, zweryfikowane)
src/runner/      runner: SSE w dół, POST w górę, procesy, sekrety
web/             Vite + React + TypeScript, buduje się do src/cockpit/static
tests/           testy backendu
```
