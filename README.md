# GrantForge

A self-hosted web app that turns a pile of project material into grant applications ready to submit, with the least possible work from you.

You do three things:

1. **Give it everything about the project:** documents, decks, papers, code repositories, notes, CVs and letters of support.
2. **Review the drafted applications** when the email arrives, and press **Authorize** on the ones you want sent.
3. **Wait for replies.**

Everything in between is automatic. GrantForge understands the project, searches for funding, judges eligibility and fit, reads each funder's full notice, writes the application section by section, checks its claims against your material, builds the budget and attachments, and emails you a review link. Nothing is ever sent to a funder without your explicit authorization on that application's review page.

## How it works

```
 your files / repo / notes
          │
          ▼
 ┌─────────────────┐    ┌────────────────────┐    ┌──────────────────────┐
 │ 1. Ingest       │───▶│ 2. Project profile │───▶│ 3. Discover          │
 │ pdf docx md zip │    │ problem, solution, │    │ Grants.gov API       │
 │ images (OCR),   │    │ stage, keywords,   │    │ + optional web search│
 │ git repo, text  │    │ search queries …   │    │   (each result is    │
 └─────────────────┘    └────────────────────┘    │   re-read on the     │
                                                  │   funder's own page) │
                                                  └──────────┬───────────┘
                                                             ▼
 ┌─────────────────┐    ┌────────────────────┐    ┌──────────────────────┐
 │ 7. Submit       │◀───│ 6. You review and  │◀───│ 4. Score             │
 │ email, or full  │    │    Authorize       │    │ 5. Draft: read NOFO →│
 │ package + steps │    │ (expiring link +   │    │ plan → each section →│
 │ exactly once    │    │  password)         │    │ length fit → fact    │
 └─────────────────┘    └────────────────────┘    │ check → budget       │
                                                  └──────────────────────┘
```

**Ingest.** Upload PDF, DOCX, Markdown, text, code files, images, or a ZIP of a code base; add an https git URL; or paste text. Scanned PDFs and photographs of documents are read by OCR, and every file shows a warning if part of it could not be read. Any uploaded file can be marked **attach**, which sends it with every application (CVs, letters of support, budget spreadsheets).

**Profile.** Each input is summarised, then the model builds one structured project profile: problem, solution, technology, stage, domains, beneficiaries, geography, funding need, evidence of traction, and 8 to 15 search queries. You can read the profile on the project page, and every later step works from it.

**Discover.** The profile's queries run against the public Grants.gov API, which needs no key. Optionally, the model's web-search tool looks for foundation, state, corporate and prize funding. Each web result is then opened on the funder's own page and its deadline, award range and eligibility are re-read from that page, so these facts never rest on a search summary alone. You can also paste any call-for-proposals URL. Fetching user-supplied and model-supplied URLs is restricted to public internet addresses.

**Score.** Opportunities that are closed, past their deadline, or that exclude your entity type are filtered out without a model call. The rest receive a 0 to 100 fit score, a written rationale and a list of blockers. Forecasted opportunities that match are held and drafted automatically on the first run after they open.

**Draft.** For each shortlisted opportunity:

