"""
Declarative base and shared helpers for every model.

Kept in its own file, separate from the models, for one practical reason:
Alembic's migration script needs to import `Base.metadata` to see the table
definitions. If Base lived inside `models/user.py`, that import would drag in
config, enums, and eventually a database connection -- all of which Alembic
does not need and some of which it cannot have yet.

One small file now saves a circular import later.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Every model in app/db/models inherits from this."""


def new_uuid() -> str:
    """
    Default for surrogate primary keys.

    A UUID rather than an auto-incrementing integer: sequential IDs leak how
    many scans the system has performed and let one user guess another's
    identifiers.
    """
    return str(uuid.uuid4())