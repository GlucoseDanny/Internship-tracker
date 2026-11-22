"""Readers for each job system ("ATS").

Each reader takes one company entry from companies.toml and returns a list of
raw postings (only internship-looking titles), as plain dicts:

    title, url, locations, us, posted, deadline, desc, pay, clearance_hint
"""

from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from html.parser import HTMLParser

from .classify import any_us, find_pay, html_to_text, is_internship
from .http import FetchError, fetch

# Search words used on systems where we have to search instead of listing all jobs
SEARCH_TERMS = ("intern", "co-op")
MAX_RESULTS_PER_SEARCH = 2000


def _date(s) -> str:
    """Best-effort YYYY-MM-DD from many date formats."""
    if not s:
        return ""
    if isinstance(s, (int, float)):
        ts = s / 1000 if s > 1e11 else s
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")
    s = str(s).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    for fmt in ("%a %b %d %H:%M:%S %Z %Y", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def _uniq(items):
    return [x for x in dict.fromkeys(i.strip() for i in items if i and i.strip())]


def _posting(title, url, locations, *, us=None, posted="", deadline="", desc="", pay="", clearance_hint="",
             employment_type=""):
    locations = _uniq(locations)
    if us is None:
        us = any_us(locations, default=None)
    return {
        "title": " ".join(title.split()),
        "url": url,
        "locations": locations,
        "us": us,
        "posted": posted,
        "deadline": deadline,
        "desc": desc,
        "pay": pay or find_pay(desc),
        "clearance_hint": clearance_hint,
        # the job system's own job-type label (e.g. "Intern"), used when the title doesn't say
        "employment_type": employment_type,
    }


# ---------------------------------------------------------------- Greenhouse

def greenhouse(c: dict) -> list[dict]:
    slug = c["slug"]
    data = fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        meta = " ".join(str(m.get("value")) for m in (j.get("metadata") or []) if isinstance(m.get("value"), str))
        if not is_internship(j.get("title", ""), meta):
            continue
        locs = [(j.get("location") or {}).get("name", "")]
        cities = {l.split(",")[0].strip().lower() for l in locs if l}
        for o in j.get("offices") or []:
            ol = o.get("location") or o.get("name") or ""
            if ol and ol.split(",")[0].strip().lower() not in cities:
                locs.append(ol)
                cities.add(ol.split(",")[0].strip().lower())
        desc = html_to_text(j.get("content"))
        out.append(_posting(
            j["title"], j.get("absolute_url", ""), locs,
            posted=_date(j.get("first_published") or j.get("updated_at")),
            deadline=_date(j.get("application_deadline")),
            desc=desc, employment_type=meta,
        ))
    return out


def greenhouse_one(slug: str, job_id: str) -> dict | None:
    j = fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}")
    return {"desc": html_to_text(j.get("content")), "posted": _date(j.get("first_published") or j.get("updated_at"))}


# --------------------------------------------------------------------- Lever

def _lever_desc(j: dict) -> str:
    parts = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
    for lst in j.get("lists") or []:
        parts.append(f"{lst.get('text', '')}\n" + html_to_text(f"<ul>{lst.get('content', '')}</ul>"))
    parts.append(j.get("additionalPlain") or "")
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def _lever_pay(j: dict) -> str:
    r = j.get("salaryRange") or {}
    if not r.get("min"):
        return ""
    unit = "/hr" if "hour" in (r.get("interval") or "") else "/yr"
    fmt = (lambda v: f"${v:,.0f}") if unit == "/yr" else (lambda v: f"${v:g}")
    return f"{fmt(r['min'])}–{fmt(r.get('max') or r['min'])}{unit}"


def lever(c: dict) -> list[dict]:
    data = fetch(f"https://api.lever.co/v0/postings/{c['slug']}?mode=json")
    out = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        if not is_internship(j.get("text", ""), cats.get("commitment", "")):
            continue
        locs = cats.get("allLocations") or [cats.get("location", "")]
        country = j.get("country")
        us = True if country == "US" else (False if country else None)
        if us is False and any_us(locs):
            us = True
        out.append(_posting(
            j["text"], j.get("hostedUrl", ""), locs, us=us,
            posted=_date(j.get("createdAt")), desc=_lever_desc(j), pay=_lever_pay(j),
            employment_type=cats.get("commitment", ""),
        ))
    return out


