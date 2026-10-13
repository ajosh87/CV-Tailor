"""Deterministic fact gate applied to everything the LLM writes.

The LLM may reword and reorder, but the output must not claim skills the profile lacks or invent
numbers. Rather than deleting individual words (which mangles prose), offending sentences and
bullets are dropped, and identity, employers, titles, dates and education always come from the profile.

Numbers come from your profile only, never from the job posting: a posting's "3+ years" is a requirement, not a
fact about you. A number of years must name a role whose dates show that long ("2 years as Research Analyst"),
so a CV never claims a total your dates don't support. Each role appears once, a bullet that repeats another is
dropped, and a rewrite that stuffs in several new keywords keeps your wording instead.
"""
import json
import re
from dataclasses import asdict

from cv_maker import skills as sk
from cv_maker import tenure
from cv_maker.jobs.match import mentions, profile_corpus
from cv_maker.models import Education, Experience, Profile


class CvDocument:
    def __init__(
        self,
        name: str,
        summary: str,
        experiences: list[Experience],
        education: list,
        skills: list[str],
        contact: list[str] | None = None,
        headline: str = "",
        extras: dict | None = None,
    ) -> None:
        self.name = name
        self.summary = summary
        self.experiences = experiences
        self.education = education
        self.skills = skills
        self.contact = contact or []
        self.headline = headline
        self.extras = extras or {}


SKILLS_SHOWN = 22  # on a CV planned for one job: enough for every relevant skill, few enough to stay focused
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


class FactCheck:
    """Record of what the filter changed and why, shown to the user next to each version."""

    def __init__(self) -> None:
        self.items: list[dict] = []  # {"where", "action", "text", "reason"}
        self.rewrites: list[dict] = []  # {"role", "original", "final"} for every bullet of every role

    def add(self, where: str, action: str, text: str, reason: str) -> None:
        self.items.append({"where": where, "action": action, "text": text, "reason": reason})

    def to_dict(self) -> dict:
        return {"items": self.items, "rewrites": self.rewrites}


def _offending(text: str, banned: list[str]) -> list[str]:
    return [b for b in banned if mentions(b, text)]


_WORD = re.compile(r"[A-Za-z][\w&+#.'-]*")
# Capitalized words that are normal in prose and never a claim about the candidate.
_COMMON_CAPITALS = {
    "I", "I'm", "I've", "I'd", "I'll", "Dear", "Mr", "Ms", "Mrs", "Dr", "Hiring", "Manager", "Team", "Kind",
    "Best", "Regards", "Sincerely", "Yours", "Thank", "Thanks", "CV",
    # Salutations: "Dear Hiring Committee", "Dear Talent Acquisition Team", "To Whom It May Concern".
    "Committee", "Panel", "Recruiter", "Recruiters", "Recruiting", "Recruitment", "Talent", "Acquisition", "Sir", "Madam",
    "Whom", "It", "Concern",
    "January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
    "November", "December", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
}


def _numbers(text: str) -> set[str]:
    return set(_NUMBER.findall(text))


_DASHES = re.compile(r"[\u2010-\u2015\u2212\u00ad]")  # the hyphens and dashes models write instead of "-"
# Endings that make a compound out of a known word ("Python-based", "LLM-powered"): the claim is the known word.
_COMPOUND_ENDS = {"based", "driven", "powered", "enabled", "focused", "first", "native", "centric", "oriented", "ready",
                  "heavy", "led", "specific", "related", "like", "style", "friendly", "aware", "grade", "scale"}


def plain_dashes(text: str) -> str:
    return _DASHES.sub("-", text or "")


def _vocabulary(text: str) -> set[str]:
    words = {w.lower().strip(".'-") for w in re.findall(r"[\w&+#.'-]+", plain_dashes(text))}
    return words | {part for w in words if "-" in w for part in w.split("-") if part}


