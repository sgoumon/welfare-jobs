#!/usr/bin/env python3
"""
Collect open animal-welfare-science vacancies from the sources in sources.yaml
and write docs/jobs.json for the search page.

Run:  python scripts/fetch_jobs.py            (uses sources.yaml, writes docs/jobs.json)
      python scripts/fetch_jobs.py --dry-run  (prints a summary, writes nothing)
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup, NavigableString, Tag
from dateutil import parser as dateparser

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "sources.yaml"
OUTPUT = ROOT / "docs" / "jobs.json"

TODAY = date.today()
SESSION = requests.Session()
SETTINGS: dict = {}


# --------------------------------------------------------------------------- helpers
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fetch(url: str) -> str:
    SESSION.headers["User-Agent"] = SETTINGS.get("user_agent", "AWS-Toolbox-JobBoard/1.0")
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    time.sleep(float(SETTINGS.get("request_delay_seconds", 1.5)))
    return r.text


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def norm_url(url: str) -> str:
    p = urlparse(url.strip())
    # drop tracking parameters and fragments, keep the rest
    query = "&".join(q for q in p.query.split("&") if q and not q.lower().startswith("utm_"))
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", query, ""))


def job_id(url: str, title: str) -> str:
    return hashlib.sha1((norm_url(url) + "|" + title.lower()).encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- dates
MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE_PATTERNS = [
    rf"\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTH},?\s+\d{{4}}",      # 27th November 2026
    rf"{MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}",                # November 27, 2026
    r"\d{4}-\d{2}-\d{2}",                                                # 2026-11-27
    r"\d{1,2}[./]\d{1,2}[./]\d{4}",                                      # 27.11.2026 / 27/11/2026
]
DATE_RE = re.compile("|".join(f"(?:{p})" for p in DATE_PATTERNS), re.I)
DEADLINE_CUE = re.compile(
    r"(closing\s+date|deadline|apply\s+by|applications?\s+(?:close|closes|due|must\s+be\s+received)"
    r"|close[sd]?\s+on|until|no\s+later\s+than|bewerbungsfrist|date\s+limite)",
    re.I,
)


def parse_date(s: str) -> date | None:
    s = re.sub(r"(\d)(st|nd|rd|th)", r"\1", s, flags=re.I).replace(" of ", " ")
    try:
        iso = re.fullmatch(r"\s*\d{4}-\d{2}-\d{2}\s*", s) is not None
        d = dateparser.parse(s, dayfirst=not iso, fuzzy=True).date()
    except (ValueError, OverflowError, TypeError):
        return None
    # sanity window: ignore dates that are clearly not application deadlines
    if TODAY - timedelta(days=400) <= d <= TODAY + timedelta(days=730):
        return d
    return None


def find_deadline(text: str) -> date | None:
    """Return the first date that follows a deadline cue within ~60 characters, else None.
    We deliberately do NOT guess from a lone date, because it is often a start or posting date."""
    text = clean(text)
    for cue in DEADLINE_CUE.finditer(text):
        window = text[cue.end(): cue.end() + 60]
        m = DATE_RE.search(window)
        if m:
            d = parse_date(m.group(0))
            if d:
                return d
    return None


# --------------------------------------------------------------------------- classification
TYPE_RULES = [
    ("Postdoc", r"post-?doc|postdoctoral|post-doctoral|research\s+fellow|research\s+associate"),
    ("PhD", r"\bph\.?d\b|\bdoctoral\s+(?:student|candidate|position|researcher)|studentship|doktorand"),
    ("Faculty", r"professor|lecturer|faculty|tenure|reader\b|chair\s+in"),
    ("Internship", r"\bintern(ship)?\b|placement|traineeship"),
    ("Technician", r"technician|animal\s+care(taker)?|stockperson|herdsperson"),
    ("Masters", r"\bm\.?sc\b|master'?s"),
    ("Scientist / Officer", r"scientist|scientific\s+officer|researcher|research\s+officer|adviser|advisor|analyst"),
    ("Veterinary", r"veterinar|\bvet\b"),
]


def classify(title: str, hint: str = "") -> str:
    t = f"{title} {hint}".lower()
    for label, pat in TYPE_RULES:
        if re.search(pat, t):
            return label
    return "Other"


def keyword_regex(words: list[str]) -> re.Pattern | None:
    if not words:
        return None
    parts = []
    for w in words:
        w = w.strip()
        if w.endswith("*"):
            parts.append(r"\b" + re.escape(w[:-1]) + r"\w*")
        else:
            parts.append(r"\b" + re.escape(w) + r"\b")
    return re.compile("|".join(parts), re.I)


# --------------------------------------------------------------------------- source parsers
def make_job(src, title, url, *, employer="", country="", deadline=None, posted=None,
             jtype="", description="") -> dict:
    title = clean(title)
    return {
        "id": job_id(url, title),
        "title": title,
        "employer": clean(employer),
        "country": clean(country) or src.get("default_country", ""),
        "type": jtype or src.get("default_type") or classify(title, description[:300]),
        "deadline": deadline.isoformat() if deadline else None,
        "posted": posted.isoformat() if posted else None,
        "url": url,
        "source": src["name"],
        "source_url": src.get("home") or src["url"],
        "_text": clean(f"{title} {description}"),
    }


def parse_rss(src: dict, previous: dict) -> list[dict]:
    feed = feedparser.parse(fetch(src["url"]))
    jobs = []
    for e in feed.entries:
        title = e.get("title", "")
        link = e.get("link", "")
        if not title or not link:
            continue
        desc_html = e.get("summary", "") or e.get("description", "")
        if e.get("content"):
            desc_html += " " + " ".join(c.get("value", "") for c in e.content)
        desc = BeautifulSoup(desc_html, "html.parser").get_text(" ")
        posted = None
        if e.get("published_parsed"):
            posted = date(*e.published_parsed[:3])
        # WP Job Manager feeds (e.g. Animal Advocacy Careers) carry a company field
        employer = next((str(v) for k, v in e.items() if "company" in k.lower() and isinstance(v, str)), "")
        employer = employer or e.get("author", "") or ""
        if src.get("employer"):
            employer = src["employer"]
        jobs.append(make_job(src, title, link, employer=employer, deadline=find_deadline(desc),
                             posted=posted, description=desc))
    return jobs


def parse_isae(src: dict, previous: dict) -> list[dict]:
    """ISAE pages: each listing is a table row  [Title, Country](detail.html) | 26th Aug 2026
    under section headings (Employment / Graduate Study / Internship)."""
    jobs, seen_pages = [], set()
    queue = [src["url"]]
    host = urlparse(src["url"]).netloc
    while queue and len(seen_pages) < int(src.get("max_pages", 10)):
        page = queue.pop(0)
        if page in seen_pages:
            continue
        seen_pages.add(page)
        soup = BeautifulSoup(fetch(page), "html.parser")
        section = ""
        for el in soup.find_all(["h1", "h2", "h3", "h4", "tr", "a"]):
            if el.name in ("h1", "h2", "h3", "h4"):
                section = clean(el.get_text())
                continue
            if el.name == "a":
                # pagination links (1, 2, », next)
                txt = clean(el.get_text())
                href = el.get("href")
                if href and (txt.isdigit() or txt in ("»", "›", "Next", "next")):
                    nxt = urljoin(page, href)
                    if urlparse(nxt).netloc == host and nxt not in seen_pages:
                        queue.append(nxt)
                continue
            cells = el.find_all("td", recursive=False) or el.find_all("td")
            if len(cells) < 2:
                continue
            a = cells[0].find("a", href=True)
            if not a:
                continue
            posted = parse_date(clean(cells[1].get_text())) if DATE_RE.search(cells[1].get_text()) else None
            if not posted:
                continue
            text = clean(a.get_text())
            title, country = (text.rsplit(",", 1) + [""])[:2] if "," in text else (text, "")
            url = urljoin(page, a["href"])
            hint = section
            jtype = ("PhD" if "graduate" in section.lower() else
                     "Internship" if "intern" in section.lower() else "")
            jobs.append(make_job(src, title, url, country=country, posted=posted,
                                 jtype=jtype or classify(title, hint)))
    # Optionally open each advert to find a closing date (cached between runs)
    if src.get("fetch_details"):
        for j in jobs:
            prev = previous.get(j["id"])
            if prev and prev.get("_details_v2"):
                j["deadline"] = prev.get("deadline")
                j["employer"] = prev.get("employer", "")
                j["title"] = prev.get("title") or j["title"]
                j["_details_v2"] = True
                continue
            try:
                detail = BeautifulSoup(fetch(j["url"]), "html.parser")
                # ISAE's list only says "PhD, USA"; the advert's first heading is the real title
                for h in detail.find_all(["h1", "h2", "h3", "h4", "strong", "b"]):
                    ht = clean(h.get_text(" "))
                    if 15 <= len(ht) <= 250 and not re.search(r"opportunities in applied|isae|apply for membership", ht, re.I):
                        j["title"] = ht
                        break
                body = clean(detail.get_text(" "))
                d = find_deadline(body)
                j["deadline"] = d.isoformat() if d else None
                j["_text"] = clean(j["_text"] + " " + body[:2000])
                j["_details_v2"] = True
            except Exception as exc:  # detail page missing or blocked: keep the listing
                log(f"  ! ISAE detail failed for {j['url']}: {exc}")
    return jobs


AWRN_MARKER = re.compile(r"^\s*(deadline\b|no\s+deadline\s+(stated|specified))", re.I)


def content_area(soup: BeautifulSoup):
    return (soup.find(class_=re.compile(r"entry-content|post-content|page-content"))
            or soup.find("main") or soup.find("article") or soup.body or soup)


def page_lines(content) -> list[str]:
    """Visible text of an element split into the lines a reader sees (on <br> and block ends)."""
    for br in content.find_all("br"):
        br.replace_with("\n")
    for blk in content.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"]):
        blk.append("\n")
    return [clean(x) for x in content.get_text().split("\n") if clean(x)]


def parse_awrn(src: dict, previous: dict) -> list[dict]:
    """AWRN job and PhD pages, read as lines of text:
         Deadline 27th November 2026                      (or 'No deadline stated/specified')
         Organisation (Country) – Job title               (PhD page: '– PhD in: Title')
    The job title is the text of a link to the advert; we match each line to the link it contains.
    Independent of the page's HTML layout (works whether entries use <p>, <br>, <strong> or lists)."""
    page = src["url"]
    html = fetch(page)
    jobs, lines = _awrn_from(html, page, src, whole_page=False)
    if not jobs:   # the guessed content area may be wrong: retry on the whole page
        jobs, lines = _awrn_from(html, page, src, whole_page=True)
    if not jobs:
        log("  ? AWRN: no entries recognised. First lines of the page text, for diagnosis:")
        for l in lines[:25]:
            log("    | " + l[:160])
    return jobs


