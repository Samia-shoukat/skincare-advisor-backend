"""add products, routines, routine_products

Revision ID: b7d2e4f81a93
Revises: a3f1c9d24e70
Create Date: 2026-09-21 14:00:00.000000

SRS 6.2 Product and Routine. FR-REC-005, FR-SUB-005, DR-005.

routine_products.product_id is ON DELETE RESTRICT: a product any routine
refers to cannot be hard-deleted (DR-005). Deactivate it instead.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b7d2e4f81a93'
down_revision: Union[str, Sequence[str], None] = 'a3f1c9d24e70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


JSONType = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite')


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('products',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('brand', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('price_tier', sa.Enum('BUDGET', 'PREMIUM', name='product_tier'), nullable=False),
    sa.Column('ingredients', JSONType, nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('catalogue_version', sa.String(length=32), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_products_is_active'), 'products', ['is_active'], unique=False)

    op.create_table('routines',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('user_auth_id', sa.String(length=255), nullable=False),
    sa.Column('scan_log_id', sa.String(length=36), nullable=False),
    sa.Column('matrix_version', sa.String(length=32), nullable=False),
    sa.Column('catalogue_version', sa.String(length=32), nullable=True),
    sa.Column('steps', JSONType, nullable=False),
    sa.Column('omitted_steps', JSONType, nullable=True),
    sa.Column('missing_tiers', JSONType, nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['scan_log_id'], ['scan_logs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_auth_id'], ['users.auth_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('scan_log_id')
    )
    op.create_index(op.f('ix_routines_created_at'), 'routines', ['created_at'], unique=False)
    op.create_index(op.f('ix_routines_user_auth_id'), 'routines', ['user_auth_id'], unique=False)

    op.create_table('routine_products',
    sa.Column('routine_id', sa.String(length=36), nullable=False),
    sa.Column('product_id', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['routine_id'], ['routines.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('routine_id', 'product_id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('routine_products')
    op.drop_index(op.f('ix_routines_user_auth_id'), table_name='routines')
    op.drop_index(op.f('ix_routines_created_at'), table_name='routines')
    op.drop_table('routines')
    op.drop_index(op.f('ix_products_is_active'), table_name='products')
    op.drop_table('products')
    sa.Enum(name='product_tier').drop(op.get_bind(), checkfirst=True)
