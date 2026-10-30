# Testy uruchamiające procesy

Źródło: retro sprintu M1.4 — HAR-35 (testy heartbeatu dwa razy zabiły runner CI pod
mutmut), HAR-50 (podproces zasiewu spoza korzenia repo psuł job `mutation`), HAR-47 +
HAR-142 (`os.killpg` w testach obok strażnika — czerwony `main`).

## Reguła

- Proces w teście startuje przez `tests/procesy.py` (`uruchom_proces`,
  `uruchom_proces_sync`) i kończy przez `zakoncz_proces` — nowa grupa procesów, sprzątanie
  tylko własnej grupy, twarde limity czasu. Bez `os.kill`/`os.killpg` w testach (pilnuje
  tego `test_procesy_testowe`).
- Podproces, który importuje kod projektu (`python -m runner…`, `tools/*.py`), startuje z
  `cwd` = korzeń repozytorium (katalog z `pyproject.toml`, liczony względem pliku testu).
  Pod mutmut kod projektu jest podmieniony w `mutants/`, a proces potomny spoza korzenia
  nie znajduje konfiguracji i pada (`FileNotFoundError: Could not figure out where the code
  to mutate is`).
- Każde czekanie w teście (`wait`, `wait_for`, kolejka, zdarzenie) ma limit czasu — mutant
  bez limitu albo bez `stop()` nie może zawiesić przebiegu.

## Pułapka

Lokalnie `pytest` przechodzi: problem wychodzi dopiero pod mutmut albo w CI (inna grupa
procesów, inny `cwd`, 2 rdzenie), czasem po godzinie, jako „runner lost communication”.

## Co sprawdzić w review

- Proces w teście uruchomiony bez `tests/procesy.py` albo podproces z kodem projektu bez
  `cwd` = korzeń repo = **-25**.
- Czekanie bez limitu czasu w teście = **-10**.
