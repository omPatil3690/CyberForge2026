"""
aggregator — CyberForge2026 Core Aggregation Layer

This package implements the Multi-Tenant Aggregation Layer that sits between
the PostgreSQL OLTP database and the dashboard frontend.

Architecture:
  PostgreSQL (OLTP, write-only for dashboard)
    → Incremental Sync Worker (every 5s watermark poll via updated_at)
    → DuckDB in-process OLAP replica
    → FastAPI REST API
    → Dashboard

Modules:
  config        — Settings loaded from .env
  database      — PostgreSQL connection pool + DuckDB store
  sync          — Incremental watermark sync worker (background thread)
  api           — FastAPI application, routers, and dependencies
  models        — Pydantic response schemas
"""

__version__ = "1.0.0"
__author__ = "Sandesh Lavshetty & Antigravity (AI)"
