# Błędy na granicy i sprzątanie zasobów

Wejście z dysku, sieci, YAML/JSON, zmiennej środowiskowej i od runnera tłumaczy każdy błąd,
który faktycznie leci na złych danych, na błąd dziedziny modułu. Wołający obsługuje ten błąd
tak, że run nigdy nie zostaje bez `step_failed` i `run_finished`.
WHY: wyjątek biblioteki, który ucieka, daje 500 zamiast 409/422 albo run wiszący w `running`;
to drugie co do częstości odjęcie (5 z 20 ostatnich ticketów, w tym „korekta WRACA”).

## Co tłumaczysz

- `OSError` (brak pliku, prawa, katalog zamiast pliku, pełny dysk — także `mkdtemp`,
  `is_file()`, `stat()`), `UnicodeDecodeError`, `YAMLError`, `ValueError`/`OverflowError`/
  `TypeError` z konwersji typów, `RecursionError`, `KeyError` przy braku klucza w odpowiedzi.
  Po uwadze o jednym odczycie przejrzyj **wszystkie** dotknięcia dysku w funkcji i w tym, co woła.
- `Path.glob`/`rglob` i `os.walk` bez `onerror` połykają `OSError` — pusty wynik z nieczytelnego
  katalogu to błąd, nie „nic nie ma”.
- Wartość z zewnątrz, od której zależy wynik (`wynik`, `kind`, `issues`), sprawdzasz co do typu
  i zamkniętej listy; nieznana wartość to błąd, nie domyślne „sukces/pusto” (`{}` ≠ „brak ticketów”).
- Pole zapisywane do `String(n)` ma `max_length` w modelu wejścia API **i** jest walidowane
  w runnerze przed wysłaniem — inaczej 500 i bezkońcowe ponawianie z bufora.
- `int(os.environ[...])` tłumaczysz z nazwą zmiennej, zanim proces wystartuje; segment URL z danych
  przechodzi przez `quote(…, safe="")`; błąd strumienia runnera kończy proces komunikatem, nie tracebackiem.

## Wąski `except` i obsługa u wołającego

- `except` łapie tylko przyczynę, którą kod obsługuje, i tylko wokół instrukcji, która ją rzuca.
  Gdy biblioteka nie rozróżnia przyczyn typem, rozróżnij po danych wyjątku i resztę przepuść.
  Gdy ma osobny typ (`TargetClosedError`), łap ten typ, nie klasę bazową.
- Nowy wyjątek dziedziny ma w tym samym PR gałąź u wołającego, która zamienia go na zdarzenie
  końca kroku albo runu; pętla `asyncio.wait` zamienia każdy wyjątek zadania poza anulowaniem.
- Wejście czytane ponownie przy wznowieniu (rejestr skilli, workflow, checkpoint) przechodzi tę samą
  walidację co przy starcie, **przed** pierwszym nieodwracalnym zapisem stanu (HAR-173).
- Strażnik albo hook, który pada przy starcie, odmawia (fail-closed).

## Zasoby

Zasób zajęty na czas połączenia (subskrypcja szyny, licznik połączeń, proces potomny) zajmujesz
**wewnątrz** bloku, którego `finally` go zwalnia. Klient strumienia obsługuje zerwanie i po
wznowieniu dociąga historię.

## Co sprawdzić w review

- Nowe wejście z granicy bez tłumaczenia powyższych wyjątków albo bez kontroli typu i listy = **-25**.
- Zajęcie zasobu poza blokiem z `finally`, który go zwalnia = **-25**.
- Szeroki `except` obejmujący więcej niż obsługiwaną przyczynę = **-25**.
- Każdy inny kształt z tej strony (obsługa u wołającego, walidacja przy wznowieniu, długość
  względem kolumny, fail-closed) = **-25**.
- Każdy typ w krotce `except` i każda gałąź tłumaczenia ma test na prawdziwej funkcji
  (zob. [test-odroznia-regule](test-odroznia-regule.md)).
