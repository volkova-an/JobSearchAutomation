# Workflow: Search Sources (hh.ru / RemoteOK / Remotive)

## Objective
Collect job postings from three public, auth-free sources, normalize them into
one shape, drop duplicates, apply the include/exclude keyword filter, and write
a CSV for review.

This workflow replaces the Indeed search layer described in
`workflows/search_jobs.md`. Everything downstream of the search step
(`tools/sheets.py`, `workflows/tailor_resume.md`, `tools/notify.py`) is
unchanged and still expects the same record shape.

## Scope Of This Version
- In scope: collection, normalization, dedup, keyword filter, CSV output.
- Out of scope for now: Google Sheets append, resume tailoring, notifications,
  scoring against the role profile.

---

## Step 0: Resource Check

Before spending API calls, confirm the run makes sense:

- the search phrases match the role being targeted
- the include/exclude lists are the ones intended for this search
- if a previous run's CSV is still unreviewed, ask whether to re-run at all

If the phrases and the keyword lists disagree with each other (searching for
"Data Scientist" while `Data Scientist` is a stop-word, for example), stop and
flag it instead of running.

---

## Step 1: Choose Sources And Phrases

| Source | Endpoint | Auth | Server-side search | Notes |
|---|---|---|---|---|
| hh.ru | `https://api.hh.ru/vacancies` | none | yes (`text`) | RU market; needs a real User-Agent; descriptions come from a second per-vacancy call |
| RemoteOK | `https://remoteok.com/api` | none | no | one JSON array of all live jobs; first element is a legal notice, not a job |
| Remotive | `https://remotive.com/api/remote-jobs` | none | yes (`search`) | remote-only by definition |

Default phrases live in `DEFAULT_QUERIES` in `tools/search_sources.py`.
Override per run with `--query`.

---

## Step 2: Run The Collector

```text
python3 tools/search_sources.py \
  --sources hh,remoteok,remotive \
  --query "n8n,integration engineer,automation engineer,workflow automation" \
  --days 30 \
  --out .tmp/jobs.csv
```

What the tool does, in order:

1. fetches each source, caching every raw payload under `.tmp/cache/`
2. normalizes each posting to
   `source, title, company, location, salary, date_posted, url, description, job_hash`
3. deduplicates by canonical URL (query string and fragment stripped, host
   lowercased, `www.` and trailing slash removed); when the same URL appears
   twice, the record with the longer description wins
4. applies the keyword filter
5. writes `.tmp/search_sources_<ts>_raw.json`, `..._filtered.json`, and the CSV
6. prints a counts summary to stderr, the filtered records as JSON to stdout

### Useful flags

| Flag | Effect |
|---|---|
| `--replay` | reuse cached payloads, no network calls — use this while tuning filters |
| `--since YYYY-MM-DD` / `--days N` | drop older postings; undated postings are kept |
| `--salary-min 3000` | monthly USD floor; postings with no stated salary are kept |
| `--stopwords-title-only` | stop-words apply to the title only, not the body |
| `--hh-all-schedules` | stop restricting hh.ru to remote postings |
| `--hh-no-details` | skip hh.ru detail calls (much faster, much weaker filtering) |
| `--include` / `--exclude` / `--keywords` | override the keyword lists |

---

## Step 3: Filter Rules

Include (keep a posting when **any** of these matches title or description):

`n8n, integration, automation, workflow, webhook, REST API, Stripe, Supabase,
Make, Zapier, LLM, OpenAI, prompt`

Exclude (drop a posting when **any** of these matches):

`PyTorch, RAG, vector database, Kubernetes, Terraform, DevOps, SRE,
machine learning, Data Scientist, Java, .NET, PHP`

Matching is regex with word boundaries, not substring matching. The terms that
need care are defined explicitly in `KEYWORD_PATTERNS`:

- `Java` must not fire on **JavaScript** — the pattern carries a negative
  lookahead. This is the single most damaging false positive in the list.
- `Make` is the automation platform, not the verb. Only `Make.com`,
  `Integromat`, or `make` sitting next to an automation word counts; otherwise
  "make an impact" would match every posting ever written.
- `RAG` and `SRE` match case-sensitively, so "storage", "drag" and "sre" inside
  other words stay quiet.
- `prompt` needs either the engineering sense ("prompt engineering",
  "prompting") or an AI word nearby, because "prompt payment" is ordinary
  English.
