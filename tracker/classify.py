"""Turn raw postings into tagged, scored internships.

Everything here is plain keyword matching, so it is fast and predictable.
Tune the word lists below to change what counts as a good match.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from html.parser import HTMLParser

# --------------------------------------------------------------------------
# HTML -> readable plain text
# --------------------------------------------------------------------------

_BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "table", "tr",
               "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "blockquote"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n\n")
        elif tag in ("td", "th"):
            self.parts.append("  ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS or tag == "li":
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    if "&lt;" in raw[:500]:  # Greenhouse sends HTML-escaped HTML
        raw = html.unescape(raw)
    if "<" not in raw:
        text = html.unescape(raw)
    else:
        p = _TextExtractor()
        try:
            p.feed(raw)
            p.close()
        except Exception:
            pass
        text = "".join(p.parts)
    text = text.replace("\xa0", " ").replace("​", "").replace("\r", "")
    lines = [re.sub(r"[ \t\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln in ("•", ""):
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(ln)
    text = "\n".join(out).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    # "• " bullets that ended up on their own paragraph look odd; tidy them
    text = re.sub(r"\n\n(• )", r"\n\1", text)
    return text


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return text[: cut if cut > limit * 0.8 else limit].rstrip() + " …"


# --------------------------------------------------------------------------
# Is it an internship?
# --------------------------------------------------------------------------

INTERN_RE = re.compile(
    r"\b(intern|interns|internship|internships|co-?op|co-?ops|coop|student|summer associate|summer analyst)\b",
    re.I,
)
NOT_INTERN_RE = re.compile(
    r"\b(intern(ship)?s?\s+(program|recruit\w*|coordinator|manager))\b"
    r"|\b(student\s+(loan|services|affairs|success|housing|support\s+specialist))\b"
    r"|\b(university|campus|early career|intern(ship)?)\s+recruit(er|ing)\b"
    r"|\bpostdoc|\bpost-doc|\bpostdoctoral|\bintern conversion\b",
    re.I,
)
HIGH_SCHOOL_RE = re.compile(r"\bhigh[- ]school\b", re.I)


def is_internship(title: str, employment_type: str = "") -> bool:
    if NOT_INTERN_RE.search(title) or HIGH_SCHOOL_RE.search(title):
        return False
    if INTERN_RE.search(title):
        return True
    return bool(re.search(r"\bintern", employment_type or "", re.I))


# --------------------------------------------------------------------------
# Location
# --------------------------------------------------------------------------

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia", "PR": "Puerto Rico",
}
_STATE_ABBR_RE = re.compile(r"(?:^|[,(/|]|\s-)\s*(" + "|".join(STATES) + r")\b(?![a-z])")
_STATE_NAME_RE = re.compile(r"\b(" + "|".join(sorted(STATES.values(), key=len, reverse=True)) + r")\b", re.I)
_US_RE = re.compile(r"\b(USA|U\.S\.A?\.?|United States|US)\b|United States of America")
_US_REMOTE_RE = re.compile(r"remote\s*(in|-|–|,)?\s*(the\s+)?(us|usa|united states)\b", re.I)
_CA_PROVINCE_RE = re.compile(r",\s*(ON|QC|BC|AB|MB|SK|NS|NB|NL|PE)\b")
_NON_US_RE = re.compile(
    r"\b(canada|united kingdom|england|scotland|wales|ireland|germany|deutschland|france|india|china|japan"
    r"|australia|new zealand|mexico|brazil|singapore|israel|netherlands|poland|spain|italy|korea|taiwan"
    r"|philippines|switzerland|sweden|norway|denmark|finland|belgium|austria|czech|romania|ukraine|uae"
    r"|united arab emirates|saudi arabia|qatar|kuwait|turkey|türkiye|egypt|south africa|argentina|chile"
    r"|colombia|costa rica|vietnam|thailand|malaysia|indonesia|hong kong|portugal|greece|hungary"
    r"|luxembourg|estonia|lithuania|latvia|bulgaria|serbia|croatia|slovakia|slovenia|ontario|quebec"
    r"|british columbia|alberta|bengaluru|bangalore|hyderabad)\b"
    r"|^(GBR|DEU|FRA|IND|CAN|AUS|JPN|KOR|ISR|MEX|ARE|SAU|KWT|QAT|POL|ESP|ITA|NLD|SGP|CHN|TWN|PHL|BRA)\b",
    re.I,
)


def location_is_us(loc: str) -> bool | None:
    """True = US, False = clearly outside the US, None = can't tell."""
    if not loc:
        return None
    if _US_REMOTE_RE.search(loc):
        return True
    if _US_RE.search(loc):
        return True
    if _CA_PROVINCE_RE.search(loc) or re.search(r"\b(UK|U\.K\.)\b", loc):
        return False
    if _NON_US_RE.search(loc):
        return False
    if _STATE_ABBR_RE.search(loc) or _STATE_NAME_RE.search(loc):
        return True
    return None


