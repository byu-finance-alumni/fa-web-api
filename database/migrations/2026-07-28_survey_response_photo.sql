-- Survey "confirm your info" submissions may now include a NEW profile photo the
-- alum uploads. It is staged (not applied) alongside the field edits, under the
-- headshots bucket at a `survey-pending/<survey_response_id>` key, until an admin
-- reviews the response. `staged_photo_path` records that staging location; on
-- apply the image becomes the alum's real headshot, on reject it's discarded.
-- Idempotent so it is safe to re-run.
ALTER TABLE survey_responses ADD COLUMN IF NOT EXISTS staged_photo_path varchar(255);