def _known(word: str, words: set[str]) -> bool:
    """A name the allowed text contains: as written, singular or plural ("SMEs"/"SME"), or a compound of known parts."""
    low = word.lower()
    if low in words or low.rstrip("s") in words or f"{low}s" in words or low.removesuffix("es") in words:
        return True
    parts = [x for x in low.split("-") if x]
    return len(parts) > 1 and all(x in words or x in _COMPOUND_ENDS or x.rstrip("s") in words for x in parts)


def _unsupported(sentence: str, words: set[str], numbers: set[str]) -> str:
    """Why a free-text sentence makes a claim the allowed facts don't contain ("" if it doesn't)."""
    sentence = plain_dashes(sentence)
    invented = sorted(_numbers(sentence) - numbers)
    if invented:
        return f"introduced numbers not in your profile ({', '.join(invented)})"
    names = []
    for token in _WORD.findall(sentence)[1:]:  # the first word is capitalized anyway
        word = re.sub(r"'s$", "", token).strip(".'-")
        if word[:1].isupper() and word not in _COMMON_CAPITALS and not _known(word, words) and word not in names:
            names.append(word)
    if names:
        return f"names {', '.join(names)}, which isn't in your profile or the job posting"
    return ""


# A CV speaks for the candidate, in an implied first person ("Data scientist who built…"). A sentence about the
# candidate ("The candidate demonstrates…") or about the job and how well it matches ("This position requires…",
# "keywords such as…") is the model's own note, not CV text: smaller models slip into it.
_NOT_CV_VOICE = re.compile(
    r"\b(the|this|our) (candidate|applicant|job ?seeker)\b|\b(candidate|applicant)['’]s\b|"
    r"\bthis (role|position|job|posting|opening|vacancy)(?![-\w])|"
    r"\bthe (job description|job posting|posting|vacancy|hiring manager)\b|"
    r"\byour (posting|job|role|position|team|company|organi[sz]ation|opening|vacancy)\b|"
    r"\b(role|position|job|posting|employer) (seeks|requires|requests|calls for|asks for|is looking for|needs|wants)\b|"
    r"\bkeywords? (such as|like|including|from)\b|\bas evidenced by\b|\baligning with\b|"
    r"\baligns? (well )?with (the|this|their)\b|\b(he|she) (has|is|demonstrates|brings|shows|possesses)\b|"
    r"\b(his|her) (experience|background|skills|expertise)\b|^(his|her|their)\b|"
    # A letter's voice: a CV's summary has no "I" or "my" ("Software Engineer I" is a title, so only "I am", "I have"...).
    r"\b(my|me)\b|\bI(['’]m|['’]ve| am| have| was| bring)\b|"
    r"\bis an? (proven|strong|ideal|excellent|great|perfect) (expert|fit|candidate|match)\b|"
    r"\b(strong|ideal|good|great|perfect) (fit|match) for\b", re.I)
# "a Master's or PhD": the posting's requirement copied, not a degree you hold.
_COPIED_REQUIREMENT = re.compile(
    r"\b(master['’]?s|bachelor['’]?s|ph\.? ?d\.?|mba)( degree)? or (an? )?(master['’]?s|bachelor['’]?s|ph\.? ?d|doctorate|"
    r"equivalent)\b", re.I)


def not_cv_voice(sentence: str) -> str:
    """Why a CV sentence isn't written as the CV itself ("" when it is)."""
    sentence = (sentence or "").strip()
    m = _COPIED_REQUIREMENT.search(sentence)
    if m:
        return f"copies the posting's requirement (“{m.group(0)}”) instead of stating your own degree"
    m = _NOT_CV_VOICE.search(sentence)
    return (f"is a note about the candidate or the job (“{m.group(0)}”), not CV text in your own voice" if m else "")


def _duration_pattern(months: int) -> str:
    """'2 yrs 5 mos', '2 years and 5 months', '2 years 5 months' for 29 months."""
    years, rest = divmod(months, 12)
    y = rf"{years}\s*(?:yrs?|years?)" if years else ""
    m = rf"{rest}\s*(?:mos?|months?)" if rest else ""
    return rf"\b{y},?\s*(?:and\s+)?{m}\b" if y and m else rf"\b{y or m}\b"


