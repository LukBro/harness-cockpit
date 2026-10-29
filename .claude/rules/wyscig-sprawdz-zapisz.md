# Warunek i zapis w jednej transakcji

Źródło: retro sprintu M1.3 — HAR-37 (`step_finished` nadpisuje `aborted` w
`zapisz_zdarzenie`), HAR-38 (liczenie właścicieli i zapis nieatomowe: zespół może
zostać bez właściciela).

## Reguła

Gdy zapis zależy od stanu przeczytanego wcześniej („jeśli to nie ostatni właściciel,
odbierz rolę”, „jeśli run nie jest przerwany, ustaw sukces”), sprawdzenie i zapis są
**atomowe**:

- w jednej transakcji z blokadą czytanych wierszy (`SELECT … FOR UPDATE` /
  `with_for_update()`), albo
- jako warunek w samym zapisie (`UPDATE … WHERE status = 'running'` i sprawdzenie
  liczby zmienionych wierszy), albo
- jako ograniczenie w bazie (unikalność, `CHECK`), którego naruszenie jest
  tłumaczone na błąd dziedziny (zob. [granica-bledow](granica-bledow.md), wąski `except`).

## Pułapka

Na `READ COMMITTED` (domyślny w PostgreSQL) dwa równoległe żądania czytają ten sam stan,
oba przechodzą sprawdzenie i oba zapisują. Testy na SQLite w jednym procesie tego nie
pokażą — wyścig wychodzi dopiero pod ruchem.

## Co sprawdzić w review

- Odczyt stanu, decyzja w Pythonie i zapis zależny od tej decyzji bez blokady,
  warunku w zapisie albo ograniczenia w bazie = **-25**.
- Test reguły wykonuje dwa przeplecione zapisy (dwie sesje) i sprawdza w danych, że
  niezmiennik przetrwał (zob. [asercja-na-danych](asercja-na-danych.md)).
