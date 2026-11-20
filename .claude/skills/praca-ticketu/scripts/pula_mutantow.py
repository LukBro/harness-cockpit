#!/usr/bin/env python3
"""Bramka mutantow w puli maszyn (audyt procesu 2026-11-20): statystyki mutmuta w shardach naraz.

Bez puli `mutanty_zmian.py` liczy statystyki mutmuta jednym procesem (ok. 25 min na serwerze). Tu
liczba shardow `tools/statystyki_mutmuta.py shard` idzie rownolegle: czesc na maszynach z
<git-common-dir>/monolynx/pula.json, reszta lokalnie, kazdy w osobnej kopii kodu. Dowody trafiaja do
katalogu tymczasowego poza kopia robocza (nie zmieniaja skrotu diffu bramki), sa scalane
(`statystyki_mutmuta.py scal`), a `mutanty_zmian.py` dostaje `--katalog-dowodow` - tak jak job
`mutation` w CI (HAR-255). Dowod niesie sha commita i odcisk zrodel, wiec zly shard to kod 2, nie zielen.

pula.json (brak pliku = wszystkie shardy lokalnie):
  {"zdalne": [{"ssh": "browar", "katalog": "~/harness-browar", "obraz": "harness-ci:24.04",
               "shardy": 2, "sloty": 4}]}
Na maszynie zdalnej: klon repozytorium w <katalog>/repo, obraz dockera ze srodowiskiem testow, `flock`.
Shard zdalny, ktory zawiodl (brak polaczenia, kod != 0, brak dowodu), liczy sie ponownie lokalnie.

Wywolanie: pula_mutantow.py [--liczba N] -- <argumenty mutanty_zmian.py>
Kod wyjscia: kod mutanty_zmian.py; 2 - brak dowodu z puli (shard lokalny zawiodl, scalenie zawiodlo);
64 - blad uzycia albo pula.json.
"""
from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LIMIT_SHARDU_S = 3600
KOD_ZAJETY = 200  # flock -E: wszystkie sloty maszyny zajete, probujemy dalej


class BladPuli(Exception):
    """Blad konfiguracji albo uzycia (kod 64)."""


def _git(korzen: Path, *a: str, binary: bool = False) -> str | bytes:
    r = subprocess.run(["git", *a], cwd=korzen, capture_output=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(a)}: {r.stderr.decode(errors='replace')[:200]}")
    return r.stdout if binary else r.stdout.decode()


def wczytaj_pule(plik: Path) -> list[dict]:
    """Maszyny zdalne z pula.json; brak pliku = pusta lista (wszystko lokalnie)."""
    try:
        dane = json.loads(plik.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise BladPuli(f"{plik}: {e}") from e
    zdalne = dane.get("zdalne") if isinstance(dane, dict) else None
    if not isinstance(zdalne, list):
        raise BladPuli(f"{plik}: brak listy 'zdalne'")
    for m in zdalne:
        if not (isinstance(m, dict) and all(isinstance(m.get(k), str) and m[k] for k in ("ssh", "katalog", "obraz"))
                and all(isinstance(m.get(k), int) and not isinstance(m[k], bool) and m[k] >= 1
                        for k in ("shardy", "sloty"))):
            raise BladPuli(f"{plik}: maszyna musi miec ssh, katalog, obraz (napisy) oraz shardy, sloty >= 1: {m}")
    return zdalne


def plan(liczba: int, zdalne: list[dict]) -> list[dict | None]:
    """Maszyna dla kazdego shardu (None = lokalnie); maszyny zdalne biora shardy od konca."""
    wynik: list[dict | None] = [None] * liczba
    i = liczba - 1
    for m in zdalne:
        for _ in range(m["shardy"]):
            if i < 1:  # shard 0 zawsze lokalnie: pula bez zadnej maszyny zdalnej nadal dziala
                return wynik
            wynik[i] = m
            i -= 1
    return wynik


def zmienia_zrodla_mutmuta(korzen: Path, baza: str) -> bool:
    """Czy diff wzgledem bazy (z nieśledzonymi) dotyka source_paths mutmuta - inaczej statystyki sa zbedne."""
    with open(korzen / "pyproject.toml", "rb") as f:
        zrodla = tomllib.load(f)["tool"]["mutmut"]["source_paths"]
    mb = _git(korzen, "merge-base", baza, "HEAD").strip()
    pliki = _git(korzen, "diff", "--name-only", "--no-renames", "--no-ext-diff", mb).split("\n")
    pliki += _git(korzen, "ls-files", "--others", "--exclude-standard").split("\n")
    return any(p.startswith(z.rstrip("/") + "/") or p == z for p in pliki if p for z in zrodla)


def paczka(korzen: Path) -> bytes:
    """Stan kopii roboczej do odtworzenia gdzie indziej: lata wzgledem HEAD i pliki nieśledzone."""
    bufor = io.BytesIO()
    with tarfile.open(fileobj=bufor, mode="w") as tar:
        lata = _git(korzen, "diff", "--binary", "--no-ext-diff", "HEAD", binary=True)
        info = tarfile.TarInfo("lata.diff")
        info.size = len(lata)
        tar.addfile(info, io.BytesIO(lata))
        for p in _git(korzen, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
            if p:
                tar.add(korzen / p, arcname=f"nowe/{p}")
    return bufor.getvalue()


def odtworz(korzen: Path, cel: Path, sha: str, dane: bytes) -> None:
    """Kopia kodu w `cel`: klon na sha + lata + pliki nieśledzone (jak w kopii roboczej)."""
    subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(korzen), str(cel)], check=True, timeout=600)
    subprocess.run(["git", "-C", str(cel), "checkout", "-q", "--detach", sha], check=True, timeout=600)
    with tarfile.open(fileobj=io.BytesIO(dane)) as tar:
        lata = tar.extractfile("lata.diff").read()
        if lata:
            subprocess.run(["git", "-C", str(cel), "apply", "--binary", "-"], input=lata, check=True, timeout=300)
        for czlon in tar.getmembers():
            if czlon.name.startswith("nowe/"):
                czlon.name = czlon.name[len("nowe/"):]
                tar.extract(czlon, cel, filter="data")