def _awrn_from(html: str, page: str, src: dict, whole_page: bool):
    soup = BeautifulSoup(html, "html.parser")
    for bad in soup.find_all(["nav", "footer", "header", "script", "style", "aside"]):
        bad.decompose()
    content = (soup.body or soup) if whole_page else content_area(soup)
    site_host = urlparse(page).netloc
    links = {}
    for a in content.find_all("a", href=True):
        t, h = clean(a.get_text(" ")), urljoin(page, a["href"])
        if len(t) >= 4 and h.startswith("http") and urlparse(h).netloc != site_host:
            links.setdefault(t, h)
    lines = page_lines(content)
    jobs, current, started = [], None, False
    for line in lines:
        m = AWRN_MARKER.search(line)
        if m and m.start() < 3:
            started = True
            current = None if line.lower().startswith("no deadline") else find_deadline(line)
            # entry may continue on the same line after the date
            line = re.sub(r"(?i)^\s*(deadline\s*:?\s*\S+\s+\S+\s+\d{4}|no deadline (stated|specified))\s*", "", line)
        if not started or not line:
            continue
        title = max((t for t in links if t in line), key=len, default=None)
        if not title:
            continue
        url = links[title]
        before = line.split(title)[0]
        before = re.sub(r"(?i)\bphd\s+in\s*:?", "", before).strip(" –—-:|")
        if not before:      # real entries always start with "Organisation (Country) –"
            continue
        parts = re.split(r"\s+[–—]\s+|\s+-\s+", before, maxsplit=1)
        if re.fullmatch(r"\S+@\S+", title):
            # only an email contact is given: title = the project text, link = the AWRN list itself
            if len(parts) < 2:
                continue
            title = re.split(r"(?i)\bcontact\b", parts[1])[0].strip(" :;(–-")
            url = src.get("home") or page
            before = parts[0]
        elif len(parts) == 2 and parts[1]:
            title = f"{parts[1].strip(' :')} – {title}"
            before = parts[0]
        before = before.strip(" –—-:|")
        country = ""
        mc = re.search(r"\(([^()]+)\)\s*$", before)
        if mc:
            country = mc.group(1)
            before = before[: mc.start()].strip()
        jobs.append(make_job(src, title, url, employer=before, country=country, deadline=current))
    return jobs, lines


