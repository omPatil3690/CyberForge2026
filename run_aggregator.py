#!/usr/bin/env python3
"""
run_aggregator.py — Entry point for the CyberForge Aggregation Layer API server.

Starts the FastAPI application using Uvicorn on 0.0.0.0:8000.

Usage::

    python run_aggregator.py

Or with a custom host/port::

    uvicorn aggregator.api.main:app --host 0.0.0.0 --port 8000
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "aggregator.api.main:app",
        host="0.0.0.0",
        port=8000,
        # uvloop currently aborts during loop initialization on some
        # macOS/Python 3.14 combinations; the stdlib loop is reliable.
        loop="asyncio",
        reload=False,
        log_level="info",
    )
