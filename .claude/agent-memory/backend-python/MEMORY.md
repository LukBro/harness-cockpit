# backend-python - pamięć roli

## Reguly z review krytyka

- HAR-13: Każde nowe wejście sieciowe lub konfiguracyjne skryptu (połączenie z bazą, URL) zamienia błędy biblioteki na komunikat dziedziny i nie zostawia użytkownika z surowym tracebackiem.
- HAR-13: Każde nowe wejście sieciowe lub konfiguracyjne skryptu zamienia na komunikat dziedziny wszystkie wyjątki, które parser tego wejścia może rzucić na złych danych, nie tylko klasy z hierarchii biblioteki.
