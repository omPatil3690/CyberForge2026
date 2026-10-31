"""Add company ownership to tenants.

Revision ID: 002_company_tenant_relationship
Revises: 001_initial_schema
Create Date: 2026-11-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "002_company_tenant_relationship"
down_revision: Union[str, None] = "001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO companies (id, name) VALUES (1, 'CyberForge Company')"
    )
    op.execute(
        "SELECT setval(pg_get_serial_sequence('companies', 'id'), "
        "(SELECT MAX(id) FROM companies), true)"
    )

    op.add_column(
        "tenants",
        sa.Column("company_id", sa.Integer(), nullable=True),
    )
    op.execute("UPDATE tenants SET company_id = 1")
    op.alter_column("tenants", "company_id", nullable=False)
    op.create_foreign_key(
        "fk_tenants_company_id_companies",
        "tenants",
        "companies",
        ["company_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("idx_tenants_company", "tenants", ["company_id"])


def downgrade() -> None:
    op.drop_index("idx_tenants_company", table_name="tenants")
    op.drop_constraint(
        "fk_tenants_company_id_companies",
        "tenants",
        type_="foreignkey",
    )
    op.drop_column("tenants", "company_id")
    op.drop_table("companies")
