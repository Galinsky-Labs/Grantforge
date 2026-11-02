# GrantForge

A self-hosted web app that turns a pile of project material into submitted grant applications with the least possible work from you.

You do three things:

1. **Give it everything about the project** — documents, decks, papers, a code repository, notes.
2. **Review the drafted applications** when the email arrives, and press **Authorize** on the ones you want sent.
3. **Receive the money.** (This step still involves a funder.)

Everything in between — understanding the project, searching for funding, judging eligibility and fit, writing the applications, building the attachments, emailing you — is automatic. Nothing is ever sent to a funder without your explicit authorization on its review page.

## How it works

```
 your files / repo / notes
          │
          ▼
 ┌─────────────────┐    ┌────────────────────┐    ┌─────────────────┐
 │ 1. Ingest       │───▶│ 2. Project profile │───▶│ 3. Discover     │
 │ pdf docx md zip │    │ structured JSON:   │    │ Grants.gov API  │
 │ git repo, text  │    │ problem, solution, │    │ (+ web search   │
 └─────────────────┘    │ stage, keywords,   │    │  for foundations│
                        │ search queries …   │    │  and prizes)    │
                        └────────────────────┘    └────────┬────────┘
                                                           ▼
 ┌─────────────────┐    ┌────────────────────┐    ┌─────────────────┐
 │ 6. Submit       │◀───│ 5. You review +    │◀───│ 4. Score + draft│
 │ email / package │    │    Authorize       │    │ hard filters →  │
 │ + audit trail   │    │ (signed link in    │    │ LLM fit score → │
 └─────────────────┘    │  your inbox)       │    │ full application│
                        └────────────────────┘    └─────────────────┘
```

**Ingest.** Upload PDF, DOCX, Markdown, text, code files, or a ZIP of a code base; add a git URL or local path; paste text. Repositories are read README-first with lockfiles and vendored directories skipped.

**Profile.** Each input is summarised, then the model builds one structured *project profile*: problem, solution, technology, stage, domains, beneficiaries, geography, funding need, evidence of traction, and 8–15 search queries. You can read it on the project page; everything downstream works from it.

**Discover.** The profile's queries run against the public Grants.gov `search2` / `fetchOpportunity` API (no key needed). Optionally the model's web-search tool looks for foundation, state, corporate and prize funding. You can also paste any call-for-proposals URL and it is parsed and scored.

**Score.** Opportunities whose deadline has passed or whose stated applicant types exclude you are filtered out without a model call. The rest get a 0–100 fit score, a written rationale, and explicit blockers (for example "requires 501(c)(3) status"). Scores at or above `GF_MATCH_THRESHOLD` are shortlisted.

**Draft.** For each shortlisted opportunity the model writes a complete application using only facts from your material, following the funder's required sections when the call states them. Output per application: `application.md`, `application.docx`, a `CHECKLIST.md` of required forms/attachments, `package.json`, and a list of *open questions* for anything the funder asks that your material does not answer (those appear as `[APPLICANT: …]` placeholders in the text).

**Notify.** One email with a table of drafts, fit scores, deadlines, and a signed review link per draft. The link works without logging in.

**Review + Authorize.** The review page shows why the grant was chosen, the open questions, the checklist, the full draft (editable), readiness checks, and the submission channel. Typing `SUBMIT` and pressing Authorize records who authorized and when, then hands the application to the submitter.

**Submit.** The channel depends on the funder:

| Channel | What Authorize does | Automatic? |
|---|---|---|
| `email` (funder accepts applications by email) | Emails the cover letter with the DOCX and Markdown attached to the funder's address | Yes |
| `grants_gov` (federal) | Finalises the package, writes `sf424_prefill.json` with every SF-424 field we know, and gives the exact Grants.gov Workspace steps | Package yes; the final upload is yours (see below) |
| `portal` (Submittable, Fluxx, SmartSimple, …) | Finalises the package and gives field-by-field instructions | Package yes; the final upload is yours |

Every state change is written to an audit table shown at the bottom of each review page.

### Why federal submission is not one click

Grants.gov accepts applications only through its Workspace web UI or its System-to-System (S2S) SOAP interface. S2S requires an organisation registered in SAM.gov, a Grants.gov account with the Authorized Organization Representative role, and a CA-signed client certificate registered with the Grants.gov programme office, followed by integration testing. There is no public submission API for individuals. The transport scaffold and the prerequisites are in `grantforge/submit/grants_gov_s2s.py`; if you set up S2S credentials for an organisation, that module is where the per-form XML assembly goes. Until then, GrantForge gets you to the last screen with everything filled in.

Two registrations matter regardless: **SAM.gov** (free; issues the Unique Entity ID; required for any federal award) and a **Grants.gov** account. Both take days to weeks, so start them before the first federal deadline. Fill in the applicant settings page so the eligibility filter knows what kind of entity you are; most federal opportunities exclude individuals and for-profits, and the filter keeps those out of your inbox.

