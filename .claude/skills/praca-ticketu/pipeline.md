Przed użyciem odczytaj [Kontrakt runtime](references/runtime.md) względem katalogu skilla work. Narzędzia pipeline odkrywaj w faktycznym katalogu runtime i sprawdzaj aktualny schemat; prefiksy poniżej są placeholderami. Brak wyszukiwarki z przykładu klientowego nie oznacza braku MCP; użyj discovery bieżącego runtime.

# Instrumentacja pipeline - protokol raportowania

Warstwa obserwowalnosci nad praca zespolu agentow. Odwzorowuje przebieg w module
Pipelines (hierarchia `pipeline -> step -> job`).

**Zasada nadrzedna**: pipeline RAPORTUJE prace, nie wykonuje jej i nie jest gate'em.
Blad ktoregokolwiek wywolania ponizej NIGDY nie przerywa pracy nad ticketem -
odnotuj i kontynuuj. Jesli toole pipeline sa niedostepne (starszy serwer MCP bez
modulu Pipelines) - pomin CALA instrumentacje.

Wszystkie wywolania w tym pliku sa **best-effort**.

---

## Dostepnosc toolow

Toole laduja sie w KROK 1 skilla przez:

Użyj dostępnego discovery schematów; przykład klienta poniżej.

<!-- runtime:claude-code -->
```
ToolSearch(query="+monolynx pipeline create job finish", max_results=10)
```

<!-- /runtime:claude-code -->
Zestaw: `create_pipeline`, `create_pipeline_job`, `update_pipeline_job`,
`append_job_log`, `finish_pipeline`.

Brak ktoregokolwiek -> pomin instrumentacje w calosci.

**Prefix tooli**: nazwy `mcp__monolynx__<tool>` w tym pliku to placeholder.
Rzeczywisty prefix zalezy od instalacji (np. `mcp__claude_ai_Monolynx__*` przy
connectorze globalnym) - uzywaj DOKLADNIE nazw z aktualnego katalogu narzędzi.

**Transport**: Pipelines nie maja komendy w CLI `monolynx`, wiec kazda operacja
z tego pliku idzie toolem MCP, takze gdy preflight transportu w KROK 1 dal
`mode=cli`. Agent z bloku w sekcji 4 raportuje przez MCP; wrappera
`monolynx_op.py` uzywa tylko do operacji ticketowych i tylko wtedy, gdy Team
Manager wkleil mu literalna sciezke.

---

## 1. Utworzenie pipeline (po KROK 2 - walidacji brancha)

```
mcp__monolynx__create_pipeline(project_slug="<PROJECT_SLUG>", pipeline_type="ticket_work", ticket_id="<ID ticketu; klucz PROJ-XX tylko jesli serwer go przyjmuje - przy bledzie uzyj ID>", branch="<aktualny_branch>")
```

Zwraca `pipeline_id` oraz 3 stepy: `research`, `coding`, `wrap-up`.
**Zapamietaj `pipeline_id`** - uzywany do konca skilla.

Nastepnie odnotuj walidacje brancha jako job:

```
mcp__monolynx__create_pipeline_job(project_slug="<PROJECT_SLUG>", pipeline_id="<pipeline_id>", step="research", name="branch-validation", agent_type="system")
mcp__monolynx__update_pipeline_job(project_slug="<PROJECT_SLUG>", job_id="<job_id>", status="success", summary="Branch zwalidowany: <branch>")
```

---

## 2. Job Researchera (KROK 3)

Przed uruchomieniem agenta `Explore`:

```
mcp__monolynx__create_pipeline_job(project_slug="<PROJECT_SLUG>", pipeline_id="<pipeline_id>", step="research", name="researcher", agent_type="Explore")
mcp__monolynx__update_pipeline_job(project_slug="<PROJECT_SLUG>", job_id="<researcher_job_id>", status="running")
```

Po otrzymaniu raportu:

