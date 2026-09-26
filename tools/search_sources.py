"""
Tool: search_sources.py

Purpose:
    Replacement for the Indeed search layer. Collects job postings from three
    public sources that need no authentication, normalizes them to the shape the
    rest of the pipeline already expects, deduplicates by URL, applies an
    include/exclude keyword filter, and writes a CSV.

Sources:
    hh.ru     https://api.hh.ru/vacancies       (open API, no auth, RU market)
    RemoteOK  https://remoteok.com/api          (single JSON feed of all live jobs)
    Remotive  https://remotive.com/api/remote-jobs

Normalized record (same keys search_indeed.py produced, plus provenance):
    {
        "source":        "hh" | "remoteok" | "remotive",
        "title":         str,
        "company":       str,
        "location":      str,
        "salary":        str,    # human readable, "" when the posting has none
        "date_posted":   str,    # ISO "YYYY-MM-DD" ("" when the source omits it)
        "url":           str,    # canonical link to the posting
        "description":   str,    # plain text, HTML stripped
        "job_hash":      str,    # stable id, "<source>:<native id>"
        "matched":       str,    # include-keywords that fired, comma separated
    }

Usage:
    # live run, defaults for everything
    python3 tools/search_sources.py --out .tmp/jobs.csv

    # narrow it down
    python3 tools/search_sources.py \
        --sources hh,remoteok,remotive \
        --query "n8n,integration,automation engineer" \
        --since 2026-08-01 \
        --salary-min 3000 \
        --out .tmp/jobs.csv

    # replay the last run's raw payloads instead of calling the network
    python3 tools/search_sources.py --replay --out .tmp/jobs.csv

Exit codes:
    0 = success (even when 0 jobs survive filtering)
    1 = error (details on stderr)

Notes:
    - Every raw API payload is cached under .tmp/cache/ so a run can be replayed
      offline with --replay. That makes filter tuning free: no repeated API load.
    - Filtering is "fail open" in the same spirit as search_indeed.py. A missing
      date or an unparseable salary never drops a job; only an explicit stop-word
      hit or a missing include-keyword does.
    - Keyword matching is regex with word boundaries, not naive substring
      matching. See KEYWORD_PATTERNS for the terms that need special care
      ("Make", "Java", "RAG", ".NET", "prompt").
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

try:
    import requests
except ImportError:  # pragma: no cover - environment problem, not logic
    print("ERROR: requests is not installed. Run: pip install requests", file=sys.stderr)
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

# hh.ru asks for a descriptive User-Agent and will throttle anonymous clients
# that do not send one.
USER_AGENT = "JobSearchAutomation/1.0 (personal job search; contact via github)"

DEFAULT_QUERIES = [
    "n8n",
    "integration engineer",
    "automation engineer",
    "workflow automation",
    "API integration",
]

DEFAULT_INCLUDE = [
    "n8n", "integration", "automation", "workflow", "webhook", "REST API",
    "Stripe", "Supabase", "Make", "Zapier", "LLM", "OpenAI", "prompt",
]

DEFAULT_EXCLUDE = [
    "PyTorch", "RAG", "vector database", "Kubernetes", "Terraform", "DevOps",
    "SRE", "machine learning", "Data Scientist", "Java", ".NET", "PHP",
]

# Static, deliberately conservative conversion rates to monthly USD. Only used
# by the optional --salary-min filter, and only when a posting states a number.
# Update them when they drift; nothing else in the pipeline depends on them.
FX_TO_USD = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "RUR": 0.011,
    "RUB": 0.011,
    "KZT": 0.0019,
    "BYR": 0.31,
    "UAH": 0.024,
}

CACHE_DIR = os.path.join(".tmp", "cache")


# ---------------------------------------------------------------------------
# Keyword matching
# ---------------------------------------------------------------------------
#
# Substring matching would be wrong here in both directions:
#   "Java"  would fire on "JavaScript"      -> a good job silently dropped
#   "Make"  would fire on "make an impact"  -> every posting looks like a match
#   "RAG"   would fire on "storage"/"drag"  -> ditto
# So each term compiles to an explicit pattern. Terms not listed here fall back
# to a word-boundary match of the literal term, with flexible separators for
# multi-word terms ("REST API" also matches "REST-API" and "RESTAPI").

KEYWORD_PATTERNS = {
    # --- include terms -----------------------------------------------------
    "n8n": r"\bn8n\b",
    # English + Russian stems: hh.ru postings are mostly in Russian.
    "integration": r"\bintegrat(?:e|es|ed|ing|ion|ions)\b|интеграц",
    "automation": r"\bautomat(?:e|es|ed|ing|ion|ions)\b|автоматизац",
    "workflow": r"\bwork[\s\-]?flows?\b|бизнес[\s\-]?процесс",
    "webhook": r"\bweb[\s\-]?hooks?\b|вебхук",
    # Bare "API" would match almost every tech posting, so require the REST
    # sense or an integration context. Loosen this first if results dry up.
    "REST API": (
        r"\brest[\s\-]?api\b|\brestful\b|\bapi\s+integration\b"
        r"|\bapis?\b(?=[^.\n]{0,60}\b(?:integrat|automat|webhook|endpoint|интеграц)\w*)"
    ),
    "Stripe": r"\bstripe\b",
    "Supabase": r"\bsupabase\b",
    # "Make" is the automation platform, not the English verb. Require the
    # branded forms or an automation word close by.
    "Make": (
        r"\bmake\.com\b|\bintegromat\b"
        r"|\bmake\b(?=[^.\n]{0,40}\b(?:zapier|n8n|automation|scenario|автоматизац)\b)"
    ),
    "Zapier": r"\bzapier\b",
    "LLM": r"\bllms?\b|\bgpt-?\d|\bclaude\b|\bgemini\b",
    "OpenAI": r"\bopen\s?ai\b",
    # "prompt" is also ordinary English ("prompt payment"). Require either the
    # engineering sense or an AI word nearby.
    "prompt": (
        r"\bprompt[\s\-]?engineer(?:ing)?\b|\bprompting\b"
        r"|\bprompts?\b(?=[^.\n]{0,60}\b(?:llm|gpt|ai|model|openai|claude|gemini|нейросет)\b)"
    ),

    # --- exclude terms -----------------------------------------------------
    "PyTorch": r"\bpy[\s\-]?torch\b",
    # Uppercase only: avoids "drag", "storage", "rag".
    "RAG": (r"\bRAG\b", 0),
    "vector database": r"\bvector\s+(?:database|db|store|search)\b|\bpinecone\b|\bweaviate\b|\bqdrant\b",
    "Kubernetes": r"\bkubernetes\b|\bk8s\b",
    "Terraform": r"\bterraform\b",
    "DevOps": r"\bdev[\s\-]?ops\b",
    "SRE": (r"\bSRE\b|\bSite Reliability Engineer\b", 0),
    "machine learning": r"\bmachine\s+learning\b|\bml\s+engineer\b|машинн\w*\s+обучени",
    "Data Scientist": r"\bdata\s+scien(?:tist|ce)\b|дата[\s\-]?саентист",
    # The single most important negative lookahead in this file.
    "Java": r"\bjava\b(?!\s*script)(?!script)",
    ".NET": r"(?<!\w)\.net\b|\bdot\s?net\b|\basp\.net\b|\bc#",
    "PHP": r"\bphp\b|\blaravel\b|\bsymfony\b",
}


def compile_term(term: str) -> tuple[str, re.Pattern]:
    """
    Compile one keyword into (term, pattern).

    Terms present in KEYWORD_PATTERNS use their curated pattern. Anything else
    (a term the user passed on the command line) becomes a word-boundary match
    with flexible whitespace, so "power automate" also matches "power-automate".
    """
    spec = KEYWORD_PATTERNS.get(term)
    flags = re.IGNORECASE
    if isinstance(spec, tuple):
        pattern, flags = spec
    elif spec:
        pattern = spec
    else:
        parts = [re.escape(p) for p in term.split()]
        pattern = r"\b" + r"[\s\-]*".join(parts) + r"\b"
    return term, re.compile(pattern, flags)


def compile_terms(terms: list[str]) -> list[tuple[str, re.Pattern]]:
    return [compile_term(t) for t in terms if t.strip()]


def parse_keywords(raw: str) -> dict:
    """
    Parse the .env KEYWORDS string, same format search_indeed.py used:
        "include:python,sql exclude:unpaid,internship"
    Kept so a shared .env works for both the old and the new search layer.
    """
    result = {"include": [], "exclude": []}
    if not raw:
        return result
    for part in raw.strip().split():
        if part.startswith("include:"):
            result["include"] = [k.strip() for k in part[8:].split(",") if k.strip()]
        elif part.startswith("exclude:"):
            result["exclude"] = [k.strip() for k in part[8:].split(",") if k.strip()]
    return result


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """Minimal HTML -> text. stdlib only, no bs4 dependency."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []

    def handle_data(self, data):
        self.chunks.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p", "li", "div", "tr"):
            self.chunks.append("\n")

    def text(self) -> str:
        return "".join(self.chunks)


