# Test odróżnia regułę od jej braku

Każdy element kodu, który zmienia wynik reguły, ma test na wejściu, na którym **tylko ten
element** decyduje o wyniku: usunięcie elementu oblewa co najmniej jeden test.
WHY: test, który przechodzi z regułą i bez niej, wygląda na pokrycie, a nie chroni niczego.
To najczęstsze odjęcie w projekcie (11 z 20 ostatnich ticketów).

## Mutanty to raport, nie bramka

Decyzja właściciela z 2026-11-24: w ticketach z 12–24.11 bramki mutacyjne nie znalazły ani jednego
prawdziwego błędu (A=0 przy 13 ustaleniach B i 9 C), a kosztowały 34 h CI, 9 PR-ów naprawczych
i ok. 47 h opóźnienia merge. Prawdziwe błędy znalazł krytyk i strażnik.

- CI (job `mutation`) raportuje mutanty zmienionych funkcji jako ostrzeżenie; czerwony jest tylko
  przy błędzie narzędzia (brak raportu to nie „czysto”).
- W sesji ticketu nie uruchamiasz `tools/mutanty_zmian.py`, `tools/mutanty_reczne.py` ani
  `tools/mutanty_uzupelniajace.py` i nie dopisujesz tabel `tests/mutanty_reczne/`.
- Regułę z tytułu sprawdza krytyk czytając diff: dla elementu, który zmienia wynik reguły, wskazuje test,
  który by oblał po jego usunięciu.

## Kształty, które łatwo zostawić bez testu

Usunięte instrukcje, elementy krotek i zbiorów, przestawione zapisy. Katalog takich kształtów, po jednym przykładzie:
`.ai/qa/ksztalty-mutantow.md` — test-writer i krytyk czytają go przy każdym tickecie z kodem.

## Co sprawdzić w review

- Element z diffu (gałąź, element zbioru/krotki `except`/wyrażenia regularnego, tryb, zakres,
  kształt z katalogu) bez testu, który oblewa po jego usunięciu = **-25**.
- Test z limitem czasu w nazwie, niezależny od mechanizmu, który ten limit realizuje = **-20**.
