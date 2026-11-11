# Kształty, których mutmut nie mutuje

Uzupełnienie `.claude/rules/test-odroznia-regule.md`. Czyta test-writer przed oddaniem tabeli
i krytyk przed oceną testów. Każdy kształt potrzebuje testu, w którym **tylko ten element**
decyduje o wyniku, i — gdy mutmut go nie generuje — wiersza `[[mutant]]` w tabeli ticketu.
Po jednym przykładzie na kształt; ticket w nawiasie to źródło.

## Kod Pythona

| Kształt | Wejście, które go chroni | Przykład |
| -- | -- | -- |
| Element krotki `except` | wejście wyzwalające tylko ten typ; podklasa innego elementu → usuń albo `[[rownowazny]]` | `except (BladDziedziny, OSError)` — test z `OSError` (HAR-173) |
| Instrukcja w `finally`, zwolnienie zasobu | test obserwuje zwolnienie (stan deskryptora, anulowane zadanie) poza ścieżką `stop()` | `wejscie.close()` (HAR-174) |
| Kolejność zapisów nakazana w ustaleniach | zamiana dwóch zapisów oblewa test na danych | artefakt → checkpoint → zdarzenie (HAR-173) |
| Element zbioru, stałej mapy, tabeli wzorców | lista przypadków testu niezależna od listy w kodzie; każdy wpis mapy ma osobny wiersz | `{running, waiting}` w agregacji (HAR-178) |
| Wyrażenie regularne w stałej modułu | po jednym wejściu na klasę znaków, wykluczony znak, kotwicę, końcowy literał; zawężenie ma wejście, które tylko ono odrzuca | `[^\[\]:]` i `\]` w `KOTWICA` (HAR-176) |
| Agregacja po wierszach | co najmniej dwa wiersze tego samego klucza w różnych stanach | dwóch agentów jednego kroku (HAR-178) |
| Wywołanie usuwalne bez zmiany typu | wejście, na którym wynik bez wywołania jest inny | `sorted(...)` przy przeplecionych krokach (HAR-170), `.strip()` (HAR-104) |
| Strażnik, wartość zastępcza, człon warunku | wartość odrzucana tylko przez ten człon, także puste wejście; także strażnik dodany w poprawce po review (`isfinite`, `is not None`) — poprawka podlega tej samej regule co kod pierwotny | `isinstance(proby, bool)` obok `int` → `"proby": true` (HAR-172, HAR-86, HAR-150) |
| Strażnik za strażnikiem z tym samym kodem wyjścia | asercja na fragmencie komunikatu, który powstaje tylko w tym strażniku | `if not tabele:` przed sprawdzeniem zera wierszy, oba kod 2 (HAR-225) |
| Warunek zapytania | rekord odrzucany tylko przez ten warunek: drugi zespół dla `tenant_id`, `ORDER BY` kolejności blokad; `with_for_update()` — kompilacja dialektem PostgreSQL | (HAR-50, HAR-206) |
| Pole i ograniczenie modelu (kod 3) | `Field(max_length=…)`, `default=`, pole dataclass, argument serwisu w trasie z dekoratorem | ciało trasy FastAPI (HAR-172) |
| Wywołanie istniejącego strażnika w nowej funkcji | test, w którym tylko to wywołanie daje kod błędu | `_wymagaj_dokladnie_raz(...)` (HAR-228) |
| Komunikat i treść wyjątku | asercja na cały napis — mutant `XX…XX` zachowuje każde słowo | (HAR-179) |
| Gałąź błędu | wywołuje prawdziwą funkcję, nie zaślepkę; `pytest.raises(DokladnyTyp)`; re-raise ma test, w którym tylko on decyduje | (HAR-26, HAR-35) |
| Wartość, nie obecność | sprawdza wartość wpisu i wartość, którą błąd mógłby zostawić | `Zabronione → 403`; `tabele=1` po błędzie (HAR-38, HAR-221) |
| Tryb, źródło, zakres, strefa czasowa | wejście dające inny wynik w każdym trybie; naive→UTC z `TZ` + `time.tzset`; dziedziczenie: u rodzica inna wartość niż oczekiwana u potomka, „nieustawiona” ≠ „pusta” | (HAR-68, HAR-85) |
| Licznik limitu | wartość, przy której `+1` i `+2` dają różny wynik | (HAR-151) |
| Kolejność i filtr rodzaju zbudowane w Pythonie | sortowanie: kolejność wstawiania różna od kolejności wyniku (powtórzony klucz przepleciony innym); filtr rodzaju: zdarzenie odrzuconego rodzaju z tymi samymi danymi | `kind == "step_finished"` — test ze `step_failed` (HAR-170) |
| Dopasowanie jednoznaczne | asercja na zdaniu, które istnieje tylko w regule; pomocnik zawężający do zakresu ma test z elementem spoza zakresu, który bez zawężenia dałby inny wynik | asercja na treści briefu (HAR-15, HAR-74) |
| „Mutant tylko wyścigowo” to nie równoważność | wiersz z testem, który wymusza kolejność zdarzeń, albo usunięcie warunku; nagłówek TOML bez wskazania, że żadne wejście nie odróżnia wersji, nie zwalnia z wiersza | `odpowiedzi_get >= 2` → `>= 1` (HAR-232) |

## Skrypty CI i tabele

- Każda gałąź `exit 1` w skrypcie CI ma wiersz ręcznego mutanta usuwający `exit 1`: komunikat
  `::error::` bez wyjścia z kodem 1 to zielony krok (HAR-233).
- `szukaj` celuje w źródło (`src/cockpit/api/schematy.py`), nie w plik generowany (`web/openapi.json`):
  po regeneracji kontraktu treść generowana przestaje być jednoznaczna, a kod skopiowany do nowej
  funkcji podwaja krótkie `szukaj` w tym samym pliku (HAR-170, HAR-179).

## Front (`web/`) i e2e

- Każdy element diffu `web/` ma test e2e i wiersz tabeli: `trim` osobno w warunku i w wartości,
  czyszczenie stanu przy zmianie pola, na starcie akcji i po ponownym zalogowaniu (HAR-70, HAR-71).
- Stan „przed pierwszą odpowiedzią”: wstrzymana odpowiedź i asercja przed jej nadejściem; gałąź
  `!r.ok` dostaje poprawny JSON; test zaczyna z innego stanu niż docelowy (HAR-77).
- Test z limitem czasu w nazwie mierzy od zdarzenia i zależy od mechanizmu limitu (HAR-68).
- Przeżywający mutant bywa kodem martwym (`onClick` przy `disabled`) — usuń go zamiast testować.
  Pusty `zamien`, który zostawia nieużytą zmienną TS, psuje build (TS6133) — użyj `void x;` (HAR-173).
