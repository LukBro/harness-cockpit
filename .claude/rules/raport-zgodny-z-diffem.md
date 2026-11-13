# Raport zgodny z diffem

Każde twierdzenie raportu o kodzie („dodałem test X”, „kryterium mówi Y”, „mutant Z zabity”)
da się wskazać w diffie, w pliku albo w źródle — nazwą testu, ścieżką i linią albo dosłownym
cytatem. Nie wiesz — piszesz „nie sprawdziłem”.
WHY: raport brzmi pewnie, więc krytyk ocenia raport zamiast diffu; zmyślony test wygląda jak
pokrycie reguły, której nikt nie chroni (HAR-52, HAR-54).

- Liczby (wiersze tabeli, testy, mutanty) bierzesz z komendy uruchomionej po ostatniej zmianie
  (`grep -c '^\[\[mutant\]\]'`, wynik pytesta), nie z pamięci.
- Komenda odrzucona przez hook to brak zmiany — piszesz „nie zapisałem”.
- Zmiana pliku spoza przydziału (cudza tabela, plik frontu wymuszony nową wartością enuma
  w OpenAPI) wymaga linii `STOP:` z powodem; Team Manager decyduje, czy wchodzi.
- Team Manager przed rekoncyliacją sprawdza w diffie każdy wymieniony test i plik, a cytat
  porównuje ze źródłem. Twierdzenie bez pokrycia = raport odrzucony.

## Co sprawdzić w review

- Raport wymienia test, plik albo cytat, którego nie ma w diffie lub w źródle = **-25**.
- Liczba w raporcie inna niż w pliku albo opis zmiany, której nie ma w diffie = **-25**.
- Zmiana pliku spoza przydziału bez `STOP:` = **-10**.
