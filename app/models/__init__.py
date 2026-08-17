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
from app.models.donation import Donation
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
from app.models.engineer_action import EngineerActionLog
from app.models.event import Event, EventAttendance
from app.models.login_attempt import LoginAttempt
from app.models.login_event import LoginEvent
from app.models.login_failure import LoginFailure
from app.models.maintenance import MaintenanceMode
from app.models.note import Note
from app.models.opportunity_link import OpportunityLink
from app.models.role_capability import RoleCapability
from app.models.survey_reset import SurveyResetLog
from app.models.survey_response import SurveyResponse
from app.models.survey_retirement import SurveyCampaignRetirement
from app.models.survey_schedule import SurveySchedule, SurveySendLog
from app.models.survey_send_config import SurveySendConfig
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
    "Donation",
    "DuplicateCandidate",
    "EducationHistory",
    "EmploymentHistory",
    "EngineerActionLog",
    "Event",
    "EventAttendance",
    "FinanceSocietyLeadership",
    "FollowUpTask",
    "Interaction",
    "LoginAttempt",
    "LoginEvent",
    "LoginFailure",
    "MaintenanceMode",
    "Note",
    "OpportunityLink",
    "Role",
    "RoleCapability",
    "StatusLabel",
    "Survey",
    "SurveyCampaignRetirement",
    "SurveyResetLog",
    "SurveyResponse",
    "SurveySchedule",
    "SurveySendConfig",
    "SurveySendLog",
    "Tag",
    "User",
    "UserRole",
    "VocabularyTerm",
]
