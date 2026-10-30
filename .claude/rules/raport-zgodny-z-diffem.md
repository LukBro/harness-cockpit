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