def shard_lokalny(korzen: Path, sha: str, dane: bytes, indeks: int, liczba: int, dowody: Path, log) -> bool:
    kopia = Path(tempfile.mkdtemp(prefix=f"pula-shard{indeks}-"))
    try:
        odtworz(korzen, kopia / "repo", sha, dane)
        r = subprocess.run([sys.executable, "tools/statystyki_mutmuta.py", "shard", "--indeks", str(indeks),
                            "--liczba", str(liczba), "--katalog-dowodow", str(dowody)],
                           cwd=kopia / "repo", stdout=log, stderr=subprocess.STDOUT, timeout=LIMIT_SHARDU_S)
        return r.returncode == 0 and (dowody / f"shard-{indeks}.json").is_file()
    except (OSError, subprocess.SubprocessError) as e:
        print(f"PULA shard {indeks} lokalnie: {e}", file=log, flush=True)
        return False
    finally:
        shutil.rmtree(kopia, ignore_errors=True)


def skrypt_zdalny(m: dict, praca: str, sha: str, indeks: int, liczba: int) -> str:
    """Komenda sh na maszynie zdalnej: kopia kodu, slot przez flock, shard w kontenerze, dowod na stdout."""
    k, q = m["katalog"], shlex.quote
    D = f"{k}/praca/{praca}"
    shard = (f"pip install -q --no-deps -e . >/dev/null 2>&1; python tools/statystyki_mutmuta.py shard --indeks {indeks}"
             f" --liczba {liczba} --katalog-dowodow .pula-dowody >&2; k=$?; chown -R $(stat -c %u:%g .) .; exit $k")
    docker = f"docker run --rm -v $D/repo:/repo -w /repo {q(m['obraz'])} bash -c {q(shard)}"
    return "\n".join([
        "set -u",
        f"D={D}; mkdir -p $D && tar -x -C $D -f $D.tar || exit 3",
        # shardy jednej maszyny dziela klon: fetch naraz konczy sie "cannot lock ref"
        f"flock {k}/repo.lock git -C {k}/repo fetch -q origin || exit 3",
        f"[ -s $D/zmiany.bundle ] && {{ flock {k}/repo.lock git -C {k}/repo fetch -q $D/zmiany.bundle || exit 3; }}",
        # bez --shared: alternates wskazuja sciezke hosta, ktorej kontener nie widzi (git w testach nie znajdzie commitow)
        f"git clone -q --no-checkout {k}/repo $D/repo && git -C $D/repo checkout -q --detach {sha} || exit 3",
        "[ -s $D/lata.diff ] && { git -C $D/repo apply --binary $D/lata.diff || exit 3; }",
        "[ -d $D/nowe ] && cp -a $D/nowe/. $D/repo/",
        f"while :; do for s in $(seq 0 {m['sloty'] - 1}); do",
        f"  flock -n -E {KOD_ZAJETY} /tmp/harness-pula-$s.lock {docker}; k=$?",
        f"  [ $k != {KOD_ZAJETY} ] && break 2",
        "done; sleep 5; done",
        f"[ $k = 0 ] && cat $D/repo/.pula-dowody/shard-{indeks}.json",
        "rm -rf $D $D.tar; exit $k",
    ])