JOB_WORDS = re.compile(
    "|".join(p for _, p in TYPE_RULES) +
    r"|position|vacanc|opening|fellowship|officer|manager|coordinator|assistant|director|\blead\b"
    r"|specialist|consultant|stelle|mitarbeiter|wissenschaftl|doktorand|poste|chercheur|ingénieur", re.I)
NOT_JOB = re.compile(r"^(careers?|jobs?|apply( now)?|read more|more|view all.*|all (jobs|vacancies)|"
                     r"job alert.*|vacancies|open positions|see all.*|learn more)$", re.I)
CLOSED_HEADING = re.compile(r"closed|past|archive|filled|previous", re.I)


def parse_links(src: dict, previous: dict) -> list[dict]:
    """Generic reader for a static careers page: every link whose text looks like a job title.
    Optional per-source settings: link_pattern (regex the link URL must match),
    exclude_pattern (regex of link texts to skip). Links under 'Closed/Past positions' headings
    are skipped. Pages that load jobs with JavaScript will return nothing."""
    page = src["url"]
    soup = BeautifulSoup(fetch(page), "html.parser")
    for bad in soup.find_all(["nav", "footer", "header", "script", "style"]):
        bad.decompose()
    link_pat = re.compile(src["link_pattern"], re.I) if src.get("link_pattern") else None
    excl = re.compile(src["exclude_pattern"], re.I) if src.get("exclude_pattern") else None
    jobs, seen, closed = [], set(), False
    for el in soup.find_all(["h1", "h2", "h3", "h4", "a"]):
        if el.name != "a":
            closed = bool(CLOSED_HEADING.search(el.get_text()))
            continue
        if closed or not el.get("href"):
            continue
        href = urljoin(page, el["href"])
        text = clean(el.get_text(" "))
        if (not href.startswith("http") or norm_url(href) == norm_url(page) or href in seen
                or not (12 <= len(text) <= 200) or NOT_JOB.match(text) or not JOB_WORDS.search(text)):
            continue
        if link_pat and not link_pat.search(href):
            continue
        if excl and excl.search(text):
            continue
        seen.add(href)
        # nearby text (the card the link sits in) may hold a deadline
        card = el.find_parent(["li", "article", "tr", "div"]) or el.parent
        ctx = clean(card.get_text(" "))[:600] if card else ""
        jobs.append(make_job(src, text, href, employer=src.get("employer", ""),
                             deadline=find_deadline(ctx), description=ctx))
    return jobs


