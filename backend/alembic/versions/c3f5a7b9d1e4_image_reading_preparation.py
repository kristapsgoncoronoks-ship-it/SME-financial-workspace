"""Record measured image preparation without inventing historical attempts.

IMPACT: two nullable Text columns; no source bytes, RLS or retention change.
PREFLIGHT/NORMALIZATION: none; historical preparation remains NULL.
POSTCONDITION: fenced workers can persist actual geometry on success or failure.
ROLLBACK: retain these additive columns for a code rollback. Dropping populated
preparation would erase measured provenance, so destructive downgrade refuses.
"""

import sqlalchemy as sa

from alembic import op

revision = "c3f5a7b9d1e4"
down_revision = "b2e4f6a8c0d3"
branch_labels = None
depends_on = None

TABLES = ("extraction_runs", "inbound_invoices")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("reading_preparation_json", sa.Text(), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(
            sa.text('LOCK TABLE "extraction_runs", "inbound_invoices" IN ACCESS EXCLUSIVE MODE')
        )
        # The existing migration/worker empty-org policy leg sees all tenants.
        # Keep FORCE RLS intact even for this non-bypass table owner.
        op.execute(sa.text("SET LOCAL app.current_org = ''"))
    for table in TABLES:
        if connection.scalar(
            sa.text(f'SELECT count(*) FROM "{table}" WHERE reading_preparation_json IS NOT NULL')
        ):
            raise RuntimeError(
                "Measured image reading preparation exists. Keep the additive columns for "
                "code rollback; do not erase recorded attempt provenance."
            )
    for table in reversed(TABLES):
        # Supported SQLite has native DROP COLUMN. Avoid rebuilding a capture
        # table and disturbing its existing composite/FK provenance.
        op.drop_column(table, "reading_preparation_json")
