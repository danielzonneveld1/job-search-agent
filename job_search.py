#!/usr/bin/env python3
"""
Job search agent: finds new early-career roles across company career sites
and job boards, filters them against a personal search profile, and emails a
daily digest with one-click "Applied" / "Not for me" buttons that feed a
Google Sheet application tracker.

Sources:
  - Greenhouse, Lever, Ashby, Workable, Rippling: public job-board APIs.
  - Workday, Oracle Recruiting Cloud, SAP SuccessFactors, TalentBrew, Jibe,
    gr8people: enterprise career sites, configured per company.
  - Adzuna, Remotive, Jobicy, Arbeitnow: aggregators, for companies that are
    not configured directly (these pass an extra company-size check).

Filters: title keywords, seniority (title and years of experience read from
the posting), location, company size, student-only internships, required
languages, and rules learned from the user's own feedback notes.

All personal settings live in search_profile.json (start from
search_profile.example.json); secrets live in .env.

Run: python3 job_search.py
"""

import csv
import hashlib
import html
import json
import os
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


PROFILE_PATH = os.environ.get("SEARCH_PROFILE", "search_profile.json")
with open(PROFILE_PATH, encoding="utf-8") as _f:
    PROFILE = json.load(_f)

TRACKER_CSV = PROFILE["tracker_csv"]
OUTPUT_CSV = "job_search_results.csv"
OUTPUT_FIELDS = ["Title", "Company", "Location", "Date Found", "Source", "URL", "First Surfaced"]

COMPANIES = PROFILE["companies"]
ALL_COMPANIES = [name for group in COMPANIES.values() for name in group]
COMPANY_ALIASES = {name: aliases for group in COMPANIES.values() for name, aliases in group.items()}
COMPANY_TO_SECTOR = {name: sector for sector, group in COMPANIES.items() for name in group}
NON_CURATED_SECTOR = "other"

WATCHED_COMPANIES_PATH = "watched_companies.json"
WATCHED_COMPANY_NAMES = set()
ATS_SLUG_OVERRIDES = {}


def load_watched_companies():
    if not os.path.exists(WATCHED_COMPANIES_PATH):
        return
    try:
        with open(WATCHED_COMPANIES_PATH, encoding="utf-8") as f:
            entries = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [warn] couldn't read {WATCHED_COMPANIES_PATH}: {e}")
        return
    for entry in entries:
        name = (entry.get("name") or "").strip()
        sector = entry.get("sector") or NON_CURATED_SECTOR
        if not name or name in COMPANY_ALIASES:
            continue
        ALL_COMPANIES.append(name)
        COMPANY_ALIASES[name] = entry.get("aliases", [])
        COMPANY_TO_SECTOR[name] = sector
        WATCHED_COMPANY_NAMES.add(name)
        if "ats_slug" in entry:
            ATS_SLUG_OVERRIDES[name] = entry["ats_slug"] or None


load_watched_companies()

WORKDAY_COMPANIES = PROFILE["workday_companies"]

TALENTBREW_COMPANIES = PROFILE["talentbrew_companies"]

JIBE_COMPANIES = PROFILE["jibe_companies"]

SUCCESSFACTORS_COMPANIES = PROFILE["successfactors_rss_companies"]
SUCCESSFACTORS_SEARCH_COMPANIES = PROFILE["successfactors_search_companies"]

ORACLE_COMPANIES = PROFILE["oracle_companies"]

GR8PEOPLE_COMPANIES = PROFILE["gr8people_companies"]

RIPPLING_COMPANIES = PROFILE["rippling_companies"]

WORKABLE_COMPANIES = PROFILE["workable_companies"]

INCLUDE_KEYWORDS = PROFILE["include_title_keywords"]
EXCLUDE_KEYWORDS = PROFILE["exclude_title_keywords"]

MANAGER_JUNIOR_QUALIFIERS = PROFILE["manager_junior_qualifiers"]

AMBIGUOUS_FUNCTION_TERMS = PROFILE["ambiguous_function_terms"]
ROLE_ANCHOR_TERMS = PROFILE["role_anchor_terms"]


def _matches_term(text, term):
    """Whole-word match for single words, substring match for phrases."""
    if " " in term:
        return term in text
    return re.search(r"\b" + re.escape(term) + r"\b", text) is not None


def _is_unqualified_manager_title(t):
    return _matches_term(t, "manager") and not any(
        _matches_term(t, q) for q in MANAGER_JUNIOR_QUALIFIERS
    )


def _is_unanchored_ambiguous_title(t):
    has_ambiguous = any(_matches_term(t, k) for k in AMBIGUOUS_FUNCTION_TERMS)
    has_anchor = any(_matches_term(t, k) for k in ROLE_ANCHOR_TERMS)
    return has_ambiguous and not has_anchor


_SENIOR_TIER_SUFFIX = re.compile(r"\b(iii|iv|v|[3-9])\s*$", re.IGNORECASE)


def _has_senior_tier_suffix(title):
    return _SENIOR_TIER_SUFFIX.search(title.strip()) is not None


_YEAR_TOKEN = re.compile(r"\b(20\d{2})\b")


def _has_future_year(title):
    return any(int(y) > date.today().year for y in _YEAR_TOKEN.findall(title))


HOME_AREA_KEYWORDS = PROFILE["home_area_keywords"]
REMOTE_KEYWORDS = ["remote", "united states", "usa", "u.s.", "nationwide", "anywhere"]
NON_US_COUNTRIES = [
    "afghanistan", "albania", "algeria", "argentina", "armenia", "australia",
    "austria", "azerbaijan", "bahrain", "bangladesh", "belarus", "belgium",
    "bolivia", "bosnia", "brazil", "brasil", "bulgaria", "cambodia",
    "cameroon", "canada", "chile", "china", "colombia", "costa rica",
    "croatia", "cuba", "cyprus", "czech", "denmark", "ecuador", "egypt",
    "estonia", "ethiopia", "finland", "france", "germany",
    "ghana", "greece", "guatemala", "honduras", "hong kong", "hungary",
    "iceland", "india", "indonesia", "iran", "iraq", "ireland", "israel",
    "italy", "jamaica", "japan", "jordan", "kazakhstan", "kenya", "kuwait",
    "latvia", "lebanon", "lithuania", "luxembourg", "malaysia", "mexico",
    "moldova", "monaco", "mongolia", "morocco", "myanmar", "nepal",
    "netherlands", "new zealand", "nicaragua", "nigeria", "norway", "oman",
    "pakistan", "panama", "paraguay", "peru", "philippines", "poland",
    "portugal", "qatar", "romania", "russia", "saudi arabia", "serbia",
    "singapore", "slovakia", "slovenia", "south africa", "south korea",
    "spain", "sri lanka", "sweden", "switzerland", "taiwan", "thailand",
    "tunisia", "turkey", "uganda", "ukraine", "united arab emirates",
    "united kingdom", " uk,", " uk ", "uruguay", "uzbekistan", "venezuela",
    "vietnam", "wales", "scotland", "england", "zambia", "zimbabwe",
]

SEARCH_TERMS = PROFILE["search_terms"]

DIGEST_TOP_N = 30
DIGEST_MAX_PER_COMPANY = 3
DIGEST_HTML_PATH = "email_digest.html"

BACKLOG_DAYS = 14

ADZUNA_MAX_DAYS_OLD = 10
ADZUNA_BACKLOG_DAYS = 5

MAX_DIGEST_ROLES = 50

SECTOR_COLORS = PROFILE["sector_colors"]

SECTOR_DISPLAY_NAMES = PROFILE["sector_display_names"]

GOOGLE_MAX_QUERIES_PER_DAY = 100
REQUEST_TIMEOUT = 15
REQUEST_DELAY = 0.3

HEADERS = {"User-Agent": "Mozilla/5.0 (job-search-script; personal use)"}