def any_us(locations: list[str], default: bool | None = None) -> bool | None:
    verdicts = [location_is_us(l) for l in locations if l]
    if any(v is True for v in verdicts):
        return True
    if any(v is False for v in verdicts):
        return False  # e.g. "Berlin, Germany" + "Berlin Office"
    return default


def explicitly_us(locations: list[str]) -> bool:
    """A location string that literally says USA / United States / remote in the US."""
    return any(_US_RE.search(l) or _US_REMOTE_RE.search(l) for l in locations if l)


NEAR_HOME_RE = re.compile(r"\b(knoxville|oak ridge|tullahoma|arnold afb|arnold air force|y-12|maryville|alcoa)\b", re.I)
TENNESSEE_RE = re.compile(r"(,\s*TN\b|\bTennessee\b|-TN-|\bTN\s*-)")
HUNTSVILLE_RE = re.compile(r"\b(huntsville|decatur, al|redstone)\b", re.I)


def near_home(locations: list[str]) -> str:
    text = " | ".join(locations)
    if NEAR_HOME_RE.search(text):
        return "Knoxville area"
    if TENNESSEE_RE.search(text):
        return "Tennessee"
    if HUNTSVILLE_RE.search(text):
        return "Huntsville area"
    return ""


def is_remote(locations: list[str]) -> bool:
    return any(re.search(r"\bremote\b|\bvirtual\b|\btelework", l, re.I) for l in locations)


# --------------------------------------------------------------------------
# Term (Summer 2027, co-op, ...)
# --------------------------------------------------------------------------

_SEASON_START = {"winter": 1, "spring": 1, "summer": 5, "fall": 8, "autumn": 8}
_SEASON = r"(?:spring|summer|fall|autumn|winter)"
_SEASONS = rf"{_SEASON}(?:\s*(?:/|,|&|\+|and|or|-|–)\s*{_SEASON})*"
_TERM_RE = re.compile(
    rf"\b({_SEASONS})\s*(?:semester|term|session|internship|intern|co-?op)?\s*(?:of\s*)?['’]?((?:20)?2[4-9])\b"
    rf"|\b(20(?:2[4-9]))\s*({_SEASONS})\b",
    re.I,
)
_YEAR_RE = re.compile(r"\b(20(?:2[4-9]))\b")


def find_terms(title: str, desc: str = "") -> tuple[list[str], bool]:
    """Returns (terms, explicit_in_title)."""
    def scan(text: str) -> list[tuple[str, int]]:
        out = []
        for m in _TERM_RE.finditer(text):
            if m.group(1):
                seasons, year = m.group(1), m.group(2)
            else:
                year, seasons = m.group(3), m.group(4)
            year = int(year) if len(year) == 4 else 2000 + int(year)
            for season in re.findall(_SEASON, seasons, re.I):
                season = "Fall" if season.lower() in ("fall", "autumn") else season.capitalize()
                out.append((season, year))
        return out

    found = scan(title)
    in_title = bool(found)
    if not found and desc:
        found = scan(desc[:2500])
    terms = [f"{s} {y}" for s, y in dict.fromkeys(found)]
    if not terms:
        years = sorted(set(int(y) for y in _YEAR_RE.findall(title)))
        terms = [str(y) for y in years]
    if re.search(r"\bco-?op\b", title, re.I):
        terms.append("Co-op")
    return terms, in_title


