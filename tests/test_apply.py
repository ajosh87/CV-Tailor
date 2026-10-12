"""The apply assistant: guards that never depend on the LLM, and a full supervised run on a mock job site."""
import json
import re
import time
import types

import pytest

from cv_maker.apply import details as app_details
from cv_maker.apply import planner
from cv_maker.models import Experience, Profile, Skill
from cv_maker.privacy import PrivateModel, identity_secrets
from cv_maker.store import Store

STANDARD = {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com", "phone": "+44 20 7946 0958",
            "city": "London", "country": "United Kingdom", "how_heard": "LinkedIn", "linkedin": "https://linkedin.com/in/ada-l",
            "work_authorization": "Yes", "sponsorship": "No", "salary": "£70,000"}
DETAILS = {"standard": {key: STANDARD.get(key, "") for key, _, _ in app_details.STANDARD}, "custom": []}


def _field(id, label, kind="text", **kw):
    return {"id": id, "label": label, "kind": kind, "required": kw.pop("required", False), "filled": False, **kw}


# ---- rules and guards (no browser) ----

def test_standard_fields_are_filled_by_rule_without_the_llm():
    page = {"fields": [
        _field("c0", "First Name *"), _field("c1", "Last name"), _field("c2", "Email address", "email"),
        _field("c3", "Confirm email"), _field("c4", "Mobile number", "tel"), _field("c5", "Country *", "select",
                                                                                     options=["Select One", "India", "United Kingdom"]),
        _field("c6", "Are you legally authorised to work in the UK?", "radio", options=["Yes", "No"], ids=["c6", "c7"]),
        _field("c8", "Name of your current employer"), _field("c9", "Resume/CV", "file"), _field("c10", "Cover letter", "file"),
    ], "buttons": []}
    actions, ask = planner.by_rule(page, DETAILS, {"cv": "cv.docx", "letter": "letter.docx"})
    got = {a.id: (a.op, a.value) for a in actions}
    assert got == {"c0": ("fill", "Ada"), "c1": ("fill", "Lovelace"), "c2": ("fill", "ada@example.com"),
                   "c3": ("fill", "ada@example.com"), "c4": ("fill", "+44 20 7946 0958"), "c5": ("choose", "United Kingdom"),
                   "c6": ("choose", "Yes"), "c9": ("upload", "cv.docx"), "c10": ("upload", "letter.docx")}
    assert ask == []  # "Name of your current employer" is left for the LLM, not mistaken for your name
    assert all("Ada" not in a.shown and "@" not in a.shown for a in actions)  # the log never shows personal details


def test_diversity_questions_are_declined_or_asked_and_never_reach_the_llm():
    page = {"fields": [
        _field("c0", "Gender", "select", options=["Female", "Male", "Decline to self-identify"]),
        _field("c1", "Are you a protected veteran?", "select", options=["Yes", "No"], required=True),
        _field("c2", "Ethnicity", "radio", options=["Asian", "White", "Prefer not to say"], ids=["c2", "c3", "c4"]),
    ], "buttons": []}
    actions, ask = planner.by_rule(page, DETAILS, {"cv": "cv.docx"})
    assert [(a.id, a.value) for a in actions] == [("c0", "Decline to self-identify"), ("c2", "Prefer not to say")]
    assert [q["id"] for q in ask] == ["c1"]
    assert planner.for_llm(page, set()) == []


def test_consent_is_yours_and_marketing_is_left_alone():
    page = {"fields": [
        _field("c0", "I agree to the Terms and Privacy Policy", "checkbox", required=True),
        _field("c1", "Send me job alerts and marketing emails", "checkbox"),
        _field("c2", "Password", "password"),
    ], "buttons": []}
    actions, ask = planner.by_rule(page, DETAILS, {"cv": "cv.docx"})
    assert actions == [] and [(q["id"], q["kind"]) for q in ask] == [("c0", "consent")]
    assert planner.for_llm(page, set()) == []


