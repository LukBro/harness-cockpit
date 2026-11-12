# Test odróżnia regułę od jej braku

Każdy element kodu, który zmienia wynik reguły, ma test na wejściu, na którym **tylko ten
element** decyduje o wyniku: usunięcie elementu oblewa co najmniej jeden test.
WHY: test, który przechodzi z regułą i bez niej, wygląda na pokrycie, a nie chroni niczego.
To najczęstsze odjęcie w projekcie (11 z 20 ostatnich ticketów).

## Bramki — uruchom przed oddaniem pracy, CI powtarza je w jobie `mutation`

| Komenda (z korzenia repo) | Kody wyjścia | Co robisz |
| -- | -- | -- |
| `python tools/mutanty_zmian.py [--baza <ref>]` | 0 czysto · 1 przeżywające mutanty · 2 błąd narzędzia · 3 zmiany poza zasięgiem mutmuta (stdout: `bez mutantów — sprawdź ręcznie`, potem `plik:linia`) | 1: test zabijający mutanta. 3: każda linia dostaje wiersz w tabeli ręcznych mutantów. 2 to nie „czysto”. |
| `python tools/mutanty_reczne.py tests/mutanty_reczne/<ticket>.toml` | 0 zabite · 1 przeżywające · 2 błąd tabeli/narzędzia | Wymagane przy kodzie 3 i przy każdej zmianie w `web/`, `tools/`, `cicd/` (CI: „brak dowodu”). |
| `python tools/mutanty_reczne.py --pokrycie-kod3 <lista> <tabele>` | 0 pokryte · 1 linia bez wiersza · 2 błąd narzędzia | Każda linia kodu 3 ma `[[mutant]]` albo `[[rownowazny]]`. |
| `python tools/mutanty_reczne.py --sprawdz-wszystkie` | 0 sprawdzono > 0 wierszy · 2 zepsuty wiersz, brak tabel albo błąd kolekcji (nigdy 1) | Przeniosłeś kod albo zmieniłeś test z cudzej tabeli — popraw tę tabelę w tym samym PR. |

Wiersz tabeli:

```toml
[[mutant]]
plik = "src/cockpit/services/runs.py"   # względem korzenia
szukaj = "if kod == 1:"                  # występuje w pliku dokładnie raz
zamien = "if kod != 1:"
test = "tests/test_runs.py::test_run_zakonczony_sukcesem_ma_stan_do_odbioru_nie_gotowe"

[[rownowazny]]
plik = "src/runner/checkpoint.py"
linia_tekst = "..."
uzasadnienie = "żadne wejście nie odróżnia wersji, bo ..."
```

- Równoważność uzasadniasz wejściem, którego nie ma, nie tym, że „inny element już to łapie”.
  W kodzie: `# pragma: no mutate` z komentarzem powodu w linii wyżej i wiersz ręcznego mutanta dla
  wariantu, który da się zabić (precedens `src/runner/checkpoint.py`, HAR-173).
- Po zmianie samych testów cache `mutants/` może dać stary wynik — uruchom w świeżym klonie
  w nowym katalogu tymczasowym, z dokopiowanymi niescommitowanymi zmianami. Nie usuwaj `mutants/`
  w sesji agenta.
- `[tool.mutmut] also_copy` obejmuje wszystko, czego potrzebują testy (np. `alembic/`, `alembic.ini`);
  inaczej skrypt kończy się kodem 2 na fazie statystyk mutmuta.
- Mutanta, którego nie da się zaimportować (błąd zbierania pytesta), nie wpisuj do tabeli.

## Czego bramki nie widzą

mutmut nie usuwa instrukcji, elementów krotek i zbiorów ani nie przestawia zapisów, więc te
elementy przechodzą przez kod 0 bez testu. Katalog takich kształtów, po jednym przykładzie:
`.ai/qa/ksztalty-mutantow.md` — test-writer i krytyk czytają go przy każdym tickecie z kodem.

## Co sprawdzić w review

- Element z diffu (gałąź, element zbioru/krotki `except`/wyrażenia regularnego, tryb, zakres,
  kształt z katalogu) bez testu, który oblewa po jego usunięciu = **-25**.
- Przeżywający mutant w zmienionej funkcji bez uzasadnienia równoważności, także fałszywe
  `[[rownowazny]]` = **-25** (CI: kod 1 → czerwony `mutation`).
- Zmiana poza zasięgiem mutmuta bez tabeli ręcznych mutantów = **-25** (CI: „brak dowodu”).
- Linia z listy kodu 3 bez wiersza i bez uzasadnienia = **-25** (CI: `--pokrycie-kod3`).
- Wiersz, którego test nadal przechodzi po mutacji (krytyk sprawdza lokalnie co najmniej
  jeden wiersz) = **-25**.
- PR, po którym wiersz cudzej tabeli przestaje trafiać w kod = **-25** (CI: `--sprawdz-wszystkie`).
- Test z limitem czasu w nazwie, niezależny od mechanizmu, który ten limit realizuje = **-20**.
