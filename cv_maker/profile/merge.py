import re
from copy import deepcopy

from cv_maker import tenure
from cv_maker.models import Education, Experience, Profile, Skill

# Words that don't tell employers apart ("Northwind" and "Northwind Software Private Ltd." are one company).
_COMPANY_FILLER = {"software", "private", "pvt", "ltd", "limited", "inc", "llc", "llp", "plc", "gmbh", "co", "corp",
                   "corporation", "company", "technologies", "technology", "solutions", "services", "group", "the",
                   "and", "of", "for", "india", "global", "international"}
_TITLE_FILLER = {"the", "and", "of", "for", "in", "at", "a", "an", "-", "i", "ii", "iii"}


def _words(text: str, filler: set) -> set:
    return {w for w in re.findall(r"[a-z0-9+#]+", (text or "").casefold()) if w not in filler}


def _same_company(a: str, b: str) -> bool:
    x, y = _words(a, _COMPANY_FILLER), _words(b, _COMPANY_FILLER)
    if not x or not y:
        return (a or "").casefold().strip() == (b or "").casefold().strip()
    small, big = (x, y) if len(x) <= len(y) else (y, x)
    return len(small & big) >= max(1, (len(small) + 1) // 2)


def _same_title(a: str, b: str) -> bool:
    x, y = _words(a, _TITLE_FILLER), _words(b, _TITLE_FILLER)
    if not x or not y:
        return False
    small, big = (x, y) if len(x) <= len(y) else (y, x)
    return small <= big or len(x & y) / len(x | y) >= 0.6


def same_role(a: Experience, b: Experience) -> bool:
    """The same job written two ways (two CVs, or a CV and a LinkedIn export): it started the same month, at the
    same employer (also under a shorter name), with the same title (also with more words)."""
    start_a, start_b = tenure.span(a), tenure.span(b)
    if start_a is None or start_b is None:
        same_start = (a.start or "").casefold().strip() == (b.start or "").casefold().strip()
    else:
        same_start = start_a[0] == start_b[0]
    return same_start and _same_company(a.company, b.company) and _same_title(a.title, b.title)


def _union_bullets(first: list[str], second: list[str]) -> list[str]:
    """First's bullets, then second's that say something new (no near-repeats)."""
    out = list(first)
    for bullet in second:
        words = _words(bullet, set())
        if not any(len(words & _words(o, set())) / max(1, len(words | _words(o, set()))) >= 0.6 for o in out):
            out.append(bullet)
    return out


def duplicate_groups(profile: Profile) -> list[list[int]]:
    """Indexes of roles that are the same job, in profile order: [[0, 6], [4, 5], ...] (groups of two or more)."""
    groups: list[list[int]] = []
    for i, exp in enumerate(profile.experiences):
        home = next((g for g in groups if same_role(profile.experiences[g[0]], exp)), None)
        if home is None:
            groups.append([i])
        else:
            home.append(i)
    return [g for g in groups if len(g) > 1]


def merge_duplicates(profile: Profile) -> tuple[Profile, int]:
    """One entry per job: the first entry's title and employer, every bullet that isn't a repeat, the later end
    (or still current). Returns (profile, roles removed)."""
    merged = deepcopy(profile)
    drop: set[int] = set()
    for group in duplicate_groups(merged):
        keep = merged.experiences[group[0]]
        for i in group[1:]:
            other = merged.experiences[i]
            keep.bullets = _union_bullets(keep.bullets, other.bullets)
            keep.location = keep.location or other.location
            if other.current:
                keep.current, keep.end = True, None
            elif not keep.current:
                ends = [(tenure.span(e) or ((0, 0), (0, 0)))[1] for e in (keep, other)]
                if ends[1] > ends[0]:
                    keep.end = other.end
            drop.add(i)
    merged.experiences = [e for i, e in enumerate(merged.experiences) if i not in drop]
    return merged, len(drop)


def merge_profile(existing: Profile | None, incoming: Profile) -> Profile:
    if existing is None:
        incoming.experiences = [
            Experience(
                company=e.company,
                title=e.title,
                location=e.location,
                start=e.start,
                end=e.end,
                current=e.current,
                bullets=list(e.bullets),
                confirmed=True,
            )
            for e in incoming.experiences
        ]
        return deepcopy(incoming)

    merged = deepcopy(existing)

    if not merged.name:
        merged.name = incoming.name
    if not merged.email:
        merged.email = incoming.email
    if not merged.phone:
        merged.phone = incoming.phone
    if not merged.location:
        merged.location = incoming.location
    merged.links = {**merged.links, **incoming.links}
    if incoming.target_role:
        merged.target_role = incoming.target_role

    merged_experiences = []
    matched: set[int] = set()  # incoming roles that are the same job as one already in the profile

    for existing_exp in merged.experiences:
        index = next((i for i, e in enumerate(incoming.experiences) if i not in matched and same_role(existing_exp, e)), None)
        if index is not None:
            matched.add(index)
            inc = incoming.experiences[index]
            merged_exp = Experience(
                company=existing_exp.company,
                title=existing_exp.title,
                location=inc.location or existing_exp.location,
                start=existing_exp.start,
                end=inc.end if inc.end else existing_exp.end,
                current=inc.current if not inc.end else False,
                bullets=list(existing_exp.bullets) if existing_exp.confirmed else list(inc.bullets),
                confirmed=existing_exp.confirmed,
            )
            merged_experiences.append(merged_exp)
        else:
            merged_experiences.append(deepcopy(existing_exp))

    for i, inc in enumerate(incoming.experiences):
        if i not in matched and not any(same_role(inc, e) for e in merged_experiences):
            merged_experiences.append(
                Experience(
                    company=inc.company,
                    title=inc.title,
                    location=inc.location,
                    start=inc.start,
                    end=inc.end,
                    current=inc.current,
                    bullets=list(inc.bullets),
                    confirmed=True,
                )
            )

    merged.experiences = merged_experiences

    existing_skills = {s.name.lower(): s for s in merged.skills}
    for s in incoming.skills:
        if s.name.lower() not in existing_skills:
            merged.skills.append(deepcopy(s))

    existing_education = {(e.school.lower(), e.credential.lower()): e for e in merged.education}
    for e in incoming.education:
        key = (e.school.lower(), e.credential.lower())
        if key not in existing_education:
            merged.education.append(deepcopy(e))

    merged.extras = {**merged.extras, **incoming.extras}

    return merged
