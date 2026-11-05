"""Actor-bound planning create receipts, additive and evidence preserving.

IMPACT: a tenant-scoped receipt table; existing assignments/intents are unchanged.
PREFLIGHT: no legacy command identity or original result is inferred.
NORMALIZATION: no historical planning, notice or calendar data is rewritten.
POSTCONDITION: one immutable outcome per org/actor/key, with RLS/model parity.
ROLLBACK: keep additive schema during code rollback. Populated downgrade refuses
receipt deletion; this introduces no automatic retention/remote-cleanup policy.
"""

import sqlalchemy as sa
from alembic import op

from app.models.base import GUID

revision = "8d1f3a5c7e90"
down_revision = "f0b2d4e6a8c1"
branch_labels = None
depends_on = None
TENANT_TABLES = ("planning_commands",)


def upgrade() -> None:
    op.create_table(
        "planning_commands",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("org_id", GUID(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_user_id", GUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("command_key", sa.String(120), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("assignment_id", GUID(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("org_id", "actor_user_id", "command_key", name="uq_planning_command_identity"),
    )
    op.create_index("ix_planning_commands_org_id", "planning_commands", ["org_id"])
    if op.get_bind().dialect.name == "postgresql":
        predicate = (
            "current_setting('app.current_org', true) IS NULL "
            "OR current_setting('app.current_org', true) = '' "
            "OR org_id::text = current_setting('app.current_org', true)"
        )
        op.execute(sa.text('ALTER TABLE "planning_commands" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text('ALTER TABLE "planning_commands" FORCE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'CREATE POLICY tenant_isolation ON "planning_commands" USING ({predicate}) WITH CHECK ({predicate})'))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # A migration connection must inspect every tenant before refusing a
        # destructive downgrade, even if its inherited GUC selected one org.
        op.execute(sa.text("SET LOCAL app.current_org = ''"))
    count = op.get_bind().scalar(sa.text('SELECT count(*) FROM "planning_commands"'))
    if count:
        raise RuntimeError("Planning receipt retention refusal: committed command evidence exists")
    op.drop_table("planning_commands")
