"""Anthropic API client wrapper with cost guard integration.

Uses the latest Anthropic Python SDK (v4.5 generation).
All Claude calls are funneled through the CostGuard gatekeeper.

Two-tier model system:
  - Screening (Haiku 4.5): Fast, cheap pre-filtering for urgency scoring
  - Strategy  (Sonnet 4.5): Deep analysis, only invoked when screening passes threshold
"""

import os
import re
import json
import anthropic
from .cost_guard import CostGuard

# ── Model constants (STRICT — DO NOT CHANGE) ──────────────────
SCREENING_MODEL = "claude-haiku-4-5-20251001"   # 一次判定
STRATEGY_MODEL  = "claude-sonnet-4-5-20250929"  # 二次分析


class AIClient:
    """Cost-guarded wrapper around the Anthropic Messages API.

    Supports two-tier model routing:
      - screening calls use Haiku 4.5 (cheap, fast)
      - strategy  calls use Sonnet 4.5 (deep, expensive)
    """

    def __init__(self, role: str):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key or api_key.startswith("sk-ant-CHANGE"):
            raise ValueError(
                "ANTHROPIC_API_KEY is not configured. "
                "Edit your .env file and set a valid key."
            )
        self.client = anthropic.Anthropic(api_key=api_key)
        self.screening_model = SCREENING_MODEL
        self.strategy_model = STRATEGY_MODEL
        # Legacy compat: default model used by Trader (Haiku)
        self.model = SCREENING_MODEL if role == "TRADER" else STRATEGY_MODEL
        self.guard = CostGuard(role)
        self.role = role

    # ── Core call methods ───────────────────────────────────

    def _call(self, model: str, system_prompt: str, user_message: str, max_tokens: int) -> str | None:
        """Internal: send a message to a specific model."""
        if not self.guard.can_call():
            return None
        self.guard.record_call()
        print(f"  [AI] Calling {model} (call #{self.guard.count}/{self.guard.max_daily})")
        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text

    def _call_json(self, model: str, system_prompt: str, user_message: str, max_tokens: int) -> dict | None:
        """Internal: call a model and parse response as JSON."""
        raw = self._call(model, system_prompt, user_message, max_tokens)
        if raw is None:
            print(f"  [AI] Budget exceeded or call failed.")
            return None
        try:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                return json.loads(match.group())
        except json.JSONDecodeError:
            print(f"  [AI] JSON parse failed: {raw[:200]}")
        return None

    # ── Screening tier (Haiku 4.5 — cheap & fast) ──────────

    def screen(self, system_prompt: str, user_message: str, max_tokens: int = 256) -> str | None:
        """Screening call via Haiku 4.5. Returns raw text or None."""
        return self._call(self.screening_model, system_prompt, user_message, max_tokens)

    def screen_json(self, system_prompt: str, user_message: str, max_tokens: int = 256) -> dict | None:
        """Screening call via Haiku 4.5. Returns parsed JSON or None."""
        return self._call_json(self.screening_model, system_prompt, user_message, max_tokens)

    # ── Strategy tier (Sonnet 4.5 — deep analysis) ─────────

    def strategize(self, system_prompt: str, user_message: str, max_tokens: int = 1024) -> str | None:
        """Strategy call via Sonnet 4.5. Returns raw text or None."""
        return self._call(self.strategy_model, system_prompt, user_message, max_tokens)

    def strategize_json(self, system_prompt: str, user_message: str, max_tokens: int = 1024) -> dict | None:
        """Strategy call via Sonnet 4.5. Returns parsed JSON or None."""
        return self._call_json(self.strategy_model, system_prompt, user_message, max_tokens)

    # ── Legacy methods (backward compat for Trader) ────────

    def think(self, system_prompt: str, user_message: str, max_tokens: int = 1024) -> str:
        """Legacy: calls the role's default model. Used by Trader."""
        result = self._call(self.model, system_prompt, user_message, max_tokens)
        if result is None:
            return f"[BUDGET_EXCEEDED] Daily API limit reached ({self.guard.max_daily}). Skipping."
        return result

    def think_json(self, system_prompt: str, user_message: str, max_tokens: int = 1024) -> dict | None:
        """Legacy: calls the role's default model and parses JSON. Used by Trader."""
        return self._call_json(self.model, system_prompt, user_message, max_tokens)

    def think_with_tools(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict],
        max_tokens: int = 1024,
    ) -> anthropic.types.Message | None:
        """Send a message with tool definitions. Returns full Message for tool_use parsing."""
        if not self.guard.can_call():
            print(f"  [BUDGET_EXCEEDED] {self.guard}")
            return None
        self.guard.record_call()
        print(f"  [AI+Tools] Calling {self.model} (call #{self.guard.count}/{self.guard.max_daily})")
        return self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            tools=tools,
        )