def parse_blocks(src: dict, previous: dict) -> list[dict]:
    """Pages where each position is a heading followed by prose (e.g. ISBE positions page).
    Settings: entry_tags (default [h3, h4]), section_tags (default [h2]) used as position type."""
    page = src["url"]
    soup = BeautifulSoup(fetch(page), "html.parser")
    content = content_area(soup)
    entry_tags = src.get("entry_tags", ["h3", "h4"])
    section_tags = src.get("section_tags", ["h2"])
    jobs, section = [], ""
    for h in content.find_all(entry_tags + section_tags):
        if h.name in section_tags:
            section = clean(h.get_text())
            continue
        title = clean(h.get_text(" "))
        if len(title) < 6:
            continue
        parts, links = [], []
        for sib in h.find_next_siblings():
            if sib.name in entry_tags + section_tags:
                break
            parts.append(sib.get_text(" "))
            links += [urljoin(page, a["href"]) for a in sib.find_all("a", href=True)]
        body = clean(" ".join(parts))
        url = next((l for l in links if l.startswith("http") and urlparse(l).netloc != urlparse(page).netloc), None)
        if not url:
            inner = h.find(id=True)
            anchor = h.get("id") or (inner.get("id") if inner else None)
            url = page + (f"#{anchor}" if anchor else "")
        jobs.append(make_job(src, title, url, deadline=find_deadline(body),
                             jtype=classify(section) if classify(section) != "Other" else "",
                             description=body[:1500]))
    return jobs