def strip_html(raw: str) -> str:
    """Turn a possibly-HTML description into collapsed plain text."""
    if not raw:
        return ""
    if "<" in raw and ">" in raw:
        parser = _TextExtractor()
        try:
            parser.feed(raw)
            raw = parser.text()
        except Exception:
            raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    raw = re.sub(r"\n\s*\n+", "\n", raw)
    return raw.strip()


def normalize_url(url: str) -> str:
    """
    Canonical form of a posting URL, used as the dedup key.

    Drops query strings and fragments (the same posting arrives with different
    tracking parameters from different sources), lowercases the host, strips a
    trailing slash and a leading "www.".
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    scheme = parts.scheme.lower() or "https"
    return urlunsplit((scheme, host, path, "", ""))


def make_job_hash(source: str, native_id: str, url: str) -> str:
    """
    Stable identifier for a posting, in the spirit of sheets.extract_job_hash().

    Prefers the source's own id; falls back to a hash of the canonical URL so
    the field is never empty.
    """
    if native_id:
        return f"{source}:{str(native_id).lower()}"
    digest = hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:16]
    return f"{source}:{digest}"


def iso_date(value) -> str:
    """Best-effort conversion of a source timestamp to 'YYYY-MM-DD'."""
    if not value:
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value).strip()
    if not text:
        return ""
    # Most sources here emit ISO 8601; normalize the trailing Z first.
    candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).date().isoformat()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text[:20].strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


# ---------------------------------------------------------------------------
# Salary helpers
# ---------------------------------------------------------------------------

def salary_to_usd_month(amount: float, currency: str, period: str) -> float | None:
    """Convert a stated salary figure to approximate monthly USD."""
    rate = FX_TO_USD.get((currency or "USD").upper())
    if rate is None or not amount:
        return None
    usd = amount * rate
    if period == "year":
        usd /= 12.0
    elif period == "hour":
        usd *= 160.0
    return usd


def guess_salary_usd_month(text: str) -> float | None:
    """
    Pull a monthly-USD figure out of a free-text salary string (Remotive).

    Handles "$60k - $80k", "USD 4,000/month", "€45,000 per year". Returns the
    lower bound, because that is what a minimum-salary filter should compare
    against. Returns None when nothing numeric is found — callers must treat
    None as "unknown", never as "too low".
    """
    if not text:
        return None
    lowered = text.lower()
    currency = "USD"
    if "€" in text or "eur" in lowered:
        currency = "EUR"
    elif "£" in text or "gbp" in lowered:
        currency = "GBP"
    elif "₽" in text or "руб" in lowered or "rub" in lowered:
        currency = "RUB"

    matches = re.findall(r"(\d[\d\s.,]*)\s*(k\b)?", lowered)
    values: list[float] = []
    for raw_number, k_suffix in matches:
        cleaned = raw_number.replace(" ", "").replace(",", "").rstrip(".")
        if not cleaned or not re.match(r"^\d+(\.\d+)?$", cleaned):
            continue
        value = float(cleaned)
        if k_suffix:
            value *= 1000
        if value >= 100:  # ignore "40 hours", "2 years"
            values.append(value)
    if not values:
        return None

    low = min(values)
    # Heuristic: a figure this large is an annual package, not a monthly one.
    period = "year" if low >= 20000 else "month"
    if "hour" in lowered or "/hr" in lowered:
        period = "hour"
    return salary_to_usd_month(low, currency, period)


# ---------------------------------------------------------------------------
# HTTP with an on-disk cache
# ---------------------------------------------------------------------------

class Fetcher:
    """
    Thin wrapper over requests that caches every response body on disk.

    Two reasons this exists:
      1. --replay lets filters be tuned against real payloads with no network
         calls and no API load.
      2. When a run fails halfway, the raw payloads are still on disk for
         debugging, which is what the CLAUDE.md failure-handling rules ask for.
    """

    def __init__(self, cache_dir: str = CACHE_DIR, replay: bool = False,
                 timeout: int = 30, pause: float = 0.34):
        self.cache_dir = cache_dir
        self.replay = replay
        self.timeout = timeout
        self.pause = pause
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        os.makedirs(self.cache_dir, exist_ok=True)

    def _cache_path(self, key: str) -> str:
        safe = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
        label = re.sub(r"[^a-z0-9]+", "_", key.lower())[:60].strip("_")
        return os.path.join(self.cache_dir, f"{label}_{safe}.json")

    def get_json(self, url: str, params: dict | None = None, label: str = ""):
        key = label or f"{url}?{sorted((params or {}).items())}"
        path = self._cache_path(key)

        if self.replay:
            if not os.path.exists(path):
                raise FileNotFoundError(
                    f"--replay was requested but no cached payload exists for '{key}' "
                    f"(expected {path}). Run once without --replay first."
                )
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)

        response = self.session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        time.sleep(self.pause)  # be a polite anonymous client
        return payload


# ---------------------------------------------------------------------------
# Source: hh.ru
# ---------------------------------------------------------------------------

HH_SEARCH_URL = "https://api.hh.ru/vacancies"


def fetch_hh(fetcher: Fetcher, queries: list[str], pages: int = 2,
             per_page: int = 50, area: str | None = None,
             remote_only: bool = True, want_details: bool = True) -> list[dict]:
    """
    Search hh.ru. Open API, no auth.

    The search endpoint returns a short snippet rather than a full description,
    so each hit is followed by a detail call — the keyword filter is only as
    good as the text it sees. Detail calls are cached like everything else.
    """
    normalized: list[dict] = []
    seen_ids: set[str] = set()

    for query in queries:
        for page in range(pages):
            params = {
                "text": query,
                "per_page": per_page,
                "page": page,
                "order_by": "publication_time",
            }
            if remote_only:
                params["schedule"] = "remote"
            if area:
                params["area"] = area

            label = f"hh_search_{query}_p{page}"
            try:
                payload = fetcher.get_json(HH_SEARCH_URL, params, label=label)
            except FileNotFoundError:
                break  # replay mode: this page was never fetched
            except Exception as exc:
                print(f"WARN hh.ru search '{query}' page {page}: {exc}", file=sys.stderr)
                break

            items = payload.get("items", [])
            for item in items:
                vacancy_id = str(item.get("id", ""))
                if not vacancy_id or vacancy_id in seen_ids:
                    continue
                seen_ids.add(vacancy_id)
                normalized.append(_normalize_hh(item, fetcher, want_details))

            if page + 1 >= payload.get("pages", 0):
                break

    return normalized


def _hh_salary_text(salary: dict | None) -> tuple[str, float | None]:
    if not salary:
        return "", None
    low, high = salary.get("from"), salary.get("to")
    currency = (salary.get("currency") or "").upper()
    parts = []
    if low:
        parts.append(f"from {low:,.0f}".replace(",", " "))
    if high:
        parts.append(f"to {high:,.0f}".replace(",", " "))
    text = " ".join(parts)
    if text and currency:
        text = f"{text} {currency}"
    usd = salary_to_usd_month(low or high or 0, currency, "month")
    return text, usd


def _normalize_hh(item: dict, fetcher: Fetcher, want_details: bool) -> dict:
    vacancy_id = str(item.get("id", ""))
    url = item.get("alternate_url") or f"https://hh.ru/vacancy/{vacancy_id}"

    description = ""
    if want_details:
        try:
            detail = fetcher.get_json(f"{HH_SEARCH_URL}/{vacancy_id}",
                                      label=f"hh_detail_{vacancy_id}")
            description = strip_html(detail.get("description", ""))
            key_skills = ", ".join(s.get("name", "") for s in detail.get("key_skills", []))
            if key_skills:
                description = f"{description}\nKey skills: {key_skills}"
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"WARN hh.ru detail {vacancy_id}: {exc}", file=sys.stderr)

    if not description:
        snippet = item.get("snippet") or {}
        description = strip_html(
            " ".join(filter(None, [snippet.get("requirement"), snippet.get("responsibility")]))
        )

    salary_text, salary_usd = _hh_salary_text(item.get("salary"))
    employer = (item.get("employer") or {}).get("name", "")
    area = (item.get("area") or {}).get("name", "")
    schedule = (item.get("schedule") or {}).get("name", "")
    location = ", ".join(filter(None, [area, schedule]))

    return {
        "source": "hh",
        "title": item.get("name", ""),
        "company": employer,
        "location": location,
        "salary": salary_text,
        "date_posted": iso_date(item.get("published_at")),
        "url": url,
        "description": description,
        "job_hash": make_job_hash("hh", vacancy_id, url),
        "salary_usd_month": salary_usd,
    }


# ---------------------------------------------------------------------------
# Source: RemoteOK
# ---------------------------------------------------------------------------

REMOTEOK_URL = "https://remoteok.com/api"


def fetch_remoteok(fetcher: Fetcher) -> list[dict]:
    """
    RemoteOK publishes every live posting in one JSON array. There is no server
    side search, so everything is filtered locally.

    The first element of the array is a legal/attribution notice, not a job —
    it is identified by the absence of an "id" and skipped.
    """
    try:
        payload = fetcher.get_json(REMOTEOK_URL, label="remoteok_all")
    except FileNotFoundError:
        return []
    except Exception as exc:
        print(f"WARN RemoteOK: {exc}", file=sys.stderr)
        return []

    if not isinstance(payload, list):
        print("WARN RemoteOK: unexpected payload shape", file=sys.stderr)
        return []

    jobs = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("id"):
            continue  # the legal notice element
        jobs.append(_normalize_remoteok(item))
    return jobs


def _normalize_remoteok(item: dict) -> dict:
    job_id = str(item.get("id", ""))
    url = item.get("url") or f"https://remoteok.com/remote-jobs/{job_id}"

    low = item.get("salary_min") or 0
    high = item.get("salary_max") or 0
    if low or high:
        salary_text = " - ".join(f"${v:,.0f}" for v in (low, high) if v) + " / year"
    else:
        salary_text = ""
    # RemoteOK salaries are annual USD.
    salary_usd = salary_to_usd_month(low or high, "USD", "year") if (low or high) else None

    tags = item.get("tags") or []
    description = strip_html(item.get("description", ""))
    if tags:
        description = f"{description}\nTags: {', '.join(tags)}"

    return {
        "source": "remoteok",
        "title": item.get("position") or item.get("title", ""),
        "company": item.get("company", ""),
        "location": item.get("location") or "Remote",
        "salary": salary_text,
        "date_posted": iso_date(item.get("date") or item.get("epoch")),
        "url": url,
        "description": description,
        "job_hash": make_job_hash("remoteok", job_id, url),
        "salary_usd_month": salary_usd,
    }


# ---------------------------------------------------------------------------
# Source: Remotive
# ---------------------------------------------------------------------------

REMOTIVE_URL = "https://remotive.com/api/remote-jobs"


def fetch_remotive(fetcher: Fetcher, queries: list[str], limit: int = 100) -> list[dict]:
    """
    Remotive supports a server-side `search` parameter, so one call per query
    keeps the payloads small.
    """
    jobs: list[dict] = []
    seen_ids: set[str] = set()

    for query in queries:
        params = {"search": query, "limit": limit}
        try:
            payload = fetcher.get_json(REMOTIVE_URL, params, label=f"remotive_{query}")
        except FileNotFoundError:
            continue
        except Exception as exc:
            print(f"WARN Remotive '{query}': {exc}", file=sys.stderr)
            continue

        for item in payload.get("jobs", []):
            job_id = str(item.get("id", ""))
            if job_id and job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            jobs.append(_normalize_remotive(item))

    return jobs


def _normalize_remotive(item: dict) -> dict:
    job_id = str(item.get("id", ""))
    url = item.get("url", "")
    salary_text = (item.get("salary") or "").strip()

    description = strip_html(item.get("description", ""))
    tags = item.get("tags") or []
    if tags:
        description = f"{description}\nTags: {', '.join(tags)}"

    return {
        "source": "remotive",
        "title": item.get("title", ""),
        "company": item.get("company_name", ""),
        "location": item.get("candidate_required_location") or "Remote",
        "salary": salary_text,
        "date_posted": iso_date(item.get("publication_date")),
        "url": url,
        "description": description,
        "job_hash": make_job_hash("remotive", job_id, url),
        "salary_usd_month": guess_salary_usd_month(salary_text),
    }


# ---------------------------------------------------------------------------
# Dedup / filter
# ---------------------------------------------------------------------------

def deduplicate(jobs: list[dict]) -> tuple[list[dict], int]:
    """
    Drop repeats by canonical URL.

    When the same URL appears twice, the record with the longer description
    wins — that is the one the keyword filter can judge properly.
    """
    best: dict[str, dict] = {}
    order: list[str] = []
    duplicates = 0

    for job in jobs:
        key = normalize_url(job.get("url", "")) or job.get("job_hash", "")
        if not key:
            continue
        if key not in best:
            best[key] = job
            order.append(key)
            continue
        duplicates += 1
        if len(job.get("description", "")) > len(best[key].get("description", "")):
            best[key] = job

    return [best[k] for k in order], duplicates


def match_terms(text: str, terms: list[tuple[str, re.Pattern]]) -> list[str]:
    return [term for term, pattern in terms if pattern.search(text)]


def apply_filters(jobs: list[dict],
                  include: list[tuple[str, re.Pattern]],
                  exclude: list[tuple[str, re.Pattern]],
                  since: date | None = None,
                  salary_min_usd: float = 0.0,
                  title_only_stopwords: bool = False) -> tuple[list[dict], dict]:
    """
    Keyword / date / salary filter.

    Rules, all deliberately fail-open except the explicit ones:
      - keep only jobs where at least one include term matches title+description
      - drop jobs where any stop-word matches (title only, when
        title_only_stopwords is set — a passing mention of Kubernetes in a
        "nice to have" list should not kill an otherwise good integration role)
      - drop jobs posted before --since, but keep jobs with no date at all
      - drop jobs whose stated salary is below --salary-min, but keep jobs that
        state no salary
    """
    kept: list[dict] = []
    stats = {"no_include": 0, "stopword": 0, "too_old": 0, "low_salary": 0,
             "stopword_hits": {}, "include_hits": {}}

    for job in jobs:
        haystack = f"{job.get('title', '')}\n{job.get('description', '')}"
        stop_haystack = job.get("title", "") if title_only_stopwords else haystack

        hits = match_terms(haystack, include)
        if include and not hits:
            stats["no_include"] += 1
            continue

        blockers = match_terms(stop_haystack, exclude)
        if blockers:
            stats["stopword"] += 1
            for term in blockers:
                stats["stopword_hits"][term] = stats["stopword_hits"].get(term, 0) + 1
            continue

        if since and job.get("date_posted"):
            try:
                if date.fromisoformat(job["date_posted"]) < since:
                    stats["too_old"] += 1
                    continue
            except ValueError:
                pass  # unparseable date -> keep the job

        if salary_min_usd:
            stated = job.get("salary_usd_month")
            if stated is not None and stated < salary_min_usd:
                stats["low_salary"] += 1
                continue

        for term in hits:
            stats["include_hits"][term] = stats["include_hits"].get(term, 0) + 1
        job["matched"] = ", ".join(hits)
        kept.append(job)

    return kept, stats


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

CSV_COLUMNS = ["source", "title", "company", "location", "salary", "date_posted",
               "url", "description", "matched", "job_hash"]


def write_csv(jobs: list[dict], path: str, desc_max: int = 2000) -> str:
    """Write the normalized records to CSV (UTF-8 BOM, so Excel opens it clean)."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for job in jobs:
            row = {key: job.get(key, "") for key in CSV_COLUMNS}
            description = row["description"] or ""
            if desc_max and len(description) > desc_max:
                description = description[:desc_max].rstrip() + " …[truncated]"
            row["description"] = description
            writer.writerow(row)
    return path


