"""
aggregator/config.py — Application configuration for the CyberForge aggregation layer.

Loads all settings from environment variables (same .env file used by the
scripts/ utilities).  Supports both a full DATABASE_URL and individual
DB_* component variables; individual components take precedence when both
are present.

Usage:
    from aggregator.config import settings
    print(settings.duckdb_path)
"""

import os
from pathlib import Path


# ---------------------------------------------------------------------------
# .env loader (zero-dependency fallback; mirrors scripts/db.py behaviour)
# ---------------------------------------------------------------------------

def _load_dotenv(env_path: Path | None = None) -> None:
    """Load key-value pairs from a .env file into os.environ.

    Only sets variables that are *not* already present in the environment,
    so explicit environment variables always take precedence over .env values.

    Args:
        env_path: Explicit path to the .env file.  Defaults to the .env at
                  the project root (two levels above this file).
    """
    if env_path is None:
        # aggregator/config.py  →  aggregator/  →  project root
        env_path = Path(__file__).resolve().parent.parent / ".env"

    if not env_path.is_file():
        return

    with open(env_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = val


# Load .env as soon as the module is imported so that all subsequent
# os.getenv() calls resolve correctly.
_load_dotenv()


# ---------------------------------------------------------------------------
# Settings dataclass (pure stdlib, no pydantic-settings dependency required)
# ---------------------------------------------------------------------------

class Settings:
    """Centralised, immutable application configuration.

    All values are read once at import time.  Mutating this object at runtime
    has no effect on already-started components.

    Attributes:
        db_name:               PostgreSQL database name.
        db_user:               PostgreSQL username.
        db_password:           PostgreSQL password (may be empty string).
        db_host:               PostgreSQL host.
        db_port:               PostgreSQL port (integer).
        database_url:          Full PostgreSQL connection URL.  Constructed
                               from DB_* components when DATABASE_URL is absent.
        duckdb_path:           Filesystem path for the DuckDB file.
        sync_interval_seconds: How often the sync worker polls PostgreSQL.
        rate_limit_per_minute: Sliding-window rate limit applied per tenant.
    """

    def __init__(self) -> None:
        """Initialise settings from environment variables."""
        # --- PostgreSQL components ---
        self.db_name: str = os.getenv("DB_NAME", "cyberforge")
        self.db_user: str = os.getenv("DB_USER", os.getenv("USER", "postgres"))
        self.db_password: str = os.getenv("DB_PASSWORD", "")
        self.db_host: str = os.getenv("DB_HOST", "localhost")
        self.db_port: int = int(os.getenv("DB_PORT", "5432"))

        # Derive a full URL from components if DATABASE_URL is not set
        _pw_segment = f":{self.db_password}" if self.db_password else ""
        _default_url = (
            f"postgresql://{self.db_user}{_pw_segment}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )
        self.database_url: str = os.getenv("DATABASE_URL", _default_url)

        # --- DuckDB ---
        self.duckdb_path: str = os.getenv("DUCKDB_PATH", "./aggregator.duckdb")

        # --- Sync worker ---
        self.sync_interval_seconds: int = int(
            os.getenv("SYNC_INTERVAL_SECONDS", "5")
        )

        # --- Rate limiting ---
        self.rate_limit_per_minute: int = int(
            os.getenv("RATE_LIMIT_PER_MINUTE", "60")
        )
        self.enable_test_ui: bool = os.getenv("ENABLE_TEST_UI", "").lower() in {
            "1", "true", "yes"
        }

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def pg_conninfo(self) -> str:
        """Return a psycopg-compatible connection info string.

        Assembles a libpq keyword=value connection string from the individual
        DB_* component settings, which is safer than a URL when passwords
        contain special characters.

        Returns:
            A psycopg/libpq conninfo string, e.g.
            ``"dbname=cyberforge user=postgres host=localhost port=5432"``
        """
        parts = [
            f"dbname={self.db_name}",
            f"user={self.db_user}",
            f"host={self.db_host}",
            f"port={self.db_port}",
        ]
        if self.db_password:
            parts.append(f"password={self.db_password}")
        return " ".join(parts)

    def __repr__(self) -> str:  # pragma: no cover
        """Return a safe representation (password redacted)."""
        return (
            f"Settings(db={self.db_name!r}, host={self.db_host!r}, "
            f"port={self.db_port}, duckdb={self.duckdb_path!r}, "
            f"sync_interval={self.sync_interval_seconds}s, "
            f"rate_limit={self.rate_limit_per_minute}/min)"
        )


# ---------------------------------------------------------------------------
# Module-level singleton — import this everywhere
# ---------------------------------------------------------------------------

settings = Settings()
