"""
Model registry.

Alembic compares `Base.metadata` against the live database to work out what
changed. A model class only appears in that metadata once its module has been
imported, so every model must be imported here -- a model that isn't listed is
a table Alembic will silently never create.
"""

from app.db.base import Base
from app.db.models.catalogue import Product
from app.db.models.review import ReviewRecord
from app.db.models.routine import Routine, routine_products
from app.db.models.scan_log import ScanLog
from app.db.models.user import SafetyAnswerChange, User

__all__ = [
    "Base",
    "Product",
    "ReviewRecord",
    "Routine",
    "SafetyAnswerChange",
    "ScanLog",
    "User",
    "routine_products",
]