# Archiwum reguł sprzed skrócenia (PR #174)

Pełna, niezmieniona treść `.claude/rules/*.md` z `499a13d` (stan `main` przed audytem 2026-11-10).
Reguły w `.claude/rules/` są skrótem tej treści. Gdy skrót nie rozstrzyga przypadku albo przykład z historii
(ticket HAR-…, regresja z retro) jest potrzebny do oceny, obowiązuje to archiwum: niczego z niego nie usunięto.
Czytają je test-writer i krytyk na żądanie, nie w każdej turze.


---

## Plik `.claude/rules/asercja-na-danych.md`

### Asercja sprawdza skutek w danych

Źródło: retro sprintu M1.0 — HAR-2, HAR-3.

#### Reguła

Test, który twierdzi, że coś **powstało, zmieniło się albo nie powstało** w danych,
czyta dane (baza, plik w katalogu runu) i sprawdza ten skutek. Test nazwany wynikiem
reguły dziedzinowej uruchamia produkcyjny kod tej reguły — prawdziwą aplikację,
prawdziwy punkt wejścia — a nie jej kopię albo osobną instancję FastAPI.

#### Pułapka

Kod odpowiedzi HTTP, `page.url` albo stan interfejsu wynikają wprost z trasy i nie
dowodzą niczego o bazie: run może powstać mimo przekierowania, a runner innego
zespołu może zostać wybrany mimo `200`.

#### Co sprawdzić w review

- Asercja „nie powstał X” / „wybrano Y” opiera się wyłącznie na odpowiedzi lub UI
  = **-25**.
- Test reguły wywołuje lokalną kopię logiki zamiast kodu produkcyjnego = **-25**.

---

## Plik `.claude/rules/brak-sygnalu-to-nie-zielone.md`

### Brak sygnału to nie zielone, kryterium musi móc zawieść

Źródło: audyt G8 (BRO-137) — cztery kryteria akceptacji, które nie mogły zawieść
(BRO-108, BRO-110, BRO-91, BRO-122), oraz BRO-99 z odhaczonym kryterium `tenant_id`.

#### Reguła

- **Brak sygnału nie jest wynikiem.** Brak ostrzeżenia, pominięta bramka, pominięty test
  i pusty plik dowodu nie są raportowane jako „bramka aktywna” ani jako zielone.
  Szczegóły i przykłady: [falszywie-zielone](falszywie-zielone.md) oraz niezmiennik
  `evidence_required` z CLAUDE.md.
- **Kryterium akceptacji ma wynik, przy którym zawodzi.** Kryterium spełnione przy każdym
  wyniku pomiaru jest niedopuszczalne. Przykłady z audytu: „poniżej 5% albo komentarz”
  (BRO-110), „zmierzone” (BRO-91), „zgodnie z rozstrzygnięciem” (BRO-122). Przy każdym
  kryterium wskaż konkretny wynik, przy którym by zawiodło (ilustracja, nie cytat:
  pomiar powyżej progu i brak komentarza). Kryterium działa jak gałąź kodu: musi istnieć wejście, na
  którym oblewa (zob. [test-odroznia-regule](test-odroznia-regule.md)).
- **Kryterium o wypchnięciu zmian sprawdza mechanizm, który je rejestruje:**
  `git reflog show origin/<branch>`, nie `git reflog` HEAD. Reflog HEAD nie rejestruje
  wypchnięcia, więc kryterium BRO-108, które sprawdzało wypchnięcie przez `git reflog`
  HEAD, nie mogło zawieść.

#### Pułapka

Kryterium prawdziwe literalnie, a obietnica niespełniona. BRO-99: kryterium `tenant_id`
(zapytania niosą `tenant_id`) było prawdą (`run_dla_najemcy` filtruje po `tenant_id`),
ale obietnica izolacji zespołów nie, bo `domyslny_tenant` wybiera dowolny zespół
(`select(Tenant).limit(1)` bez `ORDER BY`). Odhaczone kryterium wygląda jak dowód
i nikt go nie ogląda.

#### Co sprawdzić w review

- Odhaczone kryterium bez wskazania wyniku, przy którym by zawiodło = **-25**.
- Kryterium o wypchnięciu zmian sprawdzane przez `git reflog` HEAD zamiast
  `git reflog show origin/<branch>` = **-25**.
- Brak ostrzeżenia, pominięta bramka albo pusty plik dowodu raportowane jako bramka
  aktywna lub zielone — odjęcie z [falszywie-zielone](falszywie-zielone.md) (**-25**),
  nie sumuje się z tą regułą: jedna wada = jedno odjęcie (retro M1.5, HAR-54).

---

## Plik `.claude/rules/falszywie-zielone.md`

### Brak dowodu to nie zielony wynik

Źródło: retro sprintu M1.2.1 — HAR-132 (`mutation.yml`), HAR-133 (`tools/mutanty_zmian.py`),
HAR-134 (strażnik bazy w `tests/conftest.py`). Niezmiennik `evidence_required` z CLAUDE.md.

#### Reguła

Każda ścieżka, która kończy się wynikiem „czysto / nic nie znaleziono / przeszło”,
musi odróżniać **„sprawdziłem i jest czysto”** od **„nie sprawdziłem”**. Brak dowodu
(pusty wynik komendy, zero zbadanych elementów, wyjątek, brak pliku) jest błędem,
nigdy sukcesem. Dotyczy to w szczególności:

- **CI**: `continue-on-error`, `|| true` i `exit 0` wolno stosować tylko dla jawnie
  nazwanej przyczyny (np. brak narzędzia na runnerze), nie dla całego kroku;
  narzędzie zepsute (zero zabitych mutantów, zero zebranych testów) oblewa krok,
- **wyjścia narzędzi**: wynik parsowany z innego programu (`git diff`, raport testów)
  nie zależy od konfiguracji użytkownika, która zmienia format (np. `diff.noprefix`,
  `diff.mnemonicPrefix` — wymuś format flagami), a niezrozumiany wynik to błąd,
- **strażnicy i filtry**: gałąź „nie umiem ocenić” (`except …: return False`,
  ścieżka spoza `rootdir`) nie przepuszcza elementu jako poprawnego.

#### Pułapka

Fałszywie zielony wynik wygląda jak sukces i nikt go nie ogląda: CI mutacji było
zielone przez cały sprint, mierząc 0 z 2366 mutantów (HAR-132), a skrypt mutantów
zmian zgłaszałby „brak zmienionych funkcji” na maszynie z `diff.noprefix` (HAR-133).

#### Co sprawdzić w review

- Ścieżka kończąca się sukcesem bez dowodu, że coś zostało sprawdzone = **-25**.
- Każda gałąź „brak dowodu → błąd” ma test, w którym tylko ona decyduje o wyniku
  (zob. [test-odroznia-regule](test-odroznia-regule.md)).

#### Regresja (retro M1.6)

Źródło: HAR-149 (-25 `|| true` na zapadce, -15 fallback cache), HAR-144 (-25 zasięg mutmuta).

- **`|| true` na wywołaniu, którego niezerowy kod oznacza błąd wejścia** — zepsute wejście
  przechodzi na zielono; nazwana przyczyna musi dotyczyć konkretnego kodu, nie całego
  wywołania (HAR-149, `mutation.yml` zapadka).
- **Cache przywracany po samym prefiksie klucza** (`restore-keys`) przywraca stan z innymi
  testami albo z plikami usuniętymi w PR — wynik bramki pochodzi wtedy ze starego stanu.
  Klucz obejmuje wszystko, od czego zależy werdykt (testy, konfiguracja) (HAR-149).
