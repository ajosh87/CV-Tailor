"""How much AI you've used today, and how much is left: shown in the header on every page, updated as requests run.

Two sources, each said for what it is:
- this app's own count of today's requests against your daily cap (Settings → Pace and automation), and the tokens
  they used. It resets at midnight, local time;
- what the provider itself reports, where it offers that without spending anything: OpenRouter's key endpoint
  (credit left, and whether the key is on the free tier, which allows a fixed number of free-model requests a day).
  Asked at most every ten minutes; your key goes only to OpenRouter.
"""
import threading
import time
from datetime import date

import httpx

OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
PROVIDER_EVERY = 600  # seconds between asking the provider
FREE_DAILY = 50  # OpenRouter: free-model requests a day on the free tier (1,000 once $10 of credit was bought)


class Usage:
    def __init__(self, store, limits, settings) -> None:
        """`limits()` -> {"daily", ...} (settings.llm_limits); `settings()` -> the effective LLM settings."""
        self.store = store
        self._limits = limits
        self._settings = settings
        self._lock = threading.Lock()
        self._provider: dict = {}
        self._provider_at = 0.0

    def record(self, purpose: str, text: str, hidden: list, usage: dict | None = None) -> None:
        """Every request, as PrivateModel reports it: kept in the record of what was sent, and added to today's tokens."""
        self.store.log_llm_call(purpose, text, hidden, usage)
        if not usage or usage.get("cached") or usage.get("failed"):
            return
        with self._lock:
            today = date.today().isoformat()
            day = self.store.get_preference("llm_tokens_today") or {}
            if day.get("date") != today:
                day = {"date": today, "in": 0, "out": 0}
            day["in"] += int(usage.get("tokens_in") or 0)
            day["out"] += int(usage.get("tokens_out") or 0)
            day["estimated"] = bool(day.get("estimated") or usage.get("estimated"))
            self.store.set_preference("llm_tokens_today", day)

    def today(self, ask_provider: bool = True) -> dict:
        """{"used", "cap", "left", "share_left", "tokens_in", "tokens_out", "estimated", "state", "provider"}."""
        today = date.today().isoformat()
        counted = self.store.get_preference("llm_daily") or {}
        used = int(counted.get("count", 0)) if counted.get("date") == today else 0
        tokens = self.store.get_preference("llm_tokens_today") or {}
        if tokens.get("date") != today:
            tokens = {}
        cap = int(self._limits().get("daily") or 0)
        left = max(0, cap - used) if cap else None
        provider = self._provider_report() if ask_provider else {}
        share = (left / cap) if cap else None
        state = "out" if left == 0 else "low" if share is not None and share <= 0.2 else "ok"
        if provider.get("credit_left") is not None and provider["credit_left"] <= 0:
            state = "out"
        return {"used": used, "cap": cap, "left": left, "share_left": share, "tokens_in": int(tokens.get("in", 0)),
                "tokens_out": int(tokens.get("out", 0)), "estimated": bool(tokens.get("estimated")), "state": state,
                "provider": provider}

    def _provider_report(self) -> dict:
        settings = self._settings() or {}
        if (settings.get("LLM_PROVIDER") or "").casefold() != "openrouter" or not settings.get("LLM_API_KEY"):
            return {}
        with self._lock:
            if time.monotonic() - self._provider_at < PROVIDER_EVERY and self._provider_at:
                return self._provider
        report: dict = {"name": "OpenRouter"}
        try:
            response = httpx.get(OPENROUTER_KEY_URL, headers={"Authorization": f"Bearer {settings['LLM_API_KEY'].strip()}"},
                                 timeout=5.0)
            data = response.json().get("data") or {} if response.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            data = {}
        if data:
            remaining = data.get("limit_remaining")
            report.update({"credit_left": float(remaining) if remaining is not None else None,
                           "free_tier": bool(data.get("is_free_tier")),
                           "free_daily": FREE_DAILY if data.get("is_free_tier") else None})
        else:
            report["unavailable"] = True
        with self._lock:
            self._provider, self._provider_at = report, time.monotonic()
        return report
