"""Fence job workers with an additive, per-claim random lease identity.

IMPACT: one nullable GUID column; existing job data and RLS policies stay intact.
PREFLIGHT: legacy rows retain NULL; no historical lease identity is inferred.
NORMALIZATION: none. Retry counters, schedules, backoff and worker names retain
their existing meaning. Each new claim writes a fresh UUID, retained afterward.
POSTCONDITION: renewed/completed/failed writes can match the original claim even
after rollback, manual retry, worker-name reuse or a repeated clock value.
ROLLBACK/REPAIR: quiesce workers and keep this additive column for code rollback.
Dropping a populated token would erase fencing evidence, so downgrade refuses;
no token is cleared or invented by this migration. Export/resolve any retained
evidence only through a separately reviewed repair before destructive downgrade.
"""

import sqlalchemy as sa

from alembic import op
from app.models.base import GUID

revision = "b2e4f6a8c0d3"
down_revision = "8d1f3a5c7e90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("lease_token", GUID(), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        # Hold the same write exclusion through the check and schema change;
        # inspect every tenant even if this connection inherited an org GUC.
        op.execute(sa.text('LOCK TABLE "jobs" IN ACCESS EXCLUSIVE MODE'))
        op.execute(sa.text("SET LOCAL app.current_org = ''"))
    elif connection.dialect.name == "sqlite":
        # Acquire SQLite's writer reservation without modifying any row.
        op.execute(sa.text('UPDATE "jobs" SET lease_token = lease_token WHERE 1 = 0'))
    if connection.scalar(sa.text('SELECT count(*) FROM "jobs" WHERE lease_token IS NOT NULL')):
        raise RuntimeError("Job lease retention refusal: per-claim fencing evidence exists")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("lease_token")