- **Model zasięgu zewnętrznego narzędzia** liczony według składni, a nie według faktycznych
  reguł pomijania w zainstalowanej wersji (mutmut 3.8.0: dekoratory, `__new__`,
  `# pragma: no mutate`) — kod, którego narzędzie nie sprawdza, idzie do „sprawdź ręcznie”,
  nigdy do kodu 0 (HAR-144).

Co sprawdzić w review: każdy z powyższych kształtów = **-25**.

#### Regresja (retro M1.7)

Źródło: 5 ticketów M1.7 z -25 u backend-python (HAR-78, HAR-87, HAR-89, HAR-151, HAR-152 —
ten ostatni w czterech rundach z rzędu, limit poprawek wyczerpany). Nowe kształty:

- **Kod wyjścia o kilku znaczeniach** — u gita kod 1 znaczy i „różni się”, i „błąd”
  (`diff --no-index`), kod 127 z `ps` znaczy „brak narzędzia”, nie „proces nie żyje”. Kod
  sprawdzasz razem z danymi wyjścia: pusty albo niezrozumiany wynik przy kodzie „różni się”
  to błąd, a strażnik, który nie może ocenić stanu, traktuje go jak zajęty, nigdy jak zgodę
  na akcję destrukcyjną (HAR-89, HAR-151).
- **Parser pomija element niezgodny z formatem** — element wyglądający na objęty regułą
  (`## L-12` zamiast `## L-012`, niedomknięty blok kodu) jest błędem walidatora, nie zwykłą
  sekcją przepuszczaną z kodem 0 (HAR-87).
- **Zdarzenie zamykające po utraconym dowodzie** — `run_finished` (werdykt, koniec całości)
  wysyłasz dopiero po potwierdzeniu, że zdarzenie kroku, z którego kokpit wylicza wynik,
  zostało przyjęte; utracony dowód nigdy nie kończy się stanem sukcesu (HAR-78).
- **Model zasięgu zewnętrznego narzędzia** (powtórka z M1.6, HAR-144) — przybliżenie przez
  AST zamiast mechanizmu narzędzia: pragma `end` na końcu linii z kodem, `end` w miejscu,
  którego `PragmaVisitor` nie czyta, nazwy z `NEVER_MUTATE_FUNCTION_NAMES` tylko na najwyższym
  poziomie. Gdy narzędzie ma własny parser, wołasz jego kod (`PragmaVisitor` zainstalowanego
  mutmuta), a nie piszesz drugi (HAR-152).

Co sprawdzić w review: każdy z powyższych kształtów = **-25**.

---

## Plik `.claude/rules/granica-bledow.md`

### Błędy na granicy i sprzątanie zasobów

Źródło: retro sprintu M1.0 — HAR-2, HAR-5, HAR-7.

#### Reguła

- Każde wejście z dysku, sieci albo YAML tłumaczy błędy wejścia na błąd dziedziny
  modułu: `OSError` (brak pliku, prawa, katalog zamiast pliku), `UnicodeDecodeError`,
  `YAMLError` oraz błędy struktury (`TypeError`, `RecursionError` przy złośliwym
  YAML). Wyjątek biblioteki nie ucieka z funkcji, której ticket obiecuje błąd dziedziny.
- Zasób zajęty na czas połączenia (subskrypcja szyny, licznik połączeń runnera,
  proces potomny) jest zajmowany **wewnątrz** bloku, którego `finally` go zwalnia —
  nie przed `try` i nie przed startem generatora strumienia.
- Klient strumienia obsługuje zerwanie (`onerror` w EventSource) i po wznowieniu
  dociąga historię.

#### Pułapka

`read_bytes()` przed `try` albo `subscribe()` poza generatorem SSE wygląda
poprawnie w szczęśliwej ścieżce; wyciek widać dopiero, gdy klient odpadnie, zanim
strumień ruszy — runner zostaje „online” na zawsze, kolejka nigdy nie jest zwolniona.

#### Co sprawdzić w review

- Nowe wejście z dysku/sieci/YAML bez tłumaczenia powyższych wyjątków = **-25**.
- Zajęcie zasobu poza blokiem z `finally`, który go zwalnia = **-25**.
- Każda gałąź tłumaczenia wyjątku ma test, który w nią trafia
  (zob. [test-odroznia-regule](test-odroznia-regule.md)).

#### Wąski `except` (retro M1.2)

Źródło: HAR-13 (`except SQLAlchemyError` na cały przelot), HAR-25 (`except IntegrityError`
przy kolizji numeru zdarzenia).

- `except` łapie tylko wyjątek, który kod faktycznie obsługuje, i tylko wokół instrukcji,
  która go rzuca. Szeroka klasa (`SQLAlchemyError`, `IntegrityError`, `Exception`) wokół
  wielu kroków przypisuje błędowi złą przyczynę — błąd logiki staje się „błędem połączenia”,
  a naruszenie innego ograniczenia „kolizją numeru” i jest ponawiane.
- Gdy biblioteka nie rozróżnia przyczyn typem, rozróżnij je po danych wyjątku (nazwa
  ograniczenia, kod błędu) i resztę przepuść dalej.
- W review: szeroki `except` obejmujący więcej niż obsługiwaną przyczynę = **-25**.

#### Regresja (retro M1.3)

Źródło: HAR-42 (dwa razy -25 w kolejnych rundach), HAR-26, HAR-37, HAR-39. Reguła
istnieje i nadal jest łamana, w nowych kształtach:

- **Poprawka tylko w miejscu wskazanym przez krytyka** — po uwadze o jednym odczycie
  dysku przejrzyj **wszystkie** dotknięcia dysku w tej funkcji i w funkcjach, które
  woła: także `Path.is_file()`, `exists()`, `stat()`, `os.walk` (bez `onerror` pomija
  błąd po cichu). Na Pythonie 3.11–3.13 `is_file()` przepuszcza `PermissionError`.
- **Długość wejścia względem kolumny** — pole z API zapisywane do kolumny o ograniczonej
  długości (`String(36)`) ma `max_length` w modelu wejścia; inaczej PostgreSQL rzuca
  `DataError` i klient dostaje 500 zamiast 422 (HAR-39).
- **Błąd strumienia runnera** (`HTTPStatusError`, `TransportError`) kończy proces
  komunikatem dziedziny, nie tracebackiem (HAR-26).

#### Regresja (retro M1.5)

Źródło: HAR-55, HAR-63, HAR-64, HAR-66 (cztery tickety, -10 do -25). Dane z granicy
(plik, JSON od dostawcy, zdarzenie od runnera) bez kontroli kształtu:

- **Typ i zamknięta lista na wejściu** — wartość z zewnątrz, od której zależy wynik
  (`wynik`, `kind`, `issues`, `comments`), jest sprawdzana co do typu i zamkniętej listy.
  Nieznana wartość to błąd dziedziny, nigdy domyślna gałąź „sukces” albo „pusto”
  (`{}` z API to nie „brak ticketów”; HAR-63, HAR-64).
- **Funkcje, które połykają błędy** — `Path.glob`/`rglob` i `os.walk` bez `onerror`
  pomijają `OSError` po cichu: brak dowodu wychodzi jako pusty wynik (HAR-55). Wynik
  pusty z nieczytelnego katalogu to błąd, nie „nic nie ma”.
- **Długość względem kolumny także od runnera** — wartość zapisywana do `String(n)` przez
  bufor runnera jest walidowana przed wysłaniem; inaczej 500 po stronie kokpitu i
  bezkońcowe ponawianie z bufora (HAR-66, wcześniej HAR-39 dla API).
- **Segment URL z danych** (klucz ticketu, nazwa repo) przechodzi przez `quote(…, safe="")`
  (HAR-63).

#### Regresja (retro M1.6)

