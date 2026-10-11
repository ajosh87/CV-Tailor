"""Application details: the answers the assistant may use on forms, kept on this computer.

Standard details start from your profile; answers to questions forms often ask (work authorisation,
notice period...) are filled in once and reused. Questions you answer during an application can be
saved here too.
"""
import re

from cv_maker.privacy import Secret

# Labels of fields for passwords, one-time codes, security answers and ID or bank numbers: yours to type, never
# saved as answers, filled from one or sent to the LLM. ("PIN code" is a postal code in India: not one of them.)
SECRET = re.compile(r"password|passcode|passphrase|\bpin\b(?!\s*-?\s*code)|security (question|answer)|one[- ]time|\botp\b|"
                    r"verification code|social security|\bssn\b|national (insurance|identity|id)( number)?|aadhaa?r|"
                    r"\bpan( card| number)\b|passport (number|no)|\biban\b|routing number|sort code|(bank )?account number|"
                    r"card number|\bcvv\b|\bcvc\b", re.I)

# key, label, privacy kind (None: not identifying, so the LLM may read it to match a form's options)
STANDARD = [
    ("first_name", "First name", "NAME"),
    ("last_name", "Last name", "NAME"),
    ("email", "Email", "EMAIL"),
    ("phone", "Phone", "PHONE"),
    ("address", "Street address", "ADDRESS"),
    ("city", "City", "ADDRESS"),
    ("postal_code", "Postal code", "ADDRESS"),
    ("country", "Country", None),
    ("linkedin", "LinkedIn URL", "LINK"),
    ("website", "Website, GitHub or portfolio", "LINK"),
    ("work_authorization", "Are you authorised to work where the job is?", None),
    ("sponsorship", "Will you need visa sponsorship?", None),
    ("notice_period", "Notice period or earliest start date", None),
    ("salary", "Salary expectation", None),
    ("relocation", "Willing to relocate?", None),
    ("how_heard", "How did you hear about the job?", None),
]
LABELS = {key: label for key, label, _ in STANDARD}
PRIVATE_KINDS = {key: kind for key, _, kind in STANDARD if kind}


def defaults_from_profile(profile) -> dict:
    """What the profile already knows, as a starting point the user confirms."""
    if profile is None:
        return {}
    parts = profile.name.split()
    links = {k.lower(): v for k, v in (profile.links or {}).items()}
    website = links.get("portfolio") or links.get("website") or links.get("github") or ""
    return {
        "first_name": parts[0] if parts else "",
        "last_name": " ".join(parts[1:]),
        "email": profile.email,
        "phone": profile.phone,
        "linkedin": links.get("linkedin", ""),
        "website": website,
    }


def load(store) -> dict:
    """{"standard": {key: value}, "custom": [{"question", "answer"}]}, with profile defaults for anything unset."""
    saved = store.get_preference("apply_details") or {}
    standard = {**defaults_from_profile(store.get_profile()), **{k: v for k, v in (saved.get("standard") or {}).items() if v}}
    custom = [c for c in saved.get("custom") or [] if not SECRET.search(str(c.get("question", "")))]  # never reused
    return {"standard": {key: standard.get(key, "") for key, _, _ in STANDARD}, "custom": custom,
            "confirmed": bool(saved.get("confirmed")), "auto_sign_in": saved.get("auto_sign_in", True) is not False}


def set_auto_sign_in(store, on: bool) -> None:
    data = store.get_preference("apply_details") or {"standard": {}, "custom": []}
    data["auto_sign_in"] = bool(on)
    store.set_preference("apply_details", data)


def save(store, standard: dict, custom: list | None = None) -> dict:
    current = store.get_preference("apply_details") or {}
    data = {
        "standard": {key: " ".join(str(standard.get(key, "")).split()) for key, _, _ in STANDARD},
        "custom": current.get("custom", []) if custom is None else custom,
        "confirmed": True,
        "auto_sign_in": current.get("auto_sign_in", True) is not False,
    }
    store.set_preference("apply_details", data)
    return data


def remember_answer(store, question: str, answer: str) -> None:
    """Save an answer given during an application so the next form with the same question is filled by rule.
    Never one for a password, code or ID number."""
    if SECRET.search(question or ""):
        return
    data = store.get_preference("apply_details") or {"standard": {}, "custom": []}
    custom = [c for c in data.get("custom", []) if c.get("question", "").casefold() != question.casefold()]
    custom.append({"question": question, "answer": answer})
    data["custom"] = custom[-60:]
    store.set_preference("apply_details", data)


# Saved answers to questions like these are personal details too: hidden from the LLM like your contact details
# (it sees a placeholder, writes the placeholder, and the app fills the real value back in on this computer).
_PERSONAL_ANSWERS = [
    (re.compile(r"address|street|house|flat|apartment|locality|landmark|\bpin\s*-?\s*code\b|pincode|postal|post\s*code|"
                r"zip|city|town|district|village|state of residence", re.I), "ADDRESS"),
    (re.compile(r"phone|mobile|telephone|whats\s*app|contact number", re.I), "PHONE"),
    (re.compile(r"e-?mail", re.I), "EMAIL"),
    (re.compile(r"date of birth|\bdob\b|birth ?date|father|mother|spouse|guardian|next of kin", re.I), "DETAIL"),
]


def secrets(details: dict) -> list[Secret]:
    """The identifying details, hidden from the LLM like the rest of your contact details: your standard details,
    and saved answers that hold an address, a postal code, a phone number, an email or family details."""
    found = [Secret(kind, details["standard"].get(key, "")) for key, kind in PRIVATE_KINDS.items()
             if details["standard"].get(key, "").strip()]
    for item in details.get("custom") or []:
        answer = str(item.get("answer") or "").strip()
        if len(answer) < 3 or answer.casefold() in ("yes", "no", "n/a", "na", "none"):
            continue
        kind = next((k for rx, k in _PERSONAL_ANSWERS if rx.search(str(item.get("question") or ""))), None)
        if kind:
            found.append(Secret(kind, answer))
    return found
