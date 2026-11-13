# Asercja sprawdza skutek w danych

Test, który twierdzi, że coś powstało, zmieniło się albo nie powstało, czyta dane (baza, plik
w katalogu runu). Test reguły dziedzinowej uruchamia produkcyjny kod — prawdziwą aplikację
i punkt wejścia, nie kopię logiki ani osobną instancję FastAPI.
WHY: kod HTTP, `page.url` i stan UI wynikają z trasy; run może powstać mimo przekierowania,
a runner innego zespołu zostać wybrany mimo `200` (HAR-2, HAR-3).

## Co sprawdzić w review

- Asercja „nie powstał X” / „wybrano Y” oparta wyłącznie na odpowiedzi lub UI = **-25**.
- Test reguły wywołuje lokalną kopię logiki zamiast kodu produkcyjnego = **-25**.
