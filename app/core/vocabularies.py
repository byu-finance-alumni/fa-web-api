"""Controlled-vocabulary categories (#82).

The set of dropdown vocabularies stored in the editable ``vocabulary_terms``
table. These string values are the stable ``vocabulary_terms.category`` contract,
seeded by ``database/migrations/2026-06-16_vocabulary_terms.sql``.

Only the categories that were previously hardcoded constants or free text live
here. ``tag`` and ``status_label`` are intentionally NOT included — they keep
their own ``tags`` / ``status_labels`` tables and join semantics; migrating their
management here is a separate follow-up.
"""

from enum import StrEnum


class VocabularyCategory(StrEnum):
    INDUSTRY = "industry"
    EVENT_TYPE = "event_type"
    ATTENDANCE_STATUS = "attendance_status"
    INTERACTION_TYPE = "interaction_type"
