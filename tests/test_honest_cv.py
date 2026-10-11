"""A CV that says only what your dates and facts show, once; questions you've all read; AI use you can see."""
import re
from datetime import date
from types import SimpleNamespace as Role

from cv_maker import tenure
from cv_maker.honesty import CvDocument, FactCheck, filter_cv_document
from cv_maker.jobs.requirements import JobRequirements
from cv_maker.models import Experience, Profile, Skill
from cv_maker.profile.merge import duplicate_groups, merge_profile, merge_duplicates

TODAY = date(2026, 10, 11)


def _profile(**extra) -> Profile:
    return Profile(
        name="Ada", email="ada@x.com", phone="", location="London", links={}, target_role=None,
        experiences=[
            Experience("Northwind Software Private Ltd.", "Associate Software Engineer (AI/ML)", "London", "FEB 2025", "JAN 2026", False,
                       ["Built a RAG chatbot with Azure AI Search", "Wrote Python pipelines with Pandas for research articles"], True),
            Experience("Northwind Software Private Ltd.", "Research Analyst", "London", "MAR 2022", "JULY 2024", False,
                       ["Evaluated ML datasets and prompts with the team"], True),
            Experience("Northwind", "Associate Software Engineer", "", "February 2025", "January 2026", False,
                       ["Built a RAG chatbot with Azure AI Search", "Deployed services on AWS Lambda"], True),
        ],
        education=[], skills=[Skill("Python", "cv"), Skill("Pandas", "cv")], extras={}, **extra)


# ---- years: counted from your dates, never copied from the posting ----

def test_durations_come_from_the_dates_and_never_run_long():
    assert tenure.months(Role(start="NOV 2025", end=None, current=True), TODAY) == 12
    assert tenure.describe(tenure.months(Role(start="JUNE 2023", end="OCT 2025", current=False))) == "2 yrs 5 mos"
    assert tenure.months(Role(start="APRIL", end="JUNE 2021", current=False)) == 3  # the year comes from the end date
    assert tenure.months(Role(start="2019", end="2020", current=False)) == 2  # unknown months: Dec 2019 to Jan 2020
    overlapping = [Role(start="Jan 2020", end="Dec 2020", current=False), Role(start="Jun 2020", end="Jun 2021", current=False)]
    assert tenure.total_months(overlapping) == 18


def test_a_years_requirement_is_compared_not_written_onto_the_cv():
    reqs = JobRequirements.from_dict({"must_have": [
        {"term": "3+ years experience", "original": "3+ years of experience in software engineering"},
        {"term": "Agile/Scrum experience", "original": "Agile/Scrum"}, {"term": "5 years of Python", "original": "5 years of Python"}]})
    assert [m.term for m in reqs.must_have] == ["Agile/Scrum", "Python"]
    assert [(y["years"], y["of"]) for y in reqs.years] == [(3.0, "software engineering"), (5.0, "Python")]
    seen = tenure.compare(reqs.years[0], _profile())
    assert seen["roles"][0]["text"].startswith("Associate Software Engineer (AI/ML) at Northwind")


def test_a_total_of_years_or_a_number_from_the_posting_never_reaches_the_cv():
    doc = CvDocument("", "Associate Software Engineer with 3+ years of experience in software engineering. "
                         "Spent 2 years as Research Analyst evaluating ML datasets. Led 5 projects. Builds RAG chatbots.",
                     [], [], ["Python"])
    check = FactCheck()
    out = filter_cv_document(doc, _profile(), [], check, "Need 3+ years. Lead 5 projects.")
    assert out.summary == "Spent 2 years as Research Analyst evaluating ML datasets. Builds RAG chatbots."
    reasons = " ".join(i["reason"] for i in check.items)
    assert "claims 3+ years, but the role it names shows Associate Software Engineer: 1 yr" in reasons and "(5)" in reasons
    doc.summary = "Spent 4 years as Research Analyst."
    filter_cv_document(doc, _profile(), [], check := FactCheck())
    assert "Research Analyst: 2 yrs 5 mos" in check.items[-1]["reason"]


# ---- each thing once ----

def test_a_job_listed_twice_and_a_repeated_bullet_appear_once():
    profile = _profile()
    doc = CvDocument("", "", [Experience(e.company, e.title, "", "", None, False, list(e.bullets), True) for e in profile.experiences],
                     [], ["Python"])
    doc.experiences[1].bullets = ["Built a RAG chatbot with Azure AI Search for the team"]  # says what R1 already said
    check = FactCheck()
    out = filter_cv_document(doc, profile, [], check)
    assert [e.title for e in out.experiences] == ["Associate Software Engineer (AI/ML)", "Research Analyst"]
    assert out.experiences[1].bullets == []
    assert any("the same job as" in i["reason"] for i in check.items) and any(i["reason"].startswith("repeats") for i in check.items)


