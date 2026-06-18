"""ORM models.

Importing this package registers every model on ``Base.metadata`` so that
string-based relationships resolve and tooling can discover the full mapping.
Add new model modules to the imports below as they're built.
"""

from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.crm import Attachment, FollowUpTask, Interaction, Survey
from app.models.data_source import DataSource
from app.models.duplicate import DuplicateCandidate
from app.models.employment import (
    CurrentEmployment,
    EducationHistory,
    EmploymentHistory,
)
from app.models.engagement import (
    AlumniEngagement,
    AlumniProgramEngagement,
    FinanceSocietyLeadership,
)
from app.models.event import Event, EventAttendance
from app.models.login_attempt import LoginAttempt
from app.models.login_event import LoginEvent
from app.models.tags import AlumniStatusLabel, AlumniTag, StatusLabel, Tag
from app.models.user import Role, User, UserRole
from app.models.vocabulary import VocabularyTerm

__all__ = [
    "Alumni",
    "AlumniContactInfo",
    "AlumniEngagement",
    "AlumniProgramEngagement",
    "AlumniStatusLabel",
    "AlumniTag",
    "Attachment",
    "AuditLog",
    "CurrentEmployment",
    "DataSource",
    "DuplicateCandidate",
    "EducationHistory",
    "EmploymentHistory",
    "Event",
    "EventAttendance",
    "FinanceSocietyLeadership",
    "FollowUpTask",
    "Interaction",
    "LoginAttempt",
    "LoginEvent",
    "Role",
    "StatusLabel",
    "Survey",
    "Tag",
    "User",
    "UserRole",
    "VocabularyTerm",
]
