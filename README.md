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

Przelot wymaga `COCKPIT_DATABASE_URL` wskazującego bazę testową PostgreSQL (np. po
`tools/testy_pelne.sh`: `postgresql+asyncpg://cockpit:cockpit@localhost:5432/cockpit_test`).
UWAGA: skrypt robi `alembic downgrade base` przed i po przelocie — kasuje dane wskazanej bazy.

## PostgreSQL i migracje

Schemat buduje wyłącznie Alembic (`create_all` zostaje tylko w testach).

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

## Faza publiczna

Wystawienie kokpitu pod publicznym adresem za reverse proxy to zmiana zmiennych
środowiskowych, nie kodu. Samo proxy, domenę i HTTPS konfigurujesz poza tym
repozytorium. Zmienne kokpitu można ustawić w środowisku albo w pliku `.env`
w katalogu, z którego startuje `cockpit` (środowisko ma pierwszeństwo).

Do ustawienia:

- `COCKPIT_ADRES_PUBLICZNY` - publiczny adres `https://...`, z którego powstają
  linki resetu hasła. Bez niego (albo bez poczty `COCKPIT_SMTP_*`) reset jest
  wyłączony.
- `COCKPIT_URL` - na maszynie z `cockpit-runner`: ten sam publiczny adres
  `https://...` (domyślnie `http://127.0.0.1:8765`). Runner czyta go tylko ze
  środowiska; nie wpisuj go do `.env` kokpitu, bo kokpit nie wystartuje.

Zależnie od położenia proxy:

- `COCKPIT_HOST` - domyślnie `127.0.0.1`; zostaje, gdy proxy (np. Caddy) działa
  na tej samej maszynie. Dla proxy na innej maszynie ustaw adres, pod którym
  proxy widzi kokpit, i nie wystawiaj tego portu publicznie.
- `COCKPIT_PORT` - domyślnie `8765`; zmień, gdy port jest zajęty, i wskaż go
  w proxy.
- `COCKPIT_FORWARDED_ALLOW_IPS` - domyślnie `127.0.0.1`. Ma zawierać tylko adres,
  z którego łączy się proxy, nigdy `*`: kto może podać własny `X-Forwarded-For`,
  ten zmienia adres IP widziany przez kokpit i obchodzi blokadę logowania pary
  (e-mail, IP). Gdy proxy łączy się po IPv6 loopback (czyli
  `COCKPIT_HOST=::1`), ustaw `::1` - domyślna wartość go nie obejmuje. Zmienna
  uvicorna `FORWARDED_ALLOW_IPS` nie jest już czytana.

Zostaje bez zmian:

- `COCKPIT_PROXY_HEADERS` - włączone. Po wyłączeniu kokpit widzi wszystkich pod
  adresem proxy, więc blokada pary (e-mail, IP) przestaje rozróżniać klientów.
- `COCKPIT_CIASTECZKO_SECURE` - włączone. Nie ustawiaj `0`; to wartość tylko dla
  lokalnej pracy po `http://localhost`.

## Zasady

Przed pierwszą zmianą przeczytaj `CLAUDE.md` — niezmienniki i decyzje
implementacyjne. Zadania i uzasadnienia: projekt Harness Cockpit w Linearze.
