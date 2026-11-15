# GrantForge roadmap and registry

What is done, what is left, and who has to act. "Done" means merged to `main` with tests and green CI;
"Live" means also deployed to the Render service `grantforge` (srv-dbse8dgae00c73esdm1g, Free plan,
Neon project `grantforge`). Nothing here has been independently reviewed or accepted by the owner.

## Done

| Unit | What | Where |
|---|---|---|
| GF-01 | Ingest (PDF/DOCX/MD/ZIP/images with OCR/git), project profile | v0.1-v0.2 |
| GF-02 | Discovery (Grants.gov API, optional web search re-verified on the funder page, manual URL) | v0.2 |
| GF-03 | Scoring with hard filters, forecast holding | v0.2 |
| GF-04 | Section-by-section drafting, NOFO reading, length fitting, fact check, budget in code | v0.2 |
| GF-05 | Review page, Authorize with password, exactly-once submission (email / Grants.gov package / portal package) | v0.2 |
| GF-06 | Neon Postgres, files stored in the database, Alembic migrations, Render deployment | v0.3, Live |
| GF-07 | Resumable jobs: stage checkpoints, per-section draft progress, attempt cap (migration 0004) | PR #1, Live |
| GF-08 | Keep-awake ping while a job runs (Render Free sleeps after 15 idle minutes) | PR #1, Live |
| GF-09 | Resend (HTTPS) email backend for hosts that block SMTP; free-plan messages in Setup status and dashboard | PR #1, Live (no key yet) |
| GF-10 | Email failures never fail a run or mark reminders/notices as sent | PR #1, PR #2 |

## Needs the owner

| Item | Action | Unlocks |
|---|---|---|
| Email | Either set `GF_EMAIL_BACKEND=resend`, `RESEND_API_KEY` and a `GF_EMAIL_FROM` on a domain verified in Resend; or approve the Starter plan and set `GF_SMTP_PASSWORD` (Gmail App Password) | Review-link emails, deadline reminders, spend-cap notice, email submission |
| Plan | Decide whether to move the service to Render Starter (paid) | No sleeping, SMTP, autopilot and reminders on schedule |
| Applicant | Fill in Applicant settings (legal name, entity type, UEI/EIN) in the app | Eligibility filtering and form prefill |
| Federal | SAM.gov registration, then Grants.gov roles; for S2S a CA-signed client certificate | Federal submission (today it stops at the Workspace Submit button) |
| Live tests | Add `ANTHROPIC_API_KEY` as a GitHub Actions secret and run "Live tests" | Recorded end-to-end run against the real model |

## Remaining engineering

| Unit | What | Notes |
|---|---|---|
| GF-11 | Grants.gov System-to-System submission | Scaffold in `grantforge/submit/grants_gov_s2s.py`; blocked on SAM.gov/Grants.gov credentials and certificate |
| GF-12 | External scheduler for autopilot and reminders on Free | On Free they only run while the instance is awake; a Render cron job or GitHub Actions schedule calling an authenticated endpoint would fix this |
| GF-13 | Answer `HEAD` on `/` and `/health` | Render and uptime monitors send `HEAD /`, which returns 405 today (harmless) |
| GF-14 | More funding sources (state portals, foundation directories with APIs) | Today: Grants.gov, optional model web search, manual URL |
| GF-15 | Multi-user accounts and roles | One shared password today |
| GF-16 | Portal submission automation (Submittable, Fluxx) | Today: field-by-field steps and a finished package |
| GF-17 | Verify a real end-to-end run on the live service | Needs the owner to sign in and run a project; not yet observed in logs |
