-- Engineer-managed support contacts shown to logged-in users on the in-app
-- error screen ("who do I contact when something breaks?"). The engineer
-- curates the list; it is NOT shown on the public login page and there is no
-- public endpoint, so no admin PII is exposed pre-auth.
--
-- The list IS exactly what's displayed (no active flag) -- the engineer adds /
-- edits / removes rows directly. Seeded once from whoever currently holds the
-- super_admin / engineer roles in THIS database (so dev seeds dev's people and
-- prod seeds prod's); the engineer can then edit them.
BEGIN;

CREATE TABLE IF NOT EXISTS support_contacts (
    support_contact_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    role_label         varchar(100) NOT NULL,
    name               varchar(255) NOT NULL,
    email              varchar(255) NOT NULL,
    sort_order         integer NOT NULL DEFAULT 0,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- One-time seed from current role holders (only when the table is empty, so a
-- re-run never duplicates). first_name+last_name, falling back to the email.
INSERT INTO support_contacts (role_label, name, email, sort_order)
SELECT 'Super Admin',
       COALESCE(NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), ''), u.email),
       u.email, 1
FROM users u
JOIN user_roles ur ON ur.user_id = u.user_id
JOIN roles r ON r.role_id = ur.role_id
WHERE r.role_name = 'super_admin'
  AND NOT EXISTS (SELECT 1 FROM support_contacts)
ORDER BY u.user_id
LIMIT 1;

INSERT INTO support_contacts (role_label, name, email, sort_order)
SELECT 'Engineer',
       COALESCE(NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), ''), u.email),
       u.email, 2
FROM users u
JOIN user_roles ur ON ur.user_id = u.user_id
JOIN roles r ON r.role_id = ur.role_id
WHERE r.role_name = 'engineer'
  AND NOT EXISTS (SELECT 1 FROM support_contacts WHERE role_label = 'Engineer')
ORDER BY u.user_id
LIMIT 1;

COMMIT;