```
mcp__monolynx__append_job_log(project_slug="<PROJECT_SLUG>", job_id="<researcher_job_id>", content="<pelny raport Researchera w markdown>")
mcp__monolynx__update_pipeline_job(project_slug="<PROJECT_SLUG>", job_id="<researcher_job_id>", status="success", summary="<1-zdaniowe streszczenie raportu>")
```

Researcher pominiety (fallback z KROK 3c)? Job juz istnieje i ma `running` -
uproszczony raport Team Managera tez appenduj do loga (`append_job_log`), po
czym domknij job statusem `skipped` zamiast `success` (summary: kto zrobil
analize zamiast Researchera).

---

## 3. Joby zespolu (KROK 5)

### 3a. Plan pracy

```
mcp__monolynx__create_pipeline_job(project_slug="<PROJECT_SLUG>", pipeline_id="<pipeline_id>", step="coding", name="team-plan", agent_type="system")
mcp__monolynx__append_job_log(project_slug="<PROJECT_SLUG>", job_id="<team_plan_job_id>", content="<plan pracy + dobrani agenci + przydzial plikow w markdown>")
mcp__monolynx__update_pipeline_job(project_slug="<PROJECT_SLUG>", job_id="<team_plan_job_id>", status="success", summary="Dobrano zespol: <lista agentow>")
```

### 3b. Job per agent

Dla KAZDEGO dobranego agenta (wlacznie z krytykiem) utworz job `pending`:

```
mcp__monolynx__create_pipeline_job(project_slug="<PROJECT_SLUG>", pipeline_id="<pipeline_id>", step="coding", name="<nazwa wybranego agenta>", agent_type="<subagent_type>")
```

**Zapamietaj `job_id` kazdego agenta** - przekazesz go w prompcie agenta (sekcja 4).

<!-- runtime:claude-code -->
Krytyk Codex (preflight krytyka w KROK 1 skilla dał `backend=codex`): job
krytyka tworzysz tak samo, z `name="krytyk"` i `agent_type="codex-critic"`.
Jego `job_id` nie trafia do żadnego promptu - sesja Codex w sandboxie
read-only nie pisze do Monolynx, więc job prowadzi Team Manager (sekcja 5).
<!-- /runtime:claude-code -->

---

## 4. Blok do wklejenia w prompt agenta (KROK 6)

Kazdy agent raportuje siebie sam. Wklej ponizsze do promptu, podstawiajac `job_id`.
W trybie solo (runtime bez subagentow) subagentow nie ma - Team
Manager tworzy joby per ROLA (sekcja 3b) i sam wykonuje ponizszy blok dla kazdej
roli: otwiera job przed praca w roli, zamyka po niej.

Użyj dostępnego discovery schematów; przykład klienta poniżej.

<!-- runtime:claude-code -->
```
RAPORT DO PIPELINE (job_id: <twoj_job_id>):

Na poczatku pracy:
  1. Odkryj aktualne schematy capability pipeline job/log przez mechanizm dostępny w runtime
     i uzywaj DOKLADNIE zwroconych nazw (prefix moze roznic sie od mcp__monolynx__).
     Runtime bez ToolSearch (toole dostepne od razu) -> uzywaj ich bezposrednio.
     ToolSearch istnieje, ale nic nie zwrocil -> pomin cale raportowanie do pipeline.
  2. Zmierz czas startu: date +%s
  3. mcp__monolynx__update_pipeline_job(project_slug="<PROJECT_SLUG>", job_id="<twoj_job_id>", status="running")

Po zakonczeniu pracy MUSISZ:
  1. Zmierz czas konca: date +%s. Oblicz czas pracy w minutach (minimum 1).
  2. mcp__monolynx__append_job_log(project_slug="<PROJECT_SLUG>", job_id="<twoj_job_id>",
     content="<markdown: co zrobiles, proces myslowy, kluczowe decyzje, zmienione pliki>
              \n\n**Czas pracy: X min**")
  3. mcp__monolynx__update_pipeline_job(project_slug="<PROJECT_SLUG>", job_id="<twoj_job_id>",
     status="success", summary="<1-2 zdania do listy>")
     UWAGA: jesli pracy NIE ukonczyles (porazka, blokada) - ustaw status="failed"
     zamiast "success", z powodem w summary. Nigdy nie raportuj sukcesu porazki.
  4. W swojej odpowiedzi koncowej podaj linie: `CZAS PRACY: X min`

Jesli toole pipeline niedostepne lub zwroca blad - pomin raportowanie, NIE przerywaj pracy.
Linie `CZAS PRACY: X min` podaj ZAWSZE, niezaleznie od dostepnosci pipeline.
```