def years_unsupported(sentence: str, roles: list[dict]) -> str:
    """Why a sentence's number of years isn't backed by a role it names ("" if it is, or says no years)."""
    claims = tenure.years_claims(sentence)
    if not claims:
        return ""
    low = sentence.casefold()
    named = [r for r in roles if r["months"] and ((r["title"] and r["title"].casefold() in low)
                                                   or (r["company"] and r["company"].casefold().split(" ")[0] in low))]
    for years in claims:
        if not any(r["months"] >= years * 12 for r in named):
            if named:
                shown = "; ".join(f"{r['title']}: {tenure.describe(r['months'])}" for r in named)
                return f"claims {years:g}+ years, but the role it names shows {shown}"
            return (f"claims {years:g}+ years without naming a role that lasted that long (a total of years is never "
                    "stated: your roles show their own durations)")
    return ""


def filter_text(text: str, banned: list[str], check: FactCheck | None = None, where: str = "summary",
                allowed_text: str | None = None, numbers_text: str | None = None, roles: list[dict] | None = None,
                cv_voice: bool = False) -> str:
    """Drop every sentence that mentions a banned term or, given `allowed_text`, any name not in it, or a number
    not in `numbers_text` (default: `allowed_text`). Given `roles` (tenure.role_lines), a number of years must
    name a role whose dates show that long. With `cv_voice` (a CV's summary), every sentence must speak as the CV:
    never about "the candidate", the job or its keywords."""
    words = _vocabulary(allowed_text) if allowed_text is not None else None
    numbers = _numbers(numbers_text if numbers_text is not None else allowed_text) if allowed_text is not None else None
    kept = []
    for sentence in _SENTENCE.split(plain_dashes(text).strip()):
        if not sentence:
            continue
        reason = not_cv_voice(sentence) if cv_voice else ""
        hits = _offending(sentence, banned) if not reason else []
        if hits:
            reason = f"mentions {', '.join(hits)}, which your profile does not confirm"
        if not reason and roles is not None:
            reason = years_unsupported(sentence, roles)
        if not reason and words is not None:
            # A role's duration as listed ("2 yrs 5 mos") is a fact from your dates: its numbers aren't invented.
            said = sentence
            for r in roles or []:
                if r["months"]:
                    said = re.sub(_duration_pattern(r["months"]), " ", said, flags=re.I)
            reason = _unsupported(said, words, numbers)
        if reason:
            if check is not None:
                check.add(where, "removed", sentence, reason)
        else:
            kept.append(sentence)
    return " ".join(kept).strip()


def allowed_facts_text(profile: Profile, context_text: str = "") -> str:
    """Everything a free-text sentence may draw names from: the profile plus job context."""
    return json.dumps(asdict(profile), ensure_ascii=False) + "\n" + context_text


