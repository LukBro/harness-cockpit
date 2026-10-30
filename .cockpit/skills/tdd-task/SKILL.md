---
id: tdd-task
name: Zadanie metodą test-najpierw
role: author
consumes:
  - plan.md
produces:
  - tdd.md
---
# Zadanie metodą test-najpierw

Zrealizuj zadanie z `plan.md` tak, żeby test powstał przed kodem.

## Przeczytaj przed pracą

- `CLAUDE.md` w korzeniu repozytorium, sekcja „Testy" (konwencja nazw testów).
- `.ai/qa/AGENTS.md` (zasady QA tego repozytorium).
- `.claude/rules/test-odroznia-regule.md` (test ma oblewać po usunięciu reguły).

Nie kopiuj treści tych plików do testów ani do `tdd.md`; stosuj je.

## Kroki

1. Z `plan.md` wybierz zachowanie do zrealizowania i opisz je nazwą testu w języku dziedziny.
2. Napisz test i uruchom go. Ma oblewać z właściwego powodu (brak zachowania, nie błąd importu ani literówka). Zachowaj wynik.
3. Napisz minimalną implementację, która spełnia ten test, bez dodatków spoza planu.
4. Uruchom test ponownie, potem pozostałe testy zmienionego obszaru. Muszą przechodzić.
5. Powtórz dla kolejnych zachowań z planu.

## Granice

- Nie commituj i nie pushuj; commit to decyzja człowieka.
- Nie zmieniaj plików spoza zakresu planu.

## Wyjście: `tdd.md`

- nazwy dodanych testów i pliki, w których są,
- dowód czerwonego przebiegu przed implementacją (komenda, końcówka wyjścia),
- dowód zielonego przebiegu po implementacji (komenda, końcówka wyjścia),
- lista zmienionych plików.

Brak dowodu czerwonego albo zielonego przebiegu zgłoś jako błąd kroku, nie jako sukces.