def test_the_llm_can_never_click_submit_or_create_an_account():
    page = {"fields": [_field("c0", "Years of Python *", required=True)], "buttons": [
        {"id": "c1", "text": "Submit Application"}, {"id": "c2", "text": "Create Account"}, {"id": "c3", "text": "Next"}]}
    allowed = planner.for_llm(page, set())
    assert planner.parse_plan({"next": {"do": "click", "id": "c1"}}, page, allowed).next == "ready_to_submit"
    assert planner.parse_plan({"next": {"do": "click", "id": "c2"}}, page, allowed).next == "account"
    plan = planner.parse_plan({"fill": [{"id": "c0", "value": "6"}, {"id": "c9", "value": "x"}], "next": {"do": "click", "id": "c3"}},
                              page, allowed)
    assert [(a.id, a.value) for a in plan.actions] == [("c0", "6")] and (plan.next, plan.next_id) == ("click", "c3")


def test_option_matching_and_sites():
    assert planner.pick_option("Yes", ["Yes, I am authorised", "No"]) == "Yes, I am authorised"
    assert planner.pick_option("united kingdom", ["United States", "United Kingdom"]) == "United Kingdom"
    assert planner.pick_option("Mars", ["Earth"]) is None
    assert planner.site_of("https://acme.wd5.myworkdayjobs.com/en-US/careers") == "myworkdayjobs.com"
    assert planner.site_of("https://jobs.acme.co.uk/apply") == "acme.co.uk"
    assert planner.blocker({"fields": [], "text": "Please verify you are human", "captcha": False, "password_fields": 0}) == "human"
    assert planner.blocker({"fields": [_field("c0", "Password", "password")], "text": "Sign in", "captcha": False,
                            "password_fields": 1}) == "account"


def test_lessons_from_a_real_careers_site():
    # A "see how well you match" upload is not the application: your CV never goes there by rule.
    page = {"fields": [_field("c0", "Find out how well you match with this job", "file")], "buttons": []}
    assert planner.by_rule(page, DETAILS, {"cv": "cv.docx"}) == ([], [])
    # An unlabelled but required upload is asked about, not guessed.
    page = {"fields": [_field("c0", "Attachment", "file", required=True)], "buttons": []}
    assert [q["kind"] for q in planner.by_rule(page, DETAILS, {"cv": "cv.docx"})[1]] == ["upload"]
    # Email-first sign-in ("Sign in with AstraZeneca": email, then Continue) is an account step.
    page = {"fields": [_field("c0", "Email", "email")], "buttons": [{"id": "c1", "text": "Continue"}],
            "headings": ["Sign in with AstraZeneca"], "title": "", "text": "Sign in with AstraZeneca Email Continue",
            "captcha": False, "password_fields": 0}
    assert planner.blocker(page) == "account"
    # Cookie banners: only ever the privacy-preserving choice, and never accepting for you.
    banner = {"text": "We use cookies", "buttons": [{"id": "c0", "text": "Accept all"}, {"id": "c1", "text": "Reject all"}]}
    assert planner.cookie_banner(banner) == ("reject", "c1")
    assert planner.cookie_banner({"text": "We use cookies", "buttons": [{"id": "c0", "text": "Accept"}]}) == ("accept-only", "")