def allowed_numbers_text(profile: Profile) -> str:
    """Everything a free-text sentence may draw numbers from: the profile and how long each role lasted (in whole
    years and months, rounded down). Never the posting: its numbers are requirements, not facts about you."""
    durations = []
    for r in tenure.role_lines(profile):
        if r["months"]:
            durations += [str(r["months"] // 12), str(r["months"])]
    return json.dumps(asdict(profile), ensure_ascii=False) + "\n" + " ".join(durations)


_TOKENS = re.compile(r"[a-z0-9+#]+")


def _similar(a: str, b: str) -> float:
    x, y = set(_TOKENS.findall(a.casefold())), set(_TOKENS.findall(b.casefold()))
    return len(x & y) / len(x | y) if x and y else 0.0


def _added_keywords(new: str, source: str, keywords: list[str]) -> list[str]:
    return [k for k in keywords if sk.found_in(k, new) and not sk.found_in(k, source)]


def _overclaim(text: str, levels: dict) -> str:
    """Why `text` says more about a skill than the level you gave ("" if it doesn't)."""
    for skill, level in (levels or {}).items():
        if sk.overclaims(text, skill, level):
            return f"describes {skill} beyond the level you gave ({level})"
    return ""


def _within_levels(text: str, levels: dict, check: FactCheck | None, where: str) -> str:
    kept = []
    for sentence in _SENTENCE.split(text.strip()):
        reason = _overclaim(sentence, levels) if sentence else ""
        if reason:
            if check is not None:
                check.add(where, "removed", sentence, reason)
        elif sentence:
            kept.append(sentence)
    return " ".join(kept).strip()


MAX_NEW_KEYWORDS = 1  # a rewrite may bring in one posting keyword its original doesn't name, where the fact fits


def _safe_bullets(original: list[str], rewritten: list[str], banned: list[str], check: FactCheck | None, role: str,
                  levels: dict | None = None, keywords: list[str] | None = None) -> list[str]:
    if len(rewritten) != len(original):
        # Cannot pair rewrites with source facts; keep the facts as written.
        if check is not None and rewritten != original:
            check.add("experience", "kept original", role,
                      f"the model returned {len(rewritten)} bullets for {len(original)} facts, so your original wording was kept")
        out = list(original)
    else:
        out = []
        for source, new in zip(original, rewritten):
            new = (new or "").strip()
            hits = _offending(new, banned) if new else []
            invented = sorted(_numbers(new) - _numbers(source)) if new else []
            level_reason = _overclaim(new, levels) if new and not _overclaim(source, levels) else ""
            stuffed = _added_keywords(new, source, keywords or []) if new else []
            stuffing = (f"added {len(stuffed)} keywords its original doesn't mention ({', '.join(stuffed)})"
                        if len(stuffed) > MAX_NEW_KEYWORDS else "")
            if not new:
                out.append(source)
            elif hits or invented or level_reason or stuffing:
                reason = (f"mentions {', '.join(hits)}, which your profile does not confirm" if hits
                          else f"introduced numbers not in your CV ({', '.join(invented)})" if invented
                          else level_reason or stuffing)
                if check is not None:
                    check.add("experience", "kept original", new, reason)
                out.append(source)
            else:
                out.append(new)
    if check is not None:
        check.rewrites += [{"role": role, "original": o, "final": f} for o, f in zip(original, out)]
    return out


REPEAT = 0.6  # share of words two bullets have in common before the later one counts as saying the same thing


def _drop_repeats(experiences: list[Experience], check: FactCheck | None) -> None:
    """A bullet that says what an earlier one already said (the same rewrite twice, or two near-identical facts)
    is dropped, so the CV makes each point once."""
    kept: list[str] = []
    for exp in experiences:
        out = []
        for bullet in exp.bullets:
            twin = next((k for k in kept if _similar(bullet, k) >= REPEAT), None)
            if twin is not None:
                if check is not None:
                    check.add("experience", "removed", bullet, f"repeats “{twin[:80]}”")
                continue
            kept.append(bullet)
            out.append(bullet)
        exp.bullets = out


def _evidenced_parts(skill: str, profile: Profile) -> list[str]:
    """The parts of a skill ("ETL/data pipelines") that your roles, projects or certifications describe."""
    corpus = profile_corpus(profile)
    parts = [x.strip() for x in re.split(r"\s*/\s*|\s*,\s*|\s+and\s+", skill) if x.strip()]
    return [x for x in parts if len(x) > 2 and sk.found_in(x, corpus)]


def contact_line(profile: Profile) -> list[str]:
    return [v for v in [profile.email, profile.phone, profile.location, *profile.links.values()] if v]


def filter_cv_document(doc: CvDocument, allowed: Profile, banned_terms: list[str] | None = None,
                       check: FactCheck | None = None, context_text: str = "", selection: list[list[int]] | None = None,
                       drop_skills: list[str] | None = None, keywords: list[str] | None = None) -> CvDocument:
    """`context_text` (the job title, company and description) may supply names the summary uses. `selection`
    (tailor.py): which bullets of each role this CV uses, in order; `doc.experiences` then lines up with your roles
    one for one, each with one rewrite per chosen bullet ("" keeps your wording). `drop_skills`: skills that only
    dilute this job's CV, left off unless the model ranked them."""
    allowed_skills = {_norm(s.name): s.name for s in allowed.skills}
    skills: list[str] = []
    invented: list[str] = []
    for s in doc.skills:
        name = str(s.get("name", "")) if isinstance(s, dict) else str(s)
        key = _norm(name)
        if key in allowed_skills:
            if allowed_skills[key] not in skills:
                skills.append(allowed_skills[key])
        elif key and any(sk.variants(name) & sk.variants(p.name) or sk.found_in(name, p.name) for p in allowed.skills):
            if name not in skills:
                skills.append(name)  # your skill under the posting's name ("RAG" for "multimodal RAG")
        elif key and (shown := _evidenced_parts(name, allowed)):
            # Named in your roles but not your skills list ("data pipelines" in a bullet): kept, as far as it's shown.
            kept = " / ".join(shown)
            if kept not in skills:
                skills.append(kept)
            if kept != name and check is not None:
                check.add("skills", "shortened", name, f"only “{kept}” is shown by your profile")
        elif key:
            invented.append(name)
            if check is not None:
                check.add("skills", "removed", name, "not a skill in your profile, and your roles don't describe it")
    # Skills the model left out still belong on the CV, after the ones it ranked (when the CV is planned for this
    # job: not the ones that only dilute it, and not so many that the relevant ones get lost).
    dropped = {_norm(d) for d in drop_skills or []}
    rest = [s.name for s in allowed.skills if s.name not in skills and _norm(s.name) not in dropped]
    if selection is not None and drop_skills is not None:
        rest = rest[:max(0, SKILLS_SHOWN - len(skills))]
    skills += rest

    # Never strip a term the profile itself mentions (e.g. "AWS" when the skill reads "AWS (ECS, S3)").
    corpus = profile_corpus(allowed)
    banned, seen = [], set()
    for term in [*(banned_terms or []), *invented]:
        if term and _norm(term) not in seen and not mentions(term, corpus):
            seen.add(_norm(term))
            banned.append(term)

    # Skills you said you know at a beginner or intermediate level are never described as more.
    levels = {s.name: s.level for s in allowed.skills if s.level in ("beginner", "intermediate")}
    rewritten = {(_norm(e.company), _norm(e.title)): e for e in doc.experiences}
    from cv_maker.profile.merge import same_role  # (merge imports models only; no cycle)

    experiences = []
    shown: list[Experience] = []
    for i, exp in enumerate(allowed.experiences):
        role = f"{exp.title} — {exp.company}"
        twin = next((e for e in shown if same_role(e, exp)), None)
        if twin is not None:  # in your profile twice (two CVs merged): on the CV once
            if check is not None:
                check.add("experience", "removed", role, f"the same job as “{twin.title} — {twin.company}” in your "
                          "profile; it's shown once (merge them on the Profile page)")
            continue
        shown.append(exp)
        if selection is not None:
            sources = [exp.bullets[j] for j in selection[i]] if i < len(selection) else list(exp.bullets)
            match = doc.experiences[i] if i < len(doc.experiences) else None
        else:
            sources = exp.bullets
            match = rewritten.get((_norm(exp.company), _norm(exp.title)))
        bullets = _safe_bullets(sources, match.bullets if match else sources, banned, check, role, levels, keywords)
        experiences.append(Experience(exp.company, exp.title, exp.location, exp.start, exp.end, exp.current, bullets, exp.confirmed))
    _drop_repeats(experiences, check)

    summary = filter_text(doc.summary, banned, check, "summary", allowed_facts_text(allowed, context_text),
                          allowed_numbers_text(allowed), tenure.role_lines(allowed), cv_voice=True)
    return CvDocument(
        name=allowed.name or doc.name,
        summary=_within_levels(summary, levels, check, "summary"),
        experiences=experiences,
        education=[Education(e.school, e.credential, e.field, e.dates) for e in allowed.education],
        skills=skills,
        contact=contact_line(allowed),
        headline=allowed.target_role or (allowed.experiences[0].title if allowed.experiences else ""),
        extras={k: v for k, v in allowed.extras.items() if k in ("certifications", "projects", "languages") and v},
    )
