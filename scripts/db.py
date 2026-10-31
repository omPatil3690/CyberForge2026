"""
Database connection and environment utilities for CyberForge.
Automatically loads environment variables from .env file if present.
"""

import os
from pathlib import Path

try:
    import psycopg
except ImportError:
    psycopg = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


def load_dotenv(path: Path = ENV_FILE) -> None:
    """Load key-value pairs from .env into os.environ if not already set."""
    if not path.is_file():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = val


# Load .env immediately upon import
load_dotenv()


def get_db_config() -> dict:
    """Retrieve normalized PostgreSQL database configuration."""
    return {
        "dbname": os.getenv("DB_NAME", "cyberforge"),
        "user": os.getenv("DB_USER", os.getenv("USER", "postgres")),
        "password": os.getenv("DB_PASSWORD", ""),
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", 5432)),
    }


def get_connection_info(custom_args=None) -> str:
    """Format connection string for psycopg from args or environment."""
    config = get_db_config()
    dbname = getattr(custom_args, "db_name", None) or config["dbname"]
    user = getattr(custom_args, "db_user", None) or config["user"]
    password = getattr(custom_args, "db_password", None) or config["password"]
    host = getattr(custom_args, "db_host", None) or config["host"]
    port = getattr(custom_args, "db_port", None) or config["port"]

    conn_str = f"dbname={dbname} user={user} host={host} port={port}"
    if password:
        conn_str += f" password={password}"
    return conn_str


def get_connection(custom_args=None):
    """Open and return a PostgreSQL connection."""
    if psycopg is None:
        raise RuntimeError("psycopg is not installed. Run `.venv/bin/pip install 'psycopg[binary]'`")
    conn_info = get_connection_info(custom_args)
    return psycopg.connect(conn_info)
