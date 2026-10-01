"""Application settings.

Secrets are never read from the repo. They come from the environment, from a
file whose path is given by a ``*_FILE`` env var, or from the config directory,
which is where a local install keeps the ones it generated for itself. The
config directory is deliberately not the data directory: the data directory is
what a future cloud backend would sync, and a session key that syncs to a third
party is a session key you have given away.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

SECRET_FIELDS = ("session_secret", "secret_key", "spoonacular_key", "vlm_api_key")


def _default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "souschef"


def _read_secret(name: str, config_dir: Path) -> str | None:
    """Resolve a secret from ``$NAME_FILE``, then ``$NAME``, then the config dir."""
    path = os.environ.get(f"{name}_FILE")
    if path:
        p = Path(path)
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    from_env = os.environ.get(name)
    if from_env:
        return from_env
    local = config_dir / name.removeprefix("SOUSCHEF_").lower()
    if local.is_file():
        return local.read_text(encoding="utf-8").strip()
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOUSCHEF_", extra="ignore")

    data_dir: Path = REPO_ROOT / "data"
    config_dir: Path = Field(default_factory=_default_config_dir)
    db_path: Path | None = None
    storage_backend: str = "local"

    # Blocks every outbound third-party call (NYT, Spoonacular, cloud VLM).
    # Off by default now that this is an app someone runs deliberately; the
    # test suite turns it on so a test can never hit a real service.
    no_external: bool = False
    debug: bool = False
    # Skips sign-in and acts as the first active account. Only safe when
    # nothing but this machine can reach the server.
    single_user: bool = False
    log_level: str = "INFO"

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

    session_secret: str = Field(default="", repr=False)
    secret_key: str = Field(default="", repr=False)  # AES-GCM key for stored creds

    spoonacular_key: str = Field(default="", repr=False)
    spoonacular_daily_budget: int = 140  # free tier is ~150/day; leave headroom
    vlm_api_key: str = Field(default="", repr=False)
    vlm_model: str = "claude-sonnet-5"

    @field_validator("data_dir", "config_dir", mode="after")
    @classmethod
    def _abs(cls, v: Path) -> Path:
        return v.expanduser().resolve()

    def model_post_init(self, _ctx: object) -> None:
        if self.db_path is None:
            object.__setattr__(self, "db_path", self.data_dir / "souschef.db")
        for key in SECRET_FIELDS:
            if not getattr(self, key):
                found = _read_secret(f"SOUSCHEF_{key.upper()}", self.config_dir)
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
                f"{field} is not configured. Set SOUSCHEF_{field.upper()}, "
                f"SOUSCHEF_{field.upper()}_FILE, or put it in {self.config_dir / field}."
            )
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


def ensure_local_secrets() -> list[str]:
    """Generate the keys this install needs, if nobody has supplied them.

    A local install has no operator to hand it secrets, and refusing to start
    would be a setup step that buys nothing: the alternative to a generated key
    is not a better key, it is a `.env` file with the same value typed by hand.
    Returns the names of any that were created.
    """
    import secrets as stdlib_secrets

    settings = get_settings()
    generators = {
        "session_secret": lambda: stdlib_secrets.token_urlsafe(48),
        "secret_key": lambda: stdlib_secrets.token_hex(32),
    }
    created = []
    for field, generate in generators.items():
        if getattr(settings, field):
            continue
        settings.config_dir.mkdir(parents=True, exist_ok=True)
        settings.config_dir.chmod(0o700)
        value = generate()
        path = settings.config_dir / field
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o600)
        object.__setattr__(settings, field, value)
        created.append(field)
    return created