def term_is_past(term: str, today: dt.date) -> bool:
    m = re.match(r"(Winter|Spring|Summer|Fall) (\d{4})$", term)
    if m:
        start = (int(m.group(2)), _SEASON_START[m.group(1).lower()])
        return start < (today.year, today.month)
    if re.fullmatch(r"\d{4}", term):
        return int(term) < today.year
    return False


def all_terms_past(terms: list[str], today: dt.date) -> bool:
    dated = [t for t in terms if t != "Co-op"]
    return bool(dated) and all(term_is_past(t, today) for t in dated)


# --------------------------------------------------------------------------
# Field of work (from the title)
# --------------------------------------------------------------------------

_SOFTWARE_RE = re.compile(
    r"\b(software|developer|programmer|full[- ]?stack|front[- ]?end|back[- ]?end|web|devops|cloud|data (?:science|scientist|engineer|analyst|analytics)"
    r"|machine learning|\bml\b|\bai\b|artificial intelligence|cyber|information technology|\bit\b|network|database|app(lication)? developer"
    r"|site reliability|security engineer|computer science|ux|ui/ux|product design)\b",
    re.I,
)
_BUSINESS_RE = re.compile(
    r"\b(finance|financial|accounting|accountant|business|marketing|communications|public relations|supply chain|procurement|purchasing"
    r"|buyer|sourcing|human resources|\bhr\b|talent|recruit|people operations|legal|contracts?|paralegal|program management|project management"
    r"|program manager|project manager|product management|product manager|pmo|sales|strategy|economics|audit|tax|treasury|administrative|admin|office|facilities|logistics"
    r"|trade compliance|compliance|government relations|policy|communications|graphic design|events?|philanthropy|estimating)\b",
    re.I,
)
_ENGINEERING_RE = re.compile(
    r"\b(engineer|engineering|mechanical|aerospace|aeronautical|astronautical|propulsion|structures?|structural|thermal|fluids?"
    r"|manufacturing|materials?|test|integration|systems|electrical|electronics?|avionics|rf\b|hardware|design|technician|physics"
    r"|chemistry|chemical|nuclear|scientist|research|science|robotics|controls?|gnc|guidance|launch|mission|flight|quality"
    r"|reliability|industrial|metrology|machinist|welding|composites?|additive|cad|optical|optics|laser|plasma|energy|reactor)\b",
    re.I,
)


def field_of(title: str) -> str:
    """'Engineering', 'Software & data', or 'Business & other'."""
    t = title
    eng = bool(_ENGINEERING_RE.search(t))
    sw = bool(_SOFTWARE_RE.search(t))
    biz = bool(_BUSINESS_RE.search(t))
    if sw and not re.search(r"\b(mechanical|aerospace|propulsion|structures|thermal|manufacturing|electrical|hardware|test|systems)\b", t, re.I):
        return "Software & data"
    if eng and not (biz and not re.search(r"engineer", t, re.I)):
        return "Engineering"
    if biz:
        return "Business & other"
    if sw:
        return "Software & data"
    # Generic titles like "Summer 2027 Internship" - keep them visible
    return "Engineering" if not biz else "Business & other"


# --------------------------------------------------------------------------
# Flags: clearance, citizenship, grad-only, pay
# --------------------------------------------------------------------------

_CLEAR_LEVEL_RE = re.compile(
    r"(ts\s*/\s*sci|top[- ]secret|\bsecret\b)[^.\n]{0,60}clearance|clearance[^.\n]{0,60}(ts\s*/\s*sci|top[- ]secret|\bsecret\b)",
    re.I,
)
_CLEAR_ANY_RE = re.compile(r"security clearance|\bclearance\b[^.\n]{0,40}(required|eligib|obtain|maintain|active)", re.I)
_CITIZEN_RE = re.compile(
    r"u\.?\s?s\.?\s+citizen|united states citizen|citizenship is required|must be a u\.?s\.?\s+citizen|\bus person\b|u\.s\. person"
    r"|\bitar\b|export control|export-control",
    re.I,
)
_GRAD_RE = re.compile(r"\b(ph\.?\s?d\.?|doctoral|master'?s|masters|mba|graduate)\b", re.I)
_UNDERGRAD_RE = re.compile(r"\bundergrad", re.I)
_POSTGRAD_RE = re.compile(r"post[- ]?(undergraduate|baccalaureate|bacc|grad|graduate)|recent graduate|new grad", re.I)
_SKILLBRIDGE_RE = re.compile(r"skill\s?bridge", re.I)

