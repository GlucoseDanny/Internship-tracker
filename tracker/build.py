"""Collect internships from every source and build the website.

    python -m tracker.build                      # normal run
    python -m tracker.build --only "SpaceX"      # quick test on one company
    python -m tracker.build --previous-url URL   # reuse last week's data for any site that fails today

Writes:
    docs/index.html            the page (data embedded)
    docs/data/jobs.json        all internships (no long descriptions)
    docs/data/desc/<id>.json   full descriptions, one file per company
    docs/data/internships.csv  spreadsheet version
    data/seen.json             first-seen date of every posting (kept in git)
    data/last_run.json         what each source returned this run (kept in git)
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import hashlib
import json
import re
import shutil
import sys
import time
import tomllib
from pathlib import Path

from . import classify as C
from .http import FetchError, fetch
from .sources import READERS, describe_url, key_from_url, source_key

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DATA = ROOT / "data"
SITE = ROOT / "site"

COMMUNITY_LISTS = [
    {
        "name": "SimplifyJobs Summer 2027 list",
        "url": "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
        "home": "https://github.com/SimplifyJobs/Summer2027-Internships",
    },
    {
        "name": "vanshb03 Summer 2027 list",
        "url": "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
        "home": "https://github.com/vanshb03/Summer2027-Internships",
    },
]

# The community lists are mostly software jobs. By default we keep every role at
# aerospace / defense / space / lab employers, plus engineering roles anywhere.
# Set to True to keep *every* listed internship.
INCLUDE_ALL_LIST_ROLES = False

DESC_LIMIT = 9000          # characters of description kept per posting
SUMMARY_LIMIT = 260
ENRICH_BUDGET_SECONDS = 600  # time allowed for fetching descriptions of list postings
DIRECT_WORKERS = 10

ATS_LABEL = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workday": "Workday",
    "smartrecruiters": "SmartRecruiters", "oracle": "Oracle", "jibe": "iCIMS", "successfactors": "SuccessFactors",
}

_STOP_WORDS = r"\b(the|inc|incorporated|corp|corporation|co|company|llc|ltd|plc|technologies|technology|industries|group|holdings|usa|us)\b"


def norm_company(name: str) -> str:
    n = re.sub(r"\(.*?\)", " ", name.lower())
    n = re.sub(_STOP_WORDS, " ", n)
    return re.sub(r"[^a-z0-9]+", "", n)


def norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "company"


def careers_url(c: dict) -> str:
    ats = c["ats"]
    if ats == "greenhouse":
        return f"https://job-boards.greenhouse.io/{c['slug']}"
    if ats == "lever":
        return f"https://jobs.lever.co/{c['slug']}"
    if ats == "ashby":
        return f"https://jobs.ashbyhq.com/{c['slug']}"
    if ats == "smartrecruiters":
        return f"https://jobs.smartrecruiters.com/{c['slug']}"
    if ats == "workday":
        return f"https://{c['host']}/{c['site']}?q=intern"
    if ats == "oracle":
        return f"https://{c['host']}/hcmUI/CandidateExperience/en/sites/{c['site']}/requisitions?keyword=intern"
    if ats == "jibe":
        return f"https://{c['host']}/jobs?keywords=intern"
    if ats == "successfactors":
        return f"https://{c['host']}/search/?q=intern"
    return ""


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# 1. Read companies directly
# --------------------------------------------------------------------------

def read_direct(companies: list[dict]) -> tuple[list[dict], list[dict]]:
    postings, status = [], []

    def run(c):
        t0 = time.time()
        try:
            got = READERS[c["ats"]](c)
            return c, got, None, time.time() - t0
        except FetchError as e:
            return c, [], str(e), time.time() - t0
        except Exception as e:  # a bug in one reader must not stop the others
            return c, [], f"{type(e).__name__}: {e}", time.time() - t0

    with cf.ThreadPoolExecutor(DIRECT_WORKERS) as ex:
        futures = [ex.submit(run, c) for c in companies]
        for fut in cf.as_completed(futures):
            c, got, err, secs = fut.result()
            key = source_key(c)
            status.append({
                "key": key, "company": c["name"], "kind": ATS_LABEL.get(c["ats"], c["ats"]),
                "ok": err is None, "found": len(got), "error": err or "", "seconds": round(secs, 1),
                "careers": careers_url(c), "sector": c["sector"],
            })
            log(f"  {'ok ' if err is None else 'ERR'} {c['name']}: {len(got)} found ({secs:.0f}s){' - ' + err if err else ''}")
            for p in got:
                p.update(company=c["name"], sector=c["sector"], src_key=key,
                         src=f"{c['name']} careers site", from_list=False)
                postings.append(p)
    return postings, status


# --------------------------------------------------------------------------
# 2. Community lists
# --------------------------------------------------------------------------

def read_lists(direct_ok_keys: set[str], direct_ok_names: set[str]) -> tuple[list[dict], list[dict]]:
    postings, status, seen_urls = [], [], set()
    for lst in COMMUNITY_LISTS:
        try:
            rows = fetch(lst["url"], timeout=90)
        except FetchError as e:
            status.append({"key": f"list:{lst['name']}", "company": lst["name"], "kind": "Community list",
                           "ok": False, "found": 0, "error": str(e), "careers": lst["home"], "sector": ""})
            log(f"  ERR {lst['name']}: {e}")
            continue
        n = 0
        for x in rows if isinstance(rows, list) else []:
            if not x.get("active") or x.get("is_visible") is False:
                continue
            url = (x.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            company = (x.get("company_name") or "").strip()
            if key_from_url(url) in direct_ok_keys or norm_company(company) in direct_ok_names:
                continue  # we already read this company's own site
            seen_urls.add(url)
            terms = list(x.get("terms") or [])
            if not terms and x.get("season"):
                s = str(x["season"]).strip()
                terms = [s if re.search(r"\d{4}", s) else f"{s} 2027"]
            terms = [t for t in terms if t and t != "N/A"]
            postings.append({
                "company": company, "title": " ".join((x.get("title") or "").split()), "url": url,
                "locations": [l for l in (x.get("locations") or []) if l],
                "us": None, "posted": _epoch_date(x.get("date_posted")), "deadline": "", "desc": "", "pay": "",
                "clearance_hint": "", "list_terms": terms,
                "list_citizen": "citizen" in (x.get("sponsorship") or "").lower(),
                "sector": C.guess_sector(company), "src_key": f"list:{lst['name']}",
                "src": lst["name"], "from_list": True,
            })
            n += 1
        status.append({"key": f"list:{lst['name']}", "company": lst["name"], "kind": "Community list",
                       "ok": True, "found": n, "error": "", "careers": lst["home"], "sector": ""})
        log(f"  ok  {lst['name']}: {n} active postings not already covered")
    return postings, status


def _epoch_date(v) -> str:
    try:
        return dt.datetime.fromtimestamp(int(v), dt.timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


# --------------------------------------------------------------------------
# 3. Classify
# --------------------------------------------------------------------------

def classify(p: dict, today: dt.date) -> dict | None:
    title = p["title"]
    if not title:
        return None
    if p["from_list"]:
        if C.HIGH_SCHOOL_RE.search(title) or C.NOT_INTERN_RE.search(title):
            return None
    elif not C.is_internship(title, p.get("employment_type", "")):
        return None

    locs = p["locations"]
    us = p["us"] if p["us"] is not None else C.any_us(locs, default=None)
    if us is False:
        return None

    desc = p.get("desc") or ""
    terms, _ = C.find_terms(title, desc)
    if p.get("list_terms") and not any(t != "Co-op" for t in terms):
        terms = list(dict.fromkeys(p["list_terms"] + terms))
    if C.all_terms_past(terms, today):
        return None

    field = C.field_of(title)
    if p["from_list"] and not INCLUDE_ALL_LIST_ROLES:
        if p["sector"] == "other" and field != "Engineering":
            return None

    grad = bool((C._GRAD_RE.search(title) and not C._UNDERGRAD_RE.search(title)) or C._POSTGRAD_RE.search(title))
    skillbridge = bool(C._SKILLBRIDGE_RE.search(title) or C._SKILLBRIDGE_RE.search(desc[:3000]))
    clearance = C.clearance_of(desc, p.get("clearance_hint", ""))
    citizen = bool(p.get("list_citizen") or C._CITIZEN_RE.search(desc) or clearance)
    near = C.near_home(locs)
    fit, why = C.score(title, desc, field=field, near=near, clearance=clearance, grad=grad,
                       skillbridge=skillbridge, terms=terms, sector=p["sector"])
    if near:  # show the location near home first
        locs = sorted(locs, key=lambda l: not C.near_home([l]))
    return {
        **p,
        "locations": locs,
        "terms": [t for t in terms if not C.term_is_past(t, today)] or terms,
        "field": field, "grad": grad, "skillbridge": skillbridge, "clearance": clearance,
        "citizen": citizen, "near": near, "remote": C.is_remote(locs), "fit": fit, "why": why,
        "pay": p.get("pay") or C.find_pay(desc),
    }


# --------------------------------------------------------------------------
# 4. Descriptions for list postings
# --------------------------------------------------------------------------

def enrich(items: list[dict]) -> int:
    todo = [p for p in items if p["from_list"] and not p.get("desc")]
    if not todo:
        return 0
    deadline = time.time() + ENRICH_BUDGET_SECONDS
    done = 0

    def run(p):
        if time.time() > deadline:
            return p, None
        try:
            return p, describe_url(p["url"])
        except (FetchError, Exception):
            return p, None

    with cf.ThreadPoolExecutor(8) as ex:
        for p, info in ex.map(run, todo):
            if info and info.get("desc"):
                p["desc"] = info["desc"]
                for k in ("pay", "deadline", "posted"):
                    if info.get(k) and not p.get(k):
                        p[k] = info[k]
                done += 1
    return done


# --------------------------------------------------------------------------
# 5. Merge duplicates, carry over, first-seen
# --------------------------------------------------------------------------

def merge(items: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for p in items:
        key = f"{norm_company(p['company'])}|{norm_title(p['title'])}"
        jid = hashlib.sha1(key.encode()).hexdigest()[:12]
        g = groups.get(jid)
        posting = {"url": p["url"], "loc": "; ".join(p["locations"][:3]) or "See posting",
                   "posted": p.get("posted", ""), "deadline": p.get("deadline", "")}
        if g is None:
            groups[jid] = g = {
                "id": jid, "company": p["company"], "company_id": slugify(norm_company(p["company"]) or p["company"]),
                "sector": p["sector"], "title": p["title"], "field": p["field"], "terms": list(p["terms"]),
                "locations": list(p["locations"]), "near": p["near"], "remote": p["remote"],
                "postings": [], "clearance": p["clearance"], "citizen": p["citizen"], "grad": p["grad"],
                "skillbridge": p["skillbridge"], "pay": p["pay"], "fit": p["fit"], "why": list(p["why"]),
                "src": p["src"], "src_key": p["src_key"], "from_list": p["from_list"],
                "desc": C.clip(p.get("desc") or "", DESC_LIMIT),
            }
        else:
            for k in ("terms", "locations", "why"):
                g[k] = list(dict.fromkeys(g[k] + p[k]))
            if p["fit"] > g["fit"]:
                g["fit"] = p["fit"]
            g["near"] = g["near"] or p["near"]
            g["clearance"] = g["clearance"] or p["clearance"]
            g["citizen"] = g["citizen"] or p["citizen"]
            g["pay"] = g["pay"] or p["pay"]
            if not g["desc"] and p.get("desc"):
                g["desc"] = C.clip(p["desc"], DESC_LIMIT)
        if posting["url"] not in {x["url"] for x in g["postings"]}:
            g["postings"].append(posting)
    # One display name per company ("Acme" and "Acme, Inc." -> "Acme")
    names: dict[str, str] = {}
    for g in groups.values():
        k = norm_company(g["company"])
        if k not in names or len(g["company"]) < len(names[k]):
            names[k] = g["company"]
    for g in groups.values():
        g["company"] = names[norm_company(g["company"])]
        dates = [x["posted"] for x in g["postings"] if x["posted"]]
        dls = [x["deadline"] for x in g["postings"] if x["deadline"]]
        g["posted"] = max(dates) if dates else ""
        g["deadline"] = min(dls) if dls else ""
        g["summary"] = C.clip(re.sub(r"\s+", " ", g["desc"]), SUMMARY_LIMIT) if g["desc"] else ""
    return list(groups.values())


def load_previous(previous_url: str | None) -> tuple[list[dict], str]:
    """Last run's jobs (for carrying over sites that fail today) + base URL for descriptions."""
    local = DOCS / "data" / "jobs.json"
    if local.exists():
        try:
            return json.loads(local.read_text())["jobs"], str(DOCS)
        except Exception:
            pass
    if previous_url:
        try:
            d = fetch(previous_url, timeout=60)
            return d.get("jobs", []), previous_url.rsplit("/data/", 1)[0]
        except FetchError as e:
            log(f"  (could not load previous data: {e})")
    return [], ""


