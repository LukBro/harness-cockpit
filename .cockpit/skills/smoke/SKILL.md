---
id: smoke
name: Próba dymna
role: gatherer
produces:
  - smoke.md
needs: [smoke]
---
# Próba dymna

Jedno uruchomienie, które sprawdza, że całość wstaje. Wynik to `TAK` albo `NIE`.

## Kroki

1. Uruchom raz capability `smoke`. Wymaga zmiennej `COCKPIT_DATABASE_URL` i kasuje dane bazy, więc użyj wyłącznie bazy testowej. Bez tej zmiennej nie uruchamiaj capability, wynik to `NIE`.
2. Zapisz komendę, kod wyjścia (0 albo 1) i końcówkę wyjścia. Wynik drukuje na końcu `WYNIK: szkielet dziala na wylot` albo `BLAD`.
3. Nie ponawiaj uruchomienia do skutku; jedno uruchomienie, jeden wynik.

## Wynik: `smoke.md`

Pierwsza linia to `TAK` albo `NIE`. Pod nią dowód: komenda, kod wyjścia, końcówka wyjścia.

`TAK` tylko gdy kod wyjścia to 0 i w wyjściu jest linia `WYNIK: szkielet dziala na wylot`. Pusty wynik, brak linii `WYNIK`, nieuruchomione capability albo `BLAD` to `NIE`, nigdy `TAK`.

## Granice

- Nie zmieniaj plików repozytorium; artefakt `smoke.md` trafia do katalogu runu.
- Nie commituj i nie pushuj.