Źródło: HAR-67 (-25), wcześniej HAR-13 i HAR-133 (pamięć backend-python).

- **Wyjątki spoza hierarchii biblioteki** — parser YAML zamienia daty i liczby na typy
  Pythona i na złych danych rzuca `ValueError`, `OverflowError`, `TypeError`, nie `YAMLError`.
  Tłumaczysz każdy typ, który faktycznie leci na złych danych (HAR-67, `skills.py`).
- **Brak klucza w odpowiedzi runnera** (`KeyError`) to błąd dziedziny (409 i stan bez zmian),
  nie 500 (HAR-67, `katalog.py`).

Co sprawdzić w review: każdy z powyższych kształtów = **-25**; każdy typ w krotce `except`
ma test (zob. [test-odroznia-regule](test-odroznia-regule.md)).

#### Regresja (retro M1.7)

Źródło: HAR-80, HAR-146, HAR-150, HAR-175 (-10 każdy), HAR-185 (-25). Nowe kształty:

- **Konfiguracja ze zmiennej środowiskowej to wejście z granicy** — `int(os.environ[...])`
  przy `abc`, `0` albo pustym napisie rzuca `ValueError` z konstruktora; tłumaczysz go na
  błąd dziedziny z nazwą zmiennej, zanim proces wystartuje (HAR-146).
- **Katalog i plik tymczasowy** — `tempfile.TemporaryDirectory()`/`mkdtemp` rzuca `OSError`
  (pełny dysk, brak praw do `TMPDIR`) jak każdy inny zapis (HAR-150).
- **Typ przyczyny, nie klasa bazowa biblioteki** — gdy biblioteka ma osobny typ dla
  obsługiwanej przyczyny (`TargetClosedError` Playwrighta), `except` łapie ten typ, a test
  sprawdza, że błąd tej samej biblioteki innego rodzaju (bazowy `Error`) leci dalej (HAR-185).
- **Awaria strażnika przy starcie to odmowa** — hook albo strażnik, który pada przy imporcie
  lub starcie, nie przepuszcza narzędzia (fail-closed); test wywołuje prawdziwy punkt wejścia
  z zepsutym startem (HAR-175).
- **Wyjątek zadania w pętli współbieżnej** — pętla czekająca na zadania kroków
  (`asyncio.wait`) zamienia każdy wyjątek zadania poza anulowaniem na zdarzenie końca kroku
  albo runu; run nigdy nie zostaje bez `run_finished` (HAR-80).

Co sprawdzić w review: każdy z powyższych kształtów = **-25**; każda gałąź tłumaczenia ma test
na prawdziwej funkcji (zob. [test-odroznia-regule](test-odroznia-regule.md)).

#### Regresja (retro M1.8)

Źródło: HAR-172 (-10, `BladCheckpointu` wychodził z `wykonaj_run` bez obsługi), HAR-173 (-25,
`KeyError` z `graf.py` uciekał z `wznow_run`, gdy skill zniknął z rejestru między bramką a
decyzją — run zostawał `running` bez `step_failed` i `run_finished`; korekta WRACA za HAR-133).

- **Nowy wyjątek dziedziny ma obsługę u wołającego** — moduł, który wprowadza własny błąd
  (`BladCheckpointu`), ma w tym samym PR gałąź w funkcji wywołującej, która zamienia go na
  zdarzenie końca kroku albo runu; test wywołuje prawdziwą ścieżkę wołającego.
- **Wejście czytane ponownie przy wznowieniu** (rejestr skilli, workflow, `project.yaml`,
  checkpoint) przechodzi tę samą walidację co przy starcie, **przed** pierwszym nieodwracalnym
  zapisem stanu (checkpoint z `waiting_on=None`, artefakt decyzji). Błąd struktury (`KeyError`)
  nie wychodzi z funkcji, która obiecuje błąd dziedziny.

Co sprawdzić w review: każdy z powyższych kształtów = **-25**.

#### Regresja (retro M1.10)

Źródło: HAR-170 (backend-python -10 utrzymane): limity nowych pól `EventIn` (`verdict` ≤ 200, `artefakty` ≤ 100×255,
`kotwice` ≤ 1000) nie są sprawdzane w runnerze przed wysłaniem, więc 4xx z kokpitu oznacza utracone zdarzenie kroku.
To ten sam kształt co HAR-66 (M1.5, „Długość względem kolumny także od runnera”) — reguła istnieje i nadal jest łamana.

- **Nowe pole w `EventIn` z limitem** dostaje w tym samym PR walidację po stronie runnera (przed buforem i wysłaniem)
  oraz test, który wysyła wartość o jeden ponad limit i sprawdza, że runner zgłasza błąd dziedziny zamiast ponawiać.
- Egzekucja twardsza niż tekst: test porównujący limity `EventIn` z walidacją runnera — ticket z retro M1.10.

Co sprawdzić w review: nowe pole `EventIn` z limitem bez walidacji runnera = **-25**.

---

## Plik `.claude/rules/merge-rownoleglych-ticketow.md`

### Merge równoległych ticketów: konflikt bez konfliktu tekstu

Źródło: retro sprintu M1.9 — HAR-176 (`KeyError` po HAR-178), HAR-174 (dwa razy `WynikKroku` wołany pozycyjnie po HAR-176
i HAR-177; kolizja numeru migracji z HAR-178), HAR-177 (6 testów silnika padło po HAR-174, atrapa agenta bez stream-json).

#### Reguła

- **Po `git merge origin/main` uruchamiasz pełny `pytest -q -n 4`**, nie tylko testy plików z konfliktem. Git łączy bez
  konfliktu kod, który razem nie działa: HAR-178 dodał `rejestr.skille[krok.skill]`, a krok systemowy HAR-176 ma `skill=None`.
- **Nowe pole dataclass zwracanej z wielu miejsc** (`WynikKroku`) dodajesz z wartością domyślną, a wszystkie konstruktory
  w `src/` i `tests/` przepisujesz na argumenty nazwane w tym samym PR. Dwa tickety dodające „czwarte pole” jednocześnie
  dają po merge wartość w złym polu, bez błędu typu.
- **Kod czytający pole, które od tego sprintu może być puste** (`krok.skill` przy kroku systemowym) ma gałąź dla pustej
  wartości i test z takim krokiem.
- **Atrapa zewnętrznego procesu w teście** (agent, `claude`) wypisuje format, który czyta produkcyjny parser (zdarzenie
  `result` stream-json), nie surowy tekst — inaczej zmiana parsera psuje testy cudzych ticketów.
- **Numer migracji** to głowa z `alembic heads` na `origin/main` w chwili merge, nie numer z opisu ticketu.

#### Pułapka

Konflikt tekstowy rozwiązany, testy plików z konfliktem zielone, PR zielony na starym `main` — a po scaleniu drugiego PR
silnik rzuca wyjątek i run nie dostaje `run_finished`. Widać to dopiero na `main` albo przy następnym merge.

#### Co sprawdzić w review

- Merge `main` w PR bez pełnego `pytest` po merge (raport wymienia tylko testy plików z konfliktem) = **-10**.
- Nowe pole dataclass z wywołaniami pozycyjnymi w `src/` lub `tests/` = **-25**.

---

## Plik `.claude/rules/praca-w-tle.md`

### Praca w sesji w tle bez czekania na człowieka

Źródło: retro sprintu M1.2 — sesje HAR-13, HAR-24, HAR-40 i HAR-43 stały łącznie
kilkadziesiąt godzin na prośbach o zgodę, bo komendy usuwały pliki albo łączyły wiele
operacji w jednym wywołaniu.

#### Reguła