<!-- /runtime:claude-code -->
Linia `CZAS PRACY` jest zrodlem danych dla `log_time` w KROK 6.7a - Team Manager
loguje czas zgloszony przez agenta, nie wlasny stoper.

---

## 5. Oceny krytyka (KROK 6.6)

Krytyk zapisuje ocene do joba KAZDEGO ocenianego agenta:

```
mcp__monolynx__update_pipeline_job(project_slug="<PROJECT_SLUG>", job_id="<job_id ocenianego agenta>", score=<0-100>)
```

Swoj wlasny job zamyka na koncu: `append_job_log` z trescia review +
`update_pipeline_job(status="success")`. W trybie Agent Teams "koniec" to
wiadomosc "review zakonczone" od Team Managera (moga byc iteracje) - bez Teams
kazde wywolanie krytyka zamyka job po sobie.

<!-- runtime:claude-code -->
**Krytyk Codex (`backend=codex`).** Sesja Codex pracuje w sandboxie read-only
i nie ma tooli Monolynx, więc niczego z tej sekcji nie zapisuje sama - robi to
Team Manager. Przed wywołaniem wrappera ustawia job krytyka na `running`. Po
werdykcie z pliku zapisuje `score` do joba KAŻDEGO ocenianego agenta (to samo
wywołanie `update_pipeline_job` co wyżej), dokleja treść werdyktu do loga joba
krytyka (`append_job_log`, z linią `**Czas pracy: X min**` ze stopera Team
Managera wokół wywołania wrappera) i zamyka job krytyka `status="success"`
z summary, np. "Codex CLI (model: <id|inherit>): <najniższy score>/100".
Przy ponownym review, przed kolejnym wywołaniem wrappera, podbija `attempt`
joba krytyka i ustawia `status="running"`, a po werdykcie zamyka go ponownie.

Fallback na krytyka Claude (nieudane wywołanie wrappera, 6.6a skilla): Team
Manager dokleja do loga joba krytyka powód (`reason=` albo odmowa uprawnień;
przy `reason=verdict` także odrzuconą treść) i ustawia `status="failed"`
z tym powodem w summary. Nowego joba NIE tworzy: od razu podbija `attempt`
tego samego joba, ustawia `status="running"` i wkleja jego `job_id` do bloku
RAPORT DO PIPELINE krytyka Claude, który raportuje i zamyka job jak dotąd
(score do jobów developerów też zapisuje on). WHY ten sam job: job zostawiony
jako `failed` robi `failed` z całego stepu `coding` i z pipeline'u, choć review
się odbyło. `agent_type` zostaje `codex-critic` (tego pola nie da się zmienić),
dlatego summary końcowe mówi wprost, kto wydał werdykt, np. "Claude opus
(fallback z Codex CLI, reason=timeout)".
<!-- /runtime:claude-code -->

### Iteracja poprawkowa

Przy ponownym uruchomieniu agenta z feedbackiem ustaw status joba na `running`
(jesli byl juz domkniety - `success`/`failed`) i podbij `attempt`. Pierwotne
wykonanie joba to `attempt=1`, pierwsza poprawka -> `attempt=2` itd.:

```
mcp__monolynx__update_pipeline_job(project_slug="<PROJECT_SLUG>", job_id="<job_id agenta>", attempt=<numer_iteracji>, status="running")
```

Agent po poprawce ponownie zamyka swoj job (blok z sekcji 4).
Poprawki doklej do loga przez `append_job_log`.

To samo dotyczy joba KRYTYKA przy ponownym review bez Agent Teams: przed nowym
wywolaniem krytyka podbij jego `attempt` i ustaw `status="running"` - jego blok
z sekcji 4 zamknie job ponownie.

---

## 6. Job lint/test (KROK 6.5)

```
mcp__monolynx__create_pipeline_job(project_slug="<PROJECT_SLUG>", pipeline_id="<pipeline_id>", step="wrap-up", name="lint-test", agent_type="system")
mcp__monolynx__append_job_log(project_slug="<PROJECT_SLUG>", job_id="<lint_test_job_id>", content="<uzyte komendy + pelny wynik lint i testow>")
mcp__monolynx__update_pipeline_job(project_slug="<PROJECT_SLUG>", job_id="<lint_test_job_id>", status="<success|failed|skipped>", summary="<wynik w 1 zdaniu>")
```

Status:
- `success` - wszystko, co projekt MA (lint i/lub testy), przeszlo na zielono;
  projekt z samym lintem bez testow -> status wg wyniku lintu. W trybie
  `pełny przebieg: ci` (pole strony `toolchain`, `lint-test.md`) `success`
  znaczy: lint zielony i testy ZAWEZONE do zmienionego obszaru zielone (albo
  brak pasujacych testow). Pelnego zestawu ten job wtedy nie potwierdza -
  weryfikuje go pipeline merge requesta, a w logu joba zapisujesz tryb i liste
  uruchomionych sciezek
- `failed` - cokolwiek czerwone (to SKILL w KROK 6.5 blokuje `in_review` -
  sam pipeline niczego nie blokuje, tylko raportuje)
- `skipped` - brak strony wiki `toolchain`, projekt nie ma ANI lintu ANI testow,
  albo uzytkownik swiadomie pominal lint/testy

Przy powtorkach lintu/testow (iteracje poprawkowe) NIE tworz nowego joba -
doklej wynik kolejnej rundy do loga tego samego joba (`append_job_log`)
i zaktualizuj jego status.

---

## 7. Zamkniecie (KROK 7e)

Wywolaj PO udanej zmianie statusu ticketu na `in_review` - summary joba stwierdza
ten status, wiec nie moze go wyprzedzac.

```
mcp__monolynx__create_pipeline_job(project_slug="<PROJECT_SLUG>", pipeline_id="<pipeline_id>", step="wrap-up", name="team-manager-summary", agent_type="system")
mcp__monolynx__append_job_log(project_slug="<PROJECT_SLUG>", job_id="<summary_job_id>", content="<podsumowanie zadania + oceny w markdown>")
mcp__monolynx__update_pipeline_job(project_slug="<PROJECT_SLUG>", job_id="<summary_job_id>", status="success", summary="<stan faktyczny, np. 'Zadanie domkniete, status in_review' - przy nieudanej zmianie statusu wpisz prawde>")
mcp__monolynx__finish_pipeline(project_slug="<PROJECT_SLUG>", pipeline_id="<pipeline_id>")
```

`finish_pipeline` bez argumentu `status` wylicza status koncowy ze stepow
(jakikolwiek failed step -> pipeline failed, inaczej success). Jesli praca
zakonczyla sie niepowodzeniem - podaj jawnie `status="failed"`.

**Przerwanie pracy przed KROK 7** (odmowa uzytkownika, porzucenie, wyczerpany
limit poprawek): NIE zostawiaj pipeline'u w `running`. Najpierw domknij otwarte
joby (best-effort): `running` -> `failed`, `pending` -> `skipped`, z powodem
w summary. Potem `finish_pipeline(status="failed")` - bez jobow summary.
