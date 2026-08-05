-- #629 — fold the hand-applied "Mentor" / "Speaker" tags into the engagement
-- flags that now back them.
--
-- WHY
-- ---
-- The nine "ways to get involved" are now DERIVED tags: `tag=Mentor` resolves
-- to `alumni_program_engagement.mentor_willing`, not to an `alumni_tags` row.
-- That gives the mentor list one source of truth and makes withdrawal (an alum
-- answering NO next year) a single flag flip.
--
-- Two of the nine already existed as hand-applied tags, and the two stores had
-- ALREADY forked. On dev before this migration:
--
--     tag=Mentor  -> 39 people      mentor=1 -> 83 people      overlap: 16
--
-- i.e. two half-populated lists of mentors depending on which control you used.
-- Switching the tag filter to read the flag without this backfill would silently
-- drop the 23 people who were only ever tagged by hand. This migration lights up
-- their flag so they survive the switch and the two lists become one.
--
-- WHAT IT DOES NOT DO
-- -------------------
-- It does NOT delete the `alumni_tags` rows it reads. Nothing hand-applied is
-- removed; after this the rows are simply redundant (the flag is what search and
-- the profile read), and they can be swept later once this has been observed on
-- prod. Deleting real rows is not worth doing blind in the same change that
-- introduces the behaviour.
--
-- It does NOT touch the generic "Donor" tag. `piff_donor` is specifically the
-- Pay It Forward fund and is backed by the new, separate "PIFF Donor" tag, so
-- the broader hand-applied "Donor" label keeps its own independent meaning.
--
-- SAFETY
-- ------
-- Additive and idempotent: it only ever flips a flag false -> true, and only for
-- alumni who carry the matching tag today. Re-running selects nothing.

BEGIN;

-- 1. Alumni who hold the tag but have no engagement row at all yet — create one.
--    Every flag column is NOT NULL DEFAULT false, so an empty row means "no to
--    everything" and is a valid starting point.
INSERT INTO alumni_program_engagement (alumni_id)
SELECT DISTINCT at.alumni_id
FROM alumni_tags at
JOIN tags t ON t.tag_id = at.tag_id
WHERE t.tag_name IN ('Mentor', 'Speaker')
  AND NOT EXISTS (
      SELECT 1
      FROM alumni_program_engagement e
      WHERE e.alumni_id = at.alumni_id
  );

-- 2. Set the flag for everyone carrying the corresponding tag.
UPDATE alumni_program_engagement e
SET mentor_willing = true
WHERE e.mentor_willing = false
  AND EXISTS (
      SELECT 1
      FROM alumni_tags at
      JOIN tags t ON t.tag_id = at.tag_id
      WHERE at.alumni_id = e.alumni_id
        AND t.tag_name = 'Mentor'
  );

UPDATE alumni_program_engagement e
SET guest_speaker_willing = true
WHERE e.guest_speaker_willing = false
  AND EXISTS (
      SELECT 1
      FROM alumni_tags at
      JOIN tags t ON t.tag_id = at.tag_id
      WHERE at.alumni_id = e.alumni_id
        AND t.tag_name = 'Speaker'
  );

-- 3. Register the seven new involvement tag names in the `tags` lookup so the
--    vocabulary table lists all nine. The derived tags are matched by name
--    against the engagement flags rather than through `alumni_tags`, so these
--    rows are descriptive rather than load-bearing — but a name that exists in
--    the app's canonical TAGS tuple and not in the lookup table is exactly the
--    kind of drift that makes the next person distrust both.
INSERT INTO tags (tag_name, tag_description)
SELECT v.tag_name, v.tag_description
FROM (VALUES
    ('Women in Finance Mentor', 'Willing to mentor for Women in Finance. Backed by alumni_program_engagement.women_in_finance_mentor_willing.'),
    ('Event Helper',            'Willing to help at an event. Backed by alumni_program_engagement.help_at_event_willing.'),
    ('NetTrek Host',            'Willing to host a NetTrek visit. Backed by alumni_program_engagement.nettrek_host_willing.'),
    ('Finance Conference',      'Willing to take part in the finance conference. Backed by alumni_program_engagement.finance_conference_willing.'),
    ('Company Event Sponsor',   'Willing to sponsor a company event. Backed by alumni_program_engagement.company_event_sponsor_willing.'),
    ('Case Competition Host',   'Willing to host a case competition. Backed by alumni_program_engagement.case_competition_host_willing.'),
    ('PIFF Donor',              'Willing to donate to the Pay It Forward fund. Backed by alumni_program_engagement.piff_donor.')
) AS v(tag_name, tag_description)
WHERE NOT EXISTS (
    SELECT 1 FROM tags t WHERE t.tag_name = v.tag_name
);

COMMIT;