def lever_one(slug: str, job_id: str) -> dict | None:
    j = fetch(f"https://api.lever.co/v0/postings/{slug}/{job_id}?mode=json")
    return {"desc": _lever_desc(j), "pay": _lever_pay(j), "posted": _date(j.get("createdAt"))}


# --------------------------------------------------------------------- Ashby

_ashby_cache: dict[str, dict] = {}


def _ashby_board(slug: str) -> dict:
    if slug not in _ashby_cache:
        _ashby_cache[slug] = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    return _ashby_cache[slug]


def _ashby_fields(j: dict) -> dict:
    desc = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))
    comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
    return {"desc": desc, "pay": comp, "posted": _date(j.get("publishedAt"))}


def ashby(c: dict) -> list[dict]:
    data = _ashby_board(c["slug"])
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        if not is_internship(j.get("title", ""), j.get("employmentType", "")):
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in (j.get("secondaryLocations") or [])]
        country = (((j.get("address") or {}).get("postalAddress")) or {}).get("addressCountry") or ""
        us = True if country in ("United States", "USA", "US") else (False if country else None)
        if us is False and any_us(locs):
            us = True
        f = _ashby_fields(j)
        out.append(_posting(j["title"], j.get("jobUrl", ""), locs, us=us, posted=f["posted"], desc=f["desc"], pay=f["pay"],
                            employment_type=j.get("employmentType", "")))
    return out


def ashby_one(slug: str, job_id: str) -> dict | None:
    for j in _ashby_board(slug).get("jobs", []):
        if j.get("id") == job_id:
            return _ashby_fields(j)
    return None


# ------------------------------------------------------------------- Workday

def _workday_detail(base: str, path: str, host: str, site: str) -> dict:
    info = fetch(f"{base}{path}").get("jobPostingInfo") or {}
    locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
    ctry = ((info.get("jobRequisitionLocation") or {}).get("country") or {})
    code = ctry.get("alpha2Code") or ""
    us = True if code == "US" else (False if code else None)
    if us is False and any_us(locs):
        us = True  # multi-location postings that include a US site
    return {
        "title": info.get("title", ""),
        "url": info.get("externalUrl") or f"https://{host}/{site}{path}",
        "locations": locs,
        "us": us,
        "posted": _date(info.get("startDate")),
        "deadline": _date(info.get("endDate")),
        "desc": html_to_text(info.get("jobDescription")),
    }


_INTERN_FACET_RE = re.compile(r"\b(intern|interns|internship|internships|co-?op|co-?ops|student|students)\b", re.I)


def _workday_intern_facets(facets) -> dict[str, list[str]]:
    """Find filter values like 'Intern' or 'Co-op' in a Workday site's own job-type filters."""
    found: dict[str, list[str]] = {}

    def walk(fs):
        for f in fs or []:
            param = f.get("facetParameter")
            for v in f.get("values") or []:
                if v.get("facetParameter") and v.get("values"):
                    walk([v])  # nested group of filters
                    continue
                if param and v.get("id") and _INTERN_FACET_RE.search(v.get("descriptor") or ""):
                    found.setdefault(param, []).append(v["id"])

    walk(facets)
    return found


def _workday_search(base: str, body: dict, keep, candidates: dict) -> dict:
    offset, total, first = 0, None, None
    while True:
        d = fetch(f"{base}/jobs", json_body={**body, "limit": 20, "offset": offset})
        first = first or d
        posts = d.get("jobPostings") or []
        if not total:
            total = d.get("total") or 0
        for p in posts:
            if p.get("externalPath") and keep(p.get("title", "")):
                candidates.setdefault(p["externalPath"], p)
        offset += 20
        if not posts or offset >= min(total, MAX_RESULTS_PER_SEARCH):
            return first or {}


