"""Interview prep notes for one job, written only when you ask: what to brush up on (down to the subtopics), a
"tell me about yourself" pitch, likely questions with what to actually say (first person, from your own roles),
the facts to get across, what to avoid and the likely follow-up, questions to ask them, and things to watch out for.
Every answer is fact-checked against your profile like the CV is.

One LLM request per job, built from what the app already has (the requirements it read, how your profile
matches them, the posting's notes, what the application form asked, and the company research), so nothing is
read or paid for twice. Company facts come only from the research agent (research.py), with its sources.
"""
from datetime import datetime, timezone

from cv_maker import tenure
from cv_maker.honesty import FactCheck, allowed_facts_text, allowed_numbers_text, filter_text
from cv_maker.llm.chat import complete_json


def company_section(found: dict | None) -> str:
    """What the research agent found, with sources, for the prompt."""
    if not found:
        return ""
    parts = []
    if found.get("basics"):
        parts.append(f"(Wikipedia) {found['basics']['summary'][:800]}")
    if found.get("facts"):
        parts.append("(Wikidata) " + "; ".join(f"{k}: {v}" for k, v in found["facts"].items()))
    for point in ((found.get("summary") or {}).get("points") or [])[:6]:
        parts.append(f"({point['source']}) {point['point']}")
    return "\n".join(parts)


_PROMPT = """You are helping a candidate prepare for interviews for one job: an interview coach who writes what the
candidate should actually say, not advice to "talk about your experience". Be specific to this job and this candidate.
Use only the candidate profile for anything about the candidate: never invent experience, tools, numbers or results.
State company facts only from the "Company" section; if it's empty, give no company facts.

Work in this order before writing the JSON:
1. What will the interviewers test? The job's top needs, its must-haves, and the candidate's gaps against them.
2. For each likely question, pick the candidate's strongest true example: the role and the bullet it comes from.
3. Draft the answer in the candidate's own voice (first person), as they would say it aloud, 80-150 words.
   Behavioural: situation, task, what they did, result. Technical: the problem, the approach they used and why,
   the trade-offs, the result. Use the profile's numbers and tool names exactly; name the role ("At Northwind, ...").
4. Check each draft: every tool, number, role and duration must be in the profile; take out anything that isn't.
   For a gap, answer honestly: what they haven't done yet, the closest thing they have done, and the concrete steps
   they'd take to get up to speed. Never pretend.

Job: {title} at {company}
Must-haves and how the candidate matches them: {gaps}
Nice to have: {nice}
Years the job asks for, and the candidate's roles: {years}
From the posting: {notes}
The application form asks about: {asked}
Company: {company_info}

Job description (shortened):
<<<
{jd}
>>>

Candidate profile:
{profile}

Return one JSON object:
{{"role_in_brief": "2-3 sentences on what this job is really about",
  "pitch": "the candidate's answer to 'Tell me about yourself' for this job: first person, about 60 seconds spoken",
  "brush_up": [{{"topic": "...", "why": "...", "status": "strong | refresh | gap",
                 "review": "the specific subtopics to revise, e.g. 'chunking strategies, hybrid search, reranking, recall@k'"}}],
  "likely_questions": [{{"question": "...", "kind": "technical | behavioural | about a gap",
                         "why_they_ask": "what the interviewer is testing",
                         "answer": "what to say, first person, 80-150 words",
                         "key_points": ["the exact facts to get across, each from the profile"],
                         "avoid": "what not to say",
                         "follow_up": "the likely follow-up question", "follow_up_answer": "what to say to it, first person"}}],
  "ask_them": ["questions the candidate could ask the interviewers, specific to this job"],
  "watch_outs": ["things to clarify or be ready for, only from the posting or the application"],
  "company_points": ["facts worth knowing, only from the Company section"]}}
At most 8 brush_up, 8 likely_questions, 5 ask_them, 5 watch_outs, 5 company_points."""

REVIEWS_LIMIT = 8000  # characters of pasted reviews summarised in the one request

_REVIEWS_PROMPT = """Summarise these reviews of {company} that the candidate pasted. Report only what the reviews say;
note how many reviews mention each point when you can.

Reviews:
<<<
{reviews}
>>>

Return one JSON object:
{{"overall": "one or two sentences", "pros": ["..."], "cons": ["..."], "interview_experiences": ["..."],
  "ask_about": ["things worth asking in an interview because of these reviews"]}}
At most 6 items per list."""


