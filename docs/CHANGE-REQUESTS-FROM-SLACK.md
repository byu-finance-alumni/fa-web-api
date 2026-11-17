# Change requests pasted from Slack

> This file is identical in `fa-web-api/docs/` and `fa-web-app/docs/`. If you change one, change both.

Jake (the only engineer) pastes an email from the career office into Slack and tags @Claude.
Your job: understand it → ask → file issues → build → put it on **dev** → report back with the time it took.

## Talking to Jake

- **Short and plain.** No file paths, function names, PR numbers or test counts in your replies.
  Lead with what he needs to answer or check. One line per item.
- Write as "I", never "we" — he's a team of one.

## Step 0 — start the clock

Run `date -u +%FT%TZ` before anything else and keep the result as **START**.
Every time you stop to wait for Jake, write down when you stopped and when he replied, so you can
leave his reply time out of the working time.

## Step 1 — analyze, then ask (STOP after this step)

1. Split the email into separate changes. Look up the code for each one so you understand what it
   really involves.
2. Reply to Jake with:
   - a numbered list, one plain line per change ("Add a Back button to the edit screen"),
   - **only questions that really need answering** — where the email is unclear, two readings are
     both reasonable, or the change conflicts with a rule below. If nothing is unclear, say so.
   - anything you **won't** do and why (see "Stop and hand back" below).
3. **Stop and wait for his answers.** Don't file issues or write code yet.

## Step 2 — file issues

Once he answers, file **one GitHub issue per change** in the repo that owns it (`fa-web-api` for
backend/data, `fa-web-app` for screens). Title in plain words. Body: what to change, why, and
"done when…" in one or two lines. Use the `enhancement` or `bug` label.
If you can't file issues from this session, put the issue text in your reply and keep going.

⚠️ **The repos are PUBLIC.** Never put any of this in an issue, PR, commit or branch name:
alumni or student names, emails, phone numbers, BYU IDs, or the email's sender. Say
"an alum" or "the career office". Never paste the email itself.

## Step 3 — build

- **One branch per repo for the whole email**, cut from `dev` (e.g. `cr/2026-11-17-edit-back`).
  All the changes from one email go in together — **one PR, one merge per repo**. Vercel allows only
  about one deploy an hour, so never open a PR per change.
- Put `Closes #NN` for each issue in the PR description.
- **If both repos change:** do the API first. Merge it to dev, wait for the dev API deploy to finish,
  then in `fa-web-app` run `npm run gen:api-types` and commit the result with the app changes.
  If this session can only see one repo, finish that half and tell Jake to tag @Claude again
  in the other repo with the issue link.
- Database changes: add a new file in `fa-web-api/database/migrations/` (`YYYY-MM-DD_name.sql`).
  Only additive changes (new columns or tables). Never drop or rename anything. The dev migration
  runs automatically when the change merges to dev.
- Run the checks before opening the PR:
  - API: `ruff check .` and `DATABASE_URL="" pytest -q`
  - App: `npm run lint`, `npm run typecheck`, `npm run test`

## Step 4 — put it on dev

Open the PR into **`dev`**. When all checks are green, merge it, delete the branch, and wait for
the Vercel dev deploy to finish (the "Vercel" check on the merge commit turns green). Then check that
the dev site is really running it: `https://dev-fa-web-api.vercel.app/health` for the API,
`https://dev-fa-web-app.vercel.app` for the app.

**Never touch `prod`.** No PRs into `prod`, no prod database, no prod SQL. Jake promotes to prod
himself after he has reviewed dev.

## Step 5 — report back

Run `date -u +%FT%TZ` again (**END**). Reply in this shape:

> **Done — on dev.**
> 1. Back button on the edit screen — check: open any profile → Edit → Back.
> 2. …
>
> **Time:** 1 h 25 min total (START → END), about 50 min of that was work and the rest was waiting
> for your answers.

One line per change, each with what to click to see it. Mention anything you skipped or that still
needs him. No technical detail unless he asks.

## Rules that always apply

- dev is a sandbox; prod is real alumni data. Nothing from dev testing ever goes to prod.
- Any change to the alumni survey must also update the **sample survey**: the sample values and
  the email field list. A test checks this; don't weaken it.
- Alumni emails must **never** get an unsubscribe button or link. Leaving it out is the requirement.
- Old jobs in an alum's employment history must **always** be kept. Never overwrite or delete them.
- The Professor role (`view_only`) seeing email and phone is **intentional**.
- No icons in the UI. Build for desktop first; just don't break mobile.
- Follow `UX-UI.md` (in `fa-web-app`) for any visual work.

## Stop and hand back to Jake (don't build these, just say so in Step 1)

- Anything about **logins, passwords, roles, permissions, attacks or data leaks.** Say only
  "this one needs your laptop session". Don't describe it in an issue or PR.
- Anything that deletes or bulk-changes real data, or needs SQL run on prod.
- Anything that sends emails to alumni or changes survey sending.
- Anything you can't finish without guessing at what he wants.
