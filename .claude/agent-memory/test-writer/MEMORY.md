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
- HAR-12: Każda gałąź i każde przekształcenie w kodzie chroniącym niezmiennik mają test, którego asercja oblewa się po usunięciu tej gałęzi; asercja „napis zawiera X” nie chroni formatowania, które zachowuje X.
- HAR-13: Każde przekształcenie wartości wejściowej (escapowanie, `strip`, zapis z powrotem do środowiska) ma test na wejściu, na którym tylko to przekształcenie decyduje o wyniku.