def parse_bluesky(src: dict, previous: dict) -> list[dict]:
    """Posts from a Bluesky account via the public, unauthenticated API
    (app.bsky.feed.getAuthorFeed on public.api.bsky.app). 'url' in sources.yaml = the handle."""
    handle = src["url"].lstrip("@")
    api = ("https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
           f"?actor={handle}&limit=100&filter=posts_no_replies")
    data = json.loads(fetch(api))
    jobs = []
    for item in data.get("feed", []):
        post = item.get("post", {})
        rec = post.get("record", {})
        text = clean(rec.get("text", ""))
        if not text:
            continue
        link = None
        for f in rec.get("facets", []) or []:
            for feat in f.get("features", []):
                if feat.get("uri", "").startswith("http"):
                    link = link or feat["uri"]
        ext = (post.get("embed") or {}).get("external") or {}
        link = link or ext.get("uri")
        rkey = post.get("uri", "").rsplit("/", 1)[-1]
        author = (post.get("author") or {}).get("handle", handle)
        post_url = f"https://bsky.app/profile/{author}/post/{rkey}"
        if not link and not JOB_WORDS.search(text):
            continue
        sentences = [x for x in re.split(r"(?<=[.!?])\s+|\n+", text) if x.strip()]
        first = sentences[0] if sentences else text
        k = 1
        while len(first) < 45 and k < len(sentences):     # "Looking for a PhD?" → add the next sentence
            first = f"{first} {sentences[k]}"
            k += 1
        ext_title = clean(ext.get("title", ""))
        junk = (not ext_title or "{{" in ext_title or len(ext_title) < 12
                or not JOB_WORDS.search(ext_title))
        title = (first if (junk or JOB_WORDS.search(first)) else ext_title)[:140]
        posted = parse_date(rec.get("createdAt", "")[:10]) if rec.get("createdAt") else None
        jobs.append(make_job(src, title, link or post_url, deadline=find_deadline(text),
                             posted=posted, description=text + " " + ext.get("description", "")))
    return jobs


def parse_sheet(src: dict, previous: dict) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(fetch(src["url"]))))

    def col(row, *names):
        for k, v in row.items():
            if k and any(n in k.lower() for n in names):
                return (v or "").strip()
        return ""

    jobs = []
    for r in rows:
        title, link = col(r, "title"), col(r, "link", "url")
        if not title or not link.startswith("http"):
            continue
        dl = col(r, "deadline")
        posted = parse_date(col(r, "timestamp")) if col(r, "timestamp") else None
        jobs.append(make_job(src, title, link, employer=col(r, "employer", "institution"),
                             country=col(r, "country"), deadline=parse_date(dl) if dl else None,
                             posted=posted, jtype=col(r, "type")))
    return jobs


PARSERS = {"rss": parse_rss, "isae": parse_isae, "awrn": parse_awrn, "sheet": parse_sheet,
           "links": parse_links, "blocks": parse_blocks, "bluesky": parse_bluesky}