def workday(c: dict) -> list[dict]:
    host, site = c["host"], c["site"]
    tenant = host.split(".")[0]
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    candidates: dict[str, dict] = {}
    facet_hits: set[str] = set()
    # 1. keyword search. Workday's search is fuzzy, so we check every title ourselves.
    #    ("co-op" is not searched: Workday splits it and matches every "operations" job.)
    first = _workday_search(base, {"appliedFacets": {}, "searchText": "intern"}, is_internship, candidates)
    # 2. the site's own "Intern" / "Co-op" / "Student" job-type filters, if it has them
    for param, ids in _workday_intern_facets(first.get("facets")).items():
        before = set(candidates)
        _workday_search(base, {"appliedFacets": {param: ids}, "searchText": ""},
                        lambda t: is_internship(t, "Intern"), candidates)
        facet_hits |= set(candidates) - before
    out = []
    for path, p in candidates.items():
        try:
            det = _workday_detail(base, path, host, site)
        except FetchError:
            # Keep the posting even if its detail page failed
            det = {"title": p.get("title", ""), "url": f"https://{host}/{site}{path}",
                   "locations": [p.get("locationsText", "")], "us": None, "posted": "", "deadline": "", "desc": ""}
        out.append(_posting(det["title"] or p.get("title", ""), det["url"], det["locations"], us=det["us"],
                            posted=det["posted"], deadline=det["deadline"], desc=det["desc"],
                            employment_type="Intern" if path in facet_hits else ""))
    return out


def workday_one(url: str) -> dict | None:
    """Description for a Workday posting URL found on a community list."""
    u = urllib.parse.urlparse(url)
    segs = [s for s in u.path.split("/") if s]
    if segs and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segs[0]):
        segs = segs[1:]
    if len(segs) < 3 or "job" not in segs:
        return None
    site = segs[0]
    path = "/" + "/".join(segs[segs.index("job"):])
    tenant = u.netloc.split(".")[0]
    det = _workday_detail(f"https://{u.netloc}/wday/cxs/{tenant}/{site}", path, u.netloc, site)
    return {"desc": det["desc"], "posted": det["posted"], "deadline": det["deadline"]}


# ----------------------------------------------------------- SmartRecruiters