def _strings(value, limit: int, length: int = 300) -> list[str]:
    return [str(v).strip()[:length] for v in (value or []) if str(v).strip()][:limit]


def _items(value, keys: tuple, limit: int) -> list[dict]:
    out = []
    for item in (value or [])[:limit]:
        if isinstance(item, dict) and str(item.get(keys[0]) or "").strip():
            out.append({k: str(item.get(k) or "").strip()[:400] for k in keys})
    return out


def write_notes(run, profile_text: str, model, research: dict | None, profile=None) -> dict:
    """The notes, with every answer fact-checked against `profile` (when given) the way the CV is: a sentence naming a
    tool, number or duration your profile doesn't show is taken out, and listed."""
    reqs = run.requirements or {}
    labels = {"covered": "covered", "partial": "partly", "missing": "not in the profile"}
    gaps = "; ".join(f"{g['term']} ({labels.get(g['status'], g['status'])})" for g in run.gaps) or "none listed"
    asked = [q["label"] for q in (run.scan or {}).get("questions", [])][:15]
    asked += [d["what"] for d in (run.scan or {}).get("documents", [])]
    years = "none stated"
    if profile is not None and reqs.get("years"):
        roles = "; ".join(r["text"] for r in tenure.role_lines(profile))
        years = "; ".join(y["text"] for y in reqs["years"]) + f" (the candidate's roles: {roles})"
    prompt = _PROMPT.format(
        title=run.title or reqs.get("job_title") or "the role", company=run.company or reqs.get("company") or "the company",
        gaps=gaps, nice=", ".join(t["term"] for t in reqs.get("nice_to_have", [])) or "none listed", years=years,
        notes="; ".join(reqs.get("notes") or []) or "nothing in particular",
        asked=", ".join(asked) or "not checked yet",
        company_info=company_section(research) or "(nothing)",
        jd=(run.jd_text or "")[:6000], profile=profile_text,
    )
    data = complete_json(model, prompt)
    notes = {
        "role_in_brief": str(data.get("role_in_brief") or "").strip()[:800],
        "pitch": str(data.get("pitch") or "").strip()[:1500],
        "brush_up": _items(data.get("brush_up"), ("topic", "why", "status", "review"), 8),
        "likely_questions": _questions(data.get("likely_questions")),
        "ask_them": _strings(data.get("ask_them"), 5),
        "watch_outs": _strings(data.get("watch_outs"), 5),
        "company_points": _strings(data.get("company_points"), 5) if company_section(research) else [],
    }
    if profile is not None:
        notes["fact_check"] = _check(notes, profile, f"{run.title} {run.company}\n{run.jd_text or ''}")
    return notes


def _questions(value) -> list[dict]:
    out = []
    for item in (value or [])[:8]:
        if not isinstance(item, dict) or not str(item.get("question") or "").strip():
            continue
        row = {k: str(item.get(k) or "").strip()[:1200] for k in ("question", "kind", "why_they_ask", "answer", "avoid",
                                                                   "follow_up", "follow_up_answer", "draw_on")}
        row["key_points"] = _strings(item.get("key_points"), 5, 240)
        out.append(row)
    return out


def _check(notes: dict, profile, context: str) -> list[dict]:
    """Take out of the answers any sentence the profile doesn't support; returns what was taken out, and why."""
    check = FactCheck()
    allowed, numbers, roles = allowed_facts_text(profile, context), allowed_numbers_text(profile), tenure.role_lines(profile)

    def clean(text: str, where: str) -> str:
        return filter_text(text, [], check, where, allowed, numbers, roles) if text else text

    notes["pitch"] = clean(notes["pitch"], "pitch")
    for q in notes["likely_questions"]:
        q["answer"] = clean(q["answer"], f"answer: {q['question'][:60]}")
        q["follow_up_answer"] = clean(q["follow_up_answer"], f"follow-up: {q['question'][:60]}")
        q["key_points"] = [k for k in (clean(k, f"key point: {q['question'][:60]}") for k in q["key_points"]) if k]
    return check.items


def summarise_reviews(text: str, company: str, model) -> dict:
    used = text[:REVIEWS_LIMIT]
    data = complete_json(model, _REVIEWS_PROMPT.format(company=company or "the company", reviews=used))
    return {"overall": str(data.get("overall") or "").strip()[:500],
            **{k: _strings(data.get(k), 6) for k in ("pros", "cons", "interview_experiences", "ask_about")},
            "summarised_at": datetime.now(timezone.utc).isoformat(), "chars": len(used), "pasted": len(text)}
