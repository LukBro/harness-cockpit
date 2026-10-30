"""Konfiguracja: zmienna srodowiskowa ma pierwszenstwo nad plikiem (BRO-98)."""
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COCKPIT_", env_file=".env")

    # W produkcji PostgreSQL. SQLite tylko w testach szkieletu.
    database_url: str = "postgresql+asyncpg://cockpit:cockpit@localhost/cockpit"
    runner_offline_after_s: int = 60   # prog z BRO-109, w konfiguracji a nie w kodzie
    # Co ile sekund kokpit szuka runow runnerow offline (HAR-37; env COCKPIT_RUNNER_SWEEP_S).
    # ge=1: 0 oznaczaloby petle bez przerwy.
    runner_sweep_s: int = Field(default=10, ge=1)
    # Ile dni jest wazna sesja (HAR-44; env COCKPIT_SESJA_WAZNA_DNI). ge=1: 0 wygasalaby od razu.
    sesja_wazna_dni: int = Field(default=30, ge=1)
    # Ciasteczko sesji z flaga Secure (HAR-48; env COCKPIT_CIASTECZKO_SECURE). Lokalny development
    # po http://localhost ustawia COCKPIT_CIASTECZKO_SECURE=0, inaczej przegladarka nie odesle ciasteczka.
    ciasteczko_secure: bool = True
    # Blokada logowania (HAR-46; env COCKPIT_LOGOWANIE_*). ge=1: 0 blokowaloby od pierwszej proby
    # albo okno liczylo od zera.
    logowanie_limit_prob: int = Field(default=5, ge=1)            # nieudane proby pary (adres, IP)
    logowanie_okno_s: int = Field(default=900, ge=1)              # okno liczenia prob pary
    logowanie_blokada_s: int = Field(default=900, ge=1)           # czas blokady (para i globalna)
    logowanie_limit_globalny: int = Field(default=50, ge=1)       # nieudane proby adresu z dowolnych IP
    logowanie_okno_globalne_s: int = Field(default=3600, ge=1)    # okno limitu globalnego
    # Zapytania kokpitu do runnera (HAR-49; env COCKPIT_RUNNER_ZAPYTANIE_TIMEOUT_S,
    # COCKPIT_RUNNER_ODPOWIEDZ_MAX_KB). gt/ge: 0 czekaloby bez sensu albo odrzucalo wszystko.
    runner_zapytanie_timeout_s: float = Field(default=15, gt=0)
    runner_odpowiedz_max_kb: int = Field(default=256, ge=1)
    # None = ustal z progu; po konstrukcji zawsze int (walidator nizej)
    heartbeat_co_s: int | None = None
    host: str = "127.0.0.1"            # faza testowa; publicznie za Caddym (BRO-76)
    port: int = 8765

    @model_validator(mode="after")
    def _domyslny_heartbeat(self) -> "Settings":
        if self.heartbeat_co_s is None:
            # max(1, ..): prog < 4 s dalby 0, czyli petle bez przerwy
            self.heartbeat_co_s = max(1, self.runner_offline_after_s // 4)
        if self.heartbeat_co_s >= self.runner_offline_after_s:
            raise ValueError(
                f"heartbeat_co_s ({self.heartbeat_co_s}) musi byc mniejszy niz "
                f"runner_offline_after_s ({self.runner_offline_after_s}): inaczej runner "
                "bylby offline miedzy meldunkami"
            )
        return self


settings = Settings()
