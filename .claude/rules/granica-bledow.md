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
