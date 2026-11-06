# Raport zgodny z diffem

Źródło: retro sprintu M1.4 — HAR-52 (raport wymieniał testy, których nie było w pliku),
HAR-54 (backend wstawił zmyślony cytat kryterium BRO-108).

## Reguła

- Każde twierdzenie raportu agenta o kodzie („dodałem test X”, „kryterium mówi Y”,
  „mutant Z zabity”) da się wskazać w diffie, w pliku albo w źródle (ticket, Linear, wiki)
  — z nazwą testu, ścieżką i linią albo dosłownym cytatem.
- Team Manager przed rekoncyliacją sprawdza w diffie każdy wymieniony test i plik; cytat
  z ticketu porównuje z jego treścią. Twierdzenie bez pokrycia = raport odrzucony, nie
  „zaakceptowany z uwagą”.
- Agent, który nie wie, pisze „nie sprawdziłem”, zamiast zgadywać.

## Pułapka

Raport brzmi pewnie i jest spójny, więc krytyk ocenia go zamiast diffu. Zmyślony test
wygląda jak pokrycie reguły, której nikt nie chroni.

## Co sprawdzić w review

- Raport wymienia test, plik albo cytat, którego nie ma w diffie lub w źródle = **-25**.

## Regresja (retro M1.8)

Źródło: HAR-173 (pierwszy raport backend-python opisywał zmiany, których nie było w pliku —
skrypt agenta zablokował hook, wykrył to dopiero lint), HAR-226 (raport podał 12 wierszy tabeli
ręcznych mutantów, w pliku było 11; pokrycie mutanta opisane jako „po 120 s”, faktycznie ok. 10 s).
HAR-225: agent zmienił tabelę innego ticketu (`HAR-156.toml` #68) i nie zgłosił tego `STOP:`.

- **Liczby w raporcie liczysz z pliku** — liczba wierszy tabeli, testów i mutantów pochodzi z
  komendy uruchomionej po ostatniej zmianie (`grep -c '^\[\[mutant\]\]'`, wynik pytesta), nie
  z pamięci agenta.
- **Komenda odrzucona przez hook = brak zmiany** — raport po odmowie narzędzia mówi „nie
  zapisałem”, nie opisuje zamierzonej zmiany jako wykonanej.
- **Plik spoza przydziału** (tabela innego ticketu, plik frontu przy nowej wartości enuma w
  OpenAPI) — zmiana wymaga linii `STOP:` w raporcie z powodem; Team Manager decyduje, czy wchodzi
  (HAR-172: etykieta `rejected` w `WidokRunu.tsx`, bez której `npm run build` pada).

Co sprawdzić w review: liczba w raporcie inna niż w pliku albo opis zmiany, której nie ma w
diffie = **-25**; zmiana pliku spoza przydziału bez `STOP:` = **-10**.
