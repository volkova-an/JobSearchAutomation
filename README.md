# JobSearchAutomation

<p align="center">
  <img src="assets/Job%20Search%20Automation.jpg" alt="Job Search Automation logo" width="320">
</p>
Automatically searches job boards for postings matching your criteria, tailors your resume for each one, and tracks everything in a Google Sheet - on a schedule, while you sleep.

---

## Current Status: MVP

The job-search step currently collects, deduplicates, filters and exports to CSV. It does **not** yet write to Google Sheets, tailor resumes, or send notifications — those pieces exist in the codebase (`tools/sheets.py`, `tools/tailor_resume.py`, `tools/notify.py`) but are not wired to this search layer yet. See [Roadmap](#roadmap) below.

---

## What This Project Does

1. **Collects** job postings from three public, no-auth-required sources (see [Data Sources](#data-sources))
2. **Normalizes** every posting into one shape: title, company, location, salary, date posted, URL, description
3. **Deduplicates** by canonical URL, so the same posting mirrored across sources only appears once
4. **Filters** by required and stop keywords tuned for an Integration & Automation Engineer search (n8n, REST API, Stripe, Supabase, webhooks, LLM/OpenAI work — while screening out pure ML, infrastructure, and unrelated tech-stack roles)
5. **Exports** the survivors to a CSV for review

---

## Data Sources

| Source | Endpoint | Auth | Notes |
|---|---|---|---|
| [hh.ru](https://hh.ru) | `api.hh.ru/vacancies` | None | RU market. **Currently blocked from cloud/datacenter IPs** by hh.ru's own anti-scraping layer (`ddos-guard`) on the search endpoint specifically — works fine from a normal residential connection. Not a bug in this project. |
| [RemoteOK](https://remoteok.com) | `remoteok.com/api` | None | Global remote jobs, one JSON feed of everything currently live |
| [Remotive](https://remotive.com) | `remotive.com/api/remote-jobs` | None | Global remote jobs, supports server-side keyword search |

All three require no API key or login. Every raw response is cached locally under `.tmp/cache/` so filters can be re-tuned offline with `--replay` (no repeat API calls).

Indeed is no longer used for search — the earlier `tools/search_indeed.py` remains in the repo for reference but the active source layer is `tools/search_sources.py`.

---

## How to Run the Job Search

```bash
python3 tools/search_sources.py --sources remoteok,remotive --out .tmp/jobs.csv
```

The tool's own default is `--sources hh,remoteok,remotive`, but hh.ru returns nothing from most cloud environments (see the note above), so the command above drops it explicitly. Include it when running from a normal residential IP.

Useful flags:

| Flag | Effect |
|---|---|
| `--query "n8n,integration engineer,automation engineer"` | search phrases (comma separated) |
| `--since 2026-09-01` / `--days 30` | only keep postings from that date forward (undated postings are always kept) |
| `--salary-min 3000` | minimum monthly USD (postings with no stated salary are always kept) |
| `--replay` | reuse cached API responses instead of hitting the network — use this while tuning filters |
| `--include` / `--exclude` | override the keyword lists for a one-off search |

Full details, the keyword-matching rules, and how to read the run summary are in [`workflows/search_sources.md`](workflows/search_sources.md).

---

## How to Start (Full Pipeline)

The broader project — onboarding, Google Sheets tracking, resume tailoring — still runs the same way:

1. Open this folder in Claude Code
2. Type any of these:

```text
start
go
run the workflow
```

That's it. The first time you run it, you'll be walked through a one-time setup. Every run after that is silent and automatic.

Note: `run.md` and `workflows/search_jobs.md` still describe the original Indeed-based search step. Until that's rewired to `tools/search_sources.py`, use the command above for the actual job search, and treat the rest of the pipeline (Sheets, resume tailoring) as available but not yet connected to it.

---

## Before You Run for the First Time

Optional but recommended:

Complete one of these free AI-guided career workshops to define your ideal role. They take about 15 minutes and give the job search automation better inputs than manual entry.

[Find Your Way - 5-Step Career Workshop](https://chatgpt.com/g/g-69a00483e9f88191a21ef94004da7893-find-your-way-5-step-career-workshop)

[Linked Positioning Workshop](https://chatgpt.com/g/g-69ee70c2ad888191a34fcb973fd46fc3-linked-positioning-workshop)

When you finish, ask the workshop to export your results as `ideal_role.json` and drop that file into this project folder.

If you skip the workshop, Claude will use a lighter manual setup: job titles, location, salary floor, and keywords.

### Permission Warning

This repo includes project-level Claude Code settings files at `.claude/settings.json` and `.claude/settings.local.json` that are intentionally permissive so `/loop` runs can continue without repeated read/write approval prompts.

Review that file before running the workflow on your machine. It is appropriate only for a local, trusted copy of this repository where you are comfortable allowing Claude Code to read and modify project files without interactive confirmation.

## What You'll Need

| Item | Cost | Notes | Technical Difficulty |
|---|---|---|---|
| Claude subscription | $20/month | Required for access to Claude and the project workflow. | Low |
| Claude Code in VS Code or the Claude Code CLI | Included with Claude membership | Used to run the workflow locally. You can use the VS Code extension or work directly from the CLI. | Low to Medium |
| VS Code | Free | Optional if you prefer the CLI only. Useful for viewing project files and documents. | Low |
| Python 3 + `requests` | Free | Needed to run `tools/search_sources.py`. No API key or connector required — hh.ru, RemoteOK and Remotive are all open. | Low |
| Google account | Free | Only needed for the full pipeline (Sheets tracking, resume tailoring), not for the job search step itself. Claude will walk you through connecting it during setup. | Medium to High |
| Resume file | Free | Place your resume in the `resume/` folder. Supported formats: PDF, DOC, or DOCX. | Low |

---

## Why This Instead of n8n

| Category | This Project | n8n Workflow |
|---|---|---|
| Monthly cost | About $20/month with a Claude subscription. | Often closer to $70/month once you add `n8n` plus an LLM provider and related services. |
| Privacy and security | Uses your local project files and your own connected accounts. No exposed API keys are required for the main Claude-driven workflow. | Usually requires storing and managing API keys and external service credentials across multiple nodes and integrations. |
| Control over Gmail and documents | Built around your own Gmail, Google Docs, Google Drive, and Google Sheets access during guided setup. | Can do the same work, but you must wire and maintain each connection yourself inside the workflow. |
| Initial technical setup | Requires a Claude subscription, Claude Code in VS Code or the Claude Code CLI, a Google account, and one-time Google and Indeed connection setup. | Requires an `n8n` account, workflow creation, model/provider selection, API key setup, and account connections for the job-search flow. |
| AI model setup | Claude is already the core experience, so there is less model plumbing to configure. | Usually requires choosing a provider such as OpenAI, Claude, or Google and manually adding API keys and billing. |
| Tool availability | High. Claude has strong access to useful integrations and workflow tools for this use case. | High. `n8n` has a large plugin and integration ecosystem built for automation. |
| Workflow setup effort | After the files are on your computer, you open the project in Claude Code for VS Code or run Claude Code from the CLI. It walks you through the setup step by step. | You typically need to build or import the workflow, adjust nodes, connect services, and troubleshoot triggers before it is usable. |
| Runtime behavior | Runs locally through Claude Code in VS Code or the CLI. For automatic recurring runs, use the `/loop` slash command from Claude Code to schedule this workflow. | Runs in the cloud once deployed, so it can keep running continuously without your desktop app staying open. This is better for always-on automations and immediate notifications. |
| Modifying or starting the workflow | Start by opening the project and telling Claude to run it. Most setup is guided in plain language. | Starting or changing the workflow often means editing several nodes, triggers, and credentials in the `n8n` editor. |
| Best fit | Better for someone who wants guided setup with less technical overhead. | Better for someone who wants to design and maintain a more technical automation stack manually. |

### Workflow Comparison

| Workflow | Notion + n8n Version | This Project |
|---|---|---|
| Workflow 1: Auto-Fetch Jobs | Pulls new job postings from your target companies daily. | Already does this. It fetches new jobs on a schedule and avoids duplicates. |
| Workflow 2: Relevance Scoring | Scores each job based on your criteria with a detailed score breakdown. | Partially covered today. This project already filters and tailors to relevant roles, and adding explicit scoring is straightforward. |
| Workflow 3: Match Contacts | Links jobs to people you know at those companies. | Does not do this yet. This is the main non-trivial gap because it requires reliable company-to-contact matching logic and contact data structure. |
| Workflow 4: Generate Materials | AI creates personalized resumes, cover letters, and outreach messages using the Advice Triangle framework. | Largely does this already for personalized resumes. Cover letters and outreach messages are simple extensions. |
| Workflow 5: Daily Morning Briefing | Daily audio summary of new opportunities, outreach reminders, contact gaps, and networking nudges sent to your email. | Does not do this yet, but it is a relatively simple add-on. |
| Workflow 6: Auto-Update Next Action | Automatically sets follow-up dates and next steps when you update a contact's outreach status. | Does not do this yet, but it is a relatively simple add-on once contact tracking is defined. |
| Workflow 7: Auto-Delete Stale Jobs | Automatically archives jobs older than 15 days that you haven't acted on. | Does not do this yet, but it is a relatively simple add-on. |
| Workflow 8: Company Intelligence Brief | On-demand research brief with product analysis, industry positioning, recent news, and interview talking points delivered as a PDF to your email. | Does not do this yet, but it is a relatively simple add-on. |
| Workflow 9: Error Notifications | Get alerted via email if any workflow fails. | Does not do this yet, but it is a relatively simple add-on. |
| Tracking Template | Uses a Notion template. | Uses Google Sheets for tracking instead of Notion. |

In practical terms, the biggest functional gap is contact matching. Most of the other missing pieces are lighter workflow additions around reporting, reminders, notifications, or extra generated outputs.

---

## What Happens on First Run

> These steps describe the original full-pipeline onboarding (`run.md` / `workflows/onboarding.md`), which still references Indeed and has not yet been rewired to the sources above. For the job search itself, use the command in [How to Run the Job Search](#how-to-run-the-job-search) instead.

Claude walks you through setup step by step:

1. Checks Python and required packages
2. Confirms Indeed is connected
3. Sets up your Google connection
4. Confirms your resume
5. Collects your job-search criteria
   If `ideal_role.json` is present, Claude can also auto-fill a richer role profile.
6. Creates your Google Sheet tracker and uploads your resume to Drive
7. Confirms everything and reminds you how to run it from Claude Code in VS Code or the CLI, including scheduling with `/loop` if you want automation
8. Offers an optional initial run instead of starting one by default

---

## What Happens on Scheduled Runs

> Same caveat as above — this describes `workflows/search_jobs.md` (Indeed-based), not the current `tools/search_sources.py` MVP.

Everything below happens automatically:

- Searches Indeed for new matching jobs
- Skips anything already in your sheet
- Adds new jobs to the Google Sheet
- Tailors your resume for each new job
- Saves each tailored resume to Google Drive and links it in the sheet

To set up recurring runs from Claude Code, use the `/loop` slash command. Examples:

```text
/loop 5m check the deploy
/loop 30m /babysit-prs
/loop 1h run the workflow
```

---

## Folder Structure

```text
ideal_role.json          -> Optional workshop output
resume/                  -> Put your resume here
workflows/               -> Step-by-step workflow instructions Claude follows
  search_sources.md      ->   Active job search workflow (hh.ru / RemoteOK / Remotive)
  search_jobs.md         ->   Original Indeed-based workflow, not yet rewired
tools/                   -> Python scripts that do the actual work
  search_sources.py      ->   Active job search: collect, dedupe, filter, CSV
  search_indeed.py       ->   Original Indeed post-processing, kept for reference
  sheets.py, notify.py, tailor_resume.py -> Not yet wired to the new search layer
.claude/                 -> Project configuration
.tmp/                    -> Regenerable output: CSVs, cached API responses, backups
requirements.txt         -> Python packages this project needs
.env                     -> Saved preferences created during onboarding
run.md                   -> Entry point Claude reads to run the full pipeline
README.md                -> This file
```

---

## Changing Criteria

Delete the relevant line or lines from `.env`, then run the workflow again.

To update your role profile, edit `ideal_role.json` directly or redo the workshop and replace the file, then delete the relevant lines from `.env` and run again.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Google sign-in failed or expired | Run `python tools/google_auth.py` in a terminal |
| Indeed not found | Go to claude.ai -> Settings -> Integrations and reconnect (only needed for the legacy full pipeline, not the current job search step) |
| Wrong job criteria | Delete the relevant lines from `.env` and run again |
| Want to use a different resume | Replace the file in `resume/` and delete `RESUME_DRIVE_URL` from `.env` |
| `search_sources.py` returns nothing from hh.ru | Expected from most cloud/datacenter IPs — hh.ru's `ddos-guard` blocks its search endpoint there. Run from a residential connection, or drop `hh` from `--sources` |
| Tuning filters is slow because of API calls | Add `--replay` to reuse the payloads already cached in `.tmp/cache/` |
| `--replay` errors with "no cached payload exists" | Run once without `--replay` first so the cache is populated |

### Scheduled Task Keeps Asking for Approval

If a `/loop`-scheduled Claude Code run keeps asking for approval to read a `SKILL.md` file, that is normal behavior and is not specific to this project.

Claude Code uses a permission system with `allow`, `ask`, and `deny` rules. If the `/loop` task is running in a mode that still prompts for a tool or file access, the task will pause until you approve it. Anthropic's docs also note that `ask` rules take precedence over `allow` rules, so a task can keep prompting if its permissions are not fully pre-authorized.

To make the task run autonomously:

1. Run the same command manually once before relying on `/loop`
2. Approve each permission prompt and choose `always allow` when offered
3. Re-run the `/loop` schedule afterward so future executions can use the same permissions without stopping

If you want to pre-authorize access directly, you can also configure permission rules in your Claude Code settings file, such as `~/.claude/settings.json`, so the required read access is already allowed before the `/loop` schedule runs.

---

## Roadmap

Not yet wired up, in rough order:

1. Append surviving jobs to the Google Sheet via `tools/sheets.py` (the CSV already carries a stable `job_hash` per posting for that dedup path)
2. Score each posting against the role profile (Integration & Automation Engineer, n8n / REST API / Stripe / Supabase / OpenAI, remote only, $3000+/month) instead of pass/fail keyword matching alone
3. Tailor a resume per surviving job via `workflows/tailor_resume.md`
4. Notifications via `tools/notify.py`
5. Rewire `run.md` / `workflows/search_jobs.md` to call `tools/search_sources.py` instead of Indeed, so the guided onboarding flow uses the current sources end to end

---

## License

This project is licensed under `PolyForm Noncommercial 1.0.0`.