def save_json(jobs: list[dict], suffix: str) -> str:
    os.makedirs(".tmp", exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f".tmp/search_sources_{stamp}{suffix}.json"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(jobs, handle, indent=2, ensure_ascii=False)
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect jobs from hh.ru, RemoteOK and Remotive; dedupe, filter, write CSV.")
    parser.add_argument("--sources", default="hh,remoteok,remotive",
                        help="Comma-separated subset of: hh, remoteok, remotive")
    parser.add_argument("--query", default=",".join(DEFAULT_QUERIES),
                        help="Comma-separated search phrases (hh.ru and Remotive)")
    parser.add_argument("--include", default="",
                        help="Override the required keywords (comma separated)")
    parser.add_argument("--exclude", default="",
                        help="Override the stop-words (comma separated)")
    parser.add_argument("--keywords", default="",
                        help='.env-style string: "include:a,b exclude:c,d". '
                             "Takes precedence over --include/--exclude.")
    parser.add_argument("--since", default="",
                        help="ISO date YYYY-MM-DD; drop older postings (undated ones are kept)")
    parser.add_argument("--days", type=int, default=0,
                        help="Shorthand for --since N days ago")
    parser.add_argument("--salary-min", type=float, default=0,
                        help="Minimum monthly USD; postings without a stated salary are kept")
    parser.add_argument("--hh-pages", type=int, default=2, help="hh.ru pages per query")
    parser.add_argument("--hh-per-page", type=int, default=50, help="hh.ru results per page")
    parser.add_argument("--hh-area", default="", help="hh.ru area id (e.g. 113 = Russia)")
    parser.add_argument("--hh-all-schedules", action="store_true",
                        help="Do not restrict hh.ru to remote postings")
    parser.add_argument("--hh-no-details", action="store_true",
                        help="Skip hh.ru per-vacancy detail calls (faster, weaker filtering)")
    parser.add_argument("--remotive-limit", type=int, default=100, help="Remotive results per query")
    parser.add_argument("--stopwords-title-only", action="store_true",
                        help="Apply stop-words to the title only, not the whole description")
    parser.add_argument("--desc-max", type=int, default=2000,
                        help="Truncate descriptions in the CSV at N characters (0 = no limit)")
    parser.add_argument("--out", default=".tmp/jobs.csv", help="CSV output path")
    parser.add_argument("--replay", action="store_true",
                        help="Reuse cached payloads in .tmp/cache instead of calling the network")
    parser.add_argument("--cache-dir", default=CACHE_DIR, help="Where raw payloads are cached")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    sources = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    queries = [q.strip() for q in args.query.split(",") if q.strip()]

    if args.keywords:
        parsed = parse_keywords(args.keywords)
        include_terms = parsed["include"] or DEFAULT_INCLUDE
        exclude_terms = parsed["exclude"] or DEFAULT_EXCLUDE
    else:
        include_terms = [k.strip() for k in args.include.split(",") if k.strip()] or DEFAULT_INCLUDE
        exclude_terms = [k.strip() for k in args.exclude.split(",") if k.strip()] or DEFAULT_EXCLUDE

    include = compile_terms(include_terms)
    exclude = compile_terms(exclude_terms)

    since = None
    if args.days:
        since = date.today() - timedelta(days=args.days)
    elif args.since.strip():
        try:
            since = date.fromisoformat(args.since.strip())
        except ValueError:
            print(f"ERROR: --since must be YYYY-MM-DD, got {args.since!r}", file=sys.stderr)
            return 1

    fetcher = Fetcher(cache_dir=args.cache_dir, replay=args.replay)

    raw: list[dict] = []
    per_source: dict[str, int] = {}

    if "hh" in sources:
        found = fetch_hh(fetcher, queries, pages=args.hh_pages, per_page=args.hh_per_page,
                         area=args.hh_area or None, remote_only=not args.hh_all_schedules,
                         want_details=not args.hh_no_details)
        per_source["hh"] = len(found)
        raw.extend(found)

    if "remoteok" in sources:
        found = fetch_remoteok(fetcher)
        per_source["remoteok"] = len(found)
        raw.extend(found)

    if "remotive" in sources:
        found = fetch_remotive(fetcher, queries, limit=args.remotive_limit)
        per_source["remotive"] = len(found)
        raw.extend(found)

    raw_path = save_json(raw, "_raw")
    deduped, duplicates = deduplicate(raw)
    kept, stats = apply_filters(deduped, include, exclude, since=since,
                                salary_min_usd=args.salary_min,
                                title_only_stopwords=args.stopwords_title_only)
    filtered_path = save_json(kept, "_filtered")
    csv_path = write_csv(kept, args.out, desc_max=args.desc_max)

    # Everything below goes to stderr so stdout stays a clean JSON document.
    print("# Source counts: " + ", ".join(f"{k}={v}" for k, v in per_source.items()),
          file=sys.stderr)
    print(f"# Collected {len(raw)} -> {len(deduped)} unique ({duplicates} duplicate URLs)",
          file=sys.stderr)
    print(f"# Dropped: {stats['no_include']} no include-keyword, {stats['stopword']} stop-word, "
          f"{stats['too_old']} older than --since, {stats['low_salary']} below --salary-min",
          file=sys.stderr)
    if stats["stopword_hits"]:
        top = sorted(stats["stopword_hits"].items(), key=lambda kv: -kv[1])
        print("# Stop-words that fired: " + ", ".join(f"{k}×{v}" for k, v in top), file=sys.stderr)
    if stats["include_hits"]:
        top = sorted(stats["include_hits"].items(), key=lambda kv: -kv[1])
        print("# Include-words that matched: " + ", ".join(f"{k}×{v}" for k, v in top),
              file=sys.stderr)
    print(f"# Kept {len(kept)} jobs -> {csv_path}", file=sys.stderr)
    print(f"# Raw backup: {raw_path} | filtered backup: {filtered_path}", file=sys.stderr)

    print(json.dumps(kept, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
