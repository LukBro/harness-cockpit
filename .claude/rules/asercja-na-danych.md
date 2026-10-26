# Asercja sprawdza skutek w danych

Źródło: retro sprintu M1.0 — HAR-2, HAR-3.

## Reguła

Test, który twierdzi, że coś **powstało, zmieniło się albo nie powstało** w danych,
czyta dane (baza, plik w katalogu runu) i sprawdza ten skutek. Test nazwany wynikiem
reguły dziedzinowej uruchamia produkcyjny kod tej reguły — prawdziwą aplikację,
prawdziwy punkt wejścia — a nie jej kopię albo osobną instancję FastAPI.

## Pułapka

Kod odpowiedzi HTTP, `page.url` albo stan interfejsu wynikają wprost z trasy i nie
dowodzą niczego o bazie: run może powstać mimo przekierowania, a runner innego
zespołu może zostać wybrany mimo `200`.

## Co sprawdzić w review

- Asercja „nie powstał X” / „wybrano Y” opiera się wyłącznie na odpowiedzi lub UI
  = **-25**.
- Test reguły wywołuje lokalną kopię logiki zamiast kodu produkcyjnego = **-25**.