1. For Grants.gov opportunities, the full funding notice (NOFO) and related documents are downloaded and read. The model extracts, once per opportunity, the required sections with their length limits, the review criteria, the required forms and attachments, the formatting rules and any cost-sharing requirement.
2. One planning call produces the outline (using the funder's exact section names and order when the notice states them), the budget by SF-424A object class, the checklist, the open questions and the cover email.
3. Each section is written by its own call. Your material and the funder's requirements are sent as a cached block, so after the first call they are billed at the cache-read price.
4. Each section is measured against its limit. A section that is too long is **written again from the beginning** to fit, up to twice; it is never cut or trimmed. Page limits are estimated at `GF_WORDS_PER_PAGE` words per page and marked as estimates.
5. Each section's factual claims are checked against your material. Claims that are unsupported or contradicted are listed on the review page and in the checklist file.
6. Budget totals are computed in code. If the model's stated request differs from the sum of its budget lines, the sum is used and the difference is listed as an open question.
7. Facts the funder asks for that your material does not contain appear as `[APPLICANT: …]` placeholders and as open questions.

**Package.** Every file name states the opportunity, the version number, what changed in that version, and the length, for example `EPA-R1-2027-03_v3-edited_2140w_application.docx`. Saving an edit creates a new version; earlier versions stay in the folder. A package contains the application (`.docx` and `.md`), the budget (`.csv`), a checklist with open questions, unsupported claims and over-limit sections, a `package.json` with all metadata, and your attached files.

**Notify.** One email lists the drafts with fit scores, deadlines, open-question counts and unsupported-claim counts, plus a signed review link per draft. Links expire after `GF_REVIEW_LINK_DAYS` days. A digest reminder is sent 7 and 2 days before the deadline of any draft that has not been submitted.

**Review and Authorize.** The review page shows why the grant was chosen, the open questions, the checklist, the fact check, the length report, the full draft (editable in place) and every package file. Authorizing requires typing `SUBMIT` and entering the GrantForge password, and it records who authorized which version and when.

**Submit.** The channel depends on the funder:

| Channel | What Authorize does | Automatic? |
|---|---|---|
| `email` (the funder accepts applications by email) | Emails the cover letter with the application, the budget and your attached files to the funder's address | Yes |
| `grants_gov` (federal) | Finalises the package, writes an SF-424 and SF-424A prefill file, and gives the exact Grants.gov Workspace steps | The package is automatic; you click Submit in Workspace |
| `portal` (Submittable, Fluxx, SmartSimple and similar) | Finalises the package and gives field-by-field steps | The package is automatic; you paste and upload |

A submission is sent exactly once. The application is moved from `authorized` to `submitting` by a single conditional database update before anything is sent, so a double click, a duplicate job or a second process sends nothing more. If the process stops in the middle of a submission, that job is marked failed and not retried automatically, and the page tells you to check the funder's inbox before authorizing again.

### Why federal submission ends with your click

Grants.gov accepts applications only through its Workspace web interface or its System-to-System (S2S) interface. S2S requires an organisation registered in SAM.gov, a Grants.gov account with a submitting role, and a CA-signed client certificate registered with the Grants.gov programme office, followed by integration testing. The transport scaffold and the prerequisites are documented in `grantforge/submit/grants_gov_s2s.py`. Until those credentials exist, GrantForge prepares everything up to the final Submit button.

Two registrations matter for any federal grant. **SAM.gov** is free, issues the Unique Entity ID, and requires a Login.gov account, an EIN (a sole proprietor without an EIN can use an SSN), a legal name and street address matching IRS records, and a U.S. bank account. It typically takes 10 to 20 business days and must be renewed every 365 days. After SAM.gov is active, the E-Biz Point of Contact named in SAM.gov activates the organisation's **Grants.gov** account and assigns the role that may submit. Start both before the first federal deadline.

## Install and run locally

Requirements: Python 3.11 or later, `git`, and for OCR `tesseract-ocr` and `poppler-utils`.

```bash
git clone <this repo> grantforge && cd grantforge
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add ANTHROPIC_API_KEY, GF_NOTIFY_TO and the SMTP settings
uvicorn grantforge.main:app --port 8000 --workers 1
```

Open http://localhost:8000, fill in **Applicant settings**, create a project, add material, and press **Run everything**. The background worker runs inside the same process. Run exactly one process; the job queue is safe against duplicates, but daily maintenance and stale-job recovery assume a single worker (set `GF_RUN_WORKER=false` on any additional process).

### Dry run without an API key

```bash
GF_LLM_PROVIDER=mock python scripts/run_pipeline.py --name Demo --add README.md --run
```

The mock provider returns canned answers so you can see the whole flow, the package files and the notification email (written to `data/outbox/` by the console email backend).

### Command line

```bash
python scripts/run_pipeline.py --name "My project" --add ./docs --add https://github.com/me/repo --attach ./cv.pdf --run
python scripts/run_pipeline.py --list
python scripts/run_pipeline.py --backup
```

## Deploy on Render with Neon

`render.yaml` is a Render Blueprint for one Docker web service; the database is Neon Postgres. Uploads and application packages are stored in the database as well, so nothing is lost when Render replaces the container on each deploy.

1. In Neon, create a project in AWS us-east-2 (the Blueprint's `ohio` region is the same AWS region). Copy two connection strings from **Connect**: the pooled one (host contains `-pooler`) for `GF_DATABASE_URL`, and the direct one for `GF_DATABASE_URL_DIRECT`.
2. In Render, choose **New → Blueprint**, select this repository, and fill in the values Render asks for: the two database URLs, `ANTHROPIC_API_KEY`, `GF_UI_PASSWORD`, `GF_NOTIFY_TO`, `GF_EMAIL_FROM`, `GF_SMTP_USER` and `GF_SMTP_PASSWORD` (for Gmail, an App Password; on the Free plan use `RESEND_API_KEY` instead, see below). `GF_APP_SECRET` is generated for you.
3. Open the service's address, sign in, and check **Applicant settings → Setup status**. Every line should be green or amber; press **Send a test email**.

### Free plan

The live service runs on Render's Free plan, which has two limits. GrantForge handles both:

* **The instance sleeps** after 15 minutes without a visitor and wakes, in about a minute, on the next visit. While a job runs, the worker requests the app's own public `/health` address every `GF_KEEPALIVE_SECONDS` (600 on Render) so the instance stays awake until the job finishes; when idle it sends nothing, so the instance can sleep. If a run is interrupted anyway (a deploy, a restart), its progress is already in the database: the job records each finished stage (profile, discover, score, draft, notify), every draft saves its plan and each finished section and fact check, and on the next start the worker re-queues the job and continues from the first unfinished step. A job interrupted `GF_JOB_MAX_ATTEMPTS` times (default 5) is failed instead of retried. A submission is never retried (see above). Autopilot runs and deadline reminders only happen while the instance is awake.
* **Outbound SMTP is blocked.** Set `GF_EMAIL_BACKEND=resend`, `RESEND_API_KEY` and `GF_EMAIL_FROM` (an address on a domain verified in [Resend](https://resend.com)); Resend sends over HTTPS. Until email works, the dashboard says so, drafts are listed there instead of emailed, and they are emailed on a later run once email works. Nothing is lost.

Set `GF_HOSTING_PLAN=free` so the Setup status page explains these limits. The Starter plan (paid) removes both, and SMTP then works with the `GF_SMTP_*` values.

The public address comes from Render (`RENDER_EXTERNAL_URL`); set `GF_BASE_URL` only for a custom domain. While nothing is running, the worker does not query the database (it is woken when a job is queued), so Neon can suspend its compute between runs.

## Deploy on your own server with HTTPS

`deploy/deploy.sh` installs Docker on a fresh Ubuntu 22.04 or 24.04 server, checks the required settings, opens only ports 22, 80 and 443, and starts GrantForge behind Caddy, which obtains and renews a TLS certificate automatically.

1. Rent a small Ubuntu server from any provider; 2 GB of RAM is a reasonable starting size because OCR of large scanned files is the heaviest task.
2. Point a domain or subdomain (for example `grants.example.com`) at the server's IP address with a DNS A record.
3. Copy the repository to the server and create `.env` from `.env.example`, setting at least `GF_DOMAIN`, `GF_BASE_URL=https://<GF_DOMAIN>`, `GF_UI_PASSWORD`, `ANTHROPIC_API_KEY`, `GF_NOTIFY_TO` and the SMTP settings.
4. Run `sudo bash deploy/deploy.sh`.

GrantForge refuses to serve any page at a public address until `GF_UI_PASSWORD` is set.

## Setup status

**Applicant settings** opens with a setup status table: the model, your email address, how email is sent, the password, the database, email submission mode and the applicant. Each line is green, amber (works, but read the note) or red (a feature cannot work), and says which variable fixes it. The dashboard shows a warning while any line is red. Secret values are never displayed.

## Safety rails

* **Nothing is submitted without a human.** Submission requires the state `authorized`, which only the Authorize form sets, and that form requires typing `SUBMIT` and the password.
* **Test mode is on by default.** With `GF_SUBMISSION_TEST_MODE=true`, an authorized email submission goes to you with the subject prefixed `[TEST – would go to …]`. Turn it off only after you have received and read one.
* **Model requests.** Thinking is always on in current Claude models and counts toward the output cap, so every request adds `GF_LLM_THINKING_HEADROOM` tokens of room. Planning, sections and fact checks run at `GF_LLM_EFFORT`; summaries, scores and page parsing run at low effort. If the model declines a request, the job stops with that reason instead of continuing with an empty answer, and server-side fallback to the previous model is requested where the model supports it. All calls for one application share one prompt-cache entry.
* **Spend cap.** Every model call is recorded with its cost, using Anthropic's published prices (checked 2026-11-01), and all model calls stop for the month when `GF_MONTHLY_BUDGET_USD` is reached. You are emailed once when that happens.
* **Web security.** Server-side random login sessions, a login rate limit (5 failures per 15 minutes per address), CSRF tokens on every form, expiring signed review links, a strict Content-Security-Policy, sanitised rendering of all text from funders and the model, `no-referrer` so review links never leak to funder sites, upload size limits, refusal of private-network URLs, and git cloning restricted to public https URLs from the web interface.
* **Backups.** With Postgres (Neon), the database host's restore history and snapshots are the backup. With SQLite, a daily archive of the database (an online, consistent SQLite snapshot), uploads and packages is written to `data/backups/`, keeping the newest `GF_BACKUP_KEEP`. Point `GF_BACKUP_DIR` at a mounted or synced folder to keep copies off the server.
* **Migrations.** Schema changes are Alembic migrations applied at startup. A v0.1 database is detected and upgraded in place without losing data.
* **Audit trail** for each application: shortlisted, drafted (with unsupported-claim and over-limit counts), notified, reminders, edited, authorized (by whom, which version, from which address), and the submission result.

Read every draft before authorizing. The model writes well, but a grant application is a representation to a funder made in your name.

## Roadmap

[docs/ROADMAP.md](docs/ROADMAP.md) lists what is done, what the owner still has to decide or configure, and the remaining engineering work.

## Tests

```bash
python -m pytest -q                     # offline: mock model, no network
```

Two GitHub Actions workflows are included:

* `.github/workflows/ci.yml` runs the offline tests on every push.
* `.github/workflows/live-tests.yml` is started by hand. It checks the real Grants.gov search, detail and attachment-download endpoints, then (when the `ANTHROPIC_API_KEY` repository secret is set) runs the real model: a basic call, a web-search call, and an end-to-end run that drafts one real application for a fictional sample project. Raw API responses, the draft package, the scores and the spend are uploaded as a run artifact. The run is capped at $5 of model spend.

## Project layout

```
grantforge/
  config.py        settings from environment
  db.py            models: Project, Document, Opportunity, Match, ApplicantProfile, Job, AuditEvent, LLMUsage, WebSession
  migrate.py       Alembic runner; migrations/ holds 0001 (v0.1 schema), 0002 (v0.2) and 0003 (stored files)
  blobstore.py     uploads and packages kept in the database, restored to disk on demand
  setup_status.py  what is configured and what is missing, for the Settings page
  llm.py           Claude Messages API (prompt caching, pause_turn, spend recording, monthly cap) + offline mock
  net.py           safe outbound fetching (public addresses only, size cap, checked redirects)
  security.py      sessions, CSRF, login limiter, HTML sanitiser, security headers
  ingest/          extract.py (pdf/docx/zip/code/OCR), repo.py (git), profile.py (project profile)
  sources/         grants_gov.py (search, detail, NOFO download), web_search.py, manual_url.py (page reading + verification)
  matching.py      hard filters + model scoring + forecast release
  drafting.py      requirements → plan → sections → length fit → fact check → budget
  export.py        versioned DOCX / Markdown / CSV / checklist / package.json
  notify.py        expiring review links, email backends, notification, reminder and spend emails
  submit/          exactly-once submission; email, manual (SF-424/SF-424A prefill + steps), S2S scaffold
  maintenance.py   daily backups, deadline reminders, spend-cap notice
  pipeline.py      profile → discover → score → draft → notify
  worker.py        background jobs (atomic claiming), autopilot, daily maintenance
  main.py          FastAPI routes; templates/ and static/ for the UI
deploy/            Caddyfile and deploy.sh (your own server)
render.yaml        Render Blueprint (Render + Neon)
scripts/           run_pipeline.py command line
tests/             offline tests; tests/live/ live tests for GitHub Actions
```

## Extending

* **New funding source:** implement `search(profile, log_fn) -> list[RawOpportunity]` and add it in `sources/__init__.py::all_sources`.
* **New submission channel:** add a submitter in `submit/` and a branch in `submit/__init__.py::submit_match`.
* **Schema change:** edit `db.py`, then generate a migration with `alembic.command.revision(alembic_config(engine, conn), autogenerate=True)`. The test `test_models_match_migrations` fails until the migration exists.

## Attribution

This product uses the Grants.gov API but is not endorsed or certified by the U.S. Department of Health and Human Services.