def test_a_bullet_stuffed_with_new_keywords_keeps_your_wording():
    profile = _profile()
    stuffed = "Built a RAG chatbot with Azure AI Search, Kubernetes, Terraform and Kafka"
    doc = CvDocument("", "", [Experience(profile.experiences[0].company, profile.experiences[0].title, "", "", None, False,
                                         [stuffed, profile.experiences[0].bullets[1]], True)], [], ["Python"])
    check = FactCheck()
    out = filter_cv_document(doc, profile, [], check, keywords=["Kubernetes", "Terraform", "Kafka", "Pandas"])
    assert out.experiences[0].bullets[0] == "Built a RAG chatbot with Azure AI Search"
    assert any("added 3 keywords" in i["reason"] for i in check.items)


def test_the_same_job_in_other_words_is_merged_not_added():
    profile = _profile()
    assert duplicate_groups(profile) == [[0, 2]]
    merged, removed = merge_duplicates(profile)
    assert removed == 1 and len(merged.experiences) == 2
    assert merged.experiences[0].bullets == ["Built a RAG chatbot with Azure AI Search",
                                             "Wrote Python pipelines with Pandas for research articles", "Deployed services on AWS Lambda"]
    linkedin = Profile(name="", email="", phone="", location="", links={}, target_role=None, education=[], skills=[], extras={},
                       experiences=[Experience("Northwind", "Research Analyst", "", "March 2022", "Aug 2024", False, ["X"], False)])
    assert len(merge_profile(merged, linkedin).experiences) == 2  # a second CV no longer adds the job again


# ---- questions you've all read; AI use and messages you can see ----

def test_every_question_needs_an_answer_you_picked(tmp_path):
    from tests.test_app import FakeModel, _upload, fake_fetch
    from cv_maker.app import create_app

    app = create_app(data_dir=tmp_path, model_factory=FakeModel, fetcher=fake_fetch, sync_jobs=True)
    client = app.test_client()
    _upload(client)
    client.post("/jobs", data={"urls": "https://jobs.example.com/ok1"})
    run = app.store.list_runs()[0]
    page = client.get(f"/runs/{run.id}/questions").data.decode()
    assert "checked" not in page.split('id="answers"')[1].split("generate_letter")[0]  # nothing is answered for you
    assert "0</strong> of 1 answered" in page

    res = client.post(f"/runs/{run.id}/questions", data={"generate_letter": "1"})
    assert res.status_code == 400 and "1 question is still unanswered (Kubernetes)" in res.data.decode()
    assert 't-warn' in res.data.decode() and app.store.list_documents(run.id) == []

    client.post(f"/runs/{run.id}/questions", data={"answer_0": "skip"})
    assert [d.version for d in app.store.list_documents(run.id)] == [1]


def test_ai_left_today_is_in_the_header_and_counts_tokens(tmp_path):
    from tests.test_app import FakeModel, _upload, fake_fetch
    from cv_maker.app import create_app

    app = create_app(data_dir=tmp_path, model_factory=FakeModel, fetcher=fake_fetch, sync_jobs=True)
    client = app.test_client()
    client.post("/settings/automation", data={"LLM_RPM": "", "LLM_CONCURRENCY": "", "LLM_DAILY_CAP": "40"})
    _upload(client)
    used = client.get("/api/usage").get_json()
    assert used["cap"] == 40 and used["used"] >= 1 and used["left"] == 40 - used["used"] and used["tokens_in"] > 0
    header = client.get("/jobs").data.decode()
    assert 'id="ai-usage"' in header and f'data-ai="left">{used["left"]}<' in header
    app.store.set_preference("llm_daily", {"date": date.today().isoformat(), "count": 39})
    assert client.get("/api/usage").get_json()["state"] == "low"


# ---- less thrown away: the check reads tools as written, and rejected points get a second look ----

def test_tools_written_with_other_dashes_plurals_and_compounds_are_not_invented():
    from cv_maker.honesty import allowed_facts_text, allowed_numbers_text, filter_text
    profile = _profile()
    profile.experiences[1].bullets.append("Worked with SMEs on Llama-Index pipelines")
    allowed, numbers, roles = allowed_facts_text(profile), allowed_numbers_text(profile), tenure.role_lines(profile)
    for sentence in ("Built pipelines with Llama‑Index for research teams.", "Worked closely with SME reviewers.",
                     "Wrote Python-based pipelines with Pandas.", "As Research Analyst for 2 yrs 5 mos I evaluated ML datasets."):
        check = FactCheck()
        assert filter_text(sentence, [], check, "summary", allowed, numbers, roles), check.items
    check = FactCheck()
    assert filter_text("Managed 5 interns.", [], check, "summary", allowed, numbers, roles) == ""


