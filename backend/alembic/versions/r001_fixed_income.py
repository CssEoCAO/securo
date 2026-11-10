"""fixed income (renda fixa) fields on assets + BCB index table

Fork-only migration (branch ratoeira). Revision ids of the fork start with
"r" so an upstream migration never collides; on rebase, point this file's
down_revision at upstream's new head.

Revision ID: r001
Revises: 107
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "r001"
down_revision = "107"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("fi_index", sa.String(10), nullable=True))
    op.add_column("assets", sa.Column("fi_rate", sa.Numeric(12, 6), nullable=True))
    op.add_column("assets", sa.Column("fi_payment", sa.String(12), nullable=True))
    op.add_column("assets", sa.Column("fi_tax", sa.String(12), nullable=True))
    op.add_column("assets", sa.Column("fi_tax_rate", sa.Numeric(6, 3), nullable=True))
    op.add_column(
        "assets",
        sa.Column(
            "fi_payout_account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL", name="fk_assets_fi_payout_account"),
            nullable=True,
        ),
    )
    op.create_table(
        "index_rates",
        sa.Column("series", sa.String(10), primary_key=True),
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column("value", sa.Numeric(14, 8), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("index_rates")
    for col in ("fi_payout_account_id", "fi_tax_rate", "fi_tax", "fi_payment", "fi_rate", "fi_index"):
        op.drop_column("assets", col)
