---
id: e2e-scenario
name: Scenariusz e2e zdaniami
role: author
consumes:
  - plan.md
produces:
  - scenariusz-e2e.md
---
# Scenariusz e2e zdaniami

Dla zmiany z `plan.md` napisz scenariusz end-to-end i test, który go odtwarza w przeglądarce.

## Kroki

1. Zapisz scenariusz zdaniami użytkownika w pierwszej osobie, np. „otwieram workflow, klikam Uruchom, widzę stan run-u". Jedno zdanie to jeden krok testu.
2. Napisz test w Playwrighcie w `tests/e2e/`, z markerem `e2e`. Zdania scenariusza podaj w komentarzach lub nazwach kroków w tej samej kolejności.
3. Za wzorzec fixture'ów (serwer, baza, przeglądarka) weź `tests/e2e/conftest.py` i istniejące testy w tym katalogu.
4. Nazwa testu opisuje zachowanie w języku dziedziny (zasady w `CLAUDE.md`, sekcja „Testy").
5. Skutek sprawdzaj także w danych (baza, plik runu), nie tylko w stanie strony.
6. Uruchom `pytest -m e2e`. Wymaga PostgreSQL i przeglądarki dla Playwrighta; jeśli ich brakuje, zapisz to jako błąd kroku, nie jako przejście.

## Granice

- Nie commituj i nie pushuj.
- Nie zmieniaj kodu aplikacji; jeśli scenariusz ujawnia błąd, opisz go w wyjściu.

## Wyjście: `scenariusz-e2e.md`

- zdania scenariusza w kolejności,
- ścieżka pliku testu,
- wynik uruchomienia `pytest -m e2e`: komenda, kod wyjścia, końcówka wyjścia.