def test_a_skill_your_roles_describe_is_kept_as_far_as_they_show_it():
    profile = _profile()
    doc = CvDocument("", "", [], [], ["Python", "RAG chatbot/Kafka", "Terraform"])
    check = FactCheck()
    out = filter_cv_document(doc, profile, [], check)
    assert "RAG chatbot" in out.skills and "Terraform" not in out.skills
    assert any(i["action"] == "shortened" for i in check.items)
    from cv_maker import ats
    assert not ats.listable("high ownership") and not ats.listable("working with customers") and ats.listable("FastAPI")


def test_rejected_pieces_are_rewritten_from_the_facts_and_checked_again(tmp_path):
    import json

    from cv_maker.pipeline.graph import Pipeline
    from cv_maker.store import Store

    class Model:
        def __init__(self):
            self.asked = []

        def complete(self, messages, *, json_mode=False):
            prompt = messages[-1]["content"]
            self.asked.append(prompt)
            if prompt.startswith("Read this job description"):
                return json.dumps({"job_title": "AI Engineer", "must_have": [{"term": "Python", "original": "Python"}]})
            if prompt.startswith("You are tailoring"):
                return json.dumps({"summary": "AI engineer with 3+ years of experience. Builds RAG chatbots with Python.",
                                   "experiences": [{"id": "R1", "bullets": [{"id": "R1.B1", "text": "Built a RAG chatbot for 500 users"}]}],
                                   "skills": ["Python"]})
            if prompt.startswith("You are revising"):
                rejected = dict(re.findall(r"^(R\d+) · (.+?) · ", prompt, re.M))
                fixes = {"AI engineer with 3+ years of experience.": "Associate Software Engineer (AI/ML) building AI solutions for the past year",
                         "Built a RAG chatbot for 500 users": "Built a RAG chatbot with Azure AI Search",
                         "I led 9 teams.": ""}
                if "cv" in prompt.split("for one job")[0].casefold():
                    assert "claims 3+ years" in prompt and "500" in prompt and "Associate Software Engineer (AI/ML) at" in prompt
                return json.dumps({"revised": [{"id": rid, "thinking": "point, objection, fact", "text": fixes.get(text, "")}
                                               for rid, text in rejected.items()]})
            if prompt.startswith("Write a cover letter"):
                return json.dumps({"plan": ["RAG -> chatbot (Associate SE)"], "letter": "Dear team,\n\nI led 9 teams.\n\nKind regards,"})
            return "{}"

    store = Store(tmp_path / "db.sqlite")
    store.save_profile(_profile())
    run = store.create_run(job_url="https://x")
    run.jd_text, run.generate_letter = "AI Engineer. 3+ years. Python.", True
    store.update_run(run)
    model = Model()
    pipeline = Pipeline(store, lambda: model, tmp_path)
    pipeline.analyze(run.id)
    done = pipeline.generate(run.id)
    assert done.status == "ready", done.error
    assert done.draft["summary"].startswith("Associate Software Engineer (AI/ML) building AI solutions")
    assert done.draft["experiences"][0]["bullets"][0] == "Built a RAG chatbot with Azure AI Search"
    cv_meta = next(d for d in store.list_documents(run.id) if d.kind == "cv").meta
    assert [i["action"] for i in cv_meta["fact_check"]["items"]].count("revised") == 2
    assert sum(p.startswith("You are revising") for p in model.asked) == 2  # one for the CV, one for the letter
    planning = next(p for p in model.asked if p.startswith("You are planning"))
    assert "check your plan before answering" in planning


def test_interview_prep_says_what_to_say_and_only_what_your_profile_shows():
    import json

    from cv_maker import prep

    class Model:
        def complete(self, messages, *, json_mode=False):
            return json.dumps({"pitch": "I'm an Associate Software Engineer at Northwind. I led 40 engineers there.",
                               "likely_questions": [{"question": "Walk me through a RAG system you built.", "kind": "technical",
                                                     "why_they_ask": "depth in retrieval",
                                                     "answer": "At Northwind I built a RAG chatbot with Azure AI Search. It served 10,000 users.",
                                                     "key_points": ["Azure AI Search", "Saved $2M"], "avoid": "Overclaiming scale",
                                                     "follow_up": "How did you evaluate it?", "follow_up_answer": "We tested it with Python scripts."}],
                               "brush_up": [{"topic": "RAG", "why": "core", "status": "refresh", "review": "chunking, reranking"}]})

    run = Role(requirements={"years": [{"years": 3.0, "of": "", "text": "3+ years"}]}, gaps=[], scan={}, title="AI Engineer",
               company="Acme", jd_text="RAG", )
    notes = prep.write_notes(run, "{}", Model(), None, _profile())
    q = notes["likely_questions"][0]
    assert notes["pitch"] == "I'm an Associate Software Engineer at Northwind."
    assert q["answer"] == "At Northwind I built a RAG chatbot with Azure AI Search." and q["key_points"] == ["Azure AI Search"]
    assert q["follow_up_answer"] and notes["brush_up"][0]["review"] == "chunking, reranking"
    assert len(notes["fact_check"]) == 3
