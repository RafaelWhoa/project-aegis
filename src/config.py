"""
Project Aegis - Centralized Configuration (DACH Healthcare Market).
Reads environment variables with sensible local Docker defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DatabaseConfig:
    user: str = os.getenv("POSTGRES_USER", "aegis_admin")
    password: str = os.getenv("POSTGRES_PASSWORD", "aegis_secret_2026")
    host: str = os.getenv("POSTGRES_HOST", "localhost")
    port: str = os.getenv("POSTGRES_PORT", "5433")
    database: str = os.getenv("POSTGRES_DB", "hospital_aegis")

    @property
    def sqlalchemy_url(self) -> str:
        return f"postgresql+psycopg2://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


@dataclass(frozen=True)
class DachMarketDefaults:
    currency: str = "EUR"
    base_rate_eur: float = 4200.00  # Basisfallwert típico na Alemanha (G-DRG)
    consolidation_window_days: int = 30  # Fallzusammenführung (§ 2 FPV)
    bed_day_opportunity_margin_eur: float = 450.00  # Margem líquida de reposição eletiva / leito-dia


DB_CONFIG = DatabaseConfig()
DACH_DEFAULTS = DachMarketDefaults()
