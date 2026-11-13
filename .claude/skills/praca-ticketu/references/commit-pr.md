# Commit, push i PR/MR

Czytasz w kroku 7, dopiero gdy `bramka_commita.py` zwróciła 0. Każda komenda osobno, ścieżki
i nazwy literalnie (sesja w worktree odrzuca `$VAR`, `$(...)`, `&&`, pipe).

## Commit (MONOLYNX_AUTOCOMMIT=true)

1. Komunikat zapisz narzędziem zapisu do pliku w katalogu stanu: pierwsza linia `<KEY>: <tytuł>`.
   Atrybucja (Co-Authored-By) zgodnie z CLAUDE.md projektu - projekt ma pierwszeństwo przed
   domyślną atrybucją narzędzia.
2. `git add <plik> <plik> ...` - imiennie, tylko pliki z przydziału i `pliki_tm`; nigdy `git add -A`
   przy niepustym `pliki_sprzed_startu`.
3. `python3 <SKILL_DIR>/scripts/bramka_commita.py <KEY> --komunikat <plik> [--autor-email <adres>] [--bez-wspolautora]`
   - opcje bierz z CLAUDE.md projektu (harness-cockpit: obie).
4. Kod 0 -> `git commit -F <plik>`. Kod inny -> nie commitujesz; linie `BRAK:` idą do podsumowania.

`MONOLYNX_AUTOCOMMIT=false` -> wypisz człowiekowi komendy 2 i 4, nic nie wykonuj.

## Push (MONOLYNX_AUTOPUSH=true i commit się odbył)

`git push -u origin <branch>`. Nigdy `--force`. Przy `AUTOPUSH=false` nie pushujesz nawet cudzych commitów.

## PR / MR (MONOLYNX_AUTOMR=true i push się odbył)

Branch docelowy: `MONOLYNX_MR_TARGET`, inaczej `baza` ze stanu.

- GitHub (remote `github.com`; harness-cockpit):
  `gh pr create --base <baza> --head <branch> --title "<KEY>: <tytuł>" --body-file <plik opisu>`
- GitLab: `glab mr create --title '<KEY>: <tytuł>' --description '<podsumowanie>' --yes --source-branch <branch> --target-branch <baza>`
  oraz tryb `pełny przebieg: ci` - sekcja "Pipeline merge requesta" w `commit-mr.md` (bez zmian).

Brak narzędzia, błąd albo brak pushu -> PR nie powstaje; powód do podsumowania. Sesja nie czeka na CI
i go nie naprawia - czerwone CI naprawia `/monolynx:mr-queue`.
