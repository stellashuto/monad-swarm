"""Anthropic API client wrapper with cost guard integration.

Uses the latest Anthropic Python SDK (v4.5 generation).
All Claude calls are funneled through the CostGuard gatekeeper.
"""

import os
import re
import json
import anthropic
from .cost_guard import CostGuard


class AIClient:
    """Cost-guarded wrapper around the Anthropic Messages API."""

    def __init__(self, role: str):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key or api_key.startswith("sk-ant-CHANGE"):
            raise ValueError(
                "ANTHROPIC_API_KEY is not configured. "
                "Edit your .env file and set a valid key."
            )
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = os.getenv("MODEL_NAME", "claude-sonnet-4-5-20250929")
        self.guard = CostGuard(role)
        self.role = role

    def think(self, system_prompt: str, user_message: str, max_tokens: int = 1024) -> str:
        """Send a message to Claude and return the text response.

        Enforces daily API call budget via CostGuard.
        Returns "[BUDGET_EXCEEDED]..." if limit reached.
        """
        if not self.guard.can_call():
            return f"[BUDGET_EXCEEDED] Daily API limit reached ({self.guard.max_daily}). Skipping."

        self.guard.record_call()
        print(f"  [AI] Calling {self.model} (call #{self.guard.count}/{self.guard.max_daily})")

        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text

    def think_json(self, system_prompt: str, user_message: str, max_tokens: int = 1024) -> dict | None:
        """Call Claude and parse the response as JSON. Returns None on failure."""
        raw = self.think(system_prompt, user_message, max_tokens)
        if raw.startswith("[BUDGET_EXCEEDED]"):
            print(f"  {raw}")
            return None
        try:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                return json.loads(match.group())
        except json.JSONDecodeError:
            print(f"  [AI] JSON parse failed: {raw[:200]}")
        return None

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