def normalize(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def slugify_variants(company):
    if company in ATS_SLUG_OVERRIDES:
        slug = ATS_SLUG_OVERRIDES[company]
        return [slug] if slug else []
    base = company.lower().strip()
    no_amp = base.replace("&", "and")
    variants = {
        no_amp.replace(" ", ""),
        no_amp.replace(" ", "-"),
        base.split()[0].lower(),
    }
    return [v for v in variants if v]


def passes_keyword_filter(title):
    t = title.lower()
    if not any(_matches_term(t, k) for k in INCLUDE_KEYWORDS):
        return False
    if any(_matches_term(t, k) for k in EXCLUDE_KEYWORDS):
        return False
    if any(_matches_term(t, k.lower()) for k in LEARNED_RULES["exclude_title_phrases"]):
        return False
    if _is_unqualified_manager_title(t):
        return False
    if _is_unanchored_ambiguous_title(t):
        return False
    if _has_senior_tier_suffix(title):
        return False
    if _has_future_year(title):
        return False
    return True


YOE_MAX = PROFILE["max_years_experience"]
_YOE_PATTERN = re.compile(
    r"(\d{1,2})\s*(?:\+|-|\u2013|\u2014|to|or\s+more|or\s+greater|or\s+above)?\s*"
    r"\d{0,2}\+?\s*years?\b"
    r"(?=[^.]{0,80}\bexperience\b)",
    re.IGNORECASE,
)


def _strip_html(text):
    text = html.unescape(text or "")
    return re.sub(r"<[^>]+>", " ", text)


def extract_min_years_required(description):
    """First explicit 'N years [of] experience' figure in the text, or None
    if the description doesn't state one. Takes the first match rather than
    the smallest/largest, since postings that list both a minimum and a
    preferred/bonus qualification almost always state the actual minimum
    first."""
    text = _strip_html(description)
    match = _YOE_PATTERN.search(text)
    return int(match.group(1)) if match else None


YOE_STRETCH_MAX = PROFILE["stretch_years_experience"]


_COMPANY_SUFFIX_WORDS = {
    "inc", "llc", "corp", "corporation", "co", "company", "usa", "motor",
    "motors", "international", "group", "plc", "ltd", "na", "holdings",
    "of", "america", "gmbh", "ag", "the",
}


def normalize_company_for_match(name):
    n = (name or "").lower().replace("&", " and ")
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    tokens = n.split()
    while tokens and tokens[-1] in _COMPANY_SUFFIX_WORDS:
        tokens.pop()
    while tokens and tokens[0] in _COMPANY_SUFFIX_WORDS:
        tokens.pop(0)
    return " ".join(tokens)


def matches_curated_company(candidate_name):
    """Returns the canonical curated company name if candidate_name matches
    (by name or known alias, suffix-normalized), else None."""
    cand_norm = normalize_company_for_match(candidate_name)
    if not cand_norm:
        return None
    for canonical, aliases in COMPANY_ALIASES.items():
        for variant in (canonical, *aliases):
            if normalize_company_for_match(variant) == cand_norm:
                return canonical
    return None


def is_home_area(location):
    loc = (location or "").lower()
    return any(k in loc for k in HOME_AREA_KEYWORDS)


MAJOR_US_METRO_KEYWORDS = [
    "new york", "nyc", "brooklyn", "manhattan", "los angeles", "chicago",
    "dallas", "fort worth", "houston", "washington, dc", "washington dc",
    "washington, d.c.", "miami", "philadelphia", "atlanta", "boston",
    "phoenix", "detroit", "seattle", "minneapolis", "st. paul", "saint paul",
    "san diego", "tampa", "denver", "baltimore", "st. louis", "saint louis",
    "orlando", "charlotte", "san antonio", "portland", "sacramento",
    "pittsburgh", "austin", "las vegas", "cincinnati", "kansas city",
    "columbus", "indianapolis", "cleveland", "nashville", "providence",
    "milwaukee", "jacksonville", "raleigh", "salt lake city",
]


def is_major_metro(location):
    loc = (location or "").lower()
    return any(k in loc for k in MAJOR_US_METRO_KEYWORDS)


NON_US_MAJOR_CITIES = [
    "são paulo", "sao paulo", "rio de janeiro", "toronto", "vancouver",
    "montreal", "london", "dublin", "amsterdam", "berlin", "munich",
    "paris", "madrid", "barcelona", "milan", "rome", "zurich", "geneva",
    "stockholm", "copenhagen", "oslo", "helsinki", "warsaw", "prague",
    "vienna", "brussels", "lisbon", "athens", "bangalore", "bengaluru",
    "mumbai", "delhi", "hyderabad", "pune", "chennai", "gurgaon",
    "gurugram", "singapore", "hong kong", "tokyo", "osaka", "seoul",
    "shanghai", "beijing", "shenzhen", "taipei", "manila", "jakarta",
    "bangkok", "kuala lumpur", "sydney", "melbourne", "auckland",
    "mexico city", "buenos aires", "santiago", "bogota", "bogotá",
    "lima", "johannesburg", "cape town", "lagos", "nairobi", "cairo",
    "tel aviv", "dubai", "abu dhabi", "riyadh",
    "mississauga", "ottawa", "calgary", "edmonton", "winnipeg", "quebec city",
    "halifax", "brampton", "markham", "oakville", "laval", "saskatoon", "regina",
    "köln", "koln", "cologne", "hamburg", "frankfurt", "düsseldorf", "dusseldorf",
    "stuttgart", "leipzig", "dresden", "hannover", "nürnberg", "nuremberg", "bremen",
    "essen", "dortmund", "lyon", "marseille", "toulouse", "rotterdam", "the hague",
    "eindhoven", "antwerp", "kraków", "krakow", "wrocław", "wroclaw", "bucharest",
    "budapest", "porto", "valencia", "seville", "gothenburg", "edinburgh", "glasgow",
]


_FOREIGN_LANGUAGE_TITLE_MARKERS = [
    "werkstudent", "m/w/d", "w/m/d", "f/m/d", "m/f/d", "(f/m/*)", "(m/f/*)",
    "(m/w/x)", "(m/f/x)", "(w/m/x)", "(h/f)", "(f/h)",
    "praktikum", "praktikant", "stagiaire", "alternance", "alternant", "becario",
    "pasante", "tirocinio", "estágio", "estagiário", " im bereich ", " und ",
    "assistante", "chef de produit", "chargé", "chargée", "mitarbeiter", "referent",
]

UK_EU_KEYWORDS = [
    "united kingdom", "uk", "england", "scotland", "wales", "ireland", "germany",
    "deutschland", "france", "italy", "spain", "portugal", "netherlands", "belgium",
    "luxembourg", "austria", "sweden", "denmark", "finland", "poland", "czech",
    "slovakia", "hungary", "romania", "bulgaria", "greece", "croatia", "slovenia",
    "estonia", "latvia", "lithuania", "cyprus", "malta",
    "london", "manchester", "birmingham, uk", "dublin", "berlin", "munich", "münchen",
    "hamburg", "frankfurt", "stuttgart", "köln", "cologne", "düsseldorf", "wolfsburg",
    "ingolstadt", "paris", "lyon", "amsterdam", "rotterdam", "brussels", "milan",
    "turin", "torino", "rome", "madrid", "barcelona", "lisbon", "vienna",
    "stockholm", "gothenburg", "copenhagen", "helsinki", "warsaw", "prague", "budapest",
]


def is_uk_or_eu(location):
    loc = (location or "").lower()
    return any(re.search(r"\b" + re.escape(k) + r"\b", loc) for k in UK_EU_KEYWORDS)


def is_international_ok(company):
    canonical = matches_curated_company(company) or company
    return COMPANY_TO_SECTOR.get(canonical) in PROFILE["international_ok_sectors"]


def passes_location_filter(location, company="", title=""):
    if any(m in f" {(title or '').lower()} " for m in _FOREIGN_LANGUAGE_TITLE_MARKERS):
        return False
    if company and is_international_ok(company) and is_uk_or_eu(location):
        return True
    loc = (location or "").lower()
    if not loc:
        return True
    is_remote = any(k in loc for k in REMOTE_KEYWORDS)
    is_foreign = (any(c in loc for c in NON_US_COUNTRIES)
                  or any(c in loc for c in NON_US_MAJOR_CITIES))
    return is_remote or not is_foreign


US_STATE_NAMES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "district of columbia",
]


def looks_us_location(location):
    loc = (location or "").lower()
    if not loc:
        return True
    if is_home_area(loc) or any(k in loc for k in REMOTE_KEYWORDS):
        return True
    if "united states" in loc or "usa" in loc or "u.s." in loc:
        return True
    return any(state in loc for state in US_STATE_NAMES)


_STRONG_TITLE_TERMS = PROFILE["strong_title_terms"]
_CORE_SUBJECT_TERMS = PROFILE["core_subject_terms"]


def _is_clean_core_title(title):
    """A title squarely in the target function (an anchor term plus a strong
    junior title word) with no ops/strategy/compliance term riding along.
    Only these titles get the years-of-experience stretch: it is meant for
    a great-fit role, not a borderline one."""
    t = title.lower()
    has_anchor = any(_matches_term(t, k) for k in ROLE_ANCHOR_TERMS)
    has_strong = any(_matches_term(t, k) for k in _STRONG_TITLE_TERMS)
    has_ambiguous = any(_matches_term(t, k) for k in AMBIGUOUS_FUNCTION_TERMS)
    return has_anchor and has_strong and not has_ambiguous


_INTERN_TITLE = re.compile(r"\b(intern|interns|internship|co-op|mba)\b", re.IGNORECASE)
_ENROLLMENT_REQUIRED = re.compile(
    r"currently enrolled|current(ly)? (a )?student|currently pursuing|currently attending|"
    r"graduation date|year in school|degree in process|enrolled student|pursuing (an? )?"
    r"(bachelor|master|mba|degree|undergraduate|graduate)|enrolled (in|at) an? |rising "
    r"(junior|senior)|return(ing)? to (school|campus|your studies)|student status|"
    r"matriculated|mba candidate|full-time student|must be a student|"
    r"expected graduation|graduating (in|between)",
    re.IGNORECASE,
)

_SALES_DEV_TITLE = re.compile(r"\b(" + "|".join(re.escape(t) for t in PROFILE["sales_development_title_terms"]) + r")\b",
                        re.IGNORECASE)