_PAY_HOURLY_RE = re.compile(
    r"\$\s?(\d{2,3}(?:\.\d{1,2})?)\s*(?:usd)?\s*(?:/\s*(?:hr|hour)|per hour|an hour|hourly)?\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3}(?:\.\d{1,2})?)\s*(?:usd)?\s*(?:/\s*(?:hr|hour)|per hour|an hour|hourly|/h\b)",
    re.I,
)
_PAY_HOURLY_PRE_RE = re.compile(
    r"(?:hourly|per hour)[^$\n]{0,60}\$\s?(\d{2,3}(?:\.\d{1,2})?)\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3}(?:\.\d{1,2})?)", re.I
)
_PAY_SINGLE_HOURLY_RE = re.compile(r"\$\s?(\d{2,3}(?:\.\d{1,2})?)\s*(?:/\s*(?:hr|hour)|per hour|an hour)", re.I)
_PAY_ANNUAL_RE = re.compile(r"\$\s?(\d{2,3}),(\d{3})(?:\.\d{2})?\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3}),(\d{3})", re.I)


def _fmt_money(x: str) -> str:
    v = float(x)
    return f"${v:.0f}" if v == int(v) else f"${v:.2f}"


def find_pay(text: str) -> str:
    if not text:
        return ""
    m = _PAY_HOURLY_RE.search(text) or _PAY_HOURLY_PRE_RE.search(text)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if 10 <= lo <= hi <= 150:
            return f"{_fmt_money(m.group(1))}–{_fmt_money(m.group(2))}/hr"
    m = _PAY_SINGLE_HOURLY_RE.search(text)
    if m and 10 <= float(m.group(1)) <= 150:
        return f"{_fmt_money(m.group(1))}/hr"
    m = _PAY_ANNUAL_RE.search(text)
    if m:
        lo = int(m.group(1) + m.group(2))
        hi = int(m.group(3) + m.group(4))
        if 25000 <= lo <= hi <= 400000:
            return f"${lo:,}–${hi:,}/yr"
    return ""


MY_GPA = 3.77  # postings asking for more than this get flagged

_GPA_RES = [
    re.compile(r"\b(?:gpa|grade point average)\b[^.\n$]{0,60}?\b([2-4]\.\d{1,2})\b", re.I),
    re.compile(r"\b([2-4]\.\d{1,2})\s*(?:/\s*4(?:\.0+)?)?\s*(?:or (?:higher|above|better|greater)|\+)?\s*(?:cumulative\s+|overall\s+|minimum\s+)?(?:gpa|grade point average)\b", re.I),
]


def find_min_gpa(text: str) -> float | None:
    """Minimum GPA a posting asks for (highest number mentioned next to 'GPA')."""
    vals = []
    for rx in _GPA_RES:
        for m in rx.finditer(text or ""):
            v = float(m.group(1))
            if 2.0 <= v <= 4.0 and v not in (4.0,):
                vals.append(v)
    return max(vals) if vals else None


def clearance_of(text: str, extra: str = "") -> str:
    blob = f"{extra}\n{text}"
    m = _CLEAR_LEVEL_RE.search(blob)
    if m:
        lvl = (m.group(1) or m.group(2) or "").lower()
        if "sci" in lvl:
            return "TS/SCI"
        if "top" in lvl:
            return "Top Secret"
        return "Secret"
    if re.search(r"\b(TS/SCI|Top Secret|Secret)\b", extra or ""):
        return re.search(r"\b(TS/SCI|Top Secret|Secret)\b", extra).group(1)
    if _CLEAR_ANY_RE.search(blob):
        return "Clearance"
    return ""


