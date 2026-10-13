"""add product display and ranking fields

Revision ID: e5b1f7c3a9d2
Revises: d9a4c2e7f1b3
Create Date: 2026-10-12 16:00:00.000000

image_url, price, fragrance, inci and skin_types on products (ADR-030). All
nullable: the curated products predate them, and none is a safety input.
Adding nullable columns locks nothing and rewrites no rows.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5b1f7c3a9d2'
down_revision: Union[str, Sequence[str], None] = 'd9a4c2e7f1b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('products', sa.Column('image_url', sa.String(length=1000), nullable=True))
    op.add_column('products', sa.Column('price', sa.String(length=64), nullable=True))
    op.add_column('products', sa.Column('fragrance', sa.Boolean(), nullable=True))
    op.add_column('products', sa.Column('inci', sa.Text(), nullable=True))
    op.add_column('products', sa.Column('skin_types', sa.String(length=200), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('products', 'skin_types')
    op.drop_column('products', 'inci')
    op.drop_column('products', 'fragrance')
    op.drop_column('products', 'price')
    op.drop_column('products', 'image_url')