def previous_desc(base: str, company_id: str, cache: dict) -> dict:
    if company_id in cache:
        return cache[company_id]
    out = {}
    try:
        if base.startswith("http"):
            out = fetch(f"{base}/data/desc/{company_id}.json", timeout=30)
        else:
            f = Path(base) / "data" / "desc" / f"{company_id}.json"
            if f.exists():
                out = json.loads(f.read_text())
    except Exception:
        out = {}
    cache[company_id] = out
    return out


# --------------------------------------------------------------------------
# 6. Write outputs
# --------------------------------------------------------------------------

def write_outputs(jobs: list[dict], status: list[dict], companies: list[dict], meta: dict, embed_desc: bool) -> None:
    data_dir = DOCS / "data"
    if (data_dir / "desc").exists():
        shutil.rmtree(data_dir / "desc")
    (data_dir / "desc").mkdir(parents=True, exist_ok=True)

    by_company: dict[str, dict] = {}
    for j in jobs:
        by_company.setdefault(j["company_id"], {})[j["id"]] = j["desc"]
    for cid, descs in by_company.items():
        (data_dir / "desc" / f"{cid}.json").write_text(json.dumps(descs, ensure_ascii=False, separators=(",", ":")))

    light = [{k: v for k, v in j.items() if k not in ("desc", "src_key", "from_list")} | {"has_desc": bool(j["desc"])}
             for j in jobs]
    payload = {**meta, "sources": status, "watching": companies, "jobs": light}
    (data_dir / "jobs.json").write_text(json.dumps(
        {**payload, "jobs": [l | {"src_key": j["src_key"]} for l, j in zip(light, jobs)]},
        ensure_ascii=False, separators=(",", ":")))

    # Spreadsheet
    with open(data_dir / "internships.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Company", "Sector", "Title", "Field", "Term", "Locations", "Near you", "Pay", "Clearance",
                    "US citizenship / export control", "Grad students only", "Match score", "Why it matches",
                    "Posted", "Apply by", "First seen", "Apply link", "Other links", "Source", "Description"])
        for j in jobs:
            urls = [p["url"] for p in j["postings"]]
            w.writerow([j["company"], j["sector"], j["title"], j["field"], ", ".join(j["terms"]),
                        "; ".join(j["locations"]), j["near"], j["pay"], j["clearance"],
                        "Yes" if j["citizen"] else "", "Yes" if j["grad"] else "", j["fit"], ", ".join(j["why"]),
                        j["posted"], j["deadline"], j["first_seen"], urls[0] if urls else "", " ".join(urls[1:]),
                        j["src"], j["desc"][:32000]])

    # Page
    template = (SITE / "index.html").read_text()
    page_payload = payload
    if embed_desc:
        page_payload = {**payload, "descriptions": {j["id"]: j["desc"] for j in jobs}}
    blob = json.dumps(page_payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = template.replace("/*__DATA__*/null", blob)
    (DOCS / "index.html").write_text(html)
    for extra in ("favicon.svg",):
        if (SITE / extra).exists():
            shutil.copy(SITE / extra, DOCS / extra)
    (DOCS / ".nojekyll").write_text("")


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="only read companies whose name contains this text (testing)")
    ap.add_argument("--skip-lists", action="store_true", help="don't read the community lists (testing)")
    ap.add_argument("--previous-url", help="URL of last published data/jobs.json, used if a site fails")
    ap.add_argument("--embed-descriptions", action="store_true", help="put all descriptions inside index.html")
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD), for testing")
    ap.add_argument("--save-raw", help="save everything fetched to this JSON file (for testing)")
    ap.add_argument("--from-raw", help="skip fetching and reuse a file made with --save-raw (for testing)")
    args = ap.parse_args(argv)

    today = dt.date.fromisoformat(args.today) if args.today else dt.datetime.now(dt.timezone.utc).date()
    started = time.time()
    companies = tomllib.loads((ROOT / "companies.toml").read_text())["company"]
    if args.only:
        companies = [c for c in companies if args.only.lower() in c["name"].lower()]

    if args.from_raw:
        saved = json.loads(Path(args.from_raw).read_text())
        raw, list_raw, status = saved["raw"], saved["list_raw"], saved["status"]
        log(f"Loaded {len(raw) + len(list_raw)} saved postings from {args.from_raw}")
    else:
        log(f"Reading {len(companies)} company career sites...")
        raw, status = read_direct(companies)
        ok_keys = {s["key"] for s in status if s["ok"]}
        ok_names = {norm_company(c["name"]) for c in companies if source_key(c) in ok_keys}

        list_raw: list[dict] = []
        if not args.skip_lists:
            log("Reading community internship lists...")
            list_raw, list_status = read_lists(ok_keys, ok_names)
            status += list_status

    log("Classifying...")
    kept = [x for x in (classify(p, today) for p in raw + list_raw) if x]
    log(f"  kept {len(kept)} US internship postings out of {len(raw) + len(list_raw)} candidates")

    if not args.from_raw:
        log("Fetching descriptions for community-list postings...")
        n = enrich(kept)
        log(f"  added {n} descriptions")
        # keep fetched descriptions with the raw list postings, so a saved run is complete
        by_url = {p["url"]: p for p in kept if p["from_list"] and p.get("desc")}
        for p in list_raw:
            if p["url"] in by_url:
                for k in ("desc", "pay", "deadline", "posted"):
                    p[k] = p.get(k) or by_url[p["url"]].get(k, "")
        if args.save_raw:
            Path(args.save_raw).write_text(json.dumps({"raw": raw, "list_raw": list_raw, "status": status}))
            log(f"  saved raw postings to {args.save_raw}")
    # re-score list postings now that we have their descriptions
    rescored = []
    for p in kept:
        if p["from_list"] and p.get("desc"):
            p2 = classify({k: v for k, v in p.items()}, today)
            if p2:
                rescored.append(p2)
        else:
            rescored.append(p)
    jobs = merge(rescored)

    # Carry over postings from sites that failed today
    prev_jobs, prev_base = load_previous(args.previous_url)
    failed = {s["key"] for s in status if not s["ok"]}
    carried = 0
    if failed and prev_jobs:
        have = {j["id"] for j in jobs}
        cache: dict = {}
        for pj in prev_jobs:
            if pj.get("src_key") in failed and pj["id"] not in have:
                pj = dict(pj)
                pj["desc"] = previous_desc(prev_base, pj.get("company_id", ""), cache).get(pj["id"], "")
                pj["stale"] = True
                pj.setdefault("from_list", pj.get("src_key", "").startswith("list:"))
                jobs.append(pj)
                carried += 1
        for s in status:
            if s["key"] in failed:
                s["carried_over"] = sum(1 for j in jobs if j.get("src_key") == s["key"] and j.get("stale"))
    if carried:
        log(f"  carried over {carried} postings from last run for sites that failed")

    # First-seen tracking
    DATA.mkdir(exist_ok=True)
    seen_path = DATA / "seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    first_run = not seen
    tstr = today.isoformat()
    for j in jobs:
        rec = seen.get(j["id"])
        if rec is None:
            rec = seen[j["id"]] = [tstr, tstr]
        rec[1] = tstr
        j["first_seen"] = rec[0]
    cutoff = (today - dt.timedelta(days=180)).isoformat()
    seen = {k: v for k, v in seen.items() if v[1] >= cutoff}
    seen_path.write_text(json.dumps(dict(sorted(seen.items())), indent=0))

    started_on = tstr
    last_run_path = DATA / "last_run.json"
    if last_run_path.exists():
        try:
            started_on = json.loads(last_run_path.read_text()).get("tracker_started", tstr)
        except Exception:
            pass
    if first_run:
        started_on = tstr

    jobs.sort(key=lambda j: (-j["fit"], j["company"].lower(), j["title"].lower()))

    watching = [{"name": c["name"], "sector": c["sector"], "careers": careers_url(c),
                 "count": sum(1 for j in jobs if j["company"] == c["name"])} for c in companies]
    meta = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "today": tstr,
        "tracker_started": started_on,
        "counts": {
            "jobs": len(jobs),
            "companies": len({j["company"] for j in jobs}),
            "new": sum(1 for j in jobs if j["first_seen"] == tstr and tstr != started_on),
        },
    }
    write_outputs(jobs, status, watching, meta, args.embed_descriptions)
    last_run_path.write_text(json.dumps({
        **meta, "seconds": round(time.time() - started),
        "sources": [{k: s[k] for k in ("company", "kind", "ok", "found", "error")} for s in status],
    }, indent=1))

    bad = [s for s in status if not s["ok"]]
    log(f"Done in {time.time() - started:.0f}s: {len(jobs)} internships at {meta['counts']['companies']} companies.")
    if bad:
        log(f"{len(bad)} source(s) failed: " + ", ".join(s["company"] for s in bad))
    # Fail the run only if almost everything failed (so a broken week is noticed)
    return 1 if len(bad) > 0.6 * len(status) else 0


if __name__ == "__main__":
    sys.exit(main())
