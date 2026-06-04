"""ORM models.

Importing this package registers every model on ``Base.metadata`` so that
string-based relationships resolve and tooling can discover the full mapping.
Add new model modules to the imports below as they're built.
"""

from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.data_source import DataSource
from app.models.event import Event, EventAttendance
from app.models.user import Role, User, UserRole

__all__ = [
    "Alumni",
    "AuditLog",
    "DataSource",
    "Event",
    "EventAttendance",
    "Role",
    "User",
    "UserRole",
]