# --------------------------------------------------------------------------- main
def main() -> int:
    global SETTINGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--output", default=str(OUTPUT))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    SETTINGS = cfg.get("settings", {}) or {}
    topic_re = keyword_regex(cfg.get("keywords", []))
    role_re = keyword_regex(cfg.get("role_keywords", []))
    exclude = keyword_regex(cfg.get("exclude_keywords", []))
    max_age = int(SETTINGS.get("max_age_days_without_deadline", 90))

    main_out = Path(args.output)
    trial_out = main_out.with_name("trial.json")   # sources marked trial: true go here for review

    def load(p: Path) -> list[dict]:
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("jobs", []) if p.exists() else []
        except json.JSONDecodeError:
            return []

    groups = {False: {"out": main_out, "prev": load(main_out), "jobs": [], "status": []},
              True: {"out": trial_out, "prev": load(trial_out), "jobs": [], "status": []}}

    for src in cfg.get("sources", []):
        if not src.get("enabled", True) or not src.get("url"):
            continue
        g = groups[bool(src.get("trial"))]
        previous_list = g["prev"]
        previous = {j["id"]: j for j in previous_list}
        log(f"→ {src['name']}{'  [trial]' if src.get('trial') else ''}")
        try:
            got = PARSERS[src["kind"]](src, previous)
            ok = True
        except Exception as exc:
            # A broken source must not wipe its jobs: reuse last run's listings from it.
            log(f"  ! {src['name']} failed: {exc}  (keeping previous listings)")
            got = [j for j in previous_list if j.get("source") == src["name"]]
            ok = False
        if ok and src.get("accumulate"):
            # feeds that only show the newest N items: keep earlier items until they expire
            ids = {j["id"] for j in got}
            got += [j for j in previous_list if j.get("source") == src["name"] and j["id"] not in ids]
        raw = len(got)

        mode = src.get("filter", False)
        if mode:
            custom = keyword_regex(src["keywords"]) if src.get("keywords") else None
            rx = custom or (role_re if mode == "role" else topic_re)
            if mode == "role":
                txt = lambda j: j["title"]
            else:
                txt = lambda j: j.get("_text") or f"{j['title']} {j.get('employer', '')}"
            got = [j for j in got if rx and rx.search(txt(j)) and not (exclude and exclude.search(txt(j)))]
        log(f"  {raw} found, {len(got)} kept" + "".join(f"\n    · {j['title'][:90]}" for j in got[:5]))
        g["status"].append({"source": src["name"], "ok": ok, "count": len(got),
                            "url": src.get("home") or src["url"]})
        g["jobs"].extend(got)

    for is_trial, g in groups.items():
        previous = {j["id"]: j for j in g["prev"]}
        # de-duplicate (same advert URL, or same title + employer)
        merged: dict[str, dict] = {}
        for j in g["jobs"]:
            key = norm_url(j["url"])
            alt = (j["title"].lower(), j["employer"].lower())
            dup = merged.get(key) or next((m for m in merged.values()
                                           if alt[1] and (m["title"].lower(), m["employer"].lower()) == alt), None)
            if dup:
                for f in ("deadline", "employer", "country", "posted"):
                    dup[f] = dup[f] or j[f]
                if j["source"] not in dup["also_on"] and j["source"] != dup["source"]:
                    dup["also_on"].append(j["source"])
                continue
            j = dict(j, also_on=[])
            merged[key] = j

        now = datetime.now(timezone.utc).date().isoformat()
        final = []
        for j in merged.values():
            prev = previous.get(j["id"])
            j["first_seen"] = (prev or {}).get("first_seen") or j.get("first_seen") or now
            if j["deadline"]:
                if date.fromisoformat(j["deadline"]) < TODAY:
                    continue
            else:
                start = date.fromisoformat(j["posted"] or j["first_seen"])
                if (TODAY - start).days > max_age:
                    continue
            final.append(j)

        final.sort(key=lambda j: (j["deadline"] or "9999", j["title"].lower()))
        public = [{k: v for k, v in j.items() if k != "_text"} for j in final]
        payload = {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                   "count": len(public), "sources": g["status"], "jobs": public}
        log(f"✓ {len(public)} open listings → {g['out'].name}")
        if args.dry_run:
            continue
        g["out"].parent.mkdir(parents=True, exist_ok=True)
        g["out"].write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