def shard_zdalny(korzen: Path, m: dict, sha: str, dane: bytes, indeks: int, liczba: int, dowody: Path, log) -> bool:
    praca = f"{sha[:12]}-{os.getpid()}-{indeks}"
    try:
        bundle = b""
        if subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"], cwd=korzen).returncode != 0:
            bundle = _git(korzen, "bundle", "create", "-", "HEAD", "--not", "origin/main", binary=True)
        bufor = io.BytesIO(dane)
        with tarfile.open(fileobj=bufor, mode="a") as tar:
            info = tarfile.TarInfo("zmiany.bundle")
            info.size = len(bundle)
            tar.addfile(info, io.BytesIO(bundle))
        ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", m["ssh"]]
        cel = f"{m['katalog']}/praca/{praca}.tar"
        subprocess.run([*ssh, f"mkdir -p {m['katalog']}/praca && cat > {cel}"], input=bufor.getvalue(),
                       check=True, timeout=600, capture_output=True)
        r = subprocess.run([*ssh, skrypt_zdalny(m, praca, sha, indeks, liczba)], capture_output=True,
                           timeout=LIMIT_SHARDU_S)
        log.write(r.stderr.decode(errors="replace")[-4000:])
        if r.returncode != 0 or not r.stdout:
            print(f"PULA shard {indeks} na {m['ssh']}: kod {r.returncode}", file=log, flush=True)
            return False
        (dowody / f"shard-{indeks}.json").write_bytes(r.stdout)
        return True
    except (OSError, subprocess.SubprocessError, RuntimeError) as e:
        print(f"PULA shard {indeks} na {m['ssh']}: {e}", file=log, flush=True)
        return False


def uruchom(argv: list[str], cwd: Path) -> int:
    return subprocess.run(argv, cwd=cwd).returncode


def main(argv: list[str]) -> int:
    liczba = 4
    if argv[:1] == ["--liczba"] and len(argv) >= 2:
        try:
            liczba = int(argv[1])
        except ValueError:
            liczba = 0
        argv = argv[2:]
    if not argv or argv[0] != "--" or liczba < 1:
        print("pula_mutantow: uzycie: [--liczba N>=1] -- <argumenty mutanty_zmian.py>", file=sys.stderr)
        return 64
    reszta = argv[1:]
    mutanty = [sys.executable, "tools/mutanty_zmian.py", *reszta]
    baza = reszta[reszta.index("--baza") + 1] if "--baza" in reszta[:-1] else "origin/main"
    try:
        korzen = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        zdalne = wczytaj_pule(Path(_git(korzen, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
                              / "monolynx" / "pula.json")
        potrzebne = zmienia_zrodla_mutmuta(korzen, baza)
        if potrzebne:
            sha = _git(korzen, "rev-parse", "HEAD").strip()
            dane = paczka(korzen)
            # poza kopia robocza: plik nieśledzony zmienilby skrot diffu bramki
            dowody = Path(tempfile.mkdtemp(prefix="pula-dowody-"))
    except BladPuli as e:
        print(f"pula_mutantow: {e}", file=sys.stderr)
        return 64
    except (OSError, RuntimeError, KeyError, ValueError) as e:
        print(f"pula_mutantow: brak dowodu: {e}", file=sys.stderr)
        return 2
    if not potrzebne:
        print("PULA: diff nie dotyka zrodel mutmuta - bez statystyk", flush=True)
        return uruchom(mutanty, korzen)
    przydzial = plan(liczba, zdalne)

    def jeden(indeks: int) -> tuple[bool, str]:
        """(czy jest dowod, gdzie policzony)."""
        m = przydzial[indeks]
        with open(dowody / f"shard-{indeks}.log", "w", encoding="utf-8") as log:
            if m is not None:
                if shard_zdalny(korzen, m, sha, dane, indeks, liczba, dowody, log):
                    return True, m["ssh"]
                print(f"PULA shard {indeks}: {m['ssh']} zawiodl - licze lokalnie", flush=True)
            return shard_lokalny(korzen, sha, dane, indeks, liczba, dowody, log), "lokalnie"

    with ThreadPoolExecutor(max_workers=liczba) as ex:
        wyniki = list(ex.map(jeden, range(liczba)))
    for i, (ok, gdzie) in enumerate(wyniki):
        print(f"PULA shard {i}: {'ok' if ok else 'BRAK DOWODU'} ({gdzie}) log={dowody}/shard-{i}.log", flush=True)
    if not all(ok for ok, _ in wyniki):
        return 2
    if uruchom([sys.executable, "tools/statystyki_mutmuta.py", "scal", "--katalog-dowodow", str(dowody)], korzen) != 0:
        return 2
    return uruchom([*mutanty, "--katalog-dowodow", str(dowody)], korzen)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
