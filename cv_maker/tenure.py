"""How long each role lasted, from its dates, and what a job's "N years" requirement is compared with.

The writer is never allowed to state a number of years that your dates don't show. Durations are counted
conservatively: a month that's missing counts from the end of a start year and the start of an end year, so a
role is never longer here than on your CV. Overlapping roles count once in a total.
"""
import re
from datetime import date

_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_PRESENT = re.compile(r"present|current|now|today|ongoing|till date|to date", re.I)
_YEAR = re.compile(r"(19|20)\d{2}")
# "3+ years", "3-5 yrs", "at least 2 years", "two years" is rare in postings and left alone.
YEARS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d+\s*)?(?:years?|yrs?)\b", re.I)
_YEARS_OF = re.compile(YEARS.pattern + r"\.?\s*(?:of\s+)?(?:(?:professional|relevant|hands-on|industry|commercial|work)\s+)*"
                                       r"(?:experience\s*)?(?:(?:with|in|of|using|as|building|on)\s+)?(?P<of>[^.;,()]{0,60})", re.I)
_GENERIC = re.compile(r"^(?:experience|professional experience|relevant experience|work experience|industry experience|"
                      r"related experience|of experience|in the field|or more|minimum|at least)?$", re.I)


def _month_year(text: str, fallback_year: int | None = None, start: bool = True) -> tuple[int, int] | None:
    """'NOV 2025' -> (2025, 11); '04/2023' -> (2023, 4); '2021' -> (2021, 12 as a start, 1 as an end)."""
    text = (text or "").strip()
    if not text:
        return None
    if _PRESENT.search(text):
        today = date.today()
        return today.year, today.month
    year_match = _YEAR.search(text)
    year = int(year_match.group(0)) if year_match else fallback_year
    if year is None:
        return None
    month = None
    words = re.findall(r"[A-Za-z]+", text)
    for word in words:
        if word[:3].casefold() in _MONTHS:
            month = _MONTHS[word[:3].casefold()]
            break
    if month is None:
        numeric = re.search(r"\b(\d{1,2})\s*[/.-]\s*(?:19|20)\d{2}|(?:19|20)\d{2}\s*[/.-]\s*(\d{1,2})\b", text)
        if numeric:
            month = int(numeric.group(1) or numeric.group(2))
    if month is None or not 1 <= month <= 12:
        month = 12 if start else 1  # unknown month: never longer than your CV says
    return year, month


def span(exp, today: date | None = None) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """((start year, month), (end year, month)) for a role, or None when its dates can't be read."""
    today = today or date.today()
    end = (today.year, today.month) if getattr(exp, "current", False) else _month_year(getattr(exp, "end", "") or "", start=False)
    end_year = end[0] if end else None
    start = _month_year(getattr(exp, "start", "") or "", fallback_year=end_year, start=True)
    if start is None:
        return None
    if end is None:
        end = start if not getattr(exp, "end", None) and not getattr(exp, "current", False) else None
        if end is None:
            return None
    if end < start:
        return None
    return start, end


def months(exp, today: date | None = None) -> int | None:
    found = span(exp, today)
    if found is None:
        return None
    (y1, m1), (y2, m2) = found
    return (y2 - y1) * 12 + (m2 - m1) + 1


def describe(total: int | None) -> str:
    if total is None:
        return "dates unclear"
    years, rest = divmod(max(total, 0), 12)
    parts = ([f"{years} yr{'s' if years != 1 else ''}"] if years else []) + ([f"{rest} mo{'s' if rest != 1 else ''}"] if rest else [])
    return " ".join(parts) or "under a month"


def total_months(experiences, today: date | None = None) -> int:
    """All roles together, overlaps counted once."""
    covered = set()
    for exp in experiences:
        found = span(exp, today)
        if found is None:
            continue
        (y, m), end = found
        while (y, m) <= end:
            covered.add((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return len(covered)


def role_lines(profile, today: date | None = None) -> list[dict]:
    """[{"title", "company", "months", "text"}] for every role, most recent first (as in the profile)."""
    out = []
    for exp in getattr(profile, "experiences", []) or []:
        n = months(exp, today)
        out.append({"title": exp.title, "company": exp.company, "months": n,
                    "text": f"{exp.title} at {exp.company}: {describe(n)}"})
    return out


def requirements_in(texts) -> list[dict]:
    """The job's years requirements: [{"years": 3.0, "of": "software engineering", "text": "3+ years of ..."}]."""
    out, seen = [], set()
    for text in texts:
        for m in _YEARS_OF.finditer(text or ""):
            years = float(m.group(1))
            if not 0 < years <= 30:
                continue
            of = " ".join((m.group("of") or "").split()).strip(" -:")
            of = "" if _GENERIC.match(of) else of
            key = (years, of.casefold())
            if key not in seen:
                seen.add(key)
                out.append({"years": years, "of": of, "text": " ".join(m.group(0).split())})
    # "3+ years experience" next to "3+ years of experience in software engineering": the one that says in what.
    return [r for r in out if r["of"] or not any(o["years"] == r["years"] and o["of"] for o in out)]


def compare(requirement: dict, profile, today: date | None = None) -> dict:
    """A job's years requirement next to what your roles show, honestly: per role, and all roles together."""
    roles = role_lines(profile, today)
    total = total_months(getattr(profile, "experiences", []) or [], today)
    need = int(requirement["years"] * 12)
    return {**requirement, "total_months": total, "total": describe(total), "roles": roles,
            "meets_total": total >= need}


def years_claims(text: str) -> list[float]:
    return [float(m.group(1)) for m in YEARS.finditer(text or "")]
