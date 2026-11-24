---
id: smoke
name: Próba dymna
role: gatherer
produces:
  - smoke.md
consumes: [dowod-smoke.json]
verdict: {pass: [TAK], fail: [NIE]}
---
# Próba dymna

Jedno uruchomienie, które sprawdza, że całość wstaje. Wynik to `TAK` albo `NIE`.

Jesteś zbieraczem: masz tylko narzędzia `Read`, `Grep`, `Glob`. Nie uruchamiasz komend.
Silnik uruchamia przed tobą capability `smoke` w osobnym kroku `smoke-check` i zapisuje
dowód `dowod-smoke.json` w katalogu runu. Capability wymaga zmiennej `COCKPIT_DATABASE_URL`
i kasuje dane bazy, więc dopuszczalne jest wyłącznie użycie bazy testowej. Bez tej zmiennej
wynik to `NIE`.

## Kroki

1. Przeczytaj `dowod-smoke.json` z wejść kroku. Dowodem jest tylko plik z `source: engine`.
2. Odczytaj `exit_code` i `stdout_tail`. Wynik drukuje na końcu `WYNIK: szkielet dziala na wylot`
   albo `BLAD`.
3. Nie zgaduj wyniku z kodu i nie ponawiaj uruchomienia; jeden dowód, jeden wynik.

## Wynik: `smoke.md`

Pierwsza linia to `WERDYKT: TAK` albo `WERDYKT: NIE`, dokładnie w tej postaci. Pod nią dowód:
`exit_code` i końcówka wyjścia (`stdout_tail`) z pliku `dowod-smoke.json`.

`TAK` tylko gdy `exit_code` to 0 i w `stdout_tail` jest linia `WYNIK: szkielet dziala na wylot`.
Brak pliku dowodu, `source` inny niż `engine`, brak linii `WYNIK` albo `BLAD` to `NIE`, nigdy `TAK`.

## Granice

- Nie zmieniaj plików repozytorium; całą treść artefaktu `smoke.md` oddaj jako odpowiedź końcową — zapisuje go runner w katalogu runu, poza repozytorium.
- Nie commituj i nie pushuj.