- **Nie usuwaj plików ani katalogów** (`rm -rf`, `rm -r`, `shutil.rmtree`) w komendach
  narzędzi. Kopia robocza, mutant albo eksperyment idzie do **nowego** katalogu pod
  katalogiem tymczasowym zadania (`~/.claude/jobs/<id>/tmp/<nazwa>-<n>`) — ten katalog
  sprząta środowisko.
- **Jedna prosta komenda na wywołanie.** Bez pętli łączących `cp`, `sed -i`, `git`
  i testy w jednej linii; złożoną procedurę zapisz jako skrypt w katalogu tymczasowym
  i uruchom go osobno.
- **Bez `sed -i`** na plikach repozytorium — zmiany w kodzie przez edycję pliku;
  `sed -i -E` zostawia artefakty `*-E` (HAR-14).
- **Zapis tylko w swoim worktree** i w katalogu tymczasowym zadania — nie w pamięci
  sesji głównej ani w katalogach użytkownika.

#### Pułapka

W trybie `auto` każda z tych operacji kończy się prośbą o zgodę. Sesja w tle nie ma
okna, więc stoi do rana, a pętla sprintu nie startuje nowych ticketów, gdy dwie sesje
czekają na człowieka (`MONOLYNX_SPRINT_MAX_WAITING`).

#### Co sprawdzić w review

- Raport agenta wymienia komendę z `rm -rf` albo `sed -i` na pliku repozytorium = **-10**.
- Artefakty narzędzi (`*-E`, `*.orig`, katalogi `mut*`) w diffie = **-25**.

#### Reguły z review — komentarz ticketu, nie `.claude/` (retro M1.3)

Źródło: HAR-35, HAR-38 — sesje stały na prośbie o zgodę przy zapisie do
`.claude/agent-memory/*/MEMORY.md`; HAR-35 zostawił niezacommitowaną zmianę tego pliku.

- Sesja w tle **nie edytuje plików w `.claude/`** (reguły, pamięć agentów, ustawienia).
  W trybie `auto` każdy taki zapis czeka na człowieka.
- Regułę z review zapisuje w komentarzu podsumowania ticketu jako linię `REGULA: …` —
  retro zbiera ją stamtąd i przenosi do `.claude/rules/` przez osobny PR.
- Komendy bez `cd … &&` i bez przeszukiwania cudzych worktree — każda taka komenda
  także czeka na zgodę (HAR-45, HAR-52).

#### Decyzje w sesji w tle: strażnik zamiast człowieka (2026-11-10)

Źródło: HAR-219 (sesja stała na AskUserQuestion o pełne testy), HAR-180 (49 min na pytaniu). Właściciel
przekazał decyzje strażnikowi decyzji (`.git/monolynx/straznik`), który ocenia je według CLAUDE.md,
`.claude/rules` i ticketu.

- Sesja w tle **nie używa AskUserQuestion** (hook `.claude/hooks/pytania_w_tle.py` odmawia).
- Gdy decyzja nie wynika z ticketu, CLAUDE.md ani reguł: zakończ turę wiadomością, której pierwsza linia
  to `DECYZJA-POTRZEBNA: <czego dotyczy>`, dalej dowody (plik:linia, wynik komendy), opcje
  `- <etykieta>: <skutek>` i rekomendacja. Nie wykonuj żadnej opcji przed odpowiedzią.
- Odpowiedź przychodzi jako wiadomość „Decyzja strażnika …” — wykonujesz ją jak decyzję właściciela.
- PR ticketu po zielonym CI ocenia strażnik. `rework` wraca komentarzem „Strażnik decyzji: rework” z
  rekomendacjami: poprawiasz na tej samej gałęzi, bez `--force`/`--amend`, ten sam PR.

Co sprawdzić w review: AskUserQuestion albo pytanie bez linii `DECYZJA-POTRZEBNA` w sesji w tle = **-10**.

---

## Plik `.claude/rules/raport-zgodny-z-diffem.md`

### Raport zgodny z diffem

Źródło: retro sprintu M1.4 — HAR-52 (raport wymieniał testy, których nie było w pliku),
HAR-54 (backend wstawił zmyślony cytat kryterium BRO-108).

#### Reguła

- Każde twierdzenie raportu agenta o kodzie („dodałem test X”, „kryterium mówi Y”,
  „mutant Z zabity”) da się wskazać w diffie, w pliku albo w źródle (ticket, Linear, wiki)
  — z nazwą testu, ścieżką i linią albo dosłownym cytatem.
- Team Manager przed rekoncyliacją sprawdza w diffie każdy wymieniony test i plik; cytat
  z ticketu porównuje z jego treścią. Twierdzenie bez pokrycia = raport odrzucony, nie
  „zaakceptowany z uwagą”.
- Agent, który nie wie, pisze „nie sprawdziłem”, zamiast zgadywać.

#### Pułapka

Raport brzmi pewnie i jest spójny, więc krytyk ocenia go zamiast diffu. Zmyślony test
wygląda jak pokrycie reguły, której nikt nie chroni.

#### Co sprawdzić w review

- Raport wymienia test, plik albo cytat, którego nie ma w diffie lub w źródle = **-25**.

#### Regresja (retro M1.8)

