"""quota keyed off org plan, not user role (WO-47)

The system usage-limits matrix was keyed by the ACTING USER'S ROLE
(`role_policies.role`, values like "user_free"/"admin"), which is wrong for a
hybrid seats+usage commercial model — usage itself is metered ORG-WIDE
(`invoices_this_month`/`usage_count` always counted per-org, never per-user),
so the cap must key off the same dimension: the org's subscription `plan`
(`app.services.plans.PLANS` — trial/starter/pro/enterprise).

`role_policies` is dropped and replaced by `plan_policies` (same shape, new
key column `plan`). This is a GLOBAL, sysadmin-editable admin-override table,
never tenant data and never referenced by a foreign key — any existing
role-keyed override rows are semantically meaningless under the new model (a
role string like "user_free" is not a plan key), so this migration discards
them rather than attempting a lossy role→plan mapping; `access._get_or_seed`
lazily reseeds fresh, plan-keyed defaults (from `plans.Plan.monthly_invoice_
limit`/`monthly_upload_limit`) the next time each plan's row is read. No
financial data, no tenant data, nothing else references this table.

Revision ID: 01160bebbd7f
Revises: a3f556aac756
Create Date: 2026-07-28 16:37:11.064921
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app.models.base  # portable GUID type used by every table


revision: str = '01160bebbd7f'
down_revision: Union[str, None] = 'a3f556aac756'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('plan_policies',
    sa.Column('plan', sa.String(length=20), nullable=False),
    sa.Column('monthly_invoice_limit', sa.Integer(), nullable=False),
    sa.Column('monthly_upload_limit', sa.Integer(), nullable=False),
    sa.Column('id', app.models.base.GUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('plan', name='uq_plan_policy_plan')
    )
    # role_policies overrides don't map onto plan keys — see module docstring.
    op.drop_table('role_policies')


def downgrade() -> None:
    op.create_table('role_policies',
    sa.Column('role', sa.VARCHAR(length=20), nullable=False),
    sa.Column('monthly_invoice_limit', sa.INTEGER(), nullable=False),
    sa.Column('monthly_upload_limit', sa.INTEGER(), nullable=False),
    sa.Column('id', app.models.base.GUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('role', name=op.f('uq_role_policy_role'))
    )
    # plan_policies overrides likewise don't map back onto role keys.
    op.drop_table('plan_policies')
