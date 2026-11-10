# SPEC: hook PreToolUse wymuszający bramki (UNTESTED - tylko specyfikacja)

Cel: zamienić ostatnie zdania typu "nie wolno" w SKILL.md na odmowę harnessu. Bramki-skrypty mówią
modelowi, co jest dozwolone; hook sprawia, że pominięcie bramki nie przechodzi.
Miejsce: rozszerzenie `hooks/bash_guard.py` pluginu (nie repozytorium projektu).

| Wywołanie | Warunek przepuszczenia | Inaczej |
|---|---|---|
| `update_ticket` z `status=in_review` (MCP albo `monolynx_op.py update_ticket --status in_review`) | `bramka_zamkniecia.py <KEY>` zwraca 0 albo 5 dla bieżącego diffu | deny: "uruchom bramka_zamkniecia.py" |
| `git commit` w sesji głównej | `bramka_commita.py <KEY> --komunikat <plik z -F>` zwraca 0 | deny z liniami `BRAK:` |
| `Agent` z `subagent_type` krytyka (`code-reviewer`) | w stanie istnieje `krytyk-prompt-<n>.md` nowszy niż ostatni wynik bramek, a prompt Agent wskazuje ten plik | deny: "uruchom prompt_krytyka.py" |
| komenda zawierająca `bramka_testow.py` | traktowana jak komenda testów (te same reguły co pytest: subagent deny, sesja główna wg `MONOLYNX_AUTOTEST`) | - |

KEY: z nazwy brancha (`worktree-<KEY>`) albo z jedynego katalogu `<git-common-dir>/monolynx/*/` ze stanem
zmienionym w ostatnich 24 h; niejednoznaczne -> deny z prośbą o jawny KEY (fail-closed).

Kody i limity: hook wywołuje bramkę z limitem 600 s; timeout, wyjątek albo brak linii wyniku = deny
(awaria strażnika to odmowa, zob. `.claude/rules/granica-bledow.md`, HAR-175).

Fałszywe trafienia zaobserwowane w transkryptach (do naprawy w tym samym tickecie):
- 106 odmów "Subagent nie odpala lintu/testów" u subagentów w 15 sesjach, m.in. dla
  `cat >> tests/test_projekty_zmiana.py <<'E' ...` - heredoc zapisujący plik testu, nie uruchomienie testów;
- 18 odmów "git commit/push" dla komend, które tylko czytały pliki zawierające te słowa
  (np. `sed -n 2860,2880p tests/test_straz_roli.py; mkdir -p ...`).
Hook powinien dopasowywać komendę (pierwszy token po rozbiciu przez `shlex`), nie podnapis w treści.

Testy do napisania przed implementacją (nazwy w języku dziedziny):
- test_status_do_odbioru_bez_zielonej_bramki_zamkniecia_jest_odrzucony
- test_status_do_odbioru_po_zmianie_kodu_po_werdykcie_jest_odrzucony
- test_commit_z_wspolautorem_jest_odrzucony_gdy_projekt_go_zakazuje
- test_krytyk_bez_pliku_promptu_z_bramki_nie_startuje
- test_heredoc_zapisujacy_plik_testu_nie_jest_uruchomieniem_testow
- test_awaria_bramki_w_hooku_to_odmowa
