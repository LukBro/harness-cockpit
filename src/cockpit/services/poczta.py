"""Poczta SMTP kokpitu (HAR-91): stan konfiguracji, wysylka i tlumaczenie bledow.

Blad serwera tlumaczymy po TYPIE wyjatku, nigdy przez `str(exc)`: tresc wyjatku biblioteki
moze zawierac haslo albo odpowiedz serwera. Do logu idzie tylko nazwa typu.
"""
from __future__ import annotations

import asyncio
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import getaddresses

from cockpit.services.errors import BladPoczty
from cockpit.settings import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StanPoczty:
    skonfigurowana: bool
    powod: str | None   # None tylko gdy skonfigurowana


def _brak(zmienna: str) -> StanPoczty:
    return StanPoczty(False, f"poczta wyłączona: ustaw zmienną {zmienna}")


def stan() -> StanPoczty:
    """Czy da sie wyslac list; nie laczy sie z serwerem."""
    if not settings.smtp_host:
        return _brak("COCKPIT_SMTP_HOST")
    if not settings.smtp_nadawca:
        return _brak("COCKPIT_SMTP_NADAWCA")
    if settings.smtp_uzytkownik and settings.smtp_haslo is None:
        return _brak("COCKPIT_SMTP_HASLO")
    return StanPoczty(True, None)


def _wyslij_blokujaco(wiadomosc: EmailMessage, host: str, do: str) -> None:
    # UnicodeError (podklasa ValueError) lapiemy waskim except wokol konkretnej instrukcji:
    # host (IDNA w create_connection) i dane logowania (ASCII w AUTH) maja osobne komunikaty.
    try:
        smtp = smtplib.SMTP(host, settings.smtp_port, timeout=settings.smtp_timeout_s)
    except UnicodeError:
        raise BladPoczty("adres serwera poczty jest nieprawidłowy: sprawdź COCKPIT_SMTP_HOST") from None
    with smtp:
        if settings.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_uzytkownik and settings.smtp_haslo is not None:
            try:
                smtp.login(settings.smtp_uzytkownik, settings.smtp_haslo.get_secret_value())
            except UnicodeError:
                raise BladPoczty(
                    "dane logowania do poczty muszą być w ASCII: sprawdź COCKPIT_SMTP_UZYTKOWNIK i COCKPIT_SMTP_HASLO"
                ) from None
        smtp.send_message(wiadomosc, to_addrs=[do])


def _adres_zalogowanego_poprawny(do: str) -> bool:
    # Definicja biblioteki: EmailMessage odrzuca naglowek, gdy len(value.splitlines()) > 1,
    # czyli takze przy \x0b \x0c \x1c \x1d \x1e \x85    , nie tylko CR/LF.
    # Pusty tekst ma 0 linii, wiec != 1 odrzuca tez pusty adres.
    if len(do.splitlines()) != 1:
        return False
    # Separator linii na brzegu (u@x\x0b) daje jedna linie, ale trafilby do koperty SMTP.
    if do != do.strip():
        return False
    # Jedyny poprawny wynik: jeden adres bez nazwy wyswietlanej i bez dodatkow
    # ("a@b, c@d" -> 2 wpisy, "Jan <a@b>" -> nazwa niepusta, " " -> brak wpisow).
    return getaddresses([do]) == [("", do)]


_ADRES = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"
# `adres` albo `Nazwa <adres>`; nazwa bez znakow specjalnych RFC (nawiasy, cudzyslow, przecinek...).
_NADAWCA = re.compile(rf"(?:{_ADRES}|[\w .'-]+ <{_ADRES}>)")
_NADAWCA_MAX_DLUGOSC = 254


def _adres_nadawcy_poprawny(nadawca: str) -> bool:
    """Wlasna regula, niezalezna od wersji Pythona i od parsera naglowkow (ten rzuca roznie
    zaleznie od wersji, a na zagniezdzonych nawiasach RecursionError): parsera nie wolamy wcale."""
    if len(nadawca) > _NADAWCA_MAX_DLUGOSC:   # odcina tez "(" * 500 przed wyrazeniem
        return False
    if len(nadawca.splitlines()) != 1 or nadawca != nadawca.strip():
        return False
    return _NADAWCA.fullmatch(nadawca) is not None


async def wyslij(do: str, temat: str, tresc: str) -> None:
    """Wysyla jeden list; kazda awaria SMTP/sieci konczy sie `BladPoczty` bez tresci wyjatku."""
    obecny = stan()
    if not obecny.skonfigurowana:
        raise BladPoczty(obecny.powod or "poczta wyłączona")
    host = settings.smtp_host
    assert host is not None   # stan() sprawdzil
    if not _adres_zalogowanego_poprawny(do):
        raise BladPoczty("adres odbiorcy listu jest nieprawidłowy")
    if not _adres_nadawcy_poprawny(settings.smtp_nadawca or ""):
        raise BladPoczty("adres nadawcy jest nieprawidłowy: sprawdź COCKPIT_SMTP_NADAWCA")
    wiadomosc = EmailMessage()
    # Parser naglowkow biblioteki na zlej wartosci rzuca: ValueError (wiele linii),
    # AttributeError (InvalidHeaderDefect bez 'all_defects') i IndexError (pusty token).
    try:
        wiadomosc["From"] = settings.smtp_nadawca
    except (ValueError, AttributeError, IndexError):
        raise BladPoczty("adres nadawcy jest nieprawidłowy: sprawdź COCKPIT_SMTP_NADAWCA") from None
    try:
        wiadomosc["To"] = do
    except (ValueError, AttributeError, IndexError):
        raise BladPoczty("adres odbiorcy listu jest nieprawidłowy") from None
    wiadomosc["Subject"] = temat
    wiadomosc.set_content(tresc)
    try:
        await asyncio.to_thread(_wyslij_blokujaco, wiadomosc, host, do)
    except smtplib.SMTPAuthenticationError as exc:
        _zapisz(exc)
        raise BladPoczty("serwer poczty odrzucił logowanie: sprawdź COCKPIT_SMTP_UZYTKOWNIK i COCKPIT_SMTP_HASLO") from None
    except ConnectionRefusedError as exc:
        _zapisz(exc)
        raise BladPoczty("serwer poczty odmówił połączenia: sprawdź COCKPIT_SMTP_HOST i COCKPIT_SMTP_PORT") from None
    except TimeoutError as exc:
        _zapisz(exc)
        raise BladPoczty("serwer poczty nie odpowiedział w limicie czasu (COCKPIT_SMTP_TIMEOUT_S)") from None
    except smtplib.SMTPException as exc:   # przed OSError: SMTPException jest jego podklasa
        _zapisz(exc)
        raise BladPoczty("serwer poczty odrzucił wysyłkę listu") from None
    except OSError as exc:   # m.in. socket.gaierror (nieznany host), blad TLS
        _zapisz(exc)
        raise BladPoczty("nie udało się połączyć z serwerem poczty") from None


def _zapisz(exc: BaseException) -> None:
    logger.warning("wysylka poczty nie powiodla sie: %s", type(exc).__name__)