Źródło: HAR-173 (pierwszy raport backend-python opisywał zmiany, których nie było w pliku —
skrypt agenta zablokował hook, wykrył to dopiero lint), HAR-226 (raport podał 12 wierszy tabeli
ręcznych mutantów, w pliku było 11; pokrycie mutanta opisane jako „po 120 s”, faktycznie ok. 10 s).
HAR-225: agent zmienił tabelę innego ticketu (`HAR-156.toml` #68) i nie zgłosił tego `STOP:`.

- **Liczby w raporcie liczysz z pliku** — liczba wierszy tabeli, testów i mutantów pochodzi z
  komendy uruchomionej po ostatniej zmianie (`grep -c '^\[\[mutant\]\]'`, wynik pytesta), nie
  z pamięci agenta.
- **Komenda odrzucona przez hook = brak zmiany** — raport po odmowie narzędzia mówi „nie
  zapisałem”, nie opisuje zamierzonej zmiany jako wykonanej.
- **Plik spoza przydziału** (tabela innego ticketu, plik frontu przy nowej wartości enuma w
  OpenAPI) — zmiana wymaga linii `STOP:` w raporcie z powodem; Team Manager decyduje, czy wchodzi
  (HAR-172: etykieta `rejected` w `WidokRunu.tsx`, bez której `npm run build` pada).

Co sprawdzić w review: liczba w raporcie inna niż w pliku albo opis zmiany, której nie ma w
diffie = **-25**; zmiana pliku spoza przydziału bez `STOP:` = **-10**.

---

## Plik `.claude/rules/test-odroznia-regule.md`

### Test odróżnia regułę od jej braku

Źródło: retro sprintu M1.0 — wzorzec w 7 z 9 ticketów (HAR-2, 3, 5, 6, 7, 8, 9),
krytyk dwa razy oznaczył go jako „korekta WRACA” (HAR-6, HAR-9).

#### Reguła

Każdy element kodu, który zmienia wynik reguły, ma test na wejściu, na którym
**tylko ten element** decyduje o wyniku. Usunięcie elementu musi oblać co najmniej
jeden test. Dotyczy to w szczególności:

- **gałęzi** kodu chroniącego niezmiennik albo tłumaczącej wyjątek,
- **elementu zbioru lub tabeli** rozszerzającego wykrywanie (znak separatora,
  opcja, klucz) — przypadek, w którym bez tego elementu nic innego by nie wykryło,
- **trybu albo źródła** (globalny/lokalny, plik/rejestr) — wejście dające inny
  wynik w każdym trybie,
- **zakresu** (przodkowie w `needs`, kroki cyklu) — poza zakresem istnieje krok,
  który dałby inny wynik, gdyby reguła była globalna,
- **złego typu** — wartość odrzucana tylko przez implementowaną regułę
  (napis zamiast liczby, `yes` zamiast bool), nie przez każdą implementację,
- **podmiany wartości z pliku** zależnie od stanu rejestru — wartość w pliku
  istnieje i zmieniłaby wynik,
- **filtra `tenant_id`** — w bazie są co najmniej dwa zespoły, rekord innego
  zespołu nie zostaje wybrany.

#### Pułapka

Test, który przechodzi zarówno z regułą, jak i bez niej, wygląda na pokrycie,
ale nie chroni niczego: mutant usuwający gałąź lub element zbioru przeżywa.
Pojedyncze reguły z review lądowały w `.claude/agent-memory/test-writer/MEMORY.md`
i i tak wracały w następnym tickecie — to jest jedna zasada, nie jedenaście.

#### Co sprawdzić w review

- Dla każdej nowej gałęzi, elementu zbioru, trybu i zakresu w diffie: wskaż test,
  który oblewa po jej usunięciu. Brak takiego testu = **-25**.
- Przeżywający mutant w zmienionym pliku (mutation-check) = **-25**.

#### Egzekucja

Źródło: HAR-133. Przed oddaniem pracy do review test-writer albo Team Manager
uruchamia z korzenia repozytorium:

```
python tools/mutanty_zmian.py            # baza: origin/main
python tools/mutanty_zmian.py --baza <ref>
```

Skrypt mutuje wyłącznie funkcje i metody zmienione względem bazy (także
niescommitowane i nowe pliki w `source_paths` z `pyproject.toml`) i wypisuje
przeżywające mutanty, po jednym w linii.

- Kod wyjścia **0** — wszystkie mutanty zmienionych funkcji zabite (albo brak zmienionych
  funkcji) i brak zmian poza zasięgiem mutmuta.
- Kod wyjścia **1** — są przeżywające mutanty; dopisz test na wejściu, które je zabija.
- Kod wyjścia **2** — błąd narzędzia (nieznana baza, zły `pyproject.toml`, błąd składni,
  błąd mutmuta); to nie jest wynik „czysto”.
- Kod wyjścia **3** (HAR-144) — zmiany poza zasięgiem mutmuta: stdout zaczyna się od
  `bez mutantów — sprawdź ręcznie`, dalej wpisy `plik:linia`, po jednym w linii. To nie
  jest „czysto”: dopisz ręczny mutant i test. Zakres: pliki `.py` pod `src/` spoza
  `source_paths` oraz w `source_paths` linie kodu poza funkcjami i metodami (bez pustych
  linii, komentarzy, docstringów i importów) oraz funkcje i metody z dekoratorem innym niż
  pojedynczy `staticmethod`/`classmethod` (mutmut 3.8.0 ich nie mutuje); `tests/`, `web/`, `tools/` i `*.md` są
  poza zakresem. Priorytet kodów: 2 > 1 > 3 > 0; przy kodzie 1 lista bez mutantów idzie
  na stderr. W CI krok jest zielony, a lista trafia do sekcji podsumowania „bez mutantów —
  sprawdź ręcznie”.
- Przeżywający mutant w zmienionej funkcji bez uzasadnienia równoważności = **-25**
  w review.
- Skrypt nie czyści cache `mutants/`. Po zmianie samych testów (bez zmiany kodu
  źródłowego) wynik może pochodzić ze starego przebiegu (strona wiki `constitution`,
  HAR-6) — wtedy uruchom skrypt w świeżym klonie repozytorium w nowym katalogu
  tymczasowym (`git clone <repo> <katalog-tymczasowy>/mutanty-<n>`, z dokopiowanymi
  niescommitowanymi zmianami). Nie usuwaj `mutants/` w sesji agenta; usunięcie jest
  dozwolone tylko jako ręczna akcja człowieka.
- Pełny przebieg mutmuta wymaga, by `[tool.mutmut] also_copy` obejmowało wszystko,
  czego potrzebują testy (np. `alembic/`, `alembic.ini`); inaczej skrypt kończy się
  kodem 2 na fazie statystyk mutmuta.

#### Regresja

Źródło: retro sprintu M1.1. Reguła istnieje i nadal jest łamana — HAR-10
(krytyk: „korekta WRACA — wzorzec HAR-6”), wcześniej HAR-2, 3, 5, 6, 7, 8, 9.

Nowy kształt tego samego wzorca — **zawężenie wzorca albo klasy znaków**
(`[A-Za-z0-9_]` zamiast `\w`, kotwica, węższa alternatywa): test ma wejście,
które **tylko zawężenie** odrzuca. Wejście parsowane tak samo przez wersję
szerszą nie chroni zawężenia przed cofnięciem (HAR-10: `xǁAǁBǁc__mutmut_1`
zabija mutanta `\w`, bo `\w` dopasowuje `ǁ`).

Tekst reguły nie wystarcza — egzekwuje ją `tools/mutanty_zmian.py`
(zob. [Egzekucja](#egzekucja)): mutanty zmienionych funkcji przed werdyktem krytyka.

#### Regresja (retro M1.2.1)

Źródło: HAR-133 (dwa razy -25 w kolejnych rundach), HAR-138 (uwaga krytyka bez
odjęcia). Reguła i jej egzekucja istnieją, a wzorzec wraca w nowych kształtach:

- **Zaślepka zawsze zwraca sukces** — testy podmieniają funkcję uruchamiającą
  podproces albo czytającą wejście, więc gałęzie błędu (kod wyjścia, `OSError`,
  kanał stderr) nie mają testu na **prawdziwym** kodzie tej funkcji. Każda gałąź
  tłumacząca błąd ma test, który wywołuje prawdziwą funkcję na wejściu wyzwalającym
  tę gałąź.
- **Asercja przechodzi także na starym kodzie** — np. „komunikatu X nie ma w stderr”,
  gdy stary kod też go nie wypisywał z innego powodu. Przed oddaniem sprawdź, że
  asercja oblewa po cofnięciu zmiany.

Egzekucja dodatkowa: krok CI z `tools/mutanty_zmian.py` na każdym PR (ticket z retro
M1.2.1) — przeżywające mutanty zmienionych funkcji widać w raporcie PR.

#### Regresja (retro M1.3)

Źródło: HAR-26 („korekta WRACA” za HAR-29), HAR-35 („korekta WRACA” za HAR-6 i HAR-133),
HAR-141, uwagi bez odjęcia w HAR-34, HAR-36, HAR-38. Nowe kształty:

- **Test błędu sprawdza dokładny typ wyjątku** — `pytest.raises(Exception)` przyjmuje też
  wyjątek z innej gałęzi i nie chroni tej, którą test opisuje (HAR-26).
- **Gałąź przepuszczająca wyjątek dalej** (`raise`, re-raise po anulowaniu) ma test, w
  którym tylko ona decyduje, czy wołający dostanie wyjątek (HAR-35).
- **Test na produkcyjnej ścieżce wywołania** — gałąź chroniąca niezmiennik ma test, w
  którym żaden inny mechanizm (np. pętla sprzątająca w `stop()`) nie daje tego samego
  skutku (HAR-141).
- **Wartość, nie obecność** — test tabeli sprawdza wartość wpisu (np. `Zabronione → 403`),
  nie tylko to, że wpis istnieje (HAR-38).

#### Regresja (retro M1.4)

Źródło: 13 z 14 ticketów M1.4/M1.5 dostało -25 z tej reguły, w 10 „korekta WRACA”
(HAR-22, 44, 46, 47, 48, 49, 50, 51, 52, 53, 141, 142, 65). Nowe kształty:

- **Ograniczenia pól i wartości domyślne** — `Field(max_length=…, ge=…)`, `default=` w
  modelu i elementy stałych zbiorów (`METODY_BEZPIECZNE`) mają test, w którym tylko ta
  wartość decyduje (HAR-44, 48, 51). `tools/mutanty_zmian.py` takich mutantów nie generuje, ale zgłasza je kodem 3 (HAR-144) —
  „brak zmienionych funkcji” to brak dowodu, nie „czysto”: dopisz ręczny mutant i test.
- **Każdy warunek zapytania** wybierającego istniejący rekord (adres, użytkownik, zespół)
  ma w danych rekord odrzucany tylko przez ten warunek (HAR-50) — uogólnienie punktu o
  filtrze `tenant_id`.
- **Warunek typu i „użyj wartości dosłownie”** (`isinstance`, parametry tylko dla pierwszej
  strony) ma wejście, które wersja bez warunku przetwarza inaczej (HAR-53, HAR-65).
- **Gałąź sprzątania w `finally`** ma test poza ścieżką `stop()`, który ją wyzwala (HAR-49).
- **Równoważność mutanta** uzasadniasz wskazaniem, że żadne wejście nie odróżnia wersji;
  „test przechodzi” albo „to tylko kosmetyka” nie jest uzasadnieniem (HAR-52).

#### Egzekucja ręczna tam, gdzie mutmut nie sięga (retro M1.5)

Źródło: „korekta WRACA” w 7 ticketach M1.5 (HAR-49, 59, 61, 62, 65, 142, 143) mimo
sekcji Regresja z M1.1–M1.4. Kolejny akapit nie pomoże — potrzebny dowód w raporcie.

Gdy zmiana obejmuje miejsce, którego mutmut nie mutuje — front (`web/`), testy e2e,
krotki w `except`, elementy stałych zbiorów i wyrażeń regularnych, hooki pytesta
(`tryfirst`, `skipif`), pliki spoza `source_paths` (kod 3 z `tools/mutanty_zmian.py`,
HAR-144) — raport test-writera zawiera tabelę **ręcznych mutantów**:
`element | mutacja | test, który oblewa`. Po jednym wierszu na każdy zmieniony element.

Źródło: HAR-150. Tabela jest wykonywalna: żyje jako `tests/mutanty_reczne/<ticket>.toml`,
a wiersz to wpis `[[mutant]]` z polami `plik` (ścieżka względna od korzenia repo), `szukaj`
(musi wystąpić w pliku dokładnie raz), `zamien` (różne od `szukaj`) i `test` (node id pytesta):

```toml
[[mutant]]
plik = "src/cockpit/services/runs.py"
szukaj = "if kod == 1:"
zamien = "if kod != 1:"
test = "tests/test_runs.py::test_run_zakonczony_sukcesem_ma_stan_do_odbioru_nie_gotowe"
```

`python tools/mutanty_reczne.py tests/mutanty_reczne/<ticket>.toml` nakłada każdą mutację w
nowej kopii repozytorium w katalogu tymczasowym i sprawdza, że wskazany test oblewa (test
musi najpierw przejść na kopii bez mutacji). Kod wyjścia **0** — wszystkie mutanty zabite,
**1** — są przeżywające (stdout: `plik: szukaj → zamien`), **2** — błąd narzędzia: zła
tabela, `szukaj` nie raz, test czerwony bez mutacji, kod pytesta inny niż 0/1, przekroczony
limit czasu. W CI krok `mutanty ręczne` w jobie `mutation` wykonuje tabele zmienione w PR;
zmiana w `web/`, `tools/` lub `cicd/` albo kod 3 z `tools/mutanty_zmian.py` bez zmienionej
tabeli to czerwony krok z komunikatem „brak dowodu”.

- Brak tabeli przy zmianie poza zasięgiem mutmuta = **-25**.
- Wiersz z testem, który po mutacji nadal przechodzi (krytyk sprawdza co najmniej jeden
  wiersz, wprowadzając mutację lokalnie) = **-25**.

#### Regresja (retro M1.6)

Źródło: 9 z 15 ticketów M1.6 z kodem dostało -25 albo -20 z tej reguły (HAR-15, 20, 28, 67,
68, 70, 71, 74, 144); „korekta WRACA” wprost w HAR-28, HAR-71, HAR-74. Tabela ręcznych
mutantów z M1.5 istnieje, a brakujące wiersze znajduje dopiero krytyk. Nowe kształty:

- **Strażnik przed istniejącą gałęzią i wartość zastępcza** — krótkie wartościowanie
  (`rejected and …`), `isinstance(…, str)`, `and wezel.body`, `mapa.get(k, set())`,
  przepakowanie wyjątku z kontekstem: test na wejściu, na którym bez strażnika leci wyjątek
  biblioteki albo inny komunikat, także na pustym wejściu, gdzie dotąd działała starsza
  gałąź (HAR-20, HAR-28, HAR-144).
- **Element krotki `except` i blokada wiersza** — każdy typ w krotce ma wejście, które
  wyzwala tylko ten typ; `with_for_update()` ma test, który oblewa po jej usunięciu (na
  SQLite: kompilacja przechwyconego zapytania dialektem PostgreSQL) (HAR-67).
- **Front: każdy element diffu `web/` ma test e2e** — przekształcenie wejścia (`trim`: osobno
  każde wystąpienie, w warunku i w wartości), gałąź tłumacząca nieczytelną odpowiedź
  serwera, czyszczenie stanu przy zmianie pola, na początku akcji i po ponownym
  zalogowaniu. Test sprawdza skutek w następnym przejściu stanu. „Projekt nie ma testów
  frontu” nie zwalnia — `tests/e2e/` istnieje (HAR-70, HAR-71).
- **Test z limitem czasu w nazwie** zaczyna mierzyć przed zdarzeniem i zależy od mechanizmu,
  który limit realizuje (odświeżanie, push), nie od pobrania przy wejściu na stronę (HAR-68).
- **Dopasowanie jednoznaczne** — asercja na treści (brief, komunikat) sprawdza zdanie, które
  istnieje tylko w regule; pomocnik testu zawężający do zakresu ma test z elementem spoza
  zakresu, który dałby inny wynik bez zawężenia (HAR-15, HAR-74).
- **Strefa czasowa** — gałąź naive→UTC ma test ze strefą procesu inną niż UTC
  (`TZ` + `time.tzset`), bo CI biegnie w UTC i tam mutant przeżywa (HAR-68).

Tekst reguły znów nie wystarczył — egzekucja tabeli ręcznych mutantów jako krok CI to
osobny ticket z retro M1.6.

Co sprawdzić w review: element z listy bez testu, który oblewa po jego usunięciu = **-25**;
test z limitem czasu niezależny od mechanizmu limitu = **-20**.

#### Regresja (retro M1.7)

Źródło: 12 z 33 ticketów M1.7 dostało -25 z tej reguły u test-writera (HAR-77, 79, 81, 85, 86,
150, 151, 160, 163, 183, 206, 221); „korekta WRACA” wprost w HAR-81, 85, 86, 150, 163, 183.
Większość mutantów znalazł `tools/mutanty_zmian.py` albo krytyk w tabeli — test-writer
oddaje pracę przed przejściem własnej listy. Nowe kształty:

- **Komunikat na pełną treść** — każda linia ostrzeżenia i treść wyjątku w zmienionej
  funkcji ma asercję na cały napis, nie „zawiera słowo”; mutant `XX…XX` zachowuje każde
  słowo i przeżywa (HAR-183, wcześniej HAR-12).
- **Każdy element zapytania z celem** — `ORDER BY` ustalające kolejność blokad, `FOR UPDATE`,
  filtr: test, który oblewa po usunięciu właśnie tego elementu; test sąsiedniego elementu
  tego samego zapytania go nie chroni (HAR-206).
- **Lista przypadków niezależna od kodu** — parametry testu strażnika pochodzą z osobnej,
  stałej listy, nigdy z listy wzorców, której używa kod pod testem; inaczej usunięcie
  wzorca usuwa też jego test (HAR-163, wcześniej HAR-6, HAR-29).
- **Stan rodzica różny od oczekiwanego u potomka** — test dziedziczenia (stdin, zmienna
  środowiskowa) ustawia u rodzica wartość inną niż oczekiwana u potomka; pod xdist fd 0
  to już `/dev/null` i asercja przechodzi bez reguły. „Nieustawiona” i „pusta” zmienna
  to dwa osobne przypadki (HAR-85, HAR-157).
- **Sprzątanie współbieżne i liczniki limitu** — anulowanie w `finally`, drugi czytnik
  strumienia, gałąź `trap` skryptu: test na ścieżce, na której działa więcej niż jedno
  zadanie i tylko ta gałąź zwalnia zasób; licznik limitu sprawdzony wartością, przy której
  krok `+1` i `+2` dają różny wynik (HAR-151, HAR-160).
- **Wartość, którą błąd mógłby zostawić** — test gałęzi błędu sprawdza wartość, którą
  błędna zmiana by zapisała (np. `tabele=1` po błędzie skryptu), nie wartość, której żadna
  implementacja nie zapisuje (HAR-221).
- **Każdy człon warunku i strażnik z poprawki** — `isfinite` obok `<= 0`, `is not None`
  dodane po review: parametryzacja złych wartości ma wartość odrzucaną tylko przez ten
  człon; poprawka po review podlega tej samej regule co kod pierwotny (HAR-86, HAR-150).
- **Front: stan przejściowy** — stan „przed pierwszą odpowiedzią” ma test z wstrzymaną
  odpowiedzią i asercją przed jej nadejściem; gałąź `!r.ok` dostaje poprawny JSON (nie-JSON
  trafia do `catch`); test gałęzi ustawiającej stan startuje z innego stanu niż docelowy,
  bez przeładowania strony (HAR-77, pięć rund po 75).

##### Cudze tabele ręcznych mutantów

Źródło: HAR-81 (przeniesienie kodu do `WidokRunu.tsx` zepsuło wpisy HAR-77 i HAR-156),
HAR-221 (zepsuł dwa wiersze HAR-156), HAR-223 (po nim `HAR-0.toml` #7 nie trafia w kod).
Na `origin/main` po M1.7 cztery wiersze nie trafiają w kod (HAR-0 #2, #7, #10, HAR-79 #35),
bo CI wykonuje tylko tabele zmienione w PR.

- Zmiana, która przenosi kod, zmienia tekst trafiany przez `szukaj` albo nazwę testu
  wskazanego w tabeli **innego** ticketu, poprawia tę tabelę w tym samym PR (nowe `plik`,
  pełniejsze `szukaj` albo wiersz przeniesiony do tabeli bieżącego ticketu).
- Każdy wpis stałej mapy napisów ma osobny wiersz z testem, w którym tylko ta wartość decyduje.
- Egzekucja: sprawdzenie statyczne wszystkich tabel na każdym PR — HAR-225.

Co sprawdzić w review: każdy z powyższych kształtów bez testu, który oblewa po usunięciu
elementu = **-25**; PR, po którym wiersz cudzej tabeli przestaje trafiać w kod = **-25**.

#### Regresja (retro M1.8)

Źródło: 3 z 4 ticketów M1.8 dostały -25 z tej reguły u test-writera (HAR-172 dwie rundy z rzędu,
„korekta WRACA”; HAR-173; HAR-225 „korekta WRACA” za HAR-141/HAR-133). Za każdym razem brakujące
wiersze znalazł krytyk, nie autor tabeli. Nowe kształty:

- **Lista kodu 3 to lista wierszy tabeli** — każda linia, którą `tools/mutanty_zmian.py` wypisuje
  jako „bez mutantów — sprawdź ręcznie”, ma w tabeli ręcznych mutantów wiersz albo uzasadnienie
  równoważności. Dotyczy także wywołania serwisu w trasie (argument `k.tenant_id`, dekorator,
  `Depends(...)`), pól dataclass i modeli wejścia/wyjścia (HAR-172).
- **Strażnik typu** (`isinstance(x, bool)` obok `int`) ma wejście, które odrzuca tylko on
  (`proby: true` w checkpoincie) (HAR-172).
- **Kolejność zapisów nakazana w ustaleniach** (artefakt → checkpoint → zdarzenie) ma test na
  danych, który oblewa po zamianie dwóch zapisów; element krotki `except` (`OSError` obok błędu
  dziedziny) — wejście wyzwalające tylko ten typ (HAR-173).
- **Strażnik za strażnikiem z tym samym kodem wyjścia** (`if not tabele:` przed sprawdzeniem
  zera wierszy, oba dają kod 2) — asercja na fragmencie komunikatu, który powstaje tylko w nim;
  `except` wokół `is_file()` — test, który w niego trafia (HAR-225).
- **Przeżywający ręczny mutant frontu bywa kodem martwym** — zanim dopiszesz test, sprawdź, czy
  element jest osiągalny (React nie woła `onClick` przycisku z `disabled`; podwójna kontrola
  `nieaktualny()`); kod nieosiągalny usuwasz (HAR-173).
- **Mutant równoważny** oznaczasz `# pragma: no mutate` z komentarzem uzasadnienia w linii nad nim
  i wierszem ręcznego mutanta dla wariantu, który da się zabić (precedens `src/runner/checkpoint.py`,
  HAR-173 `_zapisz_decyzje`). Pusty `zamien` w pliku TypeScript, który zostawia nieużytą zmienną,
  psuje build (TS6133) — wtedy `void x;` (HAR-173).

Tekst reguły znów nie wystarczył — egzekucja „każda linia kodu 3 ma wiersz albo uzasadnienie”
jako sprawdzenie w CI to ticket z retro M1.8.

Co sprawdzić w review: linia z listy kodu 3 bez wiersza i bez uzasadnienia = **-25**; każdy
z powyższych kształtów bez testu, który oblewa po usunięciu elementu = **-25**.


#### Regresja (retro M1.9)

Źródło: 4 z 5 ticketów M1.9 dostały odjęcie z tej reguły w rundzie 1 (HAR-174 test-writer -25, HAR-176 test-writer -25 i
backend-python -25, HAR-178 test-writer -25, HAR-228 test-writer -15). Za każdym razem brak wskazał krytyk, nie autor. Nowe kształty:

- **Każdy element wyrażenia regularnego w stałej modułu** — klasa znaków, wykluczony znak (`[^\[\]:]`), kotwica i końcowy
  literał (`\]`) mają wejście, na którym tylko ten element decyduje o dopasowaniu (HAR-176, `KOTWICA` w `src/runner/kotwice.py`).
- **Element krotki `except`, którego nie da się wyzwolić w pojedynkę** (podklasa innego elementu: `UnicodeDecodeError` obok
  `ValueError`) — usuwasz go albo opisujesz jako mutant równoważny (`[[rownowazny]]` z uzasadnieniem), zamiast zostawiać bez
  testu (HAR-176).
- **Agregacja po kilku wierszach** (agenci, próby, kroki) ma test z co najmniej dwoma wierszami tego samego klucza w różnych
  stanach; na wejściu z jednym wierszem każdy element zbioru stanów i klucz grupowania są równoważne z ich brakiem (HAR-178,
  `src/cockpit/services/runs.py:325-329`).
- **Zwolnienie zasobu w `finally`** (deskryptor, uchwyt pliku) ma test obserwujący zwolnienie — stan deskryptora po wyjściu —
  i wiersz tabeli; korekta WRACA za HAR-49 z M1.4 (HAR-174, `--stdin-plik` w `src/runner/straznik.py`).
- **Wywołanie istniejącego strażnika wewnątrz nowej funkcji** (`_wymagaj_dokladnie_raz(...)`) jest elementem nowej logiki i ma
  własny wiersz z testem, w którym tylko to wywołanie decyduje o kodzie błędu (HAR-228).
- **Mutant, którego nie da się zaimportować** (mutowalna wartość domyślna dataclass, dataclass bez `frozen` jako pole `frozen`)
  nie trafia do tabeli — pytest kończy się błędem zbierania, a narzędzie zgłasza błąd, nie zabicie (HAR-174).

Co sprawdzić w review: każdy z powyższych kształtów bez testu, który oblewa po usunięciu elementu = **-25**.

#### Regresja (retro M1.10)

Źródło: HAR-170 (test-writer -25 w rundzie 1, potem cztery rundy), uwagi krytyka bez odjęcia w HAR-232 i HAR-233,
wiersze dla listy kodu 3 dopisywane osobnym commitem po pierwszym commicie ticketu (HAR-179 `62894f3`, HAR-230
`5699ba9`, HAR-234 `65d4126`), cudze tabele zepsute w HAR-179 i HAR-170. Nowe kształty:

- **Kolejność i filtr rodzaju zbudowane w Pythonie** — sortowanie stanów po słowniku i filtr `kind == "step_finished"`
  (HAR-170, `src/cockpit/services/odbior_dowody.py`) mają test, w którym tylko one decydują: dla sortowania wejście,
  w którym kolejność wstawiania różni się od kolejności wyniku (powtórzony klucz przepleciony innym), dla filtra
  zdarzenie odrzuconego rodzaju (`step_failed`) z tymi samymi danymi. Uogólnienie punktu M1.7 o `ORDER BY` na kod
  poza zapytaniem.
- **„Mutant tylko wyścigowo” to nie równoważność** — warunek liczący odpowiedzi (`odpowiedzi_get >= 2` → `>= 1`,
  HAR-232) ma wiersz z testem, który wymusza kolejność zdarzeń, albo jest usunięty; uzasadnienie w nagłówku TOML
  bez wskazania, że żadne wejście nie odróżnia wersji, nie zwalnia z wiersza.
- **Każda gałąź `exit 1` w skrypcie CI** (HAR-233, krok poprzedniego zielonego wyniku: pięć gałęzi błędu) ma wiersz
  ręcznego mutanta usuwający `exit 1` — komunikat `::error::` bez wyjścia z kodem 1 to zielony krok.
- **Lista kodu 3 przed pierwszym commitem** — test-writer uruchamia `python tools/mutanty_reczne.py --pokrycie-kod3
  <lista> tests/mutanty_reczne/<ticket>.toml` przed oddaniem pracy, nie po czerwonym kroku CI; trzy tickety M1.10
  dopisywały te wiersze osobnym commitem.
- **Cudze tabele przed PR** — `python tools/mutanty_reczne.py --sprawdz-wszystkie` po każdej zmianie kodu, nie tylko w
  CI. Dwa nowe kształty: `szukaj` z treścią `web/openapi.json` przestaje być jednoznaczny po regeneracji kontraktu
  (HAR-170 poprawiał HAR-159 #18, HAR-174 #143–144), a kod skopiowany do nowej funkcji podwaja krótkie `szukaj`
  w tym samym pliku (HAR-179: HAR-172 #10, #82, HAR-89 #22 „występuje 3 razy”). Wiersz celuje w źródło
  (`src/cockpit/api/schematy.py`), nie w plik generowany, gdy test oblewa także po mutacji źródła.

Co sprawdzić w review: każdy z powyższych kształtów bez testu, który oblewa po usunięciu elementu = **-25**.

---

## Plik `.claude/rules/testy-podprocesow.md`

### Testy uruchamiające procesy

Źródło: retro sprintu M1.4 — HAR-35 (testy heartbeatu dwa razy zabiły runner CI pod
mutmut), HAR-50 (podproces zasiewu spoza korzenia repo psuł job `mutation`), HAR-47 +
HAR-142 (`os.killpg` w testach obok strażnika — czerwony `main`).

#### Reguła

- Proces w teście startuje przez `tests/procesy.py` (`uruchom_proces`,
  `uruchom_proces_sync`) i kończy przez `zakoncz_proces` — nowa grupa procesów, sprzątanie
  tylko własnej grupy, twarde limity czasu. Bez `os.kill`/`os.killpg` w testach (pilnuje
  tego `test_procesy_testowe`).
- Podproces, który importuje kod projektu (`python -m runner…`, `tools/*.py`), startuje z
  `cwd` = korzeń repozytorium (katalog z `pyproject.toml`, liczony względem pliku testu).
  Pod mutmut kod projektu jest podmieniony w `mutants/`, a proces potomny spoza korzenia
  nie znajduje konfiguracji i pada (`FileNotFoundError: Could not figure out where the code
  to mutate is`).
- Każde czekanie w teście (`wait`, `wait_for`, kolejka, zdarzenie) ma limit czasu — mutant
  bez limitu albo bez `stop()` nie może zawiesić przebiegu.

#### Pułapka

Lokalnie `pytest` przechodzi: problem wychodzi dopiero pod mutmut albo w CI (inna grupa
procesów, inny `cwd`, 2 rdzenie), czasem po godzinie, jako „runner lost communication”.

#### Co sprawdzić w review

- Proces w teście uruchomiony bez `tests/procesy.py` albo podproces z kodem projektu bez
  `cwd` = korzeń repo = **-25**.
- Czekanie bez limitu czasu w teście = **-10**.

---

## Plik `.claude/rules/wyscig-sprawdz-zapisz.md`

### Warunek i zapis w jednej transakcji

Źródło: retro sprintu M1.3 — HAR-37 (`step_finished` nadpisuje `aborted` w
`zapisz_zdarzenie`), HAR-38 (liczenie właścicieli i zapis nieatomowe: zespół może
zostać bez właściciela).

#### Reguła

Gdy zapis zależy od stanu przeczytanego wcześniej („jeśli to nie ostatni właściciel,
odbierz rolę”, „jeśli run nie jest przerwany, ustaw sukces”), sprawdzenie i zapis są
**atomowe**:

- w jednej transakcji z blokadą czytanych wierszy (`SELECT … FOR UPDATE` /
  `with_for_update()`), albo
- jako warunek w samym zapisie (`UPDATE … WHERE status = 'running'` i sprawdzenie
  liczby zmienionych wierszy), albo
- jako ograniczenie w bazie (unikalność, `CHECK`), którego naruszenie jest
  tłumaczone na błąd dziedziny (zob. [granica-bledow](granica-bledow.md), wąski `except`).

#### Pułapka

Na `READ COMMITTED` (domyślny w PostgreSQL) dwa równoległe żądania czytają ten sam stan,
oba przechodzą sprawdzenie i oba zapisują. Testy na SQLite w jednym procesie tego nie
pokażą — wyścig wychodzi dopiero pod ruchem.

#### Co sprawdzić w review

- Odczyt stanu, decyzja w Pythonie i zapis zależny od tej decyzji bez blokady,
  warunku w zapisie albo ograniczenia w bazie = **-25**.
- Test reguły wykonuje dwa przeplecione zapisy (dwie sesje) i sprawdza w danych, że
  niezmiennik przetrwał (zob. [asercja-na-danych](asercja-na-danych.md)).
