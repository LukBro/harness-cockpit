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
