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
    # None = ustal z progu; po konstrukcji zawsze int (walidator nizej)
    heartbeat_co_s: int | None = None
    host: str = "127.0.0.1"            # faza testowa; publicznie za Caddym (BRO-76)
    port: int = 8765

    @model_validator(mode="after")
    def _domyslny_heartbeat(self) -> "Settings":
        if self.heartbeat_co_s is None:
            # max(1, ..): prog < 4 s dalby 0, czyli petle bez przerwy
            self.heartbeat_co_s = max(1, self.runner_offline_after_s // 4)
        return self


settings = Settings()
