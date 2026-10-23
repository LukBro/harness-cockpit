# Konwencje testów tego repozytorium

- Nazwy testów po polsku, w języku dziedziny: `test_runner_offline_konczy_run_jako_przerwany`
- Jeden test sprawdza jedną rzecz, którą da się zaobserwować bez czytania kodu
- Testy backendu: `pytest`, tryb asyncio automatyczny
- Baza w testach: SQLite przez `aiosqlite`. UWAGA: produkcja to PostgreSQL —
  testy dotykające składni specyficznej dla bazy muszą iść na prawdziwym Postgresie
