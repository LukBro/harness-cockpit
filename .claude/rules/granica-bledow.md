# Błędy na granicy i sprzątanie zasobów

Źródło: retro sprintu M1.0 — HAR-2, HAR-5, HAR-7.

## Reguła

- Każde wejście z dysku, sieci albo YAML tłumaczy błędy wejścia na błąd dziedziny
  modułu: `OSError` (brak pliku, prawa, katalog zamiast pliku), `UnicodeDecodeError`,
  `YAMLError` oraz błędy struktury (`TypeError`, `RecursionError` przy złośliwym
  YAML). Wyjątek biblioteki nie ucieka z funkcji, której ticket obiecuje błąd dziedziny.
- Zasób zajęty na czas połączenia (subskrypcja szyny, licznik połączeń runnera,
  proces potomny) jest zajmowany **wewnątrz** bloku, którego `finally` go zwalnia —
  nie przed `try` i nie przed startem generatora strumienia.
- Klient strumienia obsługuje zerwanie (`onerror` w EventSource) i po wznowieniu
  dociąga historię.

## Pułapka

`read_bytes()` przed `try` albo `subscribe()` poza generatorem SSE wygląda
poprawnie w szczęśliwej ścieżce; wyciek widać dopiero, gdy klient odpadnie, zanim
strumień ruszy — runner zostaje „online” na zawsze, kolejka nigdy nie jest zwolniona.

## Co sprawdzić w review

- Nowe wejście z dysku/sieci/YAML bez tłumaczenia powyższych wyjątków = **-25**.
- Zajęcie zasobu poza blokiem z `finally`, który go zwalnia = **-25**.
- Każda gałąź tłumaczenia wyjątku ma test, który w nią trafia
  (zob. [test-odroznia-regule](test-odroznia-regule.md)).

## Wąski `except` (retro M1.2)

Źródło: HAR-13 (`except SQLAlchemyError` na cały przelot), HAR-25 (`except IntegrityError`
przy kolizji numeru zdarzenia).

- `except` łapie tylko wyjątek, który kod faktycznie obsługuje, i tylko wokół instrukcji,
  która go rzuca. Szeroka klasa (`SQLAlchemyError`, `IntegrityError`, `Exception`) wokół
  wielu kroków przypisuje błędowi złą przyczynę — błąd logiki staje się „błędem połączenia”,
  a naruszenie innego ograniczenia „kolizją numeru” i jest ponawiane.
- Gdy biblioteka nie rozróżnia przyczyn typem, rozróżnij je po danych wyjątku (nazwa
  ograniczenia, kod błędu) i resztę przepuść dalej.
- W review: szeroki `except` obejmujący więcej niż obsługiwaną przyczynę = **-25**.

## Regresja (retro M1.3)

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

## Regresja (retro M1.5)

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

## Regresja (retro M1.6)

Źródło: HAR-67 (-25), wcześniej HAR-13 i HAR-133 (pamięć backend-python).

- **Wyjątki spoza hierarchii biblioteki** — parser YAML zamienia daty i liczby na typy
  Pythona i na złych danych rzuca `ValueError`, `OverflowError`, `TypeError`, nie `YAMLError`.
  Tłumaczysz każdy typ, który faktycznie leci na złych danych (HAR-67, `skills.py`).
- **Brak klucza w odpowiedzi runnera** (`KeyError`) to błąd dziedziny (409 i stan bez zmian),
  nie 500 (HAR-67, `katalog.py`).

Co sprawdzić w review: każdy z powyższych kształtów = **-25**; każdy typ w krotce `except`
ma test (zob. [test-odroznia-regule](test-odroznia-regule.md)).

## Regresja (retro M1.7)

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

## Regresja (retro M1.8)

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

## Regresja (retro M1.10)

Źródło: HAR-170 (backend-python -10 utrzymane): limity nowych pól `EventIn` (`verdict` ≤ 200, `artefakty` ≤ 100×255,
`kotwice` ≤ 1000) nie są sprawdzane w runnerze przed wysłaniem, więc 4xx z kokpitu oznacza utracone zdarzenie kroku.
To ten sam kształt co HAR-66 (M1.5, „Długość względem kolumny także od runnera”) — reguła istnieje i nadal jest łamana.

- **Nowe pole w `EventIn` z limitem** dostaje w tym samym PR walidację po stronie runnera (przed buforem i wysłaniem)
  oraz test, który wysyła wartość o jeden ponad limit i sprawdza, że runner zgłasza błąd dziedziny zamiast ponawiać.
- Egzekucja twardsza niż tekst: test porównujący limity `EventIn` z walidacją runnera — ticket z retro M1.10.

Co sprawdzić w review: nowe pole `EventIn` z limitem bez walidacji runnera = **-25**.
