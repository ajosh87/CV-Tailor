"""A second look at what the fact check rejected: fix the sentence, don't lose the point.

The fact check (honesty.py) removes a whole sentence or keeps a bullet's original wording when one detail is
wrong: a tool spelled differently, a duration rounded up, a keyword the role doesn't show. That's safe, but it
also throws away the relevant skill the sentence was there to show. One LLM request now reflects on each rejected
piece (what point it made, what exactly was objected to, which facts in the profile support the point) and rewrites
it from those facts, or gives it up. Every rewrite is checked again by the same fact check before it's used.
"""
import logging
import re

from cv_maker.llm.chat import complete_json

logger = logging.getLogger("cv_maker.revise")

MAX_ITEMS = 12
# Rejections a rewrite can't help: a role listed twice, a repeated point, a reply that couldn't be paired.
_FINAL = re.compile(r"^(repeats|the same job|the model returned)", re.I)

_PROMPT = """You are revising sentences that a fact check rejected from a candidate's {where} for one job. The check is
right about what it objects to; your job is to keep the point the sentence was making, honestly.

For each rejected piece, think it through first and write that down briefly in "thinking":
1. the point it was making for this job (which need or skill it shows);
2. exactly what the check objected to (the reason);
3. which facts in the profile support the point (quote the role or bullet).
Then rewrite it using only those facts: spell tools as the profile does, replace an unsupported detail with a
supported one or leave it out, and name a role with its duration as listed below instead of a total number of years.
Keep it about as long as the original. If the point can't be made honestly from the profile, return "" for it.
"thinking" is your private note; "text" is only the rewritten piece, written as the {where} itself speaks: for a CV,
in an implied first person ("Data analyst who built..."), never about "the candidate", the job, the posting or its
keywords.

Job: {job}
Time, counted from the candidate's dates: {tenure}

Candidate profile (the only allowed facts):
{profile}

Rejected (id · text · the check's reason):
{items}

Return one JSON object: {{"revised": [{{"id": "R1", "thinking": "...", "text": "the rewritten piece, or empty"}}]}}"""


def fixable(items: list[dict], where: tuple[str, ...]) -> list[dict]:
    return [i for i in items if i.get("where") in where and i.get("action") in ("removed", "kept original")
            and i.get("text") and not _FINAL.search(i.get("reason", ""))][:MAX_ITEMS]


def revise(model, items: list[dict], profile_text: str, tenure_text: str, job: str, where: str) -> dict[str, str]:
    """{rejected text: rewrite} for each piece the model could rewrite (empty rewrites are left out)."""
    if not items:
        return {}
    lines = "\n".join(f"R{n} · {i['text']} · {i['reason']}" for n, i in enumerate(items, 1))
    data = complete_json(model, _PROMPT.format(where=where, job=job or "(not stated)", tenure=tenure_text or "(no dates)",
                                               profile=profile_text, items=lines))
    out = {}
    for row in data.get("revised") or []:
        if not isinstance(row, dict):
            continue
        m = re.fullmatch(r"R(\d+)", str(row.get("id") or "").strip())
        text = " ".join(str(row.get("text") or "").split())
        if m and text and 1 <= int(m.group(1)) <= len(items):
            out[items[int(m.group(1)) - 1]["text"]] = text
    logger.info("revise: %d of %d rejected pieces rewritten (%s)", len(out), len(items), where)
    return out
