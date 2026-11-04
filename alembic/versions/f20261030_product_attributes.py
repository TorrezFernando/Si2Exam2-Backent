"""Structured garment attributes; existing products remain valid."""
from alembic import op
import sqlalchemy as sa

revision = 'f20261030_attributes'
down_revision = 'f20261030_enterprise'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('products') as batch:
        batch.add_column(sa.Column('weight_grams', sa.Float(), nullable=True))
        batch.add_column(sa.Column('fabric_type', sa.String(100), nullable=True))
        batch.add_column(sa.Column('design', sa.String(255), nullable=True))


def downgrade():
    with op.batch_alter_table('products') as batch:
        for name in ('design', 'fabric_type', 'weight_grams'):
            batch.drop_column(name)
