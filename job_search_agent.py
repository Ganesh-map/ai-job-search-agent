"""Autonomous cloud job search agent.

Runs one search cycle, creates a fresh Google Spreadsheet, and uploads a cloud
log file to Google Drive. Runtime state stays in memory; credentials and config
come only from environment variables.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any, Iterable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


IST = timezone(timedelta(hours=5, minutes=30))
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TARGET_ROLES = [
    "Data Analyst",
    "Business Analyst",
    "Business Intelligence Analyst",
    "BI Analyst",
    "Product Analyst",
    "Marketing Analyst",
    "Operations Analyst",
    "Revenue Analyst",
    "Strategy Analyst",
    "Financial Analyst",
    "Risk Analyst",
    "Fraud Analyst",
    "Supply Chain Analyst",
    "Customer Insights Analyst",
    "Growth Analyst",
    "Pricing Analyst",
    "Reporting Analyst",
    "Sales Analyst",
    "CRM Analyst",
    "Workforce Analyst",
    "Research Analyst",
    "Analytics Engineer",
    "Decision Scientist",
    "Data Quality Analyst",
    "Web Analyst",
    "People Analyst",
    "HR Analyst",
    "Performance Analyst",
    "FP&A Analyst",
    "Marketplace Analyst",
    "Experimentation Analyst",
    "A/B Testing Analyst",
    "Insights Analyst",
    "Commercial Analyst",
    "Data Operations Analyst",
    "MIS Analyst",
    "Program Analyst",
    "SQL Analyst",
    "Digital Analyst",
    "Customer Success Analyst",
]

BANNED_TITLE_RE = re.compile(
    r"\b(senior|sr\.?|lead|manager|director|head\s+of|vp|vice\s+president|principal)\b",
    re.IGNORECASE,
)

EARLY_CAREER_RE = re.compile(
    r"\b(intern(ship)?|fresher|fresh(er)? graduate|graduate|entry[-\s]?level|"
    r"junior|associate|trainee|0\s*-\s*2|0\s*to\s*2|0\s*-\s*3|0\s*to\s*3|"
    r"0\s*yoe|1\s*yoe|2\s*yoe|3\s*yoe)\b",
    re.IGNORECASE,
)

INDIA_RE = re.compile(
    r"\b(india|remote\s*-\s*india|bengaluru|bangalore|hyderabad|pune|mumbai|"
    r"delhi|new delhi|gurgaon|gurugram|noida|chennai|kolkata|ahmedabad|jaipur|"
    r"kochi|cochin|indore|lucknow|chandigarh|navi mumbai|thane|coimbatore|"
    r"trivandrum|thiruvananthapuram|vadodara|surat|nagpur|bhopal|mysore|mysuru|"
    r"visakhapatnam|vizag|india remote)\b",
    re.IGNORECASE,
)

REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|telecommute)\b", re.IGNORECASE)
HYBRID_RE = re.compile(r"\b(hybrid)\b", re.IGNORECASE)
ONSITE_RE = re.compile(r"\b(on[-\s]?site|office|in office)\b", re.IGNORECASE)

ROLE_GROUP_SIZE = int(os.getenv("ROLE_GROUP_SIZE", "5"))
MAX_SEARCH_QUERIES = int(os.getenv("MAX_SEARCH_QUERIES", "45"))
SEARCH_RESULTS_PER_QUERY = min(int(os.getenv("SEARCH_RESULTS_PER_QUERY", "10")), 10)
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "30"))
MIN_RELEVANT_JOBS = int(os.getenv("MIN_RELEVANT_JOBS", "10"))
PRIMARY_LOOKBACK_DAYS = int(os.getenv("PRIMARY_LOOKBACK_DAYS", "6"))
FALLBACK_LOOKBACK_DAYS = int(os.getenv("FALLBACK_LOOKBACK_DAYS", "8"))
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "12"))
PAGE_TIMEOUT_SECONDS = int(os.getenv("PAGE_TIMEOUT_SECONDS", "5"))
SEARCH_THROTTLE_SECONDS = float(os.getenv("SEARCH_THROTTLE_SECONDS", "1.0"))
SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "").strip().lower()
MAX_PAGE_FETCHES = int(os.getenv("MAX_PAGE_FETCHES", "120"))
MAX_CANDIDATES_PER_QUERY = int(os.getenv("MAX_CANDIDATES_PER_QUERY", "4"))
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", "780"))
MIN_SECONDS_FOR_OUTPUT = int(os.getenv("MIN_SECONDS_FOR_OUTPUT", "75"))


@dataclass(frozen=True)
class SearchSource:
    name: str
    query_prefixes: tuple[str, ...]


@dataclass
class Job:
    title: str
    company: str
    location: str
    experience_required: str
    work_mode: str
    match_score: float
    apply_link: str
    source_platform: str
    date_posted: datetime | None
    summary: str
    raw_text: str


SEARCH_SOURCES = [
    SearchSource("LinkedIn Jobs", ("site:linkedin.com/jobs/view",)),
    SearchSource("Indeed India", ("site:in.indeed.com/viewjob", "site:in.indeed.com/jobs")),
    SearchSource("Glassdoor India", ("site:glassdoor.co.in/job-listing", "site:glassdoor.co.in/Job")),
    SearchSource("Naukri", ("site:naukri.com/job-listings",)),
    SearchSource("Internshala", ("site:internshala.com/internship", "site:internshala.com/job")),
    SearchSource("Wellfound", ("site:wellfound.com/jobs",)),
    SearchSource("Cutshort", ("site:cutshort.io/job",)),
    SearchSource("Instahyre", ("site:instahyre.com/job",)),
    SearchSource("Y Combinator Jobs", ("site:ycombinator.com/jobs",)),
    SearchSource(
        "Public Company Career Page",
        (
            "site:jobs.lever.co",
            "site:boards.greenhouse.io",
            "site:jobs.ashbyhq.com",
            "site:apply.workable.com",
        ),
    ),
]


class AgentConfigError(RuntimeError):
    """Raised when required cloud configuration is missing."""


class SearchConfigurationError(RuntimeError):
    """Raised when Google Custom Search credentials or quota are invalid."""


class NoRelevantJobsError(RuntimeError):
    """Raised when a run cannot produce a useful spreadsheet."""


class SecretRedactionFilter(logging.Filter):
    """Remove configured secrets from logs before they are persisted."""

    SECRET_QUERY_RE = re.compile(r"([?&](?:key|access_token|refresh_token)=)[^&\s]+")

    def filter(self, record: logging.LogRecord) -> bool:
        secret_names = (
            "GOOGLE_SEARCH_API_KEY",
            "GOOGLE_CSE_ID",
            "SERPER_API_KEY",
            "GOOGLE_OAUTH_CLIENT_ID",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "GOOGLE_OAUTH_REFRESH_TOKEN",
            "GOOGLE_SERVICE_ACCOUNT_JSON",
            "GOOGLE_SERVICE_ACCOUNT_B64",
        )
        values = [os.getenv(name) for name in secret_names if os.getenv(name)]

        def redact(value: Any) -> Any:
            text = str(value)
            for secret in values:
                text = text.replace(secret, "[REDACTED]")
            return self.SECRET_QUERY_RE.sub(r"\1[REDACTED]", text)

        record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {key: redact(value) for key, value in record.args.items()}
            else:
                record.args = tuple(redact(arg) for arg in record.args)
        return True


def make_logger() -> tuple[logging.Logger, io.StringIO]:
    log_stream = io.StringIO()
    logger = logging.getLogger("job_search_agent")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(SecretRedactionFilter())
    logger.addHandler(stream_handler)

    memory_handler = logging.StreamHandler(log_stream)
    memory_handler.setFormatter(formatter)
    memory_handler.addFilter(SecretRedactionFilter())
    logger.addHandler(memory_handler)
    return logger, log_stream


LOGGER, LOG_STREAM = make_logger()


def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise AgentConfigError(f"Missing required environment variable: {name}")
    return value


def load_google_credentials() -> Credentials | UserCredentials:
    oauth_refresh_token = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN")
    oauth_client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
    oauth_client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
    if oauth_refresh_token and oauth_client_id and oauth_client_secret:
        return UserCredentials(
            token=None,
            refresh_token=oauth_refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=oauth_client_id,
            client_secret=oauth_client_secret,
            scopes=SCOPES,
        )

    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    raw_b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_B64")
    if raw_b64:
        raw_json = base64.b64decode(raw_b64).decode("utf-8")
    if not raw_json:
        raise AgentConfigError(
            "Set GOOGLE_OAUTH_CLIENT_ID/GOOGLE_OAUTH_CLIENT_SECRET/"
            "GOOGLE_OAUTH_REFRESH_TOKEN, or GOOGLE_SERVICE_ACCOUNT_JSON/"
            "GOOGLE_SERVICE_ACCOUNT_B64."
        )
    info = json.loads(raw_json)
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def build_http_session(enable_retries: bool = True) -> requests.Session:
    if enable_retries:
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=1.0,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD", "POST"]),
            respect_retry_after_header=True,
        )
    else:
        retry = Retry(total=0, connect=0, read=0, status=0)
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (compatible; JobSearchAgent/1.0; "
                "+https://github.com/actions)"
            )
        }
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def build_queries(lookback_days: int) -> list[tuple[str, str]]:
    role_groups = list(chunks(TARGET_ROLES, ROLE_GROUP_SIZE))
    queries: list[tuple[str, str]] = []
    for group in role_groups:
        for source in SEARCH_SOURCES:
            for prefix in source.query_prefixes:
                role_clause = " OR ".join(f'"{role}"' for role in group)
                query = (
                    f"{prefix} ({role_clause}) "
                    '(India OR Bengaluru OR Bangalore OR Hyderabad OR Pune OR Mumbai '
                    'OR Delhi OR Gurugram OR Gurgaon OR Noida OR Chennai OR Remote) '
                    '("fresher" OR "entry level" OR internship OR intern OR graduate '
                    'OR "0 years" OR "0-1 years" OR "0-2 years" OR "1-3 years") '
                    "-senior -manager -director -principal -vp"
                )
                queries.append((source.name, query))
                if len(queries) >= MAX_SEARCH_QUERIES:
                    LOGGER.info(
                        "Query cap reached at %s queries for d%s window.",
                        MAX_SEARCH_QUERIES,
                        lookback_days,
                    )
                    return queries
    return queries


def google_search(
    session: requests.Session,
    api_key: str,
    cse_id: str,
    query: str,
    lookback_days: int,
) -> list[dict[str, Any]]:
    params = {
        "key": api_key,
        "cx": cse_id,
        "q": query,
        "num": SEARCH_RESULTS_PER_QUERY,
        "gl": "in",
        "cr": "countryIN",
        "dateRestrict": f"d{lookback_days}",
        "safe": "off",
        "fields": "items(title,link,snippet)",
    }
    response = session.get(
        "https://www.googleapis.com/customsearch/v1",
        params=params,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code == 403:
        raise SearchConfigurationError(
            "Google Custom Search returned 403 Forbidden. Check that "
            "GOOGLE_SEARCH_API_KEY is valid, unrestricted by application, "
            "restricted to Custom Search API only, and that Custom Search API "
            f"is enabled/quota is available. Response: {response.text[:500]}"
        )
    if response.status_code == 429:
        raise SearchConfigurationError(
            "Google Custom Search returned 429 Too Many Requests. The daily "
            "quota or per-minute rate limit is exhausted; stopping this run "
            f"to avoid creating an empty spreadsheet. Response: {response.text[:500]}"
        )
    response.raise_for_status()
    return response.json().get("items", [])


def serper_search(
    session: requests.Session,
    api_key: str,
    query: str,
    lookback_days: int,
) -> list[dict[str, Any]]:
    response = session.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={
            "q": query,
            "gl": "in",
            "hl": "en",
            "num": SEARCH_RESULTS_PER_QUERY,
            "tbs": f"qdr:d{lookback_days}",
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code in (401, 403):
        raise SearchConfigurationError(
            "Serper returned an authentication/permission error. Check that "
            "SERPER_API_KEY is valid. Response: "
            f"{response.text[:500]}"
        )
    if response.status_code == 429:
        raise SearchConfigurationError(
            "Serper returned 429 Too Many Requests. The quota or rate limit is "
            f"exhausted. Response: {response.text[:500]}"
        )
    response.raise_for_status()
    organic = response.json().get("organic", [])
    return [
        {
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        }
        for item in organic
    ]


def configured_search_provider() -> str:
    if SEARCH_PROVIDER:
        return SEARCH_PROVIDER
    if os.getenv("SERPER_API_KEY"):
        return "serper"
    return "google"


def run_search(
    session: requests.Session,
    provider: str,
    query: str,
    lookback_days: int,
) -> list[dict[str, Any]]:
    if provider == "serper":
        return serper_search(session, get_required_env("SERPER_API_KEY"), query, lookback_days)
    if provider == "google":
        return google_search(
            session,
            get_required_env("GOOGLE_SEARCH_API_KEY"),
            get_required_env("GOOGLE_CSE_ID"),
            query,
            lookback_days,
        )
    raise AgentConfigError(f"Unsupported SEARCH_PROVIDER: {provider}")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = BeautifulSoup(str(value), "html.parser").get_text(" ")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def domain_from_url(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def source_from_url(url: str, fallback: str) -> str:
    domain = domain_from_url(url)
    if "linkedin.com" in domain:
        return "LinkedIn Jobs"
    if "indeed.com" in domain:
        return "Indeed India"
    if "glassdoor.co.in" in domain:
        return "Glassdoor India"
    if "naukri.com" in domain:
        return "Naukri"
    if "internshala.com" in domain:
        return "Internshala"
    if "wellfound.com" in domain:
        return "Wellfound"
    if "cutshort.io" in domain:
        return "Cutshort"
    if "instahyre.com" in domain:
        return "Instahyre"
    if "ycombinator.com" in domain:
        return "Y Combinator Jobs"
    return fallback


def flatten_jsonld(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from flatten_jsonld(child)
    elif isinstance(value, list):
        for child in value:
            yield from flatten_jsonld(child)


def find_jobposting_jsonld(soup: BeautifulSoup) -> dict[str, Any] | None:
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        if not script.string:
            continue
        try:
            parsed = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        for node in flatten_jsonld(parsed):
            node_type = node.get("@type") or node.get("type")
            node_types = node_type if isinstance(node_type, list) else [node_type]
            if any(str(t).lower() == "jobposting" for t in node_types if t):
                return node
    return None


def value_from_path(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def location_from_jsonld(jobposting: dict[str, Any]) -> str:
    locations = jobposting.get("jobLocation") or jobposting.get("applicantLocationRequirements")
    if not isinstance(locations, list):
        locations = [locations] if locations else []
    parts: list[str] = []
    for location in locations:
        if isinstance(location, str):
            parts.append(location)
            continue
        if not isinstance(location, dict):
            continue
        address = location.get("address", location)
        if isinstance(address, str):
            parts.append(address)
            continue
        if isinstance(address, dict):
            for key in ("addressLocality", "addressRegion", "addressCountry", "name"):
                value = address.get(key)
                if isinstance(value, dict):
                    value = value.get("name")
                if value:
                    parts.append(str(value))
    return ", ".join(dict.fromkeys(clean_text(part) for part in parts if part))


def company_from_jsonld(jobposting: dict[str, Any]) -> str:
    organization = jobposting.get("hiringOrganization") or jobposting.get("organization")
    if isinstance(organization, dict):
        return clean_text(organization.get("name"))
    return clean_text(organization)


def parse_absolute_date(text: str) -> datetime | None:
    if not text:
        return None
    candidate = text.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=IST)
        return parsed.astimezone(IST)
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d",
        "%d-%b-%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d, %Y",
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(candidate, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def parse_posted_date(text: str, now: datetime) -> datetime | None:
    text = clean_text(text)
    lowered = text.lower()
    if re.search(r"\b(today|just posted)\b", lowered):
        return now
    if "yesterday" in lowered:
        return now - timedelta(days=1)

    relative = re.search(r"\b(\d+)\s+(day|days|hour|hours)\s+ago\b", lowered)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        return now - (timedelta(hours=amount) if unit.startswith("hour") else timedelta(days=amount))

    for match in re.finditer(
        r"\b(20\d{2}-\d{1,2}-\d{1,2}|\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2}|"
        r"[A-Za-z]{3,9}\s+\d{1,2},\s+20\d{2})\b",
        text,
    ):
        parsed = parse_absolute_date(match.group(1))
        if parsed:
            return parsed
    return None


def extract_experience(text: str) -> tuple[str, float | None]:
    cleaned = clean_text(text)
    lowered = cleaned.lower()
    if re.search(r"\b(intern(ship)?|fresher|0\s*yoe|0\s*years?)\b", lowered):
        return "0 years / fresher", 0.0

    patterns = [
        r"\b(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?|yoe)\b",
        r"\b(\d+(?:\.\d+)?)\s*\+\s*(?:years?|yrs?|yoe)\b",
        r"\b(\d+(?:\.\d+)?)\s*(?:years?|yrs?|yoe)\b",
    ]
    matches: list[tuple[float, float]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, lowered):
            if len(match.groups()) == 2 and match.group(2):
                low = float(match.group(1))
                high = float(match.group(2))
            else:
                low = high = float(match.group(1))
            matches.append((low, high))
    if matches:
        low, high = min(matches, key=lambda pair: pair[1])
        if low == high:
            return f"{int(high) if high.is_integer() else high} years", high
        return (
            f"{int(low) if low.is_integer() else low}-{int(high) if high.is_integer() else high} years",
            high,
        )

    if EARLY_CAREER_RE.search(lowered):
        return "Entry-level / early career", 3.0
    return "Not specified", None


def detect_work_mode(text: str) -> str:
    if REMOTE_RE.search(text):
        return "Remote"
    if HYBRID_RE.search(text):
        return "Hybrid"
    if ONSITE_RE.search(text):
        return "On-site"
    return "Not specified"


def role_relevance(title: str, text: str) -> float:
    normalized_title = title.lower()
    normalized_text = text.lower()
    best = 0.0
    for role in TARGET_ROLES:
        role_l = role.lower()
        if role_l in normalized_title:
            best = max(best, 1.0)
        elif all(word in normalized_title for word in re.findall(r"[a-z]+", role_l)):
            best = max(best, 0.85)
        elif role_l in normalized_text:
            best = max(best, 0.7)
    analyst_like = re.search(
        r"\b(analyst|analytics engineer|decision scientist|sql analyst|insights|reporting)\b",
        normalized_title,
    )
    if analyst_like:
        best = max(best, 0.65)
    return best


def infer_title_company(search_title: str, fallback_company: str = "") -> tuple[str, str]:
    title = clean_text(search_title)
    company = clean_text(fallback_company)
    for sep in (" | ", " - ", " at "):
        if sep in title:
            left, right = title.split(sep, 1)
            if not company:
                company = right.split(" | ")[0].split(" - ")[0].strip()
            title = left.strip()
            break
    return title, company


def summarize(text: str, max_chars: int = 360) -> str:
    cleaned = clean_text(text)
    if len(cleaned) <= max_chars:
        return cleaned
    truncated = cleaned[:max_chars].rsplit(" ", 1)[0]
    return f"{truncated}..."


def fetch_page(session: requests.Session, url: str) -> BeautifulSoup:
    response = session.get(url, timeout=PAGE_TIMEOUT_SECONDS)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def is_apply_link_alive(session: requests.Session, url: str) -> bool:
    try:
        response = session.head(url, allow_redirects=True, timeout=PAGE_TIMEOUT_SECONDS)
        if 200 <= response.status_code < 400:
            return True
        if response.status_code in (403, 405):
            response = session.get(url, allow_redirects=True, timeout=PAGE_TIMEOUT_SECONDS)
            return 200 <= response.status_code < 400
        return False
    except requests.RequestException:
        return False


def extract_job_from_result(
    session: requests.Session,
    item: dict[str, Any],
    fallback_source: str,
    lookback_days: int,
    now: datetime,
) -> Job | None:
    url = item.get("link", "")
    if not url:
        return None

    source = source_from_url(url, fallback_source)
    search_title = clean_text(item.get("title"))
    snippet = clean_text(item.get("snippet"))
    title, company = infer_title_company(search_title)
    location = ""
    experience_required = "Not specified"
    max_experience: float | None = None
    date_posted = parse_posted_date(snippet, now)
    valid_through: datetime | None = None
    summary_source = snippet
    raw_text = f"{search_title} {snippet}"

    try:
        soup = fetch_page(session, url)
        page_text = clean_text(soup.get_text(" ", strip=True))
        raw_text = f"{raw_text} {page_text[:5000]}"
        jobposting = find_jobposting_jsonld(soup)
        if jobposting:
            title = clean_text(jobposting.get("title")) or title
            company = company_from_jsonld(jobposting) or company
            location = location_from_jsonld(jobposting)
            description = clean_text(jobposting.get("description"))
            experience_required, max_experience = extract_experience(
                " ".join(
                    clean_text(value)
                    for value in (
                        jobposting.get("experienceRequirements"),
                        jobposting.get("qualifications"),
                        description,
                    )
                )
            )
            date_posted = (
                parse_absolute_date(clean_text(jobposting.get("datePosted")))
                or date_posted
            )
            valid_through = parse_absolute_date(clean_text(jobposting.get("validThrough")))
            summary_source = description or summary_source
            if jobposting.get("jobLocationType"):
                raw_text += f" {jobposting.get('jobLocationType')}"
        else:
            meta_description = ""
            meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
                "meta", attrs={"property": "og:description"}
            )
            if meta and meta.get("content"):
                meta_description = clean_text(meta["content"])
            page_title = clean_text(soup.title.string if soup.title else "")
            if page_title:
                title, company = infer_title_company(page_title, company)
            summary_source = meta_description or summary_source
            experience_required, max_experience = extract_experience(
                f"{search_title} {snippet} {meta_description} {page_text[:3000]}"
            )
            date_posted = date_posted or parse_posted_date(
                f"{snippet} {meta_description} {page_text[:1200]}", now
            )
            location_match = INDIA_RE.search(f"{snippet} {meta_description} {page_text[:2000]}")
            if location_match:
                location = location_match.group(0)
    except requests.RequestException as exc:
        LOGGER.warning("Skipping unreachable source page %s: %s", url, exc)
        return None
    except Exception as exc:  # Keep one bad page from stopping the run.
        LOGGER.exception("Could not parse source page %s: %s", url, exc)
        return None

    if not max_experience and experience_required == "Not specified":
        experience_required, max_experience = extract_experience(raw_text)

    if not location:
        location = "India" if INDIA_RE.search(raw_text) else "Not specified"

    work_mode = detect_work_mode(raw_text)
    if not company:
        company = domain_from_url(url).split(".")[0].title()

    job = Job(
        title=title,
        company=company,
        location=location,
        experience_required=experience_required,
        work_mode=work_mode,
        match_score=0,
        apply_link=url,
        source_platform=source,
        date_posted=date_posted,
        summary=summarize(summary_source),
        raw_text=raw_text,
    )

    if not passes_filters(job, max_experience, valid_through, lookback_days, now):
        return None
    if not is_apply_link_alive(session, url):
        LOGGER.info("Discarding broken or inaccessible apply link: %s", url)
        return None

    job.match_score = calculate_match_score(job, max_experience, now)
    return job


def passes_filters(
    job: Job,
    max_experience: float | None,
    valid_through: datetime | None,
    lookback_days: int,
    now: datetime,
) -> bool:
    if not job.title or BANNED_TITLE_RE.search(job.title):
        return False
    relevance = role_relevance(job.title, job.raw_text)
    if relevance < 0.55:
        return False
    if max_experience is None and not EARLY_CAREER_RE.search(job.raw_text):
        return False
    if max_experience is not None and max_experience > 3:
        return False
    if not INDIA_RE.search(f"{job.location} {job.raw_text}"):
        return False
    if job.date_posted is None:
        return False
    if now - job.date_posted > timedelta(days=lookback_days):
        return False
    if valid_through and valid_through < now:
        return False
    return True


def calculate_match_score(job: Job, max_experience: float | None, now: datetime) -> float:
    title_component = role_relevance(job.title, job.raw_text) * 10

    if max_experience is None:
        seniority_component = 5.0
    elif max_experience <= 0:
        seniority_component = 10.0
    elif max_experience <= 1:
        seniority_component = 9.0
    elif max_experience <= 2:
        seniority_component = 8.0
    else:
        seniority_component = 6.5

    if job.date_posted is None:
        recency_component = 5.0
    else:
        age_days = max((now - job.date_posted).total_seconds() / 86400, 0)
        if age_days <= 2:
            recency_component = 10.0
        elif age_days <= 4:
            recency_component = 8.0
        elif age_days <= 6:
            recency_component = 6.0
        else:
            recency_component = 4.0

    work_mode_component = {
        "Remote": 10.0,
        "Hybrid": 8.0,
        "On-site": 7.0,
        "Not specified": 6.0,
    }.get(job.work_mode, 6.0)

    score = (
        title_component * 0.40
        + seniority_component * 0.30
        + recency_component * 0.20
        + work_mode_component * 0.10
    )
    return round(max(1.0, min(10.0, score)), 1)


def dedupe_jobs(jobs: list[Job]) -> list[Job]:
    deduped: dict[tuple[str, str], Job] = {}
    for job in jobs:
        key = (
            re.sub(r"[^a-z0-9]+", "", job.title.lower()),
            re.sub(r"[^a-z0-9]+", "", job.company.lower()),
        )
        existing = deduped.get(key)
        if not existing or job.match_score > existing.match_score:
            deduped[key] = job
    return sorted(deduped.values(), key=lambda item: item.match_score, reverse=True)


def collect_jobs(
    lookback_days: int,
    now: datetime,
    deadline: float | None = None,
) -> list[Job]:
    search_session = build_http_session(enable_retries=True)
    page_session = build_http_session(enable_retries=False)
    jobs: list[Job] = []
    seen_urls: set[str] = set()
    successful_queries = 0
    page_fetches = 0
    provider = configured_search_provider()
    deadline = deadline or time.monotonic() + MAX_RUNTIME_SECONDS - MIN_SECONDS_FOR_OUTPUT

    queries = build_queries(lookback_days)
    LOGGER.info(
        "Running %s %s search queries with d%s recency.",
        len(queries),
        provider,
        lookback_days,
    )
    for index, (source, query) in enumerate(queries, start=1):
        if time.monotonic() >= deadline:
            LOGGER.warning("Stopping search early to preserve time for spreadsheet output.")
            break
        try:
            items = run_search(search_session, provider, query, lookback_days)
            successful_queries += 1
        except SearchConfigurationError:
            raise
        except requests.RequestException as exc:
            LOGGER.warning("Search query failed for %s (%s/%s): %s", source, index, len(queries), exc)
            continue

        LOGGER.info("Search %s/%s [%s] returned %s results.", index, len(queries), source, len(items))
        for item in items[:MAX_CANDIDATES_PER_QUERY]:
            if page_fetches >= MAX_PAGE_FETCHES or time.monotonic() >= deadline:
                LOGGER.warning("Stopping page checks early to preserve time for spreadsheet output.")
                break
            url = item.get("link", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            page_fetches += 1
            job = extract_job_from_result(page_session, item, source, lookback_days, now)
            if job:
                jobs.append(job)
        if page_fetches >= MAX_PAGE_FETCHES or time.monotonic() >= deadline:
            break
        if SEARCH_THROTTLE_SECONDS > 0:
            time.sleep(SEARCH_THROTTLE_SECONDS)

    if successful_queries == 0:
        raise SearchConfigurationError(
            "No Google Custom Search queries succeeded. Check quota, API key "
            "restrictions, Custom Search API enablement, and CSE configuration."
        )

    return dedupe_jobs(jobs)


def format_date(date_value: datetime | None) -> str:
    return date_value.astimezone(IST).strftime("%d-%b-%Y") if date_value else "Unknown"


def spreadsheet_rows(jobs: list[Job]) -> list[list[Any]]:
    header = [
        "#",
        "Job Title",
        "Company",
        "Location",
        "Experience Required",
        "Work Mode",
        "Match Score",
        "Apply Link",
        "Source Platform",
        "Date Posted",
        "Job Summary (2-3 lines)",
    ]
    rows: list[list[Any]] = [header]
    for index, job in enumerate(jobs[:MAX_RESULTS], start=1):
        rows.append(
            [
                index,
                job.title,
                job.company,
                job.location,
                job.experience_required,
                job.work_mode,
                job.match_score,
                job.apply_link,
                job.source_platform,
                format_date(job.date_posted),
                job.summary,
            ]
        )
    return rows


def create_spreadsheet(
    sheets_service: Any,
    drive_service: Any,
    folder_id: str,
    jobs: list[Job],
    now: datetime,
) -> str:
    title = f"JobSearch_{now.strftime('%Y-%m-%d')}"
    created_file = (
        drive_service.files()
        .create(
            body={
                "name": title,
                "mimeType": "application/vnd.google-apps.spreadsheet",
                "parents": [folder_id],
            },
            fields="id, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
    spreadsheet_id = created_file["id"]
    spreadsheet_url = created_file.get(
        "webViewLink", f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
    )

    rows = spreadsheet_rows(jobs)
    sheets_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range="A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()

    last_row = max(len(rows), 2)
    requests_body = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": 0,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                        "backgroundColor": {"red": 0.9, "green": 0.94, "blue": 1.0},
                    }
                },
                "fields": "userEnteredFormat(textFormat,backgroundColor)",
            }
        },
        {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": 0,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": 11,
                }
            }
        },
        conditional_format_rule(0, last_row, 8, 10, {"red": 0.72, "green": 0.88, "blue": 0.66}),
        conditional_format_rule(0, last_row, 5, 7.999, {"red": 1.0, "green": 0.91, "blue": 0.49}),
        conditional_format_rule(0, last_row, 1, 4.999, {"red": 0.96, "green": 0.63, "blue": 0.61}),
    ]
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests_body}
    ).execute()
    LOGGER.info("Created spreadsheet: %s", spreadsheet_url)
    return spreadsheet_url


def conditional_format_rule(
    sheet_id: int,
    last_row: int,
    min_value: float,
    max_value: float,
    color: dict[str, float],
) -> dict[str, Any]:
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [
                    {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": last_row,
                        "startColumnIndex": 6,
                        "endColumnIndex": 7,
                    }
                ],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": f'=AND($G2>={min_value},$G2<={max_value})'}],
                    },
                    "format": {"backgroundColor": color},
                },
            },
            "index": 0,
        }
    }


def upload_cloud_log(drive_service: Any, folder_id: str, now: datetime) -> None:
    log_bytes = LOG_STREAM.getvalue().encode("utf-8")
    media = MediaIoBaseUpload(io.BytesIO(log_bytes), mimetype="text/plain", resumable=False)
    name = f"JobSearchLog_{now.strftime('%Y-%m-%d_%H%M%S_IST')}.txt"
    drive_service.files().create(
        body={"name": name, "parents": [folder_id], "mimeType": "text/plain"},
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    LOGGER.info("Uploaded cloud log file: %s", name)


def run() -> str:
    started = time.monotonic()
    deadline = started + MAX_RUNTIME_SECONDS - MIN_SECONDS_FOR_OUTPUT
    now = datetime.now(IST)
    credentials = load_google_credentials()
    sheets_service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    drive_service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    folder_id = get_required_env("GOOGLE_DRIVE_FOLDER_ID")

    jobs = collect_jobs(PRIMARY_LOOKBACK_DAYS, now, deadline)
    LOGGER.info("Found %s relevant jobs in primary window.", len(jobs))
    if len(jobs) < MIN_RELEVANT_JOBS and FALLBACK_LOOKBACK_DAYS > PRIMARY_LOOKBACK_DAYS:
        LOGGER.info(
            "Fewer than %s jobs found; extending search window to %s days.",
            MIN_RELEVANT_JOBS,
            FALLBACK_LOOKBACK_DAYS,
        )
        jobs = collect_jobs(FALLBACK_LOOKBACK_DAYS, now, deadline)
        LOGGER.info("Found %s relevant jobs in fallback window.", len(jobs))

    jobs = jobs[:MAX_RESULTS]
    if not jobs:
        raise NoRelevantJobsError(
            "No relevant jobs were found after the primary and fallback search windows; "
            "not creating an empty spreadsheet."
        )
    spreadsheet_url = create_spreadsheet(sheets_service, drive_service, folder_id, jobs, now)
    LOGGER.info("Execution completed in %.1f seconds.", time.monotonic() - started)
    upload_cloud_log(drive_service, folder_id, now)
    return spreadsheet_url


def main() -> int:
    try:
        spreadsheet_url = run()
        print(f"Spreadsheet created: {spreadsheet_url}")
        return 0
    except (
        AgentConfigError,
        SearchConfigurationError,
        NoRelevantJobsError,
        HttpError,
        requests.RequestException,
        Exception,
    ) as exc:
        LOGGER.exception("Job search agent failed: %s", exc)
        try:
            credentials = load_google_credentials()
            drive_service = build("drive", "v3", credentials=credentials, cache_discovery=False)
            upload_cloud_log(drive_service, get_required_env("GOOGLE_DRIVE_FOLDER_ID"), datetime.now(IST))
        except Exception as log_exc:
            LOGGER.error("Could not upload failure log to Drive: %s", log_exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