def _sr_detail(slug: str, pid: str) -> dict:
    d = fetch(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{pid}")
    secs = ((d.get("jobAd") or {}).get("sections")) or {}
    parts = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        s = secs.get(key) or {}
        if s.get("text"):
            parts.append((s.get("title") or "") + "\n" + html_to_text(s["text"]))
    return {"desc": "\n\n".join(p.strip() for p in parts)}


def smartrecruiters(c: dict) -> list[dict]:
    slug = c["slug"]
    out, offset = [], 0
    while True:
        d = fetch(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}")
        for p in d.get("content", []):
            emp = f"{(p.get('typeOfEmployment') or {}).get('label', '')} {(p.get('experienceLevel') or {}).get('label', '')}"
            if not is_internship(p.get("name", ""), emp):
                continue
            loc = p.get("location") or {}
            loc_s = ", ".join(x for x in (loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()) if x)
            try:
                desc = _sr_detail(slug, p["id"])["desc"]
            except FetchError:
                desc = ""
            out.append(_posting(
                p["name"], f"https://jobs.smartrecruiters.com/{slug}/{p['id']}", [loc_s],
                us=(loc.get("country") or "").lower() == "us" or None,
                posted=_date(p.get("releasedDate")), desc=desc, employment_type=emp,
            ))
        offset += 100
        if offset >= (d.get("totalFound") or 0) or not d.get("content"):
            break
    return out


def smartrecruiters_one(slug: str, pid: str) -> dict | None:
    return _sr_detail(slug, pid)


# -------------------------------------------------------------------- Oracle

def oracle(c: dict) -> list[dict]:
    host, site = c["host"], c["site"]
    api = f"https://{host}/hcmRestApi/resources/latest"
    found: dict[str, dict] = {}
    for term in SEARCH_TERMS:
        offset = 0
        while True:
            finder = f"findReqs;siteNumber={site},keyword={urllib.parse.quote(term)},limit=25,offset={offset},sortBy=POSTING_DATES_DESC"
            d = fetch(f"{api}/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}")
            item = (d.get("items") or [{}])[0]
            reqs = item.get("requisitionList") or []
            for r in reqs:
                if is_internship(r.get("Title", "")):
                    found.setdefault(str(r["Id"]), r)
            offset += 25
            if not reqs or offset >= min(item.get("TotalJobsCount") or 0, MAX_RESULTS_PER_SEARCH):
                break
    out = []
    for rid, r in found.items():
        desc = ""
        try:
            det = fetch(f"{api}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
                        f"&finder=ById;Id=%22{rid}%22,siteNumber={site}")
            it = (det.get("items") or [{}])[0]
            desc = "\n\n".join(html_to_text(it.get(k)) for k in
                               ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr") if it.get(k))
        except FetchError:
            pass
        locs = [r.get("PrimaryLocation", "")] + [s.get("Name", "") for s in (r.get("secondaryLocations") or [])]
        us = True if r.get("PrimaryLocationCountry") == "US" else None
        out.append(_posting(
            r["Title"], f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}", locs, us=us,
            posted=_date(r.get("PostedDate")), deadline=_date(r.get("PostingEndDate")), desc=desc,
        ))
    return out


# ---------------------------------------------------------------------- Jibe
# (iCIMS-powered career sites such as careers.jhuapl.edu)

def jibe(c: dict) -> list[dict]:
    host = c["host"]
    found: dict[str, dict] = {}
    for term in SEARCH_TERMS:
        page = 1
        while True:
            d = fetch(f"https://{host}/api/jobs?keywords={urllib.parse.quote(term)}&page={page}&limit=100")
            jobs = d.get("jobs") or []
            for x in jobs:
                data = x.get("data") or {}
                if is_internship(data.get("title", "")):
                    found.setdefault(str(data.get("slug") or data.get("req_id")), data)
            if not jobs or page * 100 >= (d.get("totalCount") or 0) or page >= 20:
                break
            page += 1
    out = []
    for slug, x in found.items():
        tags = " ".join(" ".join(map(str, v)) if isinstance(v, list) else str(v)
                        for k, v in x.items() if k.startswith("tags"))
        desc = html_to_text(x.get("description"))
        qual = html_to_text(x.get("qualifications"))
        if qual and qual[:80] not in desc:
            desc = f"{desc}\n\nQualifications\n{qual}"
        pay = ""
        amounts = re.findall(r"\$\s?(\d+(?:\.\d+)?)\s*/\s*hr", tags, re.I)
        if len(amounts) >= 2:
            pay = f"${amounts[0]}–${amounts[1]}/hr"
        loc = ", ".join(v for v in (x.get("city"), x.get("state")) if v)
        out.append(_posting(
            x.get("title", ""), f"https://{host}/jobs/{slug}?lang=en-us", [loc],
            us=(x.get("country_code") == "US") or None,
            posted=_date(x.get("posted_date")), deadline=_date(x.get("posting_expiry_date")),
            desc=desc, pay=pay, clearance_hint=tags,
        ))
    return out


# ------------------------------------------------------------ SuccessFactors
# (SAP SuccessFactors career sites: jobs.ornl.gov, jobs.l3harris.com, ...)

class _ClassGrabber(HTMLParser):
    """Collects the inner HTML of the first element with a given class."""
    VOID = {"br", "img", "hr", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}

    def __init__(self, cls: str):
        super().__init__(convert_charrefs=False)
        self.cls, self.depth, self.done, self.buf = cls, 0, False, []

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if self.depth:
            self.buf.append(self.get_starttag_text() or "")
            if tag not in self.VOID:
                self.depth += 1
        elif self.cls in (dict(attrs).get("class") or "").split():
            self.depth = 1

    def handle_endtag(self, tag):
        if self.depth and not self.done and tag not in self.VOID:
            self.depth -= 1
            if self.depth == 0:
                self.done = True
            else:
                self.buf.append(f"</{tag}>")

    def handle_data(self, data):
        if self.depth and not self.done:
            self.buf.append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")


def _grab_class(page: str, cls: str) -> str:
    g = _ClassGrabber(cls)
    try:
        g.feed(page)
    except Exception:
        pass
    return "".join(g.buf)


_SF_LINK_RE = re.compile(r'<a[^>]+href="(/job/[^"]+)"[^>]*class="jobTitle-link[^"]*"[^>]*>(.*?)</a>'
                         r'|<a[^>]+class="jobTitle-link[^"]*"[^>]*href="(/job/[^"]+)"[^>]*>(.*?)</a>', re.S)


def successfactors(c: dict) -> list[dict]:
    import html as _html
    host = c["host"]
    candidates: dict[str, str] = {}
    for term in SEARCH_TERMS:
        start, seen = 0, set()
        while start < MAX_RESULTS_PER_SEARCH:
            page = fetch(f"https://{host}/search/?q={urllib.parse.quote(term)}&startrow={start}", as_json=False)
            rows = []
            for m in _SF_LINK_RE.finditer(page):
                href = m.group(1) or m.group(3)
                title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(2) or m.group(4) or "")).strip()
                rows.append((href, title))
            new = [(h, t) for h, t in dict(rows).items() if h not in seen]
            if not new:
                break
            for h, t in new:
                seen.add(h)
                if is_internship(t):
                    candidates.setdefault(h, t)
            start += len(new)
    out = []
    for href, title in candidates.items():
        url = f"https://{host}{href}"
        try:
            page = fetch(url, as_json=False)
        except FetchError:
            out.append(_posting(title, url, []))
            continue
        def prop(name):
            mm = re.search(rf'itemprop="{name}"[^>]*content="([^"]*)"', page)
            return _html.unescape(mm.group(1)).strip() if mm else ""

        city, region, country = prop("addressLocality"), prop("addressRegion"), prop("addressCountry")
        loc = ", ".join(x for x in (city, region) if x)
        us = (country == "US") if country else None
        if not loc:
            m = re.search(r'data-careersite-propertyid="location"[^>]*>(.*?)</', page, re.S)
            raw = re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""
            parts = [p.strip() for p in raw.split(",")]
            if len(parts) >= 3 and re.fullmatch(r"[A-Z]{2}", parts[2]):
                us = parts[2] == "US"
                loc = f"{parts[0]}, {parts[1]}"
            else:
                loc = raw
        desc = html_to_text(_grab_class(page, "jobdescription"))
        out.append(_posting(title, url, [loc], us=us, posted=_date(prop("datePosted")),
                            deadline=_date(prop("validThrough")), desc=desc))
    return out


READERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "workday": workday,
    "smartrecruiters": smartrecruiters,
    "oracle": oracle,
    "jibe": jibe,
    "successfactors": successfactors,
}


def source_key(c: dict) -> str:
    """Identity used to match community-list links to companies we read directly."""
    ats = c["ats"]
    if ats in ("greenhouse", "lever", "ashby", "smartrecruiters"):
        return f"{ats}:{c['slug'].lower()}"
    return f"{ats}:{c['host'].lower()}"


def key_from_url(url: str) -> str:
    """Same identity, worked out from a posting URL."""
    u = urllib.parse.urlparse(url)
    host = u.netloc.lower()
    segs = [s for s in u.path.split("/") if s]
    q = urllib.parse.parse_qs(u.query)
    if "greenhouse.io" in host:
        if q.get("for"):
            return f"greenhouse:{q['for'][0].lower()}"
        return f"greenhouse:{segs[0].lower()}" if segs else ""
    if host == "jobs.lever.co" and segs:
        return f"lever:{segs[0].lower()}"
    if host == "jobs.ashbyhq.com" and segs:
        return f"ashby:{urllib.parse.unquote(segs[0]).lower()}"
    if host == "jobs.smartrecruiters.com" and segs:
        return f"smartrecruiters:{segs[0].lower()}"
    if host.endswith("myworkdayjobs.com"):
        return f"workday:{host}"
    if "oraclecloud.com" in host:
        return f"oracle:{host}"
    return f"site:{host}"


def describe_url(url: str) -> dict | None:
    """Fetch a description for a community-list posting when its site allows it."""
    u = urllib.parse.urlparse(url)
    host = u.netloc.lower()
    segs = [s for s in u.path.split("/") if s]
    q = urllib.parse.parse_qs(u.query)
    if "greenhouse.io" in host:
        slug = q.get("for", [segs[0] if segs else ""])[0]
        jid = q.get("gh_jid", [None])[0] or (segs[segs.index("jobs") + 1] if "jobs" in segs and segs.index("jobs") + 1 < len(segs) else None)
        if slug and jid and jid.isdigit():
            return greenhouse_one(slug, jid)
    elif host == "jobs.lever.co" and len(segs) >= 2:
        return lever_one(segs[0], segs[1])
    elif host == "jobs.ashbyhq.com" and len(segs) >= 2:
        return ashby_one(urllib.parse.unquote(segs[0]), segs[1])
    elif host.endswith("myworkdayjobs.com"):
        return workday_one(url)
    elif host == "jobs.smartrecruiters.com" and len(segs) >= 2:
        return smartrecruiters_one(segs[0], segs[1])
    return None
