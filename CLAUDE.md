# Harness Cockpit — zasady dla agentów

Czytaj to przed pierwszą zmianą w tym repozytorium.

## Czym to jest

Kokpit do składania, uruchamiania i obserwowania workflow agentowych.
Workflow składa się wizualnie ze skilli. Działa na dowolnym repozytorium.

Pozycjonowanie: **bez terminala, z człowiekiem w pętli.** Terminal nie jest
warunkiem korzystania — nie jest też zakazany.

## Niezmienniki — NEVER

Lista jest bytem w kodzie, nie zbiorem zdań. Każdy punkt ma sprawdzenie w `SPRAWDZENIA`; na ścieżce wykonania egzekwowane są tylko te z `EGZEKWOWANE` (oba w `src/cockpit/services/walidacja.py`).

- `role_binding` — rola ustawia tier i prawo zapisu razem; nigdy osobno
- `reviewer_readonly` — recenzent NIGDY nie dostaje prawa zapisu, w żadnym backendzie
- `approval_required` — krok z `approval` nie przechodzi bez decyzji człowieka
- `no_git_write` — **silnik** NIGDY nie commituje ani nie pushuje. Niezmiennik wiąże silnik,
  nie człowieka: commit po `integrate` jest dozwolony wyłącznie jako osobna, jawna akcja
  człowieka (własny przycisk, nie część `integrate`). Push NIGDY, w żadnym scenariuszu.
  Decyzja BRO-128, szczegóły w BRO-108.
- `run_dir_outside_repo` — artefakty runu nie lądują w repozytorium użytkownika
- `measured_or_null` — brak pomiaru zapisujemy jako brak, NIGDY jako zero
- `evidence_required` — brak dowodu (pusty wynik komendy, brak pliku werdyktu) to błąd kroku, NIGDY zielone przejście

## Role

Skill ma jedną z czterech ról (`Rola` w `src/cockpit/services/invariants.py`):
`author`, `reviewer`, `executor`, `gatherer`.

| Rola | Tier | Zapis |
| -- | -- | -- |
| `author` | `judgment` | tak |
| `reviewer` | `judgment` | nie |
| `executor` | `execution` | tak |
| `gatherer` | `retrieval` | nie |

Jedynym źródłem tieru i prawa zapisu jest `profil_roli(rola)` w `invariants.py`.
Model ustawia się w profilu backendu u runnera (BRO-98), nie w workflow.

## Pierwszeństwo warstw — MUST / NEVER

Przy sprzeczności instrukcji wygrywa warstwa wyżej na liście (BRO-96):

1. niezmienniki silnika — sekcja „Niezmienniki — NEVER” i `Niezmiennik` w `src/cockpit/services/invariants.py`
2. `.cockpit/project.yaml` repozytorium, na którym działa run
3. `CLAUDE.md` katalogu — najbliższy katalogowi, którego dotyczy krok
4. `CLAUDE.md` korzenia
5. `SKILL.md` kroku
6. `.ai/lessons.md`

- MUST: przy sprzeczności stosuj warstwę wyższą.
  WHY: pomiar BRO-126 na `palserwis` — 25 konfliktów, bo nikt nie zapisał, która warstwa wygrywa.
- NEVER: `.ai/lessons.md` nie nadpisuje poziomów 1–4; lekcja sprzeczna z nimi jest do poprawki, nie do wykonania.
- Kolejność jest na razie regułą tekstu; wymuszenie w kodzie to HAR-87.

## Instrukcje poza korzeniem — MUST / NEVER

- MUST: instrukcja potrzebna krokowi leży w korzeniu repozytorium albo brief skilla
  wymienia ją jako ścieżkę.
  WHY: instrukcje w podkatalogach nie docierają do kontekstu agenta automatycznie —
  agent, który ich nie przeczyta, działa tak, jakby ich nie było.
- MUST: przed pierwszą zmianą przeczytaj `.ai/qa/AGENTS.md` (konwencje testów)
  i `.ai/lessons.md` (pułapki z zakresem: krok, ścieżki).
- MAY: decyzje z uzasadnieniem czytaj w `.ai/specs/`, gdy krok ich dotyczy.

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

## Commity — MUST / NEVER

- MUST: autor commita to wyłącznie `LukBro <lukaszbrowar@gmail.com>`. Przed
  commitem sprawdź `git config user.email`.
  WHY: globalna tożsamość git na tej maszynie to adres służbowy, który nie może
  trafić do tego prywatnego projektu.
- NEVER: linia `Co-Authored-By: Claude` ani żaden inny współautor w commicie.
  Ta reguła ma pierwszeństwo przed domyślną atrybucją narzędzia.
  WHY: decyzja właściciela repozytorium; sesje w tle (worktree) nie widzą pamięci
  sesji głównej, więc reguła musi żyć tutaj.
- NEVER: `--amend` ani przepisywanie commita, który jest już wypchnięty, bez
  pytania.

## Deliverable — MUST / NEVER

Deliverable runu to zmiany w kopii roboczej repozytorium plus artefakty w katalogu runu.

- MUST: run zakończony sukcesem ma stan „do odbioru” (`RunStatus.to_accept`).
  NEVER: nie nazywaj go „gotowym”.
  WHY: o przyjęciu wyniku decyduje człowiek, nie silnik (stan z BRO-108).
- MUST: zmiany zostają niezacommitowane — spójnie z `no_git_write`.
- MUST: `collect` i `integrate` to akcje człowieka po odbiorze, nie kroki runu.

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
