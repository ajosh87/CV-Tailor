"""The model picker: models you've used per provider, what each provider offers (free or paid, with prices), and
your use of them today. A saved key only ever goes to the provider it was saved with."""
from cv_maker.llm import catalog
from cv_maker.store import Store
from cv_maker.usage import remember_model, remembered_models

OPENROUTER = {"data": [
    {"id": "meta/llama:free", "name": "Llama (free)", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 131072},
    {"id": "openai/gpt-4o-mini", "name": "GPT-4o mini", "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}, "context_length": 128000},
    {"id": "openrouter/auto", "name": "Auto", "pricing": {"prompt": "-1", "completion": "-1"}},
    {"id": "cheap/zero", "name": "Zero", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "google/lyria-clip", "name": "Lyria", "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text", "audio"]}}]}


def _fake(monkeypatch, seen):
    def get(url, headers=None, timeout=8.0):
        seen.append((url, (headers or {}).get("Authorization", "")))
        if url.endswith("/api/v1/models"):
            return OPENROUTER
        if url.endswith("/api/v1/key"):
            return {"data": {"is_free_tier": True, "limit_remaining": 4.5, "usage": 0.5}}
        if "groq" in url:
            return {"data": [{"id": "llama-3.3-70b-versatile", "context_window": 131072, "active": True},
                             {"id": "whisper-large-v3", "active": True}]}
        if "openai.com" in url:
            return {"data": [{"id": "gpt-4o"}, {"id": "text-embedding-3-small"}, {"id": "o3-mini"}, {"id": "dall-e-3"}]}
        if url.endswith("/api/tags"):
            return {"models": [{"name": "llama3:8b", "size": 4_700_000_000}]}
        raise catalog.CatalogError("unexpected")
    monkeypatch.setattr(catalog, "_get", get)
    catalog._cache.clear()


def test_each_provider_lists_its_models_as_free_or_paid(monkeypatch):
    seen = []
    _fake(monkeypatch, seen)
    found = catalog.models("openrouter", "sk-or-test-123456")
    assert [m["id"] for m in found["models"]] == ["cheap/zero", "meta/llama:free", "openai/gpt-4o-mini", "openrouter/auto"]  # no audio model
    assert found["models"][1]["name"] == "Llama"  # the price column says it's free
    mini = found["models"][2]
    assert not mini["free"] and (mini["price_in"], mini["price_out"]) == (0.15, 0.6)  # dollars per million tokens
    assert found["models"][3]["price_in"] is None and "varies" in found["models"][3]["free_note"]
    assert found["account"]["free_tier"] and found["account"]["credit_left"] == 4.5 and "50 free-model" in found["account"]["note"]

    assert [m["id"] for m in catalog.models("groq", "gsk_test")["models"]] == ["llama-3.3-70b-versatile"]  # chat models only
    assert [(m["id"], m["free"]) for m in catalog.models("openai", "sk-test")["models"]] == [("gpt-4o", False), ("o3-mini", False)]
    assert catalog.models("ollama", host="http://localhost:11434")["models"][0] == {
        "id": "llama3:8b", "name": "llama3:8b", "free": True, "size_gb": 4.7, "free_note": "on this computer"}
    assert catalog.models("groq")["error"].startswith("paste your Groq API key")
    assert catalog.models("bedrock") == {"models": [], "account": {}, "error": "", "listed": False}


