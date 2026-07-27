"""Application settings.

Secrets are never read from the repo. They come from the environment, or from a
file whose path is given by a ``*_FILE`` env var (the Docker secret convention,
which keeps values out of the process environment and off `docker inspect`).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_secret(name: str) -> str | None:
    """Resolve a secret from ``$NAME_FILE`` (preferred) or ``$NAME``."""
    path = os.environ.get(f"{name}_FILE")
    if path:
        p = Path(path)
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    return os.environ.get(name)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOUSCHEF_", extra="ignore")

    # --- Paths -----------------------------------------------------------
    data_dir: Path = REPO_ROOT / "data"
    db_path: Path | None = None
    receipt_dir: Path | None = None

    # --- Behaviour -------------------------------------------------------
    # Blocks every outbound third-party call (NYT, Spoonacular, cloud VLM).
    # Default-on so a dev run can never quietly hit a real service or spend quota.
    no_external: bool = True
    debug: bool = False

    # --- Estimation tuning -----------------------------------------------
    # Half-life in days for confidence in an unverified level. Bulk staples drift
    # faster than countable items because every cook subtracts an estimate rather
    # than a known amount. These are config, not code, precisely so they can be
    # re-tuned against real use without a deploy.
    confidence_half_life_discrete_days: float = 45.0
    confidence_half_life_bulk_days: float = 18.0
    # Extra confidence lost each time a cook event debits an item.
    confidence_penalty_per_cook: float = 0.06
    # Below this confidence an item is a candidate for the pre-shop audit.
    audit_confidence_threshold: float = 0.72
    # Max items the pre-shop audit will ever ask you to physically check.
    audit_max_items: int = 10

    # --- Auth / crypto ---------------------------------------------------
    session_secret: str = Field(default="", repr=False)
    secret_key: str = Field(default="", repr=False)  # AES-GCM key for stored creds

    # --- Third-party -----------------------------------------------------
    spoonacular_key: str = Field(default="", repr=False)
    spoonacular_daily_budget: int = 140  # free tier is ~150/day; leave headroom
    vlm_api_key: str = Field(default="", repr=False)
    vlm_model: str = "claude-sonnet-5"

    @field_validator("data_dir", mode="after")
    @classmethod
    def _abs(cls, v: Path) -> Path:
        return v.expanduser().resolve()

    def model_post_init(self, _ctx: object) -> None:
        if self.db_path is None:
            object.__setattr__(self, "db_path", self.data_dir / "souschef.db")
        if self.receipt_dir is None:
            object.__setattr__(self, "receipt_dir", self.data_dir / "receipts")
        for key in ("session_secret", "secret_key", "spoonacular_key", "vlm_api_key"):
            if not getattr(self, key):
                found = _read_secret(f"SOUSCHEF_{key.upper()}")
                if found:
                    object.__setattr__(self, key, found)

    @property
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.db_path}"

    def require(self, field: str) -> str:
        """Fetch a secret, failing loudly rather than silently degrading."""
        value = getattr(self, field, "")
        if not value:
            raise RuntimeError(
                f"{field} is not configured. Set SOUSCHEF_{field.upper()} or "
                f"SOUSCHEF_{field.upper()}_FILE."
            )
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