_SDR_SIGNALS = PROFILE["sales_development_signals"]

_FOREIGN_LANGUAGE_REQUIRED = re.compile(
    r"flie(ß|ss)ende?s? deutsch|verhandlungssicher|deutschkenntnisse|"
    r"(fluent|fluency|proficien\w*|native|business[- ]level)\s+(in\s+)?"
    r"(german|french|spanish|italian|dutch|portuguese|japanese|mandarin|korean)|"
    r"(german|french|spanish|italian|dutch|portuguese|japanese|mandarin|korean)"
    r"\s+(language\s+)?(is\s+)?(required|mandatory|essential)",
    re.IGNORECASE,
)


LEARNED_RULES_PATH = "learned_rules.json"
LEARNED_RULES = {"exclude_title_phrases": [], "exclude_companies": [],
                 "exclude_description_phrases": [], "rules_log": []}


def load_learned_rules():
    try:
        with open(LEARNED_RULES_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        print(f"  [warn] couldn't read {LEARNED_RULES_PATH}, ignoring learned rules: {e}")
        return
    for key in LEARNED_RULES:
        if isinstance(data.get(key), list):
            LEARNED_RULES[key] = [x for x in data[key] if x]


def rules_learned_today():
    today = date.today().isoformat()
    return [r for r in LEARNED_RULES["rules_log"]
            if isinstance(r, dict) and r.get("added") == today and r.get("rule")]


def passes_description_checks(description, title, company):
    """Everything judged from the posting text: required years, plus rules
    distilled from the user's 👎 notes (student-only internships, sales-
    development roles hiding behind a broader title, a required foreign
    language, and anything the daily feedback-learning step has added)."""
    text = _strip_html(description or "")
    if _INTERN_TITLE.search(title or ""):
        if not text or _ENROLLMENT_REQUIRED.search(text):
            return False
    if text and _FOREIGN_LANGUAGE_REQUIRED.search(text):
        return False
    if text and any(p.lower() in text.lower() for p in LEARNED_RULES["exclude_description_phrases"]):
        return False
    t = (title or "").lower()
    if text and _SALES_DEV_TITLE.search(t) and not any(a in t for a in ROLE_ANCHOR_TERMS):
        low = text.lower()
        if sum(1 for s in _SDR_SIGNALS if s in low) >= 2:
            return False
    return passes_yoe_filter(description, title, company)


def passes_yoe_filter(description, title, company):
    """Permissive when no description is available or no explicit YOE is
    stated — this only screens OUT postings that name a number, it never
    screens IN based on absence of one. A stated requirement above YOE_MAX
    is still allowed up to YOE_STRETCH_MAX, but only for a clean core
    title at one of the curated companies — never for a
    borderline ops-flavored title or a random aggregator-found company."""
    if not description:
        return True
    min_years = extract_min_years_required(description)
    if min_years is None or min_years <= YOE_MAX:
        return True
    allow_stretch = (
        matches_curated_company(company) is not None
        and _is_clean_core_title(title)
    )
    return allow_stretch and min_years <= YOE_STRETCH_MAX


def compute_fit_score(row):
    location_score = 1.0 if is_home_area(row["Location"]) else 0.4

    company_score = 1.0 if matches_curated_company(row["Company"]) else 0.5

    t = row["Title"].lower()
    role_score = 0.4
    if any(_matches_term(t, k) for k in _STRONG_TITLE_TERMS):
        role_score += 0.35
    if any(_matches_term(t, k) for k in _CORE_SUBJECT_TERMS):
        role_score += 0.25
    role_score = min(role_score, 1.0)

    match_score = (company_score + role_score) / 2
    return 0.6 * location_score + 0.4 * match_score


def load_dedup_keys():
    keys = set()
    if os.path.exists(TRACKER_CSV):
        with open(TRACKER_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            reader.fieldnames = [fn.strip() for fn in reader.fieldnames]
            for row in reader:
                title = row.get("Title", "")
                company = row.get("Company", "")
                if title and company:
                    keys.add((normalize(title), normalize(company)))
    if os.path.exists(OUTPUT_CSV):
        with open(OUTPUT_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                title = row.get("Title", "")
                company = row.get("Company", "")
                if title and company:
                    keys.add((normalize(title), normalize(company)))
    return keys


def make_row(title, company, location, source, url):
    return {
        "Title": title.strip(),
        "Company": company.strip(),
        "Location": (location or "").strip(),
        "Date Found": date.today().isoformat(),
        "Source": source,
        "URL": url,
        "First Surfaced": "",
    }


def role_id(row):
    key = f"{normalize(row['Title'])}|{normalize(row['Company'])}"
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def load_feedback(webapp_url, token):
    """{"feedback": [...], "applied": [...]} from the Sheet, or None if the
    web app isn't set up yet or can't be reached (the digest still goes out,
    it just can't hide anything resolved since the last run)."""
    if not webapp_url or not token:
        return None
    try:
        resp = requests.get(webapp_url, params={"a": "x", "t": token}, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  [warn] couldn't load feedback from the Sheet: {e}")
        return None


def _title_tokens(title):
    return set(re.findall(r"[a-z0-9]+", (title or "").lower()))


def _same_company(a, b):
    na, nb = normalize_company_for_match(a), normalize_company_for_match(b)
    if not na or not nb:
        return False
    if na == nb or na.startswith(nb + " ") or nb.startswith(na + " "):
        return True
    ca, cb = matches_curated_company(a), matches_curated_company(b)
    return ca is not None and ca == cb


def _same_title(a, b):
    if normalize(a) == normalize(b):
        return True
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return False
    small, big = sorted((ta, tb), key=len)
    if len(small) >= 3 and small <= big:
        return True
    return len(ta & tb) / len(ta | tb) >= 0.8


class Resolutions:
    """What the user has already dealt with: roles clicked ✓/👎 in a digest,
    companies hidden via the "Not this company" reason, and everything on
    the Applied tab (including roles applied to outside the digest)."""

    def __init__(self, data):
        data = data or {}
        self.resolved_ids = set()
        self.hidden_companies = []
        for fb in data.get("feedback", []):
            if fb.get("id"):
                self.resolved_ids.add(fb["id"])
            if fb.get("reason") in ("Not this company", "Company too small") and fb.get("company"):
                self.hidden_companies.append(fb["company"])
        self.applied = [a for a in data.get("applied", []) if a.get("title")]

    def is_resolved(self, row):
        if role_id(row) in self.resolved_ids:
            return True
        if any(_same_company(row["Company"], c) for c in self.hidden_companies):
            return True
        return any(_same_company(row["Company"], a.get("company", ""))
                   and _same_title(row["Title"], a["title"]) for a in self.applied)


def listing_still_live(url):
    """Only an explicit 404/410 counts as gone — anything ambiguous (403,
    timeout, redirect to a search page) keeps the role, same permissive
    stance as the Workday/TalentBrew description checks. Workday's public
    job page answers 200 even once a posting closes, so those are checked
    through the posting API instead."""
    m = re.match(r"https://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/([^/]+)(/.+)", url or "")
    if m:
        tenant, wd, site, path = m.groups()
        detail_api = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
        return _fetch_workday_job_description(detail_api, path) is not LISTING_REMOVED
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True, stream=True)
        resp.close()
        return resp.status_code not in (404, 410)
    except requests.RequestException:
        return True


def load_surfaced_rows():
    if not os.path.exists(OUTPUT_CSV):
        return []
    with open(OUTPUT_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_digest_rows(resolutions):
    """Every role surfaced in the last BACKLOG_DAYS that's still unresolved
    and still posted — today's picks flagged as new, older ones as waiting."""
    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=BACKLOG_DAYS)).isoformat()
    adzuna_cutoff = (date.today() - timedelta(days=ADZUNA_BACKLOG_DAYS)).isoformat()
    candidates = [r for r in load_surfaced_rows()
                  if (adzuna_cutoff if r["Source"] == "Adzuna" else cutoff)
                  <= (r.get("First Surfaced") or "") <= today
                  and not resolutions.is_resolved(r)
                  and (matches_curated_company(r["Company"]) or passes_size_gate(r["Company"], r["Location"]))]
    older = [r for r in candidates if r["First Surfaced"] != today]
    with ThreadPoolExecutor(max_workers=8) as pool:
        live = list(pool.map(lambda r: listing_still_live(r["URL"]), older))
    gone = {id(r) for r, ok in zip(older, live) if not ok}
    if gone:
        print(f"  Dropped {len(gone)} waiting role(s) whose listing has been taken down.")
    return [r for r in candidates if id(r) not in gone]


def cap_digest_rows(rows):
    """(rows_to_show, number_left_out)."""
    if len(rows) <= MAX_DIGEST_ROLES:
        return rows, 0
    today = date.today().isoformat()
    new = [r for r in rows if r["First Surfaced"] == today]
    waiting = sorted((r for r in rows if r["First Surfaced"] != today),
                     key=lambda r: (r["First Surfaced"], compute_fit_score(r)), reverse=True)
    shown = new + waiting[:max(0, MAX_DIGEST_ROLES - len(new))]
    return shown, len(rows) - len(shown)


_US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "DC", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_STATE_NAMES = {v.lower(): v for v in _US_STATES.values()}


def sheet_location(raw):
    """Career sites write locations every which way ("USA-Illinois-Chicago",
    "US, Minnesota, Maplewood", "Chicago, IL", "United States of America :
    Remote"). The Applied tab uses "City, State" / "Remote", so tidy to that;
    anything unrecognised is passed through as-is."""
    loc = (raw or "").strip()
    if not loc:
        return ""
    if re.search(r"\bremote\b", loc, re.IGNORECASE) and not re.search(r",\s*[A-Z]{2}\b", loc):
        return "Remote"
    loc = re.sub(r"\s*\((usa|us)\)\s*$", "", loc, flags=re.IGNORECASE)
    loc = re.sub(r",\s*(usa|us|united states( of america)?)\s*$", "", loc, flags=re.IGNORECASE)
    parts = [p.strip() for p in re.split(r"\s*[-,]\s*", loc) if p.strip()]
    if parts and parts[0].lower() in ("usa", "us", "united states", "united states of america"):
        parts = parts[1:]
    if len(parts) != 2:
        return loc if len(parts) > 2 or not parts else parts[0]
    a, b = parts
    state_of = lambda s: _US_STATES.get(s.upper()) if len(s) == 2 else _STATE_NAMES.get(s.lower())
    if state_of(b):
        return f"{a.title() if a.isupper() else a}, {state_of(b)}"
    if state_of(a):
        return f"{b.title() if b.isupper() else b}, {state_of(a)}"
    return loc


def send_digest(webapp_url, token, subject, html_body, rows):
    roles = [{
        "id": role_id(r), "title": r["Title"], "company": r["Company"],
        "location": r["Location"], "sheet_location": sheet_location(r["Location"]),
        "source": r["Source"], "url": r["URL"],
        "sector": COMPANY_TO_SECTOR.get(r["Company"], NON_CURATED_SECTOR),
    } for r in rows]
    resp = requests.post(webapp_url, json={"t": token, "action": "send", "subject": subject,
                                           "html": html_body, "roles": roles}, timeout=60)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"web app refused the send: {result.get('error')}")


def fetch_greenhouse(company):
    for slug in slugify_variants(company):
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        jobs = resp.json().get("jobs", [])
        if not jobs:
            continue
        return [
            (j.get("title", ""), j.get("location", {}).get("name", ""),
             j.get("absolute_url", ""), j.get("content", ""))
            for j in jobs
        ]
    return []


def fetch_lever(company):
    for slug in slugify_variants(company):
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        jobs = resp.json()
        if not jobs:
            continue
        return [
            (j.get("text", ""), (j.get("categories") or {}).get("location", ""),
             j.get("hostedUrl", ""), j.get("descriptionPlain", j.get("description", "")))
            for j in jobs
        ]
    return []


def fetch_ashby(company):
    for slug in slugify_variants(company):
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        jobs = resp.json().get("jobs", [])
        if not jobs:
            continue
        return [
            (j.get("title", ""), j.get("location", ""), j.get("jobUrl", ""),
             j.get("descriptionHtml", j.get("descriptionPlain", "")))
            for j in jobs
        ]
    return []


def fetch_direct_ats():
    rows = []
    for company in ALL_COMPANIES:
        for fetch_fn, source in (
            (fetch_greenhouse, "Greenhouse"),
            (fetch_lever, "Lever"),
            (fetch_ashby, "Ashby"),
        ):
            try:
                jobs = fetch_fn(company)
            except Exception as e:
                print(f"  [warn] {source} lookup failed for {company}: {e}")
                continue
            for title, location, url, description in jobs:
                if (passes_keyword_filter(title) and passes_location_filter(location, company, title)
                        and passes_description_checks(description, title, company)
                        and passes_curated_company_location_gate(company, location)):
                    rows.append(make_row(title, company, location, source, url))
            time.sleep(REQUEST_DELAY)
    return rows


LISTING_REMOVED = object()


def _fetch_workday_posting(detail_api, external_path):
    """One extra request per posting that already passed the cheap filters
    — returns the full jobPostingInfo (description, country), LISTING_REMOVED
    on a 404, or {} on any other failure. Caught GM's real "10 or more
    years" requirement that the search endpoint's summary never exposed."""
    try:
        resp = requests.get(detail_api + external_path, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return {}
    if resp.status_code == 404:
        return LISTING_REMOVED
    if resp.status_code != 200:
        return {}
    return resp.json().get("jobPostingInfo", {})


def _fetch_workday_job_description(detail_api, external_path):
    info = _fetch_workday_posting(detail_api, external_path)
    return info if info is LISTING_REMOVED else info.get("jobDescription", "")


def fetch_workday_company(company, cfg):
    base = f"https://{cfg['tenant']}.{cfg['wd']}.myworkdayjobs.com/{cfg['site']}"
    api = (f"https://{cfg['tenant']}.{cfg['wd']}.myworkdayjobs.com/"
           f"wday/cxs/{cfg['tenant']}/{cfg['site']}/jobs")
    detail_api = f"https://{cfg['tenant']}.{cfg['wd']}.myworkdayjobs.com/wday/cxs/{cfg['tenant']}/{cfg['site']}"
    rows = []
    for term in SEARCH_TERMS:
        try:
            resp = requests.post(
                api, headers={**HEADERS, "Content-Type": "application/json"},
                json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [warn] Workday lookup failed for {company} ('{term}'): {e}")
            continue
        for j in resp.json().get("jobPostings", []):
            title = j.get("title", "")
            location = j.get("locationsText", "")
            external_path = j.get("externalPath", "")
            url = base + external_path
            if not (passes_keyword_filter(title) and passes_location_filter(location, company, title)):
                continue
            info = _fetch_workday_posting(detail_api, external_path)
            time.sleep(REQUEST_DELAY)
            if info is LISTING_REMOVED:
                continue
            country = (info.get("country") or {}).get("descriptor", "")
            if not (looks_us_location(location) or country == "United States of America"
                    or (is_international_ok(company) and (is_uk_or_eu(location) or is_uk_or_eu(country)))):
                continue
            description = info.get("jobDescription", "")
            if passes_description_checks(description, title, company):
                rows.append(make_row(title, company, location, "Workday", url))
        time.sleep(REQUEST_DELAY)
    return rows


def _fetch_talentbrew_job_description(job_url):
    """Same idea as the Workday detail fetch — one extra request per
    posting that already passed the cheap filters, no LLM cost. The
    individual job page's raw HTML is passed straight to passes_yoe_filter,
    which strips tags itself; no need to isolate just the description
    section."""
    try:
        resp = requests.get(job_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return ""
    if resp.status_code == 404:
        return LISTING_REMOVED
    if resp.status_code != 200:
        return ""
    return resp.text


def fetch_talentbrew_company(company, base_url):
    rows = []
    for term in PROFILE["talentbrew_search_slugs"]:
        url = f"{base_url}/search-jobs/{term}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [warn] TalentBrew lookup failed for {company} ('{term}'): {e}")
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        listings = []
        for h2 in soup.select("h2.search-results-list__job-title"):
            a = h2.find("a")
            if a:
                listings.append((a.get_text(strip=True), a.get("href", ""), h2.parent.select_one(".job-location")))
        for a in soup.select("a[data-job-id]"):
            h2 = a.find("h2")
            if h2:
                listings.append((h2.get_text(strip=True), a.get("href", ""), a.select_one(".job-location")))
        for title, href, loc_el in listings:
            job_url = base_url + href if href.startswith("/") else href
            location = loc_el.get_text(strip=True) if loc_el else ""
            if not location:
                m = re.search(r"/job/([^/]+)/", href)
                location = m.group(1).replace("-", " ").title() if m else ""
            if not (passes_keyword_filter(title) and passes_location_filter(location, company, title)):
                continue
            description = _fetch_talentbrew_job_description(job_url)
            time.sleep(REQUEST_DELAY)
            if description is LISTING_REMOVED:
                continue
            if passes_description_checks(description, title, company):
                rows.append(make_row(title, company, location, "TalentBrew", job_url))
        time.sleep(REQUEST_DELAY)
    return rows


def fetch_amazon():
    """amazon.jobs exposes a public JSON search. Basic qualifications go
    first in the text checked, since that's where the real minimum years
    are stated (preferred qualifications often name a higher number)."""
    rows = []
    seen_paths = set()
    for term in SEARCH_TERMS:
        try:
            resp = requests.get(
                "https://www.amazon.jobs/en/search.json",
                params={"base_query": term, "country": "USA", "result_limit": 50, "sort": "recent"},
                headers=HEADERS, timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [warn] Amazon lookup failed ('{term}'): {e}")
            continue
        for j in resp.json().get("jobs", []):
            path = j.get("job_path", "")
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            title = j.get("title", "")
            location = j.get("normalized_location") or j.get("location", "")
            description = f"{j.get('basic_qualifications', '')}\n{j.get('description', '')}"
            if (passes_keyword_filter(title) and passes_location_filter(location, "Amazon", title)
                    and passes_description_checks(description, title, "Amazon")):
                rows.append(make_row(title, "Amazon", location, "Amazon Jobs",
                                     "https://www.amazon.jobs" + path))
        time.sleep(REQUEST_DELAY)
    return rows


def fetch_jibe_company(company, tenant, job_url_prefix):
    rows = []
    seen_ids = set()
    for term in SEARCH_TERMS:
        try:
            resp = requests.get(f"https://{tenant}.jibeapply.com/api/jobs",
                                params={"keywords": term, "country": "United States", "limit": 50},
                                headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [warn] Jibe lookup failed for {company} ('{term}'): {e}")
            continue
        for item in resp.json().get("jobs", []):
            j = item.get("data", {})
            req_id = j.get("req_id") or j.get("slug")
            if not req_id or req_id in seen_ids:
                continue
            seen_ids.add(req_id)
            title = j.get("title", "")
            location = j.get("full_location", "")
            description = f"{j.get('qualifications', '')}\n{j.get('description', '')}\n{j.get('responsibilities', '')}"
            if (passes_keyword_filter(title) and passes_location_filter(location, company, title)
                    and passes_description_checks(description, title, company)):
                rows.append(make_row(title, company, location, "Careers site", job_url_prefix + str(req_id)))
        time.sleep(REQUEST_DELAY)
    return rows


def fetch_successfactors_company(company, host):
    rows = []
    seen_links = set()
    for term in SEARCH_TERMS:
        try:
            resp = requests.get(f"https://{host}/services/rss/job/",
                                params={"locale": "en_US", "keywords": term},
                                headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [warn] SuccessFactors lookup failed for {company} ('{term}'): {e}")
            continue
        for item in re.findall(r"<item>(.*?)</item>", resp.text, re.S):
            def field(tag):
                m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", item, re.S)
                return html.unescape(m.group(1)).strip() if m else ""
            link = field("link")
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            m = re.match(r"(.*)\(([^()]*)\)\s*$", field("title"))
            if not m:
                continue
            title, location = m.group(1).strip(), m.group(2).strip()
            parts = [p.strip() for p in location.split(",")]
            if "US" not in parts:
                continue
            description = field("description")
            if (passes_keyword_filter(title) and passes_location_filter(location, company, title)
                    and passes_description_checks(description, title, company)):
                rows.append(make_row(title, company, ", ".join(parts[:2]), "Careers site", link))
        time.sleep(REQUEST_DELAY)
    return rows


def fetch_successfactors_search_company(company, host):
    rows = []
    seen_links = set()
    for term in SEARCH_TERMS:
        try:
            resp = requests.get(f"https://{host}/search/",
                                params={"q": term, "locationsearch": "United States"},
                                headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [warn] SuccessFactors search failed for {company} ('{term}'): {e}")
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        for row in soup.select("tr.data-row"):
            a = row.select_one("a.jobTitle-link")
            loc_el = row.select_one("span.jobLocation")
            if not a or not loc_el:
                continue
            href = a.get("href", "")
            link = f"https://{host}{href}" if href.startswith("/") else href
            if link in seen_links:
                continue
            seen_links.add(link)
            title = a.get_text(strip=True)
            location = loc_el.get_text(strip=True)
            parts = [p.strip() for p in location.split(",")]
            if "US" not in parts or not (passes_keyword_filter(title)
                                         and passes_location_filter(location, company, title)):
                continue
            try:
                page = requests.get(link, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            except requests.RequestException:
                continue
            if page.status_code == 404:
                continue
            desc_el = BeautifulSoup(page.text, "html.parser").select_one(
                '[itemprop="description"], .jobdescription, .job-description')
            description = desc_el.get_text(" ", strip=True) if desc_el else ""
            time.sleep(REQUEST_DELAY)
            if passes_description_checks(description, title, company):
                rows.append(make_row(title, company, ", ".join(parts[:2]), "Careers site", link))
        time.sleep(REQUEST_DELAY)
    return rows


def fetch_oracle_company(company, host, site):
    rows = []
    seen_ids = set()
    api = f"https://{host}/hcmRestApi/resources/latest"
    for term in SEARCH_TERMS:
        try:
            resp = requests.get(f"{api}/recruitingCEJobRequisitions", headers=HEADERS, timeout=REQUEST_TIMEOUT,
                                params={"onlyData": "true", "expand": "requisitionList",
                                        "finder": f"findReqs;siteNumber={site},keyword={term},limit=50"})
            resp.raise_for_status()
            items = resp.json().get("items", [])
        except (requests.RequestException, ValueError) as e:
            print(f"  [warn] Oracle lookup failed for {company} ('{term}'): {e}")
            continue
        for j in (items[0].get("requisitionList", []) if items else []):
            req_id = j.get("Id")
            if not req_id or req_id in seen_ids:
                continue
            seen_ids.add(req_id)
            title = j.get("Title", "")
            location = j.get("PrimaryLocation", "")
            if "United States" not in location or not (passes_keyword_filter(title)
                                                         and passes_location_filter(location, company, title)):
                continue
            description = j.get("ShortDescriptionStr", "")
            try:
                det = requests.get(f"{api}/recruitingCEJobRequisitionDetails", headers=HEADERS,
                                   timeout=REQUEST_TIMEOUT,
                                   params={"onlyData": "true", "expand": "all",
                                           "finder": f'ById;Id="{req_id}",siteNumber={site}'}).json()
                d = (det.get("items") or [{}])[0]
                description = "\n".join(filter(None, [d.get("ExternalQualificationsStr"),
                                                       d.get("ExternalDescriptionStr"), description]))
            except (requests.RequestException, ValueError):
                pass
            time.sleep(REQUEST_DELAY)
            if passes_description_checks(description, title, company):
                rows.append(make_row(title, company, location.replace(", United States", ""), "Careers site",
                                     f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{req_id}"))
        time.sleep(REQUEST_DELAY)
    return rows


def fetch_gr8people_company(company, base_url):
    rows = []
    try:
        resp = requests.get(f"{base_url}/jobs", headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [warn] gr8people lookup failed for {company}: {e}")
        return rows
    soup = BeautifulSoup(resp.text, "html.parser")
    seen = set()
    for a in soup.find_all("a", href=re.compile(r"/jobs/\d+/")):
        href = a.get("href", "")
        if href in seen:
            continue
        seen.add(href)
        row = a.find_parent(["tr", "li", "div"])
        cells = [c for c in (row.get_text(" | ", strip=True).split(" | ") if row else []) if c]
        title = a.get_text(strip=True)
        location = cells[-1] if cells else ""
        if not location.rstrip().endswith("US") or not (passes_keyword_filter(title)
                                                        and passes_location_filter(location, company, title)):
            continue
        job_url = base_url + href if href.startswith("/") else href
        try:
            page = requests.get(job_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            description = BeautifulSoup(page.text, "html.parser").get_text(" ", strip=True) if page.ok else ""
        except requests.RequestException:
            description = ""
        time.sleep(REQUEST_DELAY)
        if passes_description_checks(description, title, company):
            rows.append(make_row(title, company, re.sub(r"\s+US$", "", location), "Careers site", job_url))
    return rows


def fetch_rippling_company(company, board):
    rows = []
    try:
        resp = requests.get(f"https://api.rippling.com/platform/api/ats/v1/board/{board}/jobs",
                            headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        jobs = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  [warn] Rippling lookup failed for {company}: {e}")
        return rows
    for j in jobs:
        title = j.get("name", "")
        location = (j.get("workLocation") or {}).get("label", "")
        if not (passes_keyword_filter(title) and passes_location_filter(location, company, title)):
            continue
        job_url = j.get("url", "")
        try:
            page = requests.get(job_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            description = BeautifulSoup(page.text, "html.parser").get_text(" ", strip=True) if page.ok else ""
        except requests.RequestException:
            description = ""
        time.sleep(REQUEST_DELAY)
        if passes_description_checks(description, title, company):
            rows.append(make_row(title, company, location, "Careers site", job_url))
    return rows


def fetch_workable_company(company, account):
    rows = []
    try:
        resp = requests.get(f"https://apply.workable.com/api/v1/widget/accounts/{account}",
                            params={"details": "true"}, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
    except (requests.RequestException, ValueError) as e:
        print(f"  [warn] Workable lookup failed for {company}: {e}")
        return rows
    for j in jobs:
        title = j.get("title", "")
        if j.get("telecommuting") and not j.get("city"):
            location = f"Remote, {j.get('country') or ''}".strip(", ")
        else:
            location = ", ".join(p for p in (j.get("city"), j.get("state"), j.get("country")) if p)
        if (passes_keyword_filter(title) and passes_location_filter(location, company, title)
                and passes_description_checks(j.get("description", ""), title, company)):
            rows.append(make_row(title, company, location, "Careers site", j.get("url", "")))
    return rows


def fetch_google():
    """Google's careers results page is server-rendered: 20 jobs a page, each
    with title, location and the minimum qualifications (where the real
    years requirement is). Filtered to Google's own early- and mid-career
    levels so the pages aren't spent on Director listings."""
    rows = []
    seen = set()
    base = "https://www.google.com/about/careers/applications/"
    for term in PROFILE["google_careers_terms"]:
        for page in (1, 2):
            try:
                resp = requests.get(base + "jobs/results/", headers=HEADERS, timeout=REQUEST_TIMEOUT,
                                    params=[("q", term), ("location", "United States"), ("page", page),
                                            ("target_level", "EARLY"), ("target_level", "MID")])
                resp.raise_for_status()
            except requests.RequestException as e:
                print(f"  [warn] Google lookup failed ('{term}'): {e}")
                break
            soup = BeautifulSoup(resp.text, "html.parser")
            items = [li for li in soup.find_all("li") if li.find("a", href=re.compile(r"^jobs/results/\d+"))]
            if not items:
                break
            for li in items:
                href = li.find("a", href=re.compile(r"^jobs/results/\d+"))["href"].split("?")[0]
                if href in seen:
                    continue
                seen.add(href)
                h3 = li.find("h3")
                title = h3.get_text(strip=True) if h3 else ""
                parts = li.get_text(" | ", strip=True).split(" | ")
                location = parts[parts.index("place") + 1] if "place" in parts and parts.index("place") + 1 < len(parts) else ""
                text = li.get_text(" ", strip=True)
                quals = text[text.find("Minimum qualifications"):] if "Minimum qualifications" in text else text
                if (passes_keyword_filter(title) and passes_location_filter(location, "Google", title)
                        and passes_description_checks(quals, title, "Google")):
                    rows.append(make_row(title, "Google", location, "Careers site", base + href))
            time.sleep(REQUEST_DELAY)
    return rows


def fetch_configured_ats():
    """Companies with a hand-confirmed Workday tenant or TalentBrew site."""
    rows = []
    extra = [("Google", fetch_google)] if PROFILE.get("extra_sources", {}).get("google") else []
    for label, fn in extra:
        try:
            rows.extend(fn())
        except Exception as e:
            print(f"  [warn] {label} lookup crashed: {e}")
    for company, (host, site) in ORACLE_COMPANIES.items():
        try:
            rows.extend(fetch_oracle_company(company, host, site))
        except Exception as e:
            print(f"  [warn] Oracle lookup crashed for {company}: {e}")
    for company, base_url in GR8PEOPLE_COMPANIES.items():
        try:
            rows.extend(fetch_gr8people_company(company, base_url))
        except Exception as e:
            print(f"  [warn] gr8people lookup crashed for {company}: {e}")
    for company, board in RIPPLING_COMPANIES.items():
        try:
            rows.extend(fetch_rippling_company(company, board))
        except Exception as e:
            print(f"  [warn] Rippling lookup crashed for {company}: {e}")
    for company, account in WORKABLE_COMPANIES.items():
        try:
            rows.extend(fetch_workable_company(company, account))
        except Exception as e:
            print(f"  [warn] Workable lookup crashed for {company}: {e}")
    for company, host in SUCCESSFACTORS_SEARCH_COMPANIES.items():
        try:
            rows.extend(fetch_successfactors_search_company(company, host))
        except Exception as e:
            print(f"  [warn] SuccessFactors search crashed for {company}: {e}")
    for company, (tenant, prefix) in JIBE_COMPANIES.items():
        try:
            rows.extend(fetch_jibe_company(company, tenant, prefix))
        except Exception as e:
            print(f"  [warn] Jibe lookup crashed for {company}: {e}")
    for company, host in SUCCESSFACTORS_COMPANIES.items():
        try:
            rows.extend(fetch_successfactors_company(company, host))
        except Exception as e:
            print(f"  [warn] SuccessFactors lookup crashed for {company}: {e}")
    if PROFILE.get("extra_sources", {}).get("amazon"):
        try:
            rows.extend(fetch_amazon())
        except Exception as e:
            print(f"  [warn] Amazon lookup crashed: {e}")
    for company, cfg in WORKDAY_COMPANIES.items():
        try:
            rows.extend(fetch_workday_company(company, cfg))
        except Exception as e:
            print(f"  [warn] Workday lookup crashed for {company}: {e}")
    for company, base_url in TALENTBREW_COMPANIES.items():
        try:
            rows.extend(fetch_talentbrew_company(company, base_url))
        except Exception as e:
            print(f"  [warn] TalentBrew lookup crashed for {company}: {e}")
    return rows


HQ_ONLY_COMPANIES = PROFILE["hq_only_companies"]


def _is_remote_location(location):
    """Narrower than REMOTE_KEYWORDS (which also treats bare "United
    States"/"USA" as a permissive default for country-less formats) — this
    is specifically for "is this posting actually remote," used to gate
    contract/staffing roles to big cities or true remote only."""
    loc = (location or "").lower()
    return any(k in loc for k in ("remote", "work from home", "wfh", "anywhere"))


def passes_curated_company_location_gate(canonical_company, location):
    """A curated match normally unlocks national scope unconditionally, but
    two categories need a tighter check first: a hotel chain only counts
    nationally if it's actually corporate HQ (not a random single
    property), and contract/staffing agencies only count in a tier-1 city
    or truly remote. Used by both the
    aggregator gate and the direct-ATS path, in case one of these companies
    ever turns up a Greenhouse/Ashby/Lever board."""
    hq_hints = HQ_ONLY_COMPANIES.get(canonical_company)
    if hq_hints is not None:
        loc = (location or "").lower()
        return any(h in loc for h in hq_hints) or is_home_area(location)
    if COMPANY_TO_SECTOR.get(canonical_company) == PROFILE["contract_sector"]:
        return is_tier1_city(location) or _is_remote_location(location)
    return True


COMPANY_PROFILES_PATH = "company_profiles.json"
PENDING_COMPANIES_PATH = "pending_companies.json"
MIN_EMPLOYEES = PROFILE["min_employees"]

TIER1_CITY_KEYWORDS = [
    "new york", "nyc", "manhattan", "brooklyn", "los angeles", "chicago",
    "dallas", "fort worth", "houston", "washington, dc", "washington dc",
    "washington, d.c.", "philadelphia", "miami", "atlanta", "boston", "seattle",
]


def is_tier1_city(location):
    loc = (location or "").lower()
    return is_home_area(location) or any(k in loc for k in TIER1_CITY_KEYWORDS)

COMPANY_PROFILES = {}
PENDING_COMPANIES = set()


def load_company_profiles():
    if not os.path.exists(COMPANY_PROFILES_PATH):
        return
    with open(COMPANY_PROFILES_PATH, encoding="utf-8") as f:
        for name, profile in json.load(f).items():
            COMPANY_PROFILES[normalize_company_for_match(name)] = profile


def employee_floor(label):
    m = re.match(r"\s*([\d,]+)", label or "")
    return int(m.group(1).replace(",", "")) if m else None


def passes_size_gate(company, location):
    profile = COMPANY_PROFILES.get(normalize_company_for_match(company))
    if profile is None:
        PENDING_COMPANIES.add(company)
        return False
    if profile.get("staffing"):
        return is_tier1_city(location)
    floor = employee_floor(profile.get("employees"))
    if floor is None:
        return False
    return floor >= MIN_EMPLOYEES or (floor >= 51 and bool(profile.get("growing")))


def write_pending_companies():
    with open(PENDING_COMPANIES_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(PENDING_COMPANIES), f, indent=2)


def gate_and_canonicalize(company, location):
    """(keep, company_name_to_use) — canonicalizes to the curated name on a
    match (so "Acme Holdings Inc." shows as "Acme"); otherwise keeps the raw
    name only if the role is in the home area or a major metro AND the
    company is a verified mid-market-or-larger, non-staffing employer."""
    canonical = matches_curated_company(company)
    if canonical:
        if passes_curated_company_location_gate(canonical, location):
            return True, canonical
        return False, canonical
    if (is_home_area(location) or is_major_metro(location)) and passes_size_gate(company, location):
        return True, company
    return False, company


def adzuna_location(location_obj, fallback):
    area = (location_obj or {}).get("area") or []
    if len(area) >= 2 and area[0] == "US":
        state = area[1]
        city = area[-1] if len(area) > 2 and area[-1] != state else None
        return f"{city}, {state}" if city else state
    return fallback


def fetch_adzuna(app_id, app_key):
    rows = []
    if not app_id or not app_key:
        print("  [skip] Adzuna: no API key in .env")
        return rows
    for term in SEARCH_TERMS:
        url = "https://api.adzuna.com/v1/api/jobs/us/search/1"
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "what": term,
            "results_per_page": 50,
            "max_days_old": ADZUNA_MAX_DAYS_OLD,
            "sort_by": "date",
            "content-type": "application/json",
        }
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [warn] Adzuna query '{term}' failed: {e}")
            continue
        for j in resp.json().get("results", []):
            title = j.get("title", "")
            company = (j.get("company") or {}).get("display_name", "")
            loc_obj = j.get("location") or {}
            location = adzuna_location(loc_obj, loc_obj.get("display_name", ""))
            url_ = j.get("redirect_url", "")
            description = j.get("description", "")
            if not (passes_keyword_filter(title) and passes_location_filter(location, company, title)
                    and passes_description_checks(description, title, company)):
                continue
            keep, company_name = gate_and_canonicalize(company, location)
            if keep:
                rows.append(make_row(title, company_name, location, "Adzuna", url_))
        time.sleep(REQUEST_DELAY)
    return rows


def fetch_remotive():
    rows = []
    url = "https://remotive.com/api/remote-jobs"
    try:
        resp = requests.get(url, params={"category": PROFILE["remotive_category"]},
                             headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [warn] Remotive failed: {e}")
        return rows
    for j in resp.json().get("jobs", []):
        title = j.get("title", "")
        company = j.get("company_name", "")
        location = j.get("candidate_required_location", "")
        url_ = j.get("url", "")
        description = j.get("description", "")
        if not (passes_keyword_filter(title) and passes_location_filter(location, company, title)
                and passes_description_checks(description, title, company)):
            continue
        keep, company_name = gate_and_canonicalize(company, location)
        if keep:
            rows.append(make_row(title, company_name, location, "Remotive", url_))
    return rows


def fetch_jobicy():
    rows = []
    url = "https://jobicy.com/api/v2/remote-jobs"
    try:
        resp = requests.get(url, params={"count": 50, "tag": PROFILE["jobicy_tag"]},
                             headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [warn] Jobicy failed: {e}")
        return rows
    for j in resp.json().get("jobs", []):
        title = j.get("jobTitle", "")
        company = j.get("companyName", "")
        location = j.get("jobGeo", "")
        url_ = j.get("url", "")
        description = j.get("jobDescription", j.get("jobExcerpt", ""))
        if (j.get("jobLevel") or "").strip().lower() in ("senior", "director"):
            continue
        if not (passes_keyword_filter(title) and passes_location_filter(location, company, title)
                and passes_description_checks(description, title, company)):
            continue
        keep, company_name = gate_and_canonicalize(company, location)
        if keep:
            rows.append(make_row(title, company_name, location, "Jobicy", url_))
    return rows


def fetch_arbeitnow():
    rows = []
    url = "https://www.arbeitnow.com/api/job-board-api"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [warn] Arbeitnow failed: {e}")
        return rows
    for j in resp.json().get("data", []):
        title = j.get("title", "")
        company = j.get("company_name", "")
        location = j.get("location", "")
        url_ = j.get("url", "")
        description = j.get("description", "")
        if not (passes_keyword_filter(title) and passes_location_filter(location, company, title)
                and passes_description_checks(description, title, company)):
            continue
        keep, company_name = gate_and_canonicalize(company, location)
        if keep:
            rows.append(make_row(title, company_name, location, "Arbeitnow", url_))
    return rows


GOOGLE_CSE_ENABLED = False


def fetch_google_cse(api_key, cse_id):
    rows = []
    if not GOOGLE_CSE_ENABLED:
        print("  [skip] Google CSE: disabled (confirmed 403 for new Cloud projects)")
        return rows
    if not api_key or not cse_id:
        print("  [skip] Google CSE: GOOGLE_CSE_ID not set in .env yet")
        return rows
    query_count = 0
    for company in ALL_COMPANIES:
        if query_count >= GOOGLE_MAX_QUERIES_PER_DAY:
            print(f"  [cap] Hit {GOOGLE_MAX_QUERIES_PER_DAY} Google queries, stopping")
            break
        q = (
            f'"{company}" (' + " OR ".join(f'"{t}"' for t in SEARCH_TERMS) + ') '
            f'(site:boards.greenhouse.io OR site:jobs.lever.co OR site:jobs.ashbyhq.com '
            f'OR site:myworkdayjobs.com)'
        )
        params = {"key": api_key, "cx": cse_id, "q": q, "num": 10}
        try:
            resp = requests.get("https://www.googleapis.com/customsearch/v1",
                                 params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [warn] Google CSE query failed for {company}: {e}")
            query_count += 1
            continue
        query_count += 1
        for item in resp.json().get("items", []):
            title = item.get("title", "")
            url_ = item.get("link", "")
            if passes_keyword_filter(title):
                rows.append(make_row(title, company, "", "Google CSE", url_))
        time.sleep(REQUEST_DELAY)
    print(f"  Google CSE queries used today: {query_count}/{GOOGLE_MAX_QUERIES_PER_DAY}")
    return rows


def apply_company_cap(rows_sorted_by_score, cap):
    counts = {}
    kept = []
    for row in rows_sorted_by_score:
        c = row["Company"]
        if counts.get(c, 0) >= cap:
            continue
        counts[c] = counts.get(c, 0) + 1
        kept.append(row)
    return kept


def select_top_rows(all_rows, total_slots, company_cap):
    """Rank within each sector by fit score, cap per company, then split
    total_slots across sectors as evenly as possible — but a sector with
    fewer qualifying rows than its even share doesn't hog unused slots;
    those get redistributed to sectors that still have more to offer."""
    by_sector = {}
    for row in all_rows:
        sector = COMPANY_TO_SECTOR.get(row["Company"], NON_CURATED_SECTOR)
        by_sector.setdefault(sector, []).append(row)

    for sector, rows in by_sector.items():
        rows.sort(key=compute_fit_score, reverse=True)
        by_sector[sector] = apply_company_cap(rows, company_cap)

    pools = {s: rows for s, rows in by_sector.items() if rows}
    allocation = {s: 0 for s in pools}
    slots_left = total_slots
    while slots_left > 0 and pools:
        share = max(1, slots_left // len(pools))
        for sector in list(pools.keys()):
            available = len(pools[sector]) - allocation[sector]
            take = min(share, available, slots_left)
            if take <= 0:
                continue
            allocation[sector] += take
            slots_left -= take
            if allocation[sector] >= len(pools[sector]):
                del pools[sector]
            if slots_left <= 0:
                break

    selected = []
    for sector, count in allocation.items():
        selected.extend(by_sector[sector][:count])
    selected.sort(key=compute_fit_score, reverse=True)
    return selected


def _esc(s):
    return html.escape(s or "", quote=True)


_SECTOR_DISPLAY_ORDER = PROFILE["sector_display_order"]


DIGEST_WIDTH = 700


def _feedback_link(webapp_url, token, action, row):
    return f"{webapp_url}?a={action}&i={role_id(row)}&t={token}"


_DIGEST_CSS = (
    ".c{border:1px solid #E5E7EB;border-radius:8px}.i{padding:10px 14px}"
    ".t{font-size:14.5px;font-weight:700;color:#111827}"
    ".n{font-size:10px;font-weight:800;color:#059669;border:1px solid #059669;"
    "border-radius:4px;padding:0 4px;margin-left:6px;white-space:nowrap}"
    ".s{font-size:11px;font-weight:400;color:#9CA3AF;margin-left:6px;white-space:nowrap}"
    ".a{font-size:12px;font-weight:700;text-decoration:none;margin-left:8px}"
    ".m{font-size:12px;color:#6B7280;margin-top:3px}"
    ".b{font-size:11.5px;text-decoration:none;border:1px solid #D1D5DB;border-radius:999px;"
    "padding:2px 8px;white-space:nowrap}.y{color:#059669}.x{color:#6B7280}.f{margin-left:8px;white-space:nowrap}"
)


def _render_digest_card(row, index, color, webapp_url, token):
    location = _esc(row["Location"]) or "Location not listed"
    funded_marker = " 🔥" if row["Company"] in WATCHED_COMPANY_NAMES else ""
    if row.get("First Surfaced") == date.today().isoformat():
        tag = '<span class="n">NEW</span>'
    else:
        tag = f'<span class="s">since {date.fromisoformat(row["First Surfaced"]).strftime("%b %-d")}</span>'
    buttons = ""
    if webapp_url and token:
        buttons = (f'<span class="f"><a class="b y" href="{_esc(_feedback_link(webapp_url, token, "y", row))}">'
                   f'&#10003; Applied</a> <a class="b x" href="{_esc(_feedback_link(webapp_url, token, "n", row))}">'
                   f'&#128078; Not for me</a></span>')
    return (
        f'<table class="c" bgcolor="#FAFAFA" width="100%" cellpadding="0" cellspacing="0"><tr><td class="i">'
        f'<div style="border-left:3px solid {color};padding-left:10px">'
        f'<div class="t">{index}&nbsp; {_esc(row["Title"])}{tag}'
        f'<a class="a" style="color:{color}" href="{_esc(row["URL"])}">Apply &rarr;</a></div>'
        f'<div class="m"><b style="color:{color}">{_esc(row["Company"])}{funded_marker}</b> &middot; '
        f'{location} &middot; via {_esc(row["Source"])}{buttons}</div></div></td></tr></table>'
    )


def digest_subject():
    return f"Job Search Digest — {date.today().strftime('%B %-d, %Y')}"


def render_html_digest(rows, webapp_url="", token="", left_out=0):
    """Plain inline-styled HTML, table-based layout for email-client safety.
    Gmail's send pipeline strips `background`/`background-color` outright
    (confirmed by inspecting the stored message), so every accent here is
    done with `border` and `color` instead — no fills. Single wide column
    (a 2-column grid felt cramped). Within each sector, today's new roles
    come first, then still-waiting ones, newest first."""
    today_iso = date.today().isoformat()
    today = date.today().strftime("%B %-d, %Y")
    new_count = sum(1 for r in rows if r.get("First Surfaced") == today_iso)
    waiting_count = len(rows) - new_count

    grouped = {}
    for row in rows:
        sector = COMPANY_TO_SECTOR.get(row["Company"], NON_CURATED_SECTOR)
        grouped.setdefault(sector, []).append(row)
    for sector_rows in grouped.values():
        sector_rows.sort(key=lambda r: (r.get("First Surfaced") or "", compute_fit_score(r)), reverse=True)
    ordered_sectors = [s for s in _SECTOR_DISPLAY_ORDER if s in grouped]
    ordered_sectors += [s for s in grouped if s not in ordered_sectors]

    sections = []
    counter = 0
    for sector in ordered_sectors:
        color = SECTOR_COLORS.get(sector, SECTOR_COLORS[NON_CURATED_SECTOR])
        label = SECTOR_DISPLAY_NAMES.get(sector, sector)
        sections.append(
            f'<tr><td style="padding:24px 28px 10px 28px;"><div style="display:inline-block;font-size:12px;'
            f'font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:{color};'
            f'border-bottom:2.5px solid {color};padding-bottom:5px;">{_esc(label)}</div></td></tr>')
        for row in grouped[sector]:
            counter += 1
            card_html = _render_digest_card(row, counter, color, webapp_url, token)
            sections.append(f'<tr><td style="padding:0 28px 8px 28px;">{card_html}</td></tr>')

    sections_html = "".join(sections) if sections else """
        <tr><td style="padding:32px 28px;text-align:center;color:#6B7280;font-size:14px;">
          No new qualifying roles today, and nothing waiting on you.
        </td></tr>"""
    counts = f"{new_count} new"
    if waiting_count:
        counts += f" &middot; {waiting_count} waiting"
    footer = ("&#10003; Applied adds it to your Applied tab &middot; &#128078; Not for me hides it "
              "and teaches the search" if webapp_url and token else "Automated daily search")
    if left_out:
        footer = (f"{left_out} older waiting role{'s' if left_out != 1 else ''} left out to keep this "
                  f"email from being cut off &middot; " + footer)
    learned = rules_learned_today()
    if learned:
        footer = ("Learned from your notes today: " + "; ".join(_esc(r["rule"]) for r in learned)
                  + "<br>" + footer)

    return f"""\
<!doctype html>
<html>
<head><meta charset="utf-8"><style>{_DIGEST_CSS}</style></head>
<body style="margin:0;padding:0;background:#F3F4F6;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F3F4F6;padding:24px 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="{DIGEST_WIDTH}" cellpadding="0" cellspacing="0"
               style="background:#FFFFFF;border-radius:10px;overflow:hidden;max-width:{DIGEST_WIDTH}px;width:100%;
                      border-top:4px solid #111827;">
          <tr>
            <td style="padding:24px 28px 18px 28px;border-bottom:1px solid #E5E7EB;">
              <div style="font-size:20px;font-weight:800;color:#111827;">Job Search Digest</div>
              <div style="font-size:13px;color:#6B7280;margin-top:4px;">{_esc(today)} &middot; {counts}</div>
            </td>
          </tr>
          {sections_html}
          <tr>
            <td style="padding:18px 28px;text-align:center;border-top:1px solid #E5E7EB;">
              <div style="font-size:12px;color:#9CA3AF;">{footer}</div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def main():
    load_dotenv()
    adzuna_id = os.environ.get("ADZUNA_APP_ID", "")
    adzuna_key = os.environ.get("ADZUNA_APP_KEY", "")
    google_key = os.environ.get("GOOGLE_API_KEY", "")
    google_cse_id = os.environ.get("GOOGLE_CSE_ID", "")
    webapp_url = os.environ.get("DIGEST_WEBAPP_URL", "")
    token = os.environ.get("DIGEST_TOKEN", "")

    load_company_profiles()
    print(f"Loaded size profiles for {len(COMPANY_PROFILES)} companies.")
    print("Loading feedback and Applied tab from the Sheet...")
    resolutions = Resolutions(load_feedback(webapp_url, token))
    load_learned_rules()
    resolutions.hidden_companies.extend(LEARNED_RULES["exclude_companies"])
    print(f"  Learned rules: {len(LEARNED_RULES['exclude_title_phrases'])} title, "
          f"{len(LEARNED_RULES['exclude_companies'])} company, "
          f"{len(LEARNED_RULES['exclude_description_phrases'])} description.")
    print(f"  {len(resolutions.resolved_ids)} role(s) already clicked, "
          f"{len(resolutions.applied)} on the Applied tab, "
          f"{len(resolutions.hidden_companies)} hidden compan(ies).")

    seen = load_dedup_keys()
    print(f"Loaded {len(seen)} existing title+company pairs to dedup against.")

    all_new_rows = []
    counts = {}

    print("Checking Greenhouse / Lever / Ashby for target companies...")
    for row in fetch_direct_ats():
        key = (normalize(row["Title"]), normalize(row["Company"]))
        if key in seen:
            continue
        seen.add(key)
        all_new_rows.append(row)
        counts[row["Source"]] = counts.get(row["Source"], 0) + 1

    print("Checking Workday / TalentBrew for configured companies...")
    for row in fetch_configured_ats():
        key = (normalize(row["Title"]), normalize(row["Company"]))
        if key in seen:
            continue
        seen.add(key)
        all_new_rows.append(row)
        counts[row["Source"]] = counts.get(row["Source"], 0) + 1

    print("Querying Adzuna...")
    for row in fetch_adzuna(adzuna_id, adzuna_key):
        key = (normalize(row["Title"]), normalize(row["Company"]))
        if key in seen:
            continue
        seen.add(key)
        all_new_rows.append(row)
        counts[row["Source"]] = counts.get(row["Source"], 0) + 1

    print("Querying Remotive, Jobicy, Arbeitnow...")
    for fetch_fn in (fetch_remotive, fetch_jobicy, fetch_arbeitnow):
        for row in fetch_fn():
            key = (normalize(row["Title"]), normalize(row["Company"]))
            if key in seen:
                continue
            seen.add(key)
            all_new_rows.append(row)
            counts[row["Source"]] = counts.get(row["Source"], 0) + 1

    print("Querying Google Custom Search (targeted, rate-capped)...")
    for row in fetch_google_cse(google_key, google_cse_id):
        key = (normalize(row["Title"]), normalize(row["Company"]))
        if key in seen:
            continue
        seen.add(key)
        all_new_rows.append(row)
        counts[row["Source"]] = counts.get(row["Source"], 0) + 1

    candidates = [r for r in all_new_rows if not resolutions.is_resolved(r)]
    picks = select_top_rows(candidates, DIGEST_TOP_N, DIGEST_MAX_PER_COMPANY)
    for row in picks:
        row["First Surfaced"] = date.today().isoformat()

    file_exists = os.path.exists(OUTPUT_CSV)
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        if not file_exists:
            writer.writeheader()
        for row in picks:
            writer.writerow(row)

    print(f"\nFound {len(all_new_rows)} qualifying new rows across all sources "
          f"({len(all_new_rows) - len(candidates)} already applied to or hidden); "
          f"surfacing the top {len(picks)} today. The rest stay unseen and compete again tomorrow.")
    print("Found by source:")
    for source, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {source}: {n}")

    print("Checking the still-waiting roles...")
    digest_rows, left_out = cap_digest_rows(build_digest_rows(resolutions))
    write_pending_companies()
    print(f"{len(PENDING_COMPANIES)} unvetted compan(ies) held back for tomorrow's size check.")
    html_body = render_html_digest(digest_rows, webapp_url, token, left_out)
    with open(DIGEST_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_body)
    print(f"Wrote {len(digest_rows)}-role digest ({len(html_body) // 1024} KB) to {DIGEST_HTML_PATH}.")

    if webapp_url and token:
        send_digest(webapp_url, token, digest_subject(), html_body, digest_rows)
        print("DIGEST_SENT")
    else:
        print("DIGEST_NOT_SENT: DIGEST_WEBAPP_URL / DIGEST_TOKEN not set in .env")


def notify_failure(error_text):
    """Best-effort heads-up email when a run crashes, so a broken day shows
    up in the inbox instead of as a silently missing digest."""
    load_dotenv()
    webapp_url = os.environ.get("DIGEST_WEBAPP_URL", "")
    token = os.environ.get("DIGEST_TOKEN", "")
    if not webapp_url or not token:
        return
    body = (f"<p>Today's job search run failed, so no digest went out.</p>"
            f"<pre style='font-size:11px;white-space:pre-wrap'>{_esc(error_text[-3000:])}</pre>")
    try:
        requests.post(webapp_url, json={"t": token, "action": "send", "roles": [],
                                        "subject": digest_subject() + " (run failed)",
                                        "html": body}, timeout=60)
    except requests.RequestException:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        notify_failure(traceback.format_exc())
        raise