def test_interrupted_applications_can_be_resumed_after_a_restart(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    run = store.create_run(job_url="https://x")
    application = store.create_application(run, 1, "https://x/apply")
    application.status = "working"
    store.update_application(application)
    store.reset_interrupted()
    assert store.get_application(application.id).status == "stopped"


def test_a_field_that_couldnt_be_filled_is_asked_about_not_skipped(tmp_path, monkeypatch):
    from cv_maker.apply import session as apply_session

    store = Store(tmp_path / "db.sqlite")
    store.save_profile(Profile(name="Ada Lovelace", email="ada@example.com", phone="", location="London", links={},
                               target_role=None, experiences=[Experience("Northwind", "Engineer", "London", "2019", None, True,
                                                                         ["Built Python APIs used by 3 teams"], True)],
                               education=[], skills=[Skill("Python", "cv")], extras={}))
    app_details.save(store, STANDARD)
    run = store.create_run(job_url="https://careers.acme.com/job/1", status="ready")
    application = store.create_application(run, 1, "https://careers.acme.com/apply")
    info = {"url": "https://careers.acme.com/apply", "title": "Experience", "headings": ["Experience"], "text": "",
            "fields": [_field("c0", "Years of experience with Python *", required=True)],
            "buttons": [{"id": "c1", "text": "Continue", "box": [0, 0, 10, 10]}], "errors": [], "password_fields": 0}

    class Model:
        def complete(self, messages, *, json_mode=False):
            return json.dumps({"fill": [{"id": "c0", "value": "6"}], "next": {"do": "click", "id": "c1"}})

    clicked = []

    def perform(page, action, field):
        if action.op == "fill":
            raise TimeoutError("the custom widget didn't respond")
        clicked.append(action.id)

    monkeypatch.setattr(apply_session.pg, "observe", lambda page: info)
    monkeypatch.setattr(apply_session.pg, "perform", perform)
    monkeypatch.setattr(apply_session.pg, "settle", lambda page: None)
    session = apply_session.ApplySession(application=application, store=store, get_model=lambda: Model(),
                                         browser_dir=tmp_path / "browser", files={"cv": "cv.docx", "letter": ""},
                                         job_label="Platform Engineer at Acme", job_context="Platform Engineer Acme Python")
    session.page = types.SimpleNamespace(url=info["url"], wait_for_timeout=lambda ms: None)
    session._details = app_details.load(store)
    session._step()
    waiting = store.get_application(application.id).waiting
    assert clicked == [] and waiting["kind"] == "question"  # it stopped instead of pressing Continue
    assert [q["id"] for q in waiting["questions"]] == ["c0"] and "couldn't fill" in waiting["questions"][0]["question"]


# ---- a full run on a mock job site ----

def _browser_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


needs_browser = pytest.mark.skipif(not _browser_available(), reason="Playwright with Chromium is not installed")


class ScriptedModel:
    """Answers like a good model would on the mock site's experience page, and records every prompt."""

    def __init__(self) -> None:
        self.prompts = []

    def complete(self, messages, *, json_mode=False):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        field = re.search(r"^(\S+) · \w+ · Years of experience with Python", prompt, re.M)
        button = re.search(r"^(\S+) · Save and Continue$", prompt, re.M)
        if field and button:
            return json.dumps({"fill": [{"id": field.group(1), "value": "6"}], "next": {"do": "click", "id": button.group(1)},
                               "note": "Filled years of Python from the profile."})
        return json.dumps({"next": {"do": "wait"}})


def _wait(store, application_id, predicate, what, timeout=90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        application = store.get_application(application_id)
        if application is not None and predicate(application):
            return application
        if application is not None and application.status == "failed":
            raise AssertionError(f"failed while waiting for {what}: {application.error}")
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}: {store.get_application(application_id)}")


def _click(session, text):
    """What you'd do in the live view: click an element on the page."""
    observation = session.last_observation
    box = next((b["box"] for b in observation["buttons"] if b["text"] == text), None) or \
        next(f["box"] for f in observation["fields"] if text in f["label"])
    session.send({"action": "input", "type": "click", "x": box[0] + box[2] / 2, "y": box[1] + box[3] / 2})


def _setup(tmp_path, start_path):
    from fixtures.mock_ats import Running

    from cv_maker.apply.session import ApplySession

    site = Running()
    store = Store(tmp_path / "db.sqlite")
    store.save_profile(Profile(name="Ada Lovelace", email="ada@example.com", phone="+44 20 7946 0958", location="London",
                               links={"linkedin": "https://linkedin.com/in/ada-l"}, target_role=None,
                               experiences=[Experience("Northwind", "Engineer", "London", "2019", None, True,
                                                       ["Built Python APIs used by 3 teams"], True)],
                               education=[], skills=[Skill("Python", "cv")], extras={}))
    app_details.save(store, STANDARD)
    run = store.create_run(job_url=f"{site.url}{start_path}", status="ready")
    cv, letter = tmp_path / "cv_test_v1.docx", tmp_path / "letter_test_v1.docx"
    cv.write_bytes(b"cv")
    letter.write_bytes(b"letter")
    application = store.create_application(run, 1, f"{site.url}{start_path}")
    model = ScriptedModel()
    session = ApplySession(application=application, store=store,
                           get_model=lambda: PrivateModel(model, lambda: identity_secrets(store.get_profile(), [])),
                           browser_dir=tmp_path / "browser", files={"cv": str(cv), "letter": str(letter)},
                           job_label="Platform Engineer at Acme", job_context="Platform Engineer Acme Python")
    return site, store, application, session, model


@needs_browser
def test_a_supervised_application_from_job_posting_to_submit(tmp_path):
    site, store, application, session, model = _setup(tmp_path, "/job/1")
    try:
        session.start()
        wall = _wait(store, application.id, lambda a: a.waiting.get("kind") == "account", "the sign-in page")
        assert wall.waiting["creating"] is False  # a sign-in form: nothing was typed into it
        _click(session, "Create Account")  # you open the sign-up form in the view
        session.send({"action": "resume"})
        _wait(store, application.id, lambda a: a.waiting.get("kind") == "account" and a.waiting.get("creating"), "the sign-up form")
        session.send({"action": "account_fill"})
        _wait(store, application.id, lambda a: a.waiting.get("kind") == "account_check", "the filled sign-up form")
        assert len(session.new_password) == 18  # no system password store here: shown to you once
        _click(session, "I agree to the Terms")  # the terms are yours to accept
        _click(session, "Create Account")
        session.send({"action": "resume"})

        asked = _wait(store, application.id, lambda a: a.waiting.get("kind") == "question", "the certification question")
        assert [q["kind"] for q in asked.waiting["questions"]] == ["consent"]
        session.send({"action": "answer", "answers": [{"id": asked.waiting["questions"][0]["id"], "value": "yes"}]})

        ready = _wait(store, application.id, lambda a: a.waiting.get("kind") == "submit", "the review page")
        assert site.app.received.get("submitted") is None  # nothing is submitted without you
        assert ready.waiting["button"] == "Submit Application"
        session.send({"action": "approve_submit"})
        done = _wait(store, application.id, lambda a: a.status == "submitted", "the confirmation")
        session.join(timeout=30)
    finally:
        session.send({"action": "stop"})
        session.join(timeout=30)
        site.close()

    got = site.app.received
    assert got["account"] == {"email": "ada@example.com", "password_length": 18}
    assert {k: got["info"][k] for k in ("first", "last", "email", "country", "heard")} == {
        "first": "Ada", "last": "Lovelace", "email": "ada@example.com", "country": "United Kingdom", "heard": "LinkedIn"}
    assert "alerts" not in got["info"]  # marketing left unticked
    assert (got["experience"]["resume"], got["experience"]["letter"], got["experience"]["years"]) == (
        "cv_test_v1.docx", "letter_test_v1.docx", "6")
    assert (got["questions"]["auth"], got["questions"]["sponsor"], got["questions"]["salary"]) == ("yes", "no", "£70,000")
    assert (got["voluntary"]["gender"], got["voluntary"]["veteran"]) == ("Decline to self-identify", "I don't wish to answer")
    assert got["submitted"] is True and done.submitted_by == "assistant"
    assert any(f["label"].startswith("Years of experience") and f["source"] == "llm" for f in done.filled)
    # The LLM was asked once (the one field no rule could fill) and never saw a personal detail.
    assert len(model.prompts) == 1 and "Years of experience with Python" in model.prompts[0]
    assert not [p for p in ("Ada", "Lovelace", "ada@example.com", "7946", "linkedin.com/in/ada-l") if p in model.prompts[0]]


@needs_browser
def test_a_captcha_is_handed_to_you(tmp_path):
    site, store, application, session, model = _setup(tmp_path, "/apply/2/start")
    try:
        session.start()
        check = _wait(store, application.id, lambda a: a.waiting.get("kind") == "human", "the CAPTCHA")
        assert "human" in check.waiting["text"]
        _click(session, "I'm not a robot")
        _click(session, "Continue")
        session.send({"action": "resume"})
        _wait(store, application.id, lambda a: a.waiting.get("kind") == "account", "the sign-in page after the check")
        session.send({"action": "stop"})
        session.join(timeout=30)
        assert store.get_application(application.id).status == "stopped"
        assert model.prompts == []
    finally:
        session.send({"action": "stop"})
        session.join(timeout=30)
        site.close()


@needs_browser
def test_real_world_widgets_partial_answers_and_a_pin_you_type_yourself(tmp_path, caplog):
    """Job 4: a typeahead, a dropdown whose options exist only once opened, a field without a <label>, a
    "select all that apply" group, radios named by aria-labelledby, a PIN, and a site that re-renders on every change."""
    caplog.set_level("INFO")
    site, store, application, session, model = _setup(tmp_path, "/job/4")
    try:
        session.start()
        asked = _wait(store, application.id, lambda a: a.waiting.get("kind") == "question", "the questions")
        questions = {q["label"]: q for q in asked.waiting["questions"]}
        assert set(questions) == {"Marital status", "Which of these do you use at work?", "Willing to relocate?", "Enter your PIN"}
        assert questions["Marital status"]["options"] == ["Single", "Married", "Prefer not to say"]  # read by opening it
        assert questions["Which of these do you use at work?"]["multiple"]
        assert questions["Willing to relocate?"]["options"] == ["Yes", "No"]
        assert questions["Enter your PIN"]["kind"] == "secret"
        assert not any("asking" in q["question"] for q in questions.values())
        assert any(f["label"].startswith("City or Town") and f["value"] == "London" for f in asked.filled)  # picked from its list

        _click(session, "Enter your PIN")  # you type the PIN yourself, in the view
        session.send({"action": "input", "type": "type", "text": "4321"})
        session.send({"action": "answer", "answers": [
            {"id": questions["Marital status"]["id"], "value": "Single", "save": False},
            {"id": questions["Which of these do you use at work?"]["id"], "value": "Python; SQL", "save": True},
            {"id": questions["Willing to relocate?"]["id"], "value": "No", "save": False},
            {"id": questions["Enter your PIN"]["id"], "value": "9999", "save": True},  # never taken, even if sent
        ]})
        ready = _wait(store, application.id, lambda a: a.waiting.get("kind") == "submit", "the review page")
        session.send({"action": "approve_submit"})
        _wait(store, application.id, lambda a: a.status == "submitted", "the confirmation")
    finally:
        session.send({"action": "stop"})
        session.join(timeout=30)
        site.close()

    got = site.app.received["hard"]
    assert got["city"] == "London, England" and got["marital"] == "Single" and got["tools"] == ["python", "sql"]
    assert got["relocate"] == "no" and got["pin"] == "4321"
    saved = app_details.load(store)["custom"]
    assert [c["question"] for c in saved] == ["Which of these do you use at work? *"]  # nothing about the PIN
    everything = json.dumps([ready.filled, model.prompts, store.get_application(application.id).answers]) + caplog.text
    assert "4321" not in everything and "9999" not in everything


def test_secret_fields_vague_questions_and_answers_that_outlive_a_re_render():
    pin = _field("c0", "Enter your PIN *", required=True)
    pin_code = _field("c1", "PIN code", autocomplete="postal-code")
    page = {"fields": [pin, pin_code, _field("c2", "Notice period", required=True)], "buttons": []}
    details = {**DETAILS, "standard": {**DETAILS["standard"], "postal_code": "600001"}}
    actions, _ = planner.by_rule(page, details, {})
    assert [a.id for a in actions] == ["c1"]  # an Indian PIN code is a postal code; a PIN is never filled
    assert [f["id"] for f in planner.for_llm(page, set())] == ["c2"]  # the PIN never reaches the LLM; optional fields stay yours
    assert [f["id"] for f in planner.optional_left(page, set())] == ["c1"]
    assert planner.ask_required(pin)["kind"] == "secret"

    vague = planner.parse_plan({"ask": [{"id": "c2", "question": "What is this question asking?"}]}, page, page["fields"])
    assert vague.ask[0]["label"] == "Notice period" and "asking" not in vague.ask[0]["question"]
    unlabelled = planner.ask_required(_field("c9", "", required=True))
    assert unlabelled["unlabelled"] and "outlined in the view" in unlabelled["label"]

    # The site re-rendered: same question, new id. Your answer still finds it, and fills it by rule next time.
    later = [_field("c7", "Notice period *", required=True)]
    assert planner.find_field({"id": "c2", "label": "Notice period", "kind": "text"}, later)["id"] == "c7"
    mine = {**DETAILS, "custom": [{"question": "notice period", "answer": "30 days"}]}
    assert [(a.id, a.value) for a in planner.by_rule({"fields": later}, mine, {})[0]] == [("c7", "30 days")]

    group = _field("c4", "Which of these do you use?", kind="multi", options=["Python", "SQL", "Excel"], ids=["a", "b", "c"])
    plan = planner.parse_plan({"fill": [{"id": "c4", "value": ["python", "sql"]}]}, {"buttons": []}, [group])
    assert plan.actions[0].value == "Python; SQL"
    assert planner._rule_key({"label": "City or Town *"}) == "city"


def test_saved_answers_never_keep_a_password_or_pin(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    app_details.remember_answer(store, "Enter your PIN", "4321")
    app_details.remember_answer(store, "Notice period", "30 days")
    store.set_preference("apply_details", {**store.get_preference("apply_details"),
                                           "custom": store.get_preference("apply_details")["custom"] + [{"question": "Password", "answer": "x"}]})
    assert [c["question"] for c in app_details.load(store)["custom"]] == ["Notice period"]


def test_linkedins_apply_tab_is_followed_once_it_has_somewhere_to_go(tmp_path):
    from cv_maker.apply import session as apply_session

    store = Store(tmp_path / "db.sqlite")
    run = store.create_run(job_url="https://www.linkedin.com/jobs/view/1", status="ready")
    application = store.create_application(run, 1, "https://www.linkedin.com/jobs/view/1")
    session = apply_session.ApplySession(application=application, store=store, get_model=lambda: None,
                                         browser_dir=tmp_path / "b", files={"cv": "cv.docx", "letter": ""},
                                         job_label="Engineer", job_context="")
    waited = []
    session.page = types.SimpleNamespace(url="about:blank", wait_for_timeout=waited.append)
    session._wait_for_you("site", "This part is on LinkedIn", site="linkedin.com", allow=False, linkedin=True)
    session._last_watch = 0
    session._watch()
    assert store.get_application(application.id).allowed_sites == []  # "about" is not a website to allow
    session._step()
    assert waited and session._steps == 0  # waits for the tab to load; no step spent, nothing asked


def test_working_at_the_hiring_company_is_answered_from_your_roles_not_guessed():
    details = {**DETAILS, "hiring": "Acme Scientific", "employers": [
        {"company": "Northwind Software Private Ltd.", "title": "Engineer", "current": True},
        {"company": "Babbage & Co", "title": "Analyst", "current": False}]}
    page = {"fields": [
        _field("c1", "Are you currently a full time or part time employee at Acme Scientific? *", kind="select",
               options=["Yes", "No"], required=True),
        _field("c2", "Have you ever worked for Babbage & Co or its affiliates?", kind="radio", options=["Yes", "No"], ids=["a", "b"]),
        _field("c3", "Are you currently employed by Babbage?", kind="radio", options=["Yes", "No"], ids=["c", "d"])], "buttons": []}
    actions, _ = planner.by_rule(page, details, {})
    assert [(a.id, a.value) for a in actions] == [("c1", "No"), ("c2", "Yes"), ("c3", "No")]
    assert "no role at Acme Scientific" in actions[0].shown and "Analyst at Babbage & Co" in actions[1].shown

    prompt = planner.build_prompt(page, [], "{}", details, "Engineer at Acme Scientific")
    assert "Hiring company: Acme Scientific" in prompt and "Engineer at Northwind Software Private Ltd. (current)" in prompt
    assert "never confuse the employer you are applying to" in prompt.casefold()


def test_replies_in_another_shape_never_crash_a_step():
    page = {"fields": [_field("c1", "Notice period")], "buttons": [{"id": "c2", "text": "Next"}]}
    plan = planner.parse_plan({"fill": ["c1", {"id": "c1", "value": "30 days", "why": "saved answer"}], "ask": ["c1"],
                               "next": "click", "next_id": "c2"}, page, page["fields"])
    assert [(a.id, a.value, a.why) for a in plan.actions] == [("c1", "30 days", "saved answer")]
    assert (plan.next, plan.next_id) == ("click", "c2")


def test_saved_answers_with_an_address_or_pin_code_are_hidden_from_the_llm():
    from cv_maker.privacy import mask
    details = {**DETAILS, "custom": [{"question": "Address Line 1", "answer": "12 Long Street"},
                                     {"question": "Pin Code", "answer": "600001"},
                                     {"question": "Willing to relocate?", "answer": "yes"}]}
    secrets = app_details.secrets(details)
    masked, _ = mask("Address Line 1: 12 Long Street\nPin Code: 600001\nWilling to relocate?: yes", secrets)
    assert "Long Street" not in masked and "600001" not in masked and "relocate?: yes" in masked


def test_a_local_model_has_no_cap_or_pace(tmp_path):
    from cv_maker.settings import llm_limits, save_automation, save_settings
    save_automation(tmp_path, {"LLM_RPM": "10", "LLM_DAILY_CAP": "45"})
    save_settings(tmp_path, {"LLM_PROVIDER": "openrouter", "LLM_MODEL": "x:free", "LLM_API_KEY": "sk-or-test"})
    assert (llm_limits(tmp_path)["rpm"], llm_limits(tmp_path)["daily"]) == (10, 45)
    save_settings(tmp_path, {"LLM_PROVIDER": "ollama", "LLM_MODEL": "llama3"})
    local = llm_limits(tmp_path)
    assert (local["rpm"], local["daily"], local["local"]) == (0, 0, True)


@needs_browser
def test_buttons_the_assistant_used_to_miss_are_found_first():
    from playwright.sync_api import sync_playwright

    from cv_maker.apply import page as pg

    menu = "".join(f'<a href="/p{i}">Menu item {i}</a>' for i in range(50))
    html = f"""<header><nav>{menu}<a href="/careers">Careers</a></nav></header>
      <main><h1>ML Engineer</h1><p>Send us your résumé.</p>
      <div class="btn-apply" onclick="1">Apply now →</div>
      <div id="host"></div>
      <label for="cv" class="upload-btn">Upload résumé</label><input type="file" id="cv" name="resume_file" style="display:none">
      </main>
      <script>
        const root = document.getElementById('host').attachShadow({{mode: 'open'}});
        root.innerHTML = '<button>Apply with LinkedIn</button>';
      </script>"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        info = pg.observe(page)
        browser.close()
    texts = [b["text"] for b in info["buttons"]]
    assert texts[:3] == ["Apply now →", "Apply with LinkedIn", "Upload résumé"] or set(texts[:3]) == {"Apply now →", "Apply with LinkedIn", "Upload résumé"}
    assert "Menu item 3" not in texts  # site menus don't crowd out the buttons that matter
    assert planner.navigation(info)[0] == "click"  # "Apply now →" is a start button, arrow and all
    upload = next(f for f in info["fields"] if f["kind"] == "file")
    assert planner.pending_documents(info)["cv"]["id"] == upload["id"]  # known as the résumé upload by its name


def test_submit_needs_a_submit_button_and_echoes_are_not_answers():
    page = {"fields": [_field("c0", "Middle Name *", required=True)], "buttons": [{"id": "c1", "text": "Next"}]}
    plan = planner.parse_plan({"fill": [{"id": "c0", "value": "Middle Name"}], "next": {"do": "ready_to_submit"},
                               "note": "one short sentence on what you did"}, page, page["fields"])
    assert plan.actions == [] and (plan.next, plan.next_id) == ("click", "c1") and plan.note == ""


def test_a_pause_says_which_fields_need_you_and_what_you_typed_is_kept(tmp_path, monkeypatch):
    from cv_maker.apply import session as apply_session

    store = Store(tmp_path / "db.sqlite")
    app_details.save(store, STANDARD)
    run = store.create_run(job_url="https://careers.acme.com/job/1", status="ready")
    application = store.create_application(run, 1, "https://careers.acme.com/apply")
    session = apply_session.ApplySession(application=application, store=store, get_model=lambda: None,
                                         browser_dir=tmp_path / "b", files={"cv": "cv.docx", "letter": ""}, job_label="x", job_context="")
    info = {"title": "Questions", "errors": ["Please enter your notice period"], "fields": [
        _field("c0", "Notice period *", required=True, box=[1, 2, 3, 4]),
        _field("c1", "Enter your PIN *", required=True, secret=True),
        _field("c2", "Middle name")], "buttons": []}
    session._optional = [info["fields"][2]]
    attention = session._attention(info)
    assert [(a["label"], a["why"]) for a in attention][:3] == [
        ("Notice period", "required and still empty"), ("Enter your PIN", "required: a password, code or ID number, for you to type"),
        ("The page says", "Please enter your notice period")]
    assert attention[-1]["why"].startswith("optional")

    session.last_observation = info
    typed = {**info, "fields": [{**info["fields"][0], "filled": True, "value": "30 days"},
                                {**info["fields"][1], "filled": True, "value": "1234"}]}
    monkeypatch.setattr(apply_session.pg, "observe", lambda page: typed)
    session.page = types.SimpleNamespace(url="https://careers.acme.com/apply")
    session._note_what_you_typed()
    saved = store.get_application(application.id)
    assert saved.answers == {"notice period": "30 days"}  # never the PIN
    assert [(f["label"], f["value"], f["source"]) for f in saved.filled] == [("Notice period *", "30 days", "typed")]
    session._command({"action": "save_typed"})
    assert app_details.load(store)["custom"][0] == {"question": "Notice period *", "answer": "30 days"}


def test_forms_get_a_compact_profile_unless_they_ask_for_writing():
    from cv_maker.apply.session import _form_profile
    profile = Profile(name="Ada", email="", phone="", location="", links={}, target_role=None, education=[],
                      skills=[Skill("Python", "cv")], extras={},
                      experiences=[Experience("Northwind", "Engineer", "", "2021", None, True, ["Built Python APIs used by 3 teams"], True)])
    assert "Built Python APIs" not in _form_profile(profile, [_field("c0", "Notice period")])
    assert "Built Python APIs" in _form_profile(profile, [_field("c0", "Why do you want this job?", kind="textarea")])


@needs_browser
def test_chat_widgets_are_ignored_harmless_popups_closed_and_a_covered_button_named():
    from playwright.sync_api import sync_playwright

    from cv_maker.apply import page as pg

    html = """<main><h1>Your details</h1><label for="n">Notice period *</label><input id="n" required>
        <button id="next" style="position:relative">Next</button></main>
      <div id="intercom-container" style="position:fixed;right:0;bottom:0;width:300px;height:300px">
        <input placeholder="Type a message"><button>Send</button></div>
      <div role="dialog" aria-modal="true" class="modal" style="position:fixed;left:30px;top:30px;width:400px;height:200px;background:#fff">
        <p>Join our talent community and get job alerts first!</p><input type="email"><button>Subscribe</button>
        <button aria-label="Close">×</button></div>
      <div role="dialog" class="modal" style="position:fixed;left:30px;top:260px;width:400px;height:120px;background:#fff">
        <p>Before you continue your application, confirm your email.</p><button>Close</button></div>"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        info = pg.observe(page)
        assert info["chat"] and not [f for f in info["fields"] if "message" in f["label"].lower()]
        assert "Send" not in [b["text"] for b in info["buttons"]]  # the chat widget never reaches the LLM
        popups = planner.dismissable(info)
        assert [p["text"][:20] for p in popups] == ["Join our talent comm"]  # never the one about the application
        pg.perform(page, planner.Action("click", popups[0]["close_id"]), None)
        page.evaluate("document.querySelectorAll('[role=dialog]').forEach(d => { if (d.textContent.includes('talent')) d.remove(); })")
        page.evaluate("""() => { const o = document.createElement('div'); o.className = 'newsletter-overlay';
                               o.style.cssText = 'position:fixed;inset:0;z-index:9'; document.body.appendChild(o); }""")
        info = pg.observe(page)
        nxt = next(b for b in info["buttons"] if b["text"] == "Next")
        with pytest.raises(pg.Covered) as covered:
            pg.perform(page, planner.Action("click", nxt["id"]), None)
        browser.close()
    assert "newsletter-overlay" in covered.value.cover
