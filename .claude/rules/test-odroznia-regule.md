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

## Egzekucja

Źródło: HAR-133. Przed oddaniem pracy do review test-writer albo Team Manager
uruchamia z korzenia repozytorium:

```
python tools/mutanty_zmian.py            # baza: origin/main
python tools/mutanty_zmian.py --baza <ref>
```

Skrypt mutuje wyłącznie funkcje i metody zmienione względem bazy (także
niescommitowane i nowe pliki w `source_paths` z `pyproject.toml`) i wypisuje
przeżywające mutanty, po jednym w linii.

- Kod wyjścia **0** — brak przeżywających mutantów albo brak zmienionych funkcji.
- Kod wyjścia **1** — są przeżywające mutanty; dopisz test na wejściu, które je zabija.
- Kod wyjścia **2** — błąd narzędzia (nieznana baza, zły `pyproject.toml`, błąd składni,
  błąd mutmuta); to nie jest wynik „czysto”.
- Przeżywający mutant w zmienionej funkcji bez uzasadnienia równoważności = **-25**
  w review.
- Skrypt nie czyści cache `mutants/`. Po zmianie samych testów (bez zmiany kodu
  źródłowego) wynik może pochodzić ze starego przebiegu (strona wiki `constitution`,
  HAR-6) — wtedy uruchom skrypt w świeżym klonie repozytorium w nowym katalogu
  tymczasowym (`git clone <repo> <katalog-tymczasowy>/mutanty-<n>`, z dokopiowanymi
  niescommitowanymi zmianami). Nie usuwaj `mutants/` w sesji agenta; usunięcie jest
  dozwolone tylko jako ręczna akcja człowieka.
- Pełny przebieg mutmuta wymaga, by `[tool.mutmut] also_copy` obejmowało wszystko,
  czego potrzebują testy (np. `alembic/`, `alembic.ini`); inaczej skrypt kończy się
  kodem 2 na fazie statystyk mutmuta.

## Regresja

Źródło: retro sprintu M1.1. Reguła istnieje i nadal jest łamana — HAR-10
(krytyk: „korekta WRACA — wzorzec HAR-6”), wcześniej HAR-2, 3, 5, 6, 7, 8, 9.

Nowy kształt tego samego wzorca — **zawężenie wzorca albo klasy znaków**
(`[A-Za-z0-9_]` zamiast `\w`, kotwica, węższa alternatywa): test ma wejście,
które **tylko zawężenie** odrzuca. Wejście parsowane tak samo przez wersję
szerszą nie chroni zawężenia przed cofnięciem (HAR-10: `xǁAǁBǁc__mutmut_1`
zabija mutanta `\w`, bo `\w` dopasowuje `ǁ`).

Tekst reguły nie wystarcza — egzekwuje ją `tools/mutanty_zmian.py`
(zob. [Egzekucja](#egzekucja)): mutanty zmienionych funkcji przed werdyktem krytyka.