# --------------------------------------------------------------------------
# Match score for this student's profile
# --------------------------------------------------------------------------
# (pattern, points if in the title, points if only in the description, label)
FIT_RULES = [
    (r"propulsion|rocket engine|liquid rocket|thrust chamber|combustion|injector|turbopump|regenerative(?:ly)?[- ]cool|regen cool|hot[- ]?fire|\bthruster",
     30, 12, "propulsion"),
    (r"\bcfd\b|computational fluid|converge cfd|\bconverge\b|star-?ccm|ansys fluent|\bfluent\b", 22, 12, "CFD"),
    (r"fluids?\b|thermal|heat transfer|thermofluid|cryogenic|turbomachinery", 12, 5, "thermal/fluids"),
    (r"aerospace|astronaut|aeronaut|spacecraft|space vehicle|launch vehicle|satellite|rocket|starship|falcon|new glenn|vulcan|orbital|lunar|space systems", 14, 5, "space/aero"),
    (r"\btest\b|testing|test engineer|test stand|test operations|integration and test|i&t", 10, 3, "test"),
    (r"mechanical|structures|structural|mechanisms|mechatronics", 12, 3, "mechanical"),
    (r"manufactur|machin|fabricat|welding|additive|\bcnc\b|mastercam|production engineer", 8, 3, "manufacturing"),
    (r"materials|coatings?|ceramic|thermal barrier|metallurg|x-ray diffraction|\bxrd\b|characterization", 8, 5, "materials"),
    (r"solidworks|\bcad\b|fusion 360|siemens nx|\bcreo\b|catia", 4, 3, "CAD"),
]
_FIT_COMPILED = [(re.compile(p, re.I), wt, wd, lab) for p, wt, wd, lab in FIT_RULES]


_CHIP_RE = re.compile(r"\b(asic|rtl|fpga|silicon|soc|vlsi|analog design|physical design|digital design|verification engineer|firmware|semiconductor)\b", re.I)
SECTOR_BONUS = {"space": 15, "defense": 10, "aviation": 10, "lab": 8}


def score(title: str, desc: str, *, field: str, near: str, clearance: str, grad: bool,
          skillbridge: bool, terms: list[str], sector: str = "") -> tuple[int, list[str]]:
    s = SECTOR_BONUS.get(sector, 0)
    why: list[str] = []
    d = desc[:12000]
    for rx, wt, wd, label in _FIT_COMPILED:
        if rx.search(title):
            s += wt
            why.append(label)
        elif d and rx.search(d):
            s += wd
            why.append(label)
    if _CHIP_RE.search(title):
        s -= 10  # chip / firmware work is far from mechanical-aerospace
    if field == "Engineering":
        s += 12
    elif field == "Software & data":
        s -= 8
    else:
        s -= 25
    if near:
        s += {"Knoxville area": 15, "Huntsville area": 7}.get(near, 6)
        why.append(near)
    if clearance:
        s += 6
        why.append("clearance helps")
    if any(t.startswith("Summer 2027") for t in terms):
        s += 3
    if grad:
        s -= 35
    if skillbridge:
        s -= 45
    return max(0, min(100, s)), why


# --------------------------------------------------------------------------
# Sector guess for companies that only come from the community lists
# --------------------------------------------------------------------------

def guess_sector(company: str) -> str:
    c = company.lower()
    if re.search(r"space|rocket|launch|orbit|satellite|lunar|astro|propulsion|nasa|jet propulsion", c):
        return "space"
    if re.search(r"national lab|laboratory|\blab\b|institute|nasa|university", c):
        return "lab"
    if re.search(r"defen[cs]e|missile|lockheed|northrop|raytheon|rtx|l3harris|general dynamics|bae|leidos|anduril|saic|caci|booz|mitre|textron|huntington|kratos|mantech|parsons", c):
        return "defense"
    if re.search(r"aero|aviation|aircraft|airline|air\b|avionics|boeing|airbus|gulfstream|cessna|bell|sikorsky|embraer|helicopter|jet\b|flight", c):
        return "aviation"
    return "other"