## Install and run

Requirements: Python 3.11+, `git` on PATH (for repository ingestion).

```bash
git clone <this repo> grantforge && cd grantforge
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add ANTHROPIC_API_KEY, GF_NOTIFY_TO, and SMTP settings
uvicorn grantforge.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000, fill in **Applicant settings**, create a project, add material, press **Run everything**. The background worker runs in the same process; the project page refreshes while a job is active and each job has a live log.

Docker:

```bash
cp .env.example .env && edit .env
docker compose up -d
```

### Dry run without an API key

```bash
GF_LLM_PROVIDER=mock python scripts/run_pipeline.py --name Demo --add README.md --run
```

The mock provider returns canned answers so you can see the whole flow, the package files and the notification email (written to `data/outbox/` with the console email backend).

### Command line

```bash
python scripts/run_pipeline.py --name "My project" --add ./docs --add https://github.com/me/repo --run
python scripts/run_pipeline.py --list
```

### Autopilot

Set `GF_AUTOPILOT_INTERVAL_HOURS=24` and the worker re-searches, re-scores and drafts new matches for every profiled project daily. Already-handled opportunities are left alone, so you are emailed only when there is something new.

## Safety rails

* **Nothing is submitted without a human.** `submit_match()` refuses any match whose `authorized_at` is empty, and the only thing that sets it is the Authorize form, which requires typing `SUBMIT`.
* **Test mode is on by default.** With `GF_SUBMISSION_TEST_MODE=true`, an authorized email submission is sent to *you* with the subject prefixed `[TEST – would go to …]`. Turn it off only after you have seen one arrive and read it.
* **Email submission can be disabled entirely** with `GF_ALLOW_EMAIL_SUBMISSION=false`; every channel then produces a package for you to send.
* **Review links are signed** with an HMAC of the match id; the rest of the UI sits behind `GF_UI_PASSWORD`. Set the password before exposing the app beyond localhost.
* **The drafter is told to use only your material** and to leave visible `[APPLICANT: …]` placeholders rather than invent facts; the review page counts remaining placeholders and open questions above the Authorize button.
* **Audit trail** per application: shortlisted, drafted, notified, edited, authorized (by whom), submission result.

Read every draft before authorizing. The model writes well, but a grant application is a representation to a funder made in your name.

## Configuration

See `.env.example`; every setting is documented there. The important ones:

| Variable | Meaning |
|---|---|
| `ANTHROPIC_API_KEY`, `GF_LLM_MODEL` | Model used for summarising, profiling, scoring and drafting |
| `GF_ENABLE_WEB_SEARCH_SOURCE` | Also search the web for non-federal funding |
| `GF_MATCH_THRESHOLD` | Minimum fit score to draft (default 65) |
| `GF_MAX_DRAFTS_PER_RUN` | Drafts per run; run **Draft shortlisted** again for more |
| `GF_NOTIFY_TO`, `GF_EMAIL_BACKEND`, `GF_SMTP_*` | Where and how review emails go |
| `GF_SUBMISSION_TEST_MODE` | Keep `true` until you trust the pipeline |
| `GF_AUTOPILOT_INTERVAL_HOURS` | Recurring discovery |

## Project layout

```
grantforge/
  config.py        settings from environment
  db.py            SQLAlchemy models: Project, Document, Opportunity, Match, ApplicantProfile, Job, AuditEvent
  llm.py           Claude Messages API wrapper + offline mock provider
  ingest/          extract.py (pdf/docx/zip/code), repo.py (git), profile.py (project profile)
  sources/         grants_gov.py, web_search.py, manual_url.py; RawOpportunity + upsert
  matching.py      hard filters + LLM scoring
  drafting.py      application writer; markdown ↔ sections sync for the editor
  export.py        DOCX / Markdown / checklist / package.json writer
  notify.py        signed review links, email backends, notification email
  submit/          email_submitter.py, manual_submitter.py (SF-424 prefill + steps), grants_gov_s2s.py (scaffold)
  pipeline.py      profile → discover → score → draft → notify
  worker.py        background job thread + autopilot loop
  main.py          FastAPI routes; templates/ and static/ for the UI
scripts/run_pipeline.py   CLI
tests/                    offline test-suite (mock LLM, recorded Grants.gov responses)
```

Run the tests with `python -m pytest -q`.

## Extending

* **New funding source:** implement `search(profile, log_fn) -> list[RawOpportunity]` and add it in `sources/__init__.py::all_sources`.
* **New submission channel:** add a submitter in `submit/` and a branch in `submit/__init__.py::submit_match`; set `submission_channel` on the opportunity.
* **Different model vendor:** add a provider class in `llm.py` with a `text(task, system, user, max_tokens, web_search)` method.

## Attribution

This product uses the Grants.gov API but is not endorsed or certified by the U.S. Department of Health and Human Services.