- `REST API` needs the REST sense or an integration context. Bare "API" is
  deliberately not enough — it would match nearly every technical posting.
  **Loosen this one first if the result set dries up.**
- `integration`, `automation`, `workflow`, `webhook` also match their Russian
  stems, because hh.ru postings are written in Russian.
- `integration`, `automation`, `workflow` **on their own only count when the
  job title reads as a technical/automation role** (title matches
  `TITLE_TECH_PATTERN`: engineer, developer, architect, automation,
  integration, implementation, sysadmin). Calibrated against a live
  RemoteOK + Remotive run on 2026-09-26: unguarded, these three words alone
  matched HR onboarding, payroll, marketing, an ERP lead role ("M&A
  integration"), and clinical-ops postings — 26 of 44 initially-kept jobs in
  that run were noise from exactly this. Every other include term (n8n,
  webhook, REST API, Stripe, Supabase, Make, Zapier, LLM, OpenAI, prompt) had
  zero false positives in the same run, so they still count on their own.
  Known residual case this doesn't catch: a title containing "Engineer" whose
  description uses "integrating" in a non-technical sense (e.g. "integrating
  the ecosystem of stakeholders") slips through — keyword filtering can't
  fully close that gap; the scoring step (Step 5 of the roadmap below) will.

Fail-open rules, matching the spirit of the old Indeed tool:

- a posting with no date is never dropped by `--since`
- a posting with no stated salary is never dropped by `--salary-min`
- a description that failed to download falls back to the search snippet

---

## Step 4: Review And Tune

Read the stderr summary before the CSV. It reports per-source counts, how many
duplicates collapsed, how many postings each stop-word killed, and which
include-words matched.

Tuning loop — no network calls needed:

```text
python3 tools/search_sources.py --replay --out .tmp/jobs.csv
```

Symptoms and first moves:

| Symptom | First move |
|---|---|
| Almost nothing survives | check `Stop-words that fired` — one broad term is usually doing the damage; try `--stopwords-title-only` |
| Too much noise | tighten the phrases in `--query` before touching the keyword lists |
| Good roles dropped over a "nice to have" line | `--stopwords-title-only` |
| hh.ru returns very little | drop `--hh-all-schedules` in or widen `--query`; remote postings are a small slice of hh.ru |

---

## Error Handling

| Error | Action |
|---|---|
| 403 on CONNECT / proxy denial | the environment's network policy blocks the host. Report the host; do not route around it |
| HTTP 403 from RemoteOK | it rejects clients without a real User-Agent; verify `USER_AGENT` is being sent |
| `{"errors":[{"type":"forbidden"}]}` from hh.ru with a `ddos-guard` server header, specifically on `/vacancies` while `/areas` still works | hh.ru's own anti-scraping layer is blocking the client's IP range on the search endpoint, not the environment's network policy (confirmed 2026-09-26: this happens even once the host is allowed through the proxy). Do not attempt to route around a site's own anti-bot protection (rotating IPs, spoofing headers, headless-browser rendering). Report it and run hh.ru from a non-datacenter connection instead, or drop it from `--sources` for cloud-environment runs |
| hh.ru 400 with `captcha_required` | too many anonymous calls too fast; raise the pause in `Fetcher`, rerun later |
| A single source fails | the tool logs a `WARN` line and continues with the others — a partial run is better than none |
| `--replay` with no cache | run once without `--replay` first |

All raw payloads stay in `.tmp/cache/`, so a failed run can be diagnosed
without re-hitting the APIs.

---

## Tools Used
- `tools/search_sources.py` — collection, normalization, dedup, filter, CSV
- `tools/sheets.py` — not used in this version (Sheets comes later)

---

## Not Yet Wired Up
When this moves past MVP:

1. append survivors to the Google Sheet via `tools/sheets.py` (the `job_hash`
   field is already populated for that dedup path)
2. score against the role profile: Integration & Automation Engineer, 5 years,
   n8n / REST API / Stripe / Supabase / OpenAI / Gemini / Claude API / Bubble /
   Railway; remote only; from $3000/month; written English fluent, spoken
   intermediate; not pure ML, not infrastructure, not night-shift time zones
3. tailor a resume per surviving job via `workflows/tailor_resume.md`
4. notify via `tools/notify.py`
