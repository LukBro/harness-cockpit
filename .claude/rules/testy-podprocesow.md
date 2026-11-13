# Testy uruchamiające procesy

Lokalnie wszystko przechodzi; problem wychodzi pod mutmut albo w CI (inna grupa procesów, inny
`cwd`, 2 rdzenie) jako „runner lost communication” (HAR-35, HAR-47, HAR-50, HAR-142).

- Proces w teście startuje przez `tests/procesy.py` (`uruchom_proces`, `uruchom_proces_sync`)
  i kończy przez `zakoncz_proces`. Sygnały do grup poza tym helperem blokuje test
  `test_testy_nie_wysylaja_sygnalow_do_grup_procesow_poza_wspolnym_helperem`.
- Podproces importujący kod projektu (`python -m runner…`, `tools/*.py`) startuje z `cwd` =
  korzeń repo (katalog z `pyproject.toml`, liczony od pliku testu) — pod mutmut kod leży
  w `mutants/` i proces spoza korzenia pada.
- Każde czekanie w teście (`wait`, `wait_for`, `.result()`, kolejka, zdarzenie) ma limit czasu.

## Co sprawdzić w review

- Proces w teście bez `tests/procesy.py` albo podproces z kodem projektu bez `cwd` = korzeń
  repo = **-25**.
- Czekanie bez limitu czasu w teście = **-10**.
