# Harness Cockpit

Kokpit do składania, uruchamiania i obserwowania workflow agentowych.

## Szkielet na wylot

Dowód, że architektura się trzyma: klik w przeglądarce → kokpit → runner →
proces na maszynie użytkownika → zdarzenie → ekran.

```bash
pip install -e ".[dev]"
python3 tools/demo_szkielet.py     # kokpit + runner + run, wszystko w jednym procesie
pytest                             # testy backendu
```

## PostgreSQL i migracje

Schemat buduje wyłącznie Alembic (`create_all` zostaje w testach i demo).

Baza lokalnie jest taka sama jak w CI: `postgres:16`, użytkownik `cockpit/cockpit`,
bazy `cockpit` i `cockpit_test` (`compose.yaml`, dane w wolumenie Dockera poza
repozytorium). Wymaga Dockera z `docker compose` v2, npm i aktywnego venv
z `pip install -e ".[dev]"`.

Pełny zestaw testów (migracje i e2e) jednym poleceniem:

```bash
tools/testy_pelne.sh                          # podnosi bazę, buduje front, pytest -q i pytest -m e2e
COCKPIT_PG_PORT=5433 tools/testy_pelne.sh     # gdy port 5432 zajmuje inny Postgres
```

Skrypt kończy się błędem, gdy czegoś brakuje albo gdy test migracji lub e2e
został pominięty. Zmienna `PYTHON` wskazuje interpreter (domyślnie `python`).

Baza aplikacji `cockpit` powstaje przy `tools/testy_pelne.sh` (przy innym
`COCKPIT_PG_PORT` zmień port w URL):

```bash
export COCKPIT_DATABASE_URL=postgresql+asyncpg://cockpit:cockpit@localhost/cockpit
alembic upgrade head                          # przed pierwszym `cockpit`
```

Osobno:

```bash
cockpit
COCKPIT_RUNNER_TOKEN=... COCKPIT_URL=http://127.0.0.1:8765 cockpit-runner
cd web && npm install && npm run dev
```

## Szkielet w przeglądarce

Jeden port, bez CORS: kokpit serwuje zbudowany front i `/api/*` na tym samym adresie.

```bash
tools/testy_pelne.sh                             # Postgres z compose.yaml (patrz wyżej; tworzy też bazę `cockpit`)
export COCKPIT_DATABASE_URL=postgresql+asyncpg://cockpit:cockpit@localhost/cockpit
alembic upgrade head
python tools/seed_dev.py                         # zespół, runner, token w pliku 0600
(cd web && npm install && npm run build)         # buduje do src/cockpit/static
cockpit                                          # terminal 1
cockpit-runner                                   # terminal 2
```

Otwórz http://127.0.0.1:8765/. Token nie pojawia się na ekranie - `seed_dev`
wypisuje tylko ścieżkę pliku. Zmienna `COCKPIT_CONFIG` pozwala trzymać plik
z tokenem poza `~/.config` (np. w katalogu tymczasowym), a `COCKPIT_RUNNER_TOKEN`
w środowisku ma pierwszeństwo przed plikiem. Uruchom `seed_dev` i `cockpit-runner`
z tym samym `COCKPIT_CONFIG`. Ponowne `seed_dev` generuje nowy token.

## Zasady

Przed pierwszą zmianą przeczytaj `CLAUDE.md` — niezmienniki i decyzje
implementacyjne. Zadania i uzasadnienia: projekt Harness Cockpit w Linearze.
