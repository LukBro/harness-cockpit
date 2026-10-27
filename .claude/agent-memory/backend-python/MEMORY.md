# backend-python - pamięć roli

## Reguly z review krytyka

- HAR-13: Każde nowe wejście sieciowe lub konfiguracyjne skryptu (połączenie z bazą, URL) zamienia błędy biblioteki na komunikat dziedziny i nie zostawia użytkownika z surowym tracebackiem.
- HAR-13: Każde nowe wejście sieciowe lub konfiguracyjne skryptu zamienia na komunikat dziedziny wszystkie wyjątki, które parser tego wejścia może rzucić na złych danych, nie tylko klasy z hierarchii biblioteki.
- HAR-133: Każde wejście konfiguracyjne zamienia na błąd dziedziny wszystkie wyjątki, które może rzucić na złych danych, także błędy struktury przy przechodzeniu po zagnieżdżonych kluczach (tablica zamiast wartości, wartość zamiast tablicy), a nie tylko wyjątki samego parsera.
- HAR-42: Każde wywołanie, które czyta dysk (także przez pomocniczą funkcję z innego modułu), jest objęte wąskim tłumaczeniem `OSError` na wynik dziedziny funkcji, która taki wynik obiecuje.
- HAR-42 (powtórzona): Każde wywołanie, które dotyka dysku (także `is_file`/`exists`/`stat`, nie tylko `read`), jest w funkcji obiecującej wynik dziedziny objęte tłumaczeniem `OSError` — przy poprawce przejrzyj wszystkie odczyty dysku w funkcji, nie tylko te wskazane w review.
