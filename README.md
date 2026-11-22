# Internship Tracker

A web page listing U.S. aerospace, space, defense, national-lab and engineering
internships, grouped by company, with job descriptions. It updates itself every
Monday.

**Live page:** https://glucosedanny.github.io/Internship-tracker/

## What it checks

1. **Company career sites, read directly** (full descriptions, deadlines when
   the company lists them). About 80 employers are listed in
   [`companies.toml`](companies.toml): SpaceX, Blue Origin, ULA, Rocket Lab,
   Firefly, JPL, Boeing, Northrop Grumman, RTX, L3Harris/Aerojet Rocketdyne,
   GE Aerospace, Honeywell Aerospace, Leidos/Dynetics, ORNL, Johns Hopkins APL,
   and more.
2. **Community internship lists** ([SimplifyJobs](https://github.com/SimplifyJobs/Summer2027-Internships)
   and [vanshb03](https://github.com/vanshb03/Summer2027-Internships)) for every
   other company. From these it keeps every role at aerospace, defense, space
   and lab employers, plus engineering roles anywhere. Where the posting's site
   allows it, the description is fetched too.

Not covered automatically (the page links to them): Lockheed Martin's own site,
NASA OSTEM internships, USAJOBS Pathways, ORISE/Zintellect, DoD SMART.

## How each posting is tagged

- **Match score**: keyword matching against my profile (propulsion, CFD,
  thermal/fluids, test, mechanical, manufacturing, materials, CAD), with a boost
  for Tennessee and Huntsville locations and for postings that mention a
  security clearance. Grad-only and SkillBridge roles score low. Edit
  `FIT_RULES` in [`tracker/classify.py`](tracker/classify.py) to change it.
- **Clearance / U.S. citizens**: the posting mentions a clearance, U.S.
  citizenship, ITAR or export control. These are labeled, never hidden.
- **New this week**: first seen in the latest weekly run.
- **Closes soon**: the company lists an application deadline within 14 days.

## How the weekly update works

[`.github/workflows/update.yml`](.github/workflows/update.yml) runs every Monday
(and whenever you click **Actions → Weekly internship update → Run workflow**):

1. `python -m tracker.build` reads every source, filters to U.S. internships for
   current and upcoming terms, tags and scores them, and builds `docs/`.
2. `data/seen.json` (the first-seen date for each posting) is committed back so
   "New this week" works.
3. `docs/` is published to GitHub Pages. The link never changes.

If a company's site is down that week, its postings from the previous week are
kept and labeled.

## Adding a company

Add an entry to [`companies.toml`](companies.toml). The comments at the top of
that file explain the fields for each job system (Greenhouse, Lever, Ashby,
Workday, ...). The quickest way to find the right values is to open one of the
company's job postings and look at its web address.

## Running it yourself

Python 3.11 or newer, with no extra packages:

```bash
python -m tracker.build                  # full run (about 10 to 15 minutes)
python -m tracker.build --only SpaceX    # quick test on one company
```

Then open `docs/index.html` in a browser. Add `--embed-descriptions` to make
that file work offline with all descriptions.

## One-time GitHub settings

- **Settings → Pages → Build and deployment → Source: GitHub Actions**
- **Settings → Actions → General → Workflow permissions**: the workflow asks for
  the permissions it needs, so the default setting is fine.
