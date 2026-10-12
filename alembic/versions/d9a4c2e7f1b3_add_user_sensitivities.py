"""add user sensitivities

Revision ID: d9a4c2e7f1b3
Revises: b7d2e4f81a93
Create Date: 2026-10-12 12:00:00.000000

Sensitive skin and fragrance allergy, for product matching. Nullable: null
means not asked yet. Not part of onboarding_complete -- see the User model.
Adding nullable columns locks nothing and rewrites no rows.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9a4c2e7f1b3'
down_revision: Union[str, Sequence[str], None] = 'b7d2e4f81a93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('sensitive_skin', sa.Boolean(), nullable=True))
    op.add_column('users', sa.Column('fragrance_allergy', sa.Boolean(), nullable=True))
    op.add_column('users', sa.Column('sensitivities_set_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'sensitivities_set_at')
    op.drop_column('users', 'fragrance_allergy')
    op.drop_column('users', 'sensitive_skin')
