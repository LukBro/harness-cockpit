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

- Kod wyjścia **0** — wszystkie mutanty zmienionych funkcji zabite (albo brak zmienionych
  funkcji) i brak zmian poza zasięgiem mutmuta.
- Kod wyjścia **1** — są przeżywające mutanty; dopisz test na wejściu, które je zabija.
- Kod wyjścia **2** — błąd narzędzia (nieznana baza, zły `pyproject.toml`, błąd składni,
  błąd mutmuta); to nie jest wynik „czysto”.
- Kod wyjścia **3** (HAR-144) — zmiany poza zasięgiem mutmuta: stdout zaczyna się od
  `bez mutantów — sprawdź ręcznie`, dalej wpisy `plik:linia`, po jednym w linii. To nie
  jest „czysto”: dopisz ręczny mutant i test. Zakres: pliki `.py` pod `src/` spoza
  `source_paths` oraz w `source_paths` linie kodu poza funkcjami i metodami (bez pustych
  linii, komentarzy, docstringów i importów) oraz funkcje i metody z dekoratorem innym niż
  pojedynczy `staticmethod`/`classmethod` (mutmut 3.8.0 ich nie mutuje); `tests/`, `web/`, `tools/` i `*.md` są
  poza zakresem. Priorytet kodów: 2 > 1 > 3 > 0; przy kodzie 1 lista bez mutantów idzie
  na stderr. W CI krok jest zielony, a lista trafia do sekcji podsumowania „bez mutantów —
  sprawdź ręcznie”.
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

## Regresja (retro M1.2.1)

Źródło: HAR-133 (dwa razy -25 w kolejnych rundach), HAR-138 (uwaga krytyka bez
odjęcia). Reguła i jej egzekucja istnieją, a wzorzec wraca w nowych kształtach:

- **Zaślepka zawsze zwraca sukces** — testy podmieniają funkcję uruchamiającą
  podproces albo czytającą wejście, więc gałęzie błędu (kod wyjścia, `OSError`,
  kanał stderr) nie mają testu na **prawdziwym** kodzie tej funkcji. Każda gałąź
  tłumacząca błąd ma test, który wywołuje prawdziwą funkcję na wejściu wyzwalającym
  tę gałąź.
- **Asercja przechodzi także na starym kodzie** — np. „komunikatu X nie ma w stderr”,
  gdy stary kod też go nie wypisywał z innego powodu. Przed oddaniem sprawdź, że
  asercja oblewa po cofnięciu zmiany.

Egzekucja dodatkowa: krok CI z `tools/mutanty_zmian.py` na każdym PR (ticket z retro
M1.2.1) — przeżywające mutanty zmienionych funkcji widać w raporcie PR.

## Regresja (retro M1.3)

Źródło: HAR-26 („korekta WRACA” za HAR-29), HAR-35 („korekta WRACA” za HAR-6 i HAR-133),
HAR-141, uwagi bez odjęcia w HAR-34, HAR-36, HAR-38. Nowe kształty:

- **Test błędu sprawdza dokładny typ wyjątku** — `pytest.raises(Exception)` przyjmuje też
  wyjątek z innej gałęzi i nie chroni tej, którą test opisuje (HAR-26).
- **Gałąź przepuszczająca wyjątek dalej** (`raise`, re-raise po anulowaniu) ma test, w
  którym tylko ona decyduje, czy wołający dostanie wyjątek (HAR-35).
- **Test na produkcyjnej ścieżce wywołania** — gałąź chroniąca niezmiennik ma test, w
  którym żaden inny mechanizm (np. pętla sprzątająca w `stop()`) nie daje tego samego
  skutku (HAR-141).
- **Wartość, nie obecność** — test tabeli sprawdza wartość wpisu (np. `Zabronione → 403`),
  nie tylko to, że wpis istnieje (HAR-38).

## Regresja (retro M1.4)

Źródło: 13 z 14 ticketów M1.4/M1.5 dostało -25 z tej reguły, w 10 „korekta WRACA”
(HAR-22, 44, 46, 47, 48, 49, 50, 51, 52, 53, 141, 142, 65). Nowe kształty:

- **Ograniczenia pól i wartości domyślne** — `Field(max_length=…, ge=…)`, `default=` w
  modelu i elementy stałych zbiorów (`METODY_BEZPIECZNE`) mają test, w którym tylko ta
  wartość decyduje (HAR-44, 48, 51). `tools/mutanty_zmian.py` takich mutantów nie generuje, ale zgłasza je kodem 3 (HAR-144) —
  „brak zmienionych funkcji” to brak dowodu, nie „czysto”: dopisz ręczny mutant i test.
- **Każdy warunek zapytania** wybierającego istniejący rekord (adres, użytkownik, zespół)
  ma w danych rekord odrzucany tylko przez ten warunek (HAR-50) — uogólnienie punktu o
  filtrze `tenant_id`.
- **Warunek typu i „użyj wartości dosłownie”** (`isinstance`, parametry tylko dla pierwszej
  strony) ma wejście, które wersja bez warunku przetwarza inaczej (HAR-53, HAR-65).
- **Gałąź sprzątania w `finally`** ma test poza ścieżką `stop()`, który ją wyzwala (HAR-49).
- **Równoważność mutanta** uzasadniasz wskazaniem, że żadne wejście nie odróżnia wersji;
  „test przechodzi” albo „to tylko kosmetyka” nie jest uzasadnieniem (HAR-52).

## Egzekucja ręczna tam, gdzie mutmut nie sięga (retro M1.5)

Źródło: „korekta WRACA” w 7 ticketach M1.5 (HAR-49, 59, 61, 62, 65, 142, 143) mimo
sekcji Regresja z M1.1–M1.4. Kolejny akapit nie pomoże — potrzebny dowód w raporcie.

Gdy zmiana obejmuje miejsce, którego mutmut nie mutuje — front (`web/`), testy e2e,
krotki w `except`, elementy stałych zbiorów i wyrażeń regularnych, hooki pytesta
(`tryfirst`, `skipif`), pliki spoza `source_paths` (kod 3 z `tools/mutanty_zmian.py`,
HAR-144) — raport test-writera zawiera tabelę **ręcznych mutantów**:
`element | mutacja | test, który oblewa`. Po jednym wierszu na każdy zmieniony element.

- Brak tabeli przy zmianie poza zasięgiem mutmuta = **-25**.
- Wiersz z testem, który po mutacji nadal przechodzi (krytyk sprawdza co najmniej jeden
  wiersz, wprowadzając mutację lokalnie) = **-25**.
