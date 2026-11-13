# Warunek i zapis w jednej transakcji

Gdy zapis zależy od stanu przeczytanego wcześniej („jeśli to nie ostatni właściciel, odbierz
rolę”, „jeśli run nie jest przerwany, ustaw sukces”), sprawdzenie i zapis są atomowe:

- blokada czytanych wierszy (`with_for_update()`) w tej samej transakcji, albo
- warunek w samym zapisie (`UPDATE … WHERE status = 'running'` + liczba zmienionych wierszy), albo
- ograniczenie w bazie (unikalność, `CHECK`) tłumaczone na błąd dziedziny wąskim `except`.

WHY: na `READ COMMITTED` dwa równoległe żądania przechodzą to samo sprawdzenie i oba zapisują;
SQLite w jednym procesie tego nie pokaże (HAR-37, HAR-38).

## Co sprawdzić w review

- Odczyt stanu, decyzja w Pythonie i zapis zależny od niej bez blokady, warunku w zapisie albo
  ograniczenia w bazie = **-25**.
- Test reguły wykonuje dwa przeplecione zapisy (dwie sesje) i sprawdza w danych, że niezmiennik
  przetrwał (zob. [asercja-na-danych](asercja-na-danych.md)).
