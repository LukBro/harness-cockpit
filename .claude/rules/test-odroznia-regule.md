# Test odróżnia regułę od jej braku

Źródło: retro sprintu M1.0 — wzorzec w 7 z 9 ticketów (HAR-2, 3, 5, 6, 7, 8, 9),
krytyk dwa razy oznaczył go jako „korekta WRACA” (HAR-6, HAR-9).

## Reguła

Każdy element kodu, który zmienia wynik reguły, ma test na wejściu, na którym
**tylko ten element** decyduje o wyniku. Usunięcie elementu musi oblać co najmniej
jeden test. Dotyczy to w szczególności:

- **gałęzi** kodu chroniącego niezmiennik albo tłumaczącej wyjątek,
- **elementu zbioru lub tabeli** rozszerzającego wykrywanie (znak separatora,
  opcja, klucz) — przypadek, w którym bez tego elementu nic innego by nie wykryło,
- **trybu albo źródła** (globalny/lokalny, plik/rejestr) — wejście dające inny
  wynik w każdym trybie,
- **zakresu** (przodkowie w `needs`, kroki cyklu) — poza zakresem istnieje krok,
  który dałby inny wynik, gdyby reguła była globalna,
- **złego typu** — wartość odrzucana tylko przez implementowaną regułę
  (napis zamiast liczby, `yes` zamiast bool), nie przez każdą implementację,
- **podmiany wartości z pliku** zależnie od stanu rejestru — wartość w pliku
  istnieje i zmieniłaby wynik,
- **filtra `tenant_id`** — w bazie są co najmniej dwa zespoły, rekord innego
  zespołu nie zostaje wybrany.

## Pułapka

Test, który przechodzi zarówno z regułą, jak i bez niej, wygląda na pokrycie,
ale nie chroni niczego: mutant usuwający gałąź lub element zbioru przeżywa.
Pojedyncze reguły z review lądowały w `.claude/agent-memory/test-writer/MEMORY.md`
i i tak wracały w następnym tickecie — to jest jedna zasada, nie jedenaście.

## Co sprawdzić w review

- Dla każdej nowej gałęzi, elementu zbioru, trybu i zakresu w diffie: wskaż test,
  który oblewa po jej usunięciu. Brak takiego testu = **-25**.
- Przeżywający mutant w zmienionym pliku (mutation-check) = **-25**.
