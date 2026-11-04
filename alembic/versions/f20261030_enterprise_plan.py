"""Add the third subscription tier without changing existing subscriptions."""
from alembic import op
import sqlalchemy as sa

revision = 'f20261030_enterprise'
down_revision = 'f20261030'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("""INSERT INTO subscription_plans
        (name, code, max_branches, has_ai_reports, has_offline_sync, price, is_active)
        SELECT 'Plan Empresarial', 'enterprise', 30, true, true, 199.99, true
        WHERE NOT EXISTS (SELECT 1 FROM subscription_plans WHERE code = 'enterprise')"""))


def downgrade():
    # Preserve subscriptions and configured prices; hide the tier on rollback.
    op.execute(sa.text("UPDATE subscription_plans SET is_active = false WHERE code = 'enterprise'"))
