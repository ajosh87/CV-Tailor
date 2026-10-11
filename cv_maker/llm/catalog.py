"""The models a provider offers, asked from the provider itself, with what each costs: for the model picker in
Settings.

- OpenRouter: its public model list (no key needed): prices per million tokens, context size, and which are free.
  With your key, also your credit left and whether the key is on the free tier.
- Groq: its model list (your key): all usable on Groq's free tier within its rate limits, paid with billing on.
- OpenAI: its model list (your key), chat models only: always paid (OpenAI has no free API tier).
- Ollama: the models installed on this computer: free, nothing leaves it.
- Azure OpenAI and Bedrock list deployments through separate management APIs, so only the models you've used are
  offered there.

Lists are kept for an hour per provider. A key goes only to the provider it belongs to.
"""
import re
import threading
import time

import httpx

TTL = 3600
_CHAT = re.compile(r"^(gpt-|o\d|chatgpt-)", re.I)
_NOT_CHAT = re.compile(r"(embedding|whisper|tts|dall-e|image|audio|realtime|transcribe|moderation|search-preview|"
                       r"guard|distil-whisper|playai)", re.I)
LISTED = ("openrouter", "groq", "openai", "ollama")
FREE_TIER_DAILY = {"openrouter": "50 free-model requests a day on the free tier (1,000 once you've bought $10 of credit)",
                   "groq": "free tier: limited requests per minute and per day for each model"}

_cache: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()


class CatalogError(Exception):
    pass


def _get(url: str, headers: dict | None = None, timeout: float = 8.0) -> dict:
    try:
        response = httpx.get(url, headers=headers or {}, timeout=timeout)
    except httpx.HTTPError as exc:
        raise CatalogError(f"couldn't reach it ({type(exc).__name__})") from exc
    if response.status_code == 401:
        raise CatalogError("it rejected the API key")
    if response.status_code != 200:
        raise CatalogError(f"it answered HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise CatalogError("its answer couldn't be read") from exc


def _per_million(value) -> float | None:
    """A price per token as US dollars per million tokens; None when it isn't fixed (routers report -1)."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return round(price * 1_000_000, 4) if price >= 0 else None


def _openrouter(key: str) -> dict:
    models = []
    for m in _get("https://openrouter.ai/api/v1/models").get("data") or []:
        arch = m.get("architecture") or {}
        if set(arch.get("output_modalities") or ["text"]) != {"text"} or "text" not in (arch.get("input_modalities") or ["text"]):
            continue  # models that make images, audio or video: for writing a CV, only text-to-text ones
        pricing = m.get("pricing") or {}
        price_in, price_out = _per_million(pricing.get("prompt")), _per_million(pricing.get("completion"))
        free = str(m.get("id", "")).endswith(":free") or (price_in == 0 and price_out == 0)
        name = re.sub(r"\s*\(free\)\s*$", "", str(m.get("name") or m.get("id", "")), flags=re.I)
        models.append({"id": m.get("id", ""), "name": name, "free": free,
                       "price_in": price_in, "price_out": price_out, "context": m.get("context_length"),
                       **({"free_note": "price varies by the model it picks"} if price_in is None and not free else {})})
    account = {}
    if key:
        try:
            data = _get("https://openrouter.ai/api/v1/key", {"Authorization": f"Bearer {key}"}).get("data") or {}
            account = {"free_tier": bool(data.get("is_free_tier")), "credit_left": data.get("limit_remaining"),
                       "spent": data.get("usage"), "note": FREE_TIER_DAILY["openrouter"] if data.get("is_free_tier") else ""}
        except CatalogError as exc:
            account = {"error": f"OpenRouter didn't show your key's usage: {exc}"}
    return {"models": models, "account": account}


def _groq(key: str) -> dict:
    if not key:
        raise CatalogError("paste your Groq API key to list its models")
    data = _get("https://api.groq.com/openai/v1/models", {"Authorization": f"Bearer {key}"})
    models = [{"id": m["id"], "name": m["id"], "free": True, "context": m.get("context_window"), "free_note": "free tier, rate-limited"}
              for m in data.get("data") or [] if m.get("id") and m.get("active", True) and not _NOT_CHAT.search(m["id"])]
    return {"models": models, "account": {"note": FREE_TIER_DAILY["groq"]}}


def _openai(key: str) -> dict:
    if not key:
        raise CatalogError("paste your OpenAI API key to list its models")
    data = _get("https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"})
    models = [{"id": m["id"], "name": m["id"], "free": False}
              for m in data.get("data") or [] if _CHAT.match(m.get("id", "")) and not _NOT_CHAT.search(m.get("id", ""))]
    return {"models": sorted(models, key=lambda m: m["id"]), "account": {"note": "OpenAI's API is paid per token: no free tier."}}


def _ollama(host: str) -> dict:
    data = _get((host or "http://localhost:11434").rstrip("/") + "/api/tags", timeout=4.0)
    models = [{"id": m["name"], "name": m["name"], "free": True, "size_gb": round((m.get("size") or 0) / 1e9, 1),
               "free_note": "on this computer"} for m in data.get("models") or [] if m.get("name")]
    return {"models": models, "account": {"note": "Runs on this computer: free, and nothing leaves it."}}


def models(provider: str, key: str = "", host: str = "", refresh: bool = False) -> dict:
    """{"models": [{"id", "name", "free", "price_in", "price_out", "context", ...}], "account": {...}, "error": ""}.
    Prices are US dollars per million tokens. Never raises: a provider that can't be asked says why in "error"."""
    provider = (provider or "").casefold()
    if provider not in LISTED:
        return {"models": [], "account": {}, "error": "", "listed": False}
    cache_key = (provider, key[-6:] if key else "", host)
    with _lock:
        hit = None if refresh else _cache.get(cache_key)  # Refresh asks the provider again
        if hit and time.monotonic() - hit[0] < TTL:
            return hit[1]
    try:
        found = {"openrouter": lambda: _openrouter(key), "groq": lambda: _groq(key), "openai": lambda: _openai(key),
                 "ollama": lambda: _ollama(host)}[provider]()
        result = {**found, "error": "", "listed": True, "fetched_at": time.time()}
    except CatalogError as exc:
        return {"models": [], "account": {}, "error": str(exc), "listed": True}
    # Free first, then paid from cheapest; models without a fixed price last.
    result["models"].sort(key=lambda m: (not m["free"], m.get("price_in") is None and not m["free"], m.get("price_in") or 0, m["id"]))
    with _lock:
        _cache[cache_key] = (time.monotonic(), result)
    return result