def test_models_you_used_are_remembered_per_provider(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    for provider, model in [("openrouter", "a"), ("openrouter", "b"), ("groq", "g"), ("openrouter", "a")]:
        remember_model(store, provider, model)
    assert remembered_models(store, "openrouter") == ["a", "b"] and remembered_models(store, "groq") == ["g"]


def test_the_picker_endpoint_never_sends_a_saved_key_to_another_provider(tmp_path, monkeypatch):
    from tests.test_app import FakeModel, fake_fetch
    from cv_maker.app import create_app
    from cv_maker.settings import save_settings

    seen = []
    _fake(monkeypatch, seen)
    save_settings(tmp_path, {"LLM_PROVIDER": "openrouter", "LLM_MODEL": "openai/gpt-4o-mini", "LLM_API_KEY": "sk-or-saved-999999"})
    app = create_app(data_dir=tmp_path, model_factory=FakeModel, fetcher=fake_fetch)
    client = app.test_client()
    remember_model(app.store, "openrouter", "openai/gpt-4o-mini")
    app.store.set_preference("llm_models_today", {"date": __import__("datetime").date.today().isoformat(),
                                                  "counts": {"openrouter/openai/gpt-4o-mini": 3}})

    got = client.post("/settings/models", json={"provider": "openrouter"}).get_json()
    assert got["remembered"] == ["openai/gpt-4o-mini"] and got["today"] == {"openai/gpt-4o-mini": 3}
    assert ("https://openrouter.ai/api/v1/key", "Bearer sk-or-saved-999999") in seen  # its own provider: fine

    seen.clear()
    groq = client.post("/settings/models", json={"provider": "groq"}).get_json()
    assert groq["models"] == [] and "paste your Groq API key" in groq["error"]
    assert not [s for s in seen if "saved" in s[1]]  # the OpenRouter key never went to Groq
    typed = client.post("/settings/models", json={"provider": "groq", "key": "gsk_typed"}).get_json()
    assert typed["models"] and seen[-1][1] == "Bearer gsk_typed"

    page = client.get("/settings").data.decode()
    assert 'id="model-pick"' in page and 'data-saved="openai/gpt-4o-mini"' in page


def test_refresh_asks_the_provider_again(monkeypatch):
    seen = []
    _fake(monkeypatch, seen)
    catalog.models("openrouter")
    catalog.models("openrouter")
    assert len([s for s in seen if s[0].endswith("/models")]) == 1  # kept for an hour
    assert catalog.models("openrouter", refresh=True)["fetched_at"]
    assert len([s for s in seen if s[0].endswith("/models")]) == 2


def test_what_each_result_was_written_by_is_kept_and_shown_small(tmp_path):
    from tests.test_app import FakeModel, _ready_job, fake_fetch
    from cv_maker.app import create_app
    from cv_maker.events import feed

    app = create_app(data_dir=tmp_path, model_factory=FakeModel, fetcher=fake_fetch, sync_jobs=True)
    client = app.test_client()
    run = _ready_job(app, client)
    assert run.analysed_with == "custom model"
    assert app.store.list_uploads()[0].read_with == "custom model"
    assert all(c.get("model") == "custom model" for c in app.store.list_llm_calls())  # the record of what was sent
    assert any("Writing a tailored CV · custom model" in e["text"] for e in feed.since(0))  # the nerdbar and the log

    client.post(f"/runs/{run.id}/prep")
    assert app.store.get_run(run.id).prep["notes"]["model"] == "custom model"
    page = client.get(f"/runs/{run.id}").data.decode()
    assert '<p class="by-model">Requirements, reading and questions by custom model</p>' in page
    assert '<span class="by-model">by custom model</span>' in page  # under the prep notes
    assert client.get("/api/usage").get_json()["model"] == "custom model"
    assert 'data-ai="model"' in client.get("/jobs").data.decode()
    assert "custom model" in client.get("/settings/sent").data.decode()


def test_each_settings_section_saves_only_its_own_settings(tmp_path):
    from tests.test_app import FakeModel, fake_fetch
    from cv_maker.app import create_app
    from cv_maker.settings import automation_settings

    app = create_app(data_dir=tmp_path, model_factory=FakeModel, fetcher=fake_fetch)
    client = app.test_client()
    client.post("/settings/automation", data={"section": "research", "RESEARCH_AUTO": "1", "RESEARCH_DEPTH": "thorough",
                                              "sources_shown": "1", "RESEARCH_SOURCES": ["wikipedia"]})
    client.post("/settings/automation", data={"section": "automation", "SCAN_APPLICATIONS": "1"})  # RESEARCH_AUTO not sent
    saved = automation_settings(tmp_path)
    assert (saved["RESEARCH_AUTO"], saved["SCAN_APPLICATIONS"], saved["APPLY_ALONGSIDE"]) == ("1", "1", "0")
    res = client.post("/settings/automation", data={"section": "pace", "LLM_RPM": "12"})
    assert res.headers["Location"].endswith("#pace") and automation_settings(tmp_path)["RESEARCH_AUTO"] == "1"
