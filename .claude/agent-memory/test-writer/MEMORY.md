# Pamięć agenta test-writer

## Reguly z review krytyka

- HAR-2: Test nazwany wynikiem reguły dziedzinowej uruchamia produkcyjny kod tej reguły (prawdziwą aplikację, prawdziwy punkt wejścia) i sprawdza jej skutek w danych (np. który runner dostał run), a nie tylko kod odpowiedzi.
- HAR-2: Test reguły wyboru zasobu (runner, run, zdarzenie) zawsze ma w bazie co najmniej dwa zespoły i sprawdza, że rekord innego zespołu nie zostaje wybrany, bo filtr `tenant_id` jest niezmiennikiem, a nie szczegółem zapytania.
- HAR-3: Asercja, która twierdzi, że coś nie powstało w danych, sprawdza dane; stan interfejsu wynikający wprost z kodu odpowiedzi nie dowodzi niczego o bazie.
- HAR-5: Każda reguła ticketu, która różnicuje zachowanie zależnie od źródła albo trybu, ma test na wejściu, które daje różny wynik w każdym trybie, a każda gałąź tłumaczenia wyjątku ma test, który w nią trafia.
- HAR-7: Test złego typu używa wartości, którą odrzuca tylko implementowana reguła (napis zamiast liczby, `yes` zamiast bool), a nie wartości, którą odrzuca każda implementacja, bo tylko taki test chroni regułę przed usunięciem.
- HAR-7: Każda reguła zapisu, która ma osobną ścieżkę dla kroków niepoprawnych, ma test zapisu z takim krokiem w pliku, bo bez niego usunięcie tej ścieżki oznacza ciche kasowanie danych użytkownika.
- HAR-8: Każda reguła ticketu ograniczona do przodków w needs (albo do kroków cyklu) ma test, w którym poza tym zakresem istnieje krok, który dałby inny wynik, bo tylko taki test odróżnia regułę od wersji globalnej.
- HAR-6: Każda gałąź kodu chroniącego niezmiennik ma test na wejściu, które trafia dokładnie w tę gałąź, bo bez niego usunięcie gałęzi zostawia cichą lukę w ochronie.
- HAR-6: Każdy element zbioru lub tabeli, który rozszerza wykrywanie naruszenia niezmiennika (np. znak separatora powłoki), ma własny przypadek testowy, bo usunięcie jednego elementu otwiera konkretne obejście, którego nie złapie żaden inny przypadek.
- HAR-6: Każdy element zbioru, który rozszerza wykrywanie naruszenia niezmiennika, ma przypadek testowy, w którym tylko ten element decyduje o wykryciu (np. separator doklejony bez spacji, `ls|git push`, albo opcja z osobnym argumentem), bo przypadek wykrywany także bez tego elementu nie chroni go przed usunięciem.
- HAR-9: Każda reguła, która usuwa albo podmienia wartość z pliku zależnie od stanu rejestru, ma test na wejściu, w którym ta wartość z pliku istnieje i zmieniłaby wynik, gdyby reguły nie było.
- HAR-10: Każde zawężenie wzorca albo klasy znaków ma test na wejściu, które tylko to zawężenie odrzuca, bo wejście parsowane tak samo przez wersję szerszą nie chroni zawężenia przed cofnięciem.
- HAR-31: Każdy element kodu, który chroni przed wyjątkiem biblioteki (rzutowanie, konwersja typu), ma test na wejściu, na którym tylko ten element decyduje, czy funkcja zwróci wynik, czy rzuci wyjątek.
- HAR-29: Każda gałąź, każdy element zbioru i każdy zakres reguły ma test na wejściu, w którym tylko ten element decyduje o wyniku; test, którego asercja dopuszcza kilka wyników, nie chroni żadnego z nich.
- HAR-12: Każda gałąź i każde przekształcenie w kodzie chroniącym niezmiennik mają test, którego asercja oblewa się po usunięciu tej gałęzi; asercja „napis zawiera X” nie chroni formatowania, które zachowuje X.
- HAR-13: Każde przekształcenie wartości wejściowej (escapowanie, `strip`, zapis z powrotem do środowiska) ma test na wejściu, na którym tylko to przekształcenie decyduje o wyniku.
- HAR-43: Każda pętla albo przekształcenie, które zmienia wynik reguły, ma test na wejściu, na którym tylko ten element decyduje o wyniku; wejście, które jedno przejście w kolejności workflow już rozstrzyga, nie chroni pętli punktu stałego.
- HAR-43: Każda pętla przeglądająca zakres reguły (domknięcie przechodnie `needs`) ma test na grafie z rombem, w którym przerwanie pętli przy pierwszym ponownie spotkanym kroku gubi przodka, a ten przodek decyduje o wyniku.
- HAR-24: Każdy element filtra zapytania liczącego (run, krok, rodzaj) ma test, w którym w danych istnieje rekord odrzucany wyłącznie przez ten element, bo inaczej usunięcie tego elementu przeżywa.
- HAR-41: Każdy element klasy znaków i każdy parametr formatu wyjścia ustalony w decyzji ma test, w którym tylko ten element decyduje o wyniku (przyjęcie wartości albo dokładne bajty pliku); test, który czyta wynik przez `json.loads` albo sprawdza „zawiera”, nie chroni formatowania.
- HAR-25: Każda funkcja wyliczająca wartość w zakresie (numer w obrębie runu, licznik w obrębie kroku) ma test, w którym poza zakresem istnieje rekord, który zmieniłby wynik, gdyby zakres zniknął.
- HAR-133: Każda gałąź sprawdzająca kod wyjścia podprocesu, każdy kanał wyjścia (stdout/stderr) i każdy warunek typu w konfiguracji ma test na wejściu, na którym tylko ten element decyduje o wyniku; zaślepka, która zawsze zwraca sukces, nie chroni gałęzi błędu.
- HAR-133: Każda gałąź tłumacząca wyjątek procesu albo wejścia (także w jedynej funkcji, którą testy podmieniają) ma test na prawdziwym kodzie tej funkcji, a każdy warunek pomijający dane ma test, w którym tylko ten warunek decyduje o wyniku.
- HAR-44: Każda gałąź sprzątająca lub tłumacząca wyjątek (rollback, element krotki `except`) i każde nowe ograniczenie ustawień (wartość domyślna, `ge`) ma test, który oblewa po jej usunięciu; zamknięcie sesji w teście, które samo cofa transakcję, nie chroni jawnego rollbacku.
