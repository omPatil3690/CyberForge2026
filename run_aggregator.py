#!/usr/bin/env python3
"""
run_aggregator.py — Entry point for the CyberForge Aggregation Layer API server.

Starts the FastAPI application using Uvicorn on 0.0.0.0:8000.

Usage::

    python run_aggregator.py

Or with a custom host/port::

    uvicorn aggregator.api.main:app --host 0.0.0.0 --port 8000
"""

import os
import sys
from pathlib import Path

# Auto-re-execute using the project .venv if run with system Python
_PROJECT_ROOT = Path(__file__).resolve().parent
_VENV_PYTHON = _PROJECT_ROOT / ".venv" / "bin" / "python"
if _VENV_PYTHON.exists() and sys.executable != str(_VENV_PYTHON):
    os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON)] + sys.argv)

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "aggregator.api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
