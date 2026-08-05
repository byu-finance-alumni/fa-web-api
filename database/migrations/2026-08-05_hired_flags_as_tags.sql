-- Register the two "has already hired one of our students" flags as tags.
--
-- #629 turned the nine WILLINGNESS flags into derived tags so they render in the
-- profile header for every role. The two hiring facts were left out because they
-- are not willingness -- they record something the alum has already done.
--
-- That left them visible ONLY inside the editor-only Tags tab. When the
-- "Ways to get involved" panel was removed (Jake, 2026-08-05: "the tags already
-- show up in their header"), these two would have become invisible to
-- view-only staff and professors -- the exact bug #629 was filed to end, brought
-- back by a layout change. Jake's call: make them tags too.
--
-- Descriptive rows only. The derived tags are matched by NAME against the
-- engagement flags rather than through `alumni_tags`, so nothing here is
-- load-bearing -- but a name in the app's canonical map that is missing from the
-- lookup table is the kind of drift that makes the next person distrust both.
--
-- Additive and idempotent: inserts nothing that already exists, deletes nothing,
-- and touches no alumni row.

BEGIN;

INSERT INTO tags (tag_name, tag_description)
SELECT v.tag_name, v.tag_description
FROM (VALUES
    ('Hired a Finance Intern', 'Has hired a BYU finance intern. Backed by alumni_program_engagement.hired_finance_intern.'),
    ('Hired a Finance Grad',   'Has hired a BYU finance graduate full-time. Backed by alumni_program_engagement.hired_finance_full_time.')
) AS v(tag_name, tag_description)
WHERE NOT EXISTS (
    SELECT 1 FROM tags t WHERE t.tag_name = v.tag_name
);

COMMIT;
