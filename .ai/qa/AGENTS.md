# Konwencje testów tego repozytorium

- Nazwy testów po polsku, w języku dziedziny: `test_runner_offline_konczy_run_jako_przerwany`
- Jeden test sprawdza jedną rzecz, którą da się zaobserwować bez czytania kodu
- Testy backendu: `pytest`, tryb asyncio automatyczny
- Baza w testach: SQLite przez `aiosqlite`. UWAGA: produkcja to PostgreSQL —
  testy dotykające składni specyficznej dla bazy muszą iść na prawdziwym Postgresie

## Moment → skrypt (HAR-240)

W powtarzalnych momentach sesji ticketu uruchamiasz gotowy skrypt z `tools/`, nie składasz komend sama.
Skrypt daje jeden werdykt (kod wyjścia) i krótkie wyjście, które wklejasz do raportu. Komendy złożone
(`sed -n …; grep …`, pętle, pipe do `tail`) są odmawiane przez regułę `praca-w-tle` i nie dają kodu pytesta.
`python` to interpreter z `pytest`, `pytest-xdist` i `alembic` (venv projektu).

| Moment | Skrypt | Werdykt |
| -- | -- | -- |
| Testy w trakcie pracy (wąskie i pełne) | `python tools/testy.py <argumenty pytesta>` | kod pytesta; **2** = zawieszenie (po 20 min bez postępu, zmiana: `--bez-postepu SEKUNDY`), stdout: `zawieszenie: <node id>` |
| Przed review (pełne testy, mutanty zmian) | `bash tools/testy_pelne.sh`, potem `python tools/mutanty_zmian.py` (baza: `origin/main`) | kody jak w `test-odroznia-regule`: 0 czysto, 1 przeżywające, 2 błąd narzędzia, 3 sprawdź ręcznie |
| Tabele ręcznych mutantów (po zmianie kodu) | `python tools/mutanty_reczne.py --sprawdz-wszystkie`, swoja tabela: `python tools/mutanty_reczne.py tests/mutanty_reczne/<ticket>.toml` | 0 / 1 przeżywające / 2 błąd |
| Po `git merge origin/main` | `bash tools/po_merge_main.sh` | ostatnia linia `PO-MERGE: ok` albo `PO-MERGE: blad <etap>` (`konflikt`, `alembic`, `mutanty`, `testy`); kod 0 / 1 / 2 |
| Raport z liczbami | `python tools/liczby_raportu.py --pliki "<lista z sekcji Pliki dotykane>"` | markdown do wklejenia: wiersze `[[mutant]]` i `[[rownowazny]]`, nowe i usunięte testy, cudze tabele i pliki spoza listy z dopiskiem `wymaga STOP:` |
| Oddanie PR | `bash tools/oddaj_pr.sh --tytul "<tytuł>" --tresc <plik z opisem PR>` | ostatnia linia `ODDAJ-PR: ok <url>` albo `ODDAJ-PR: odmowa <powód>`; ścieżka wrażliwa w diffie: PR powstaje, skrypt pisze `merge reczny` |

Zasady, które egzekwują skrypty (zamiast pamięci agenta):

- `raport-zgodny-z-diffem`: liczby w raporcie pochodzą z wyjścia `liczby_raportu.py`; zmiana cudzej tabeli albo pliku
  spoza przydziału ma w raporcie linię `STOP:` z powodem.
- `merge-rownoleglych-ticketow`: po merge `main` idzie cały `po_merge_main.sh`, nie testy plików z konfliktem.
- CLAUDE.md „Commity”: `oddaj_pr.sh` odmawia przy innym `user.email`/`user.name` niż `lukaszbrowar@gmail.com` / `LukBro`,
  przy `Co-Authored-By` w commicie i przy brudnym drzewie; nigdy nie robi `--force` ani `--amend` i nie pushuje z `main`.
- `testy-podprocesow`: `testy.py` uruchamia pytest w nowej grupie procesów i sprząta tylko własną.

Moment „przed review” wskazuje dziś `testy_pelne.sh` + `mutanty_zmian.py`. `tools/bramki_lokalne.sh` (HAR-236) po
wejściu na `main` zastępuje te dwa wpisy jednym; do tego czasu nie wołaj go z wiersza tej tabeli.
