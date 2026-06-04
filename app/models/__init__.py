"""ORM models.

Importing this package registers every model on ``Base.metadata`` so that
string-based relationships resolve and tooling can discover the full mapping.
Add new model modules to the imports below as they're built.
"""

from app.models.alumni import Alumni
from app.models.data_source import DataSource
from app.models.user import Role, User, UserRole

__all__ = ["Alumni", "DataSource", "Role", "User", "UserRole"]
