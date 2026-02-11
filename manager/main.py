#!/usr/bin/env python3
"""Manager Agent — The Venture Protocol

Responsibilities:
  Tool A: analyze_narrative — Scrape trends, have Claude evaluate narrative potential
  Tool B: deploy_token_on_nad — Deploy tokens on nad.fun via headless Chromium
  Tool C: coordinate_market_making — Fund the Trader and orchestrate the flow

Cost optimization (The Gatekeeper):
  Python-level pre-filters run BEFORE any API call.
  Only significant trend changes trigger Claude reasoning.
  No change = no API call = zero cost.
"""

import os
import sys
import re
import time
import json
import signal
import hashlib
import requests
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from shared.ai_client import AIClient
from shared.chain import get_web3, get_account, get_balance_mon, send_mon


# ═══════════════════════════════════════════════
#  State tracking (persistent across restarts)
# ═══════════════════════════════════════════════
STATE_FILE = Path("/tmp/manager_state.json")
RUNNING = True


def signal_handler(sig, frame):
    global RUNNING
    print("\n[Manager] Shutting down gracefully...")
    RUNNING = False


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, KeyError):
            pass
    return {"last_trend_hash": "", "deployed_tokens": [], "total_funded_mon": 0.0}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ═══════════════════════════════════════════════
#  Tool A: analyze_narrative
# ═══════════════════════════════════════════════

_gmgn_browser_ctx = None  # reuse across calls to avoid re-solving challenge


def _get_gmgn_page():
    """Launch (or reuse) a Chromium context that has passed Cloudflare."""
    global _gmgn_browser_ctx
    if _gmgn_browser_ctx is not None:
        try:
            # Check the context is still alive
            _gmgn_browser_ctx["page"].title()
            return _gmgn_browser_ctx["page"], False
        except Exception:
            _gmgn_browser_ctx = None

    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True,
        executable_path=os.getenv("CHROMIUM_PATH") or None,
        args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
    )
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    )
    page = context.new_page()
    # Visit main page and wait for Cloudflare challenge to resolve
    page.goto("https://gmgn.ai/", timeout=30000)
    # Wait until the real page loads (Cloudflare challenge disappears)
    page.wait_for_selector("body", timeout=30000)
    page.wait_for_timeout(5000)
    _gmgn_browser_ctx = {"pw": pw, "browser": browser, "page": page}
    return page, True


def fetch_gmgn_trends() -> list[dict]:
    """Fetch trending tokens from GMGN via headless Chromium (bypasses Cloudflare)."""
    global _gmgn_browser_ctx
    url = os.getenv("GMGN_API_URL", "https://gmgn.ai/defi/quotation/v1/rank/monad/swaps/1h")
    try:
        page, _ = _get_gmgn_page()
        # Use in-page fetch so cookies/CF clearance carry over
        raw = page.evaluate(
            """async (url) => {
                const r = await fetch(url, {headers: {"Accept": "application/json"}});
                return await r.text();
            }""",
            url,
        )
        data = json.loads(raw)
        ranks = data.get("data", {}).get("rank", [])
        if not ranks:
            ranks = data.get("data", [])
        return ranks[:10] if isinstance(ranks, list) else []
    except Exception as e:
        print(f"  [GMGN] Fetch failed: {e}")
        # Invalidate browser context on failure so next call retries
        _gmgn_browser_ctx = None
        return []


def fetch_x_trends() -> list[str]:
    """Fetch trending topics from Google Trends RSS. Zero AI cost."""
    try:
        resp = requests.get(
            "https://trends.google.com/trending/rss?geo=US",
            headers={"User-Agent": "MonadSwarm/1.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            titles = re.findall(r"<title>(.+?)</title>", resp.text)
            return [t for t in titles[1:11] if t != "Daily Search Trends"]
    except Exception as e:
        print(f"  [Trends] Google Trends fetch failed: {e}")
    return []


def compute_trend_fingerprint(tokens: list, trends: list) -> str:
    """Create a hash to detect changes. Pure Python, zero cost."""
    raw = json.dumps(
        {"tokens": [str(t)[:50] for t in tokens[:5]], "trends": trends[:5]},
        sort_keys=True,
    )
    return hashlib.md5(raw.encode()).hexdigest()


def analyze_narrative(ai: AIClient, tokens: list, trends: list) -> dict | None:
    """Tool A: Have Claude Sonnet 4.5 analyze narrative potential.

    ONLY called when the Gatekeeper detects changes.
    """
    token_summary = json.dumps(tokens[:5], indent=2, default=str)[:2000]
    trend_summary = ", ".join(trends[:10]) if trends else "No trend data available"

    return ai.think_json(
        system_prompt="You are a crypto narrative analyst. Respond ONLY with valid JSON.",
        user_message=f"""You are a crypto narrative analyst for the Monad ecosystem.

TRENDING TOKENS (GMGN top movers, last 1h):
{token_summary}

TRENDING TOPICS (Google/X):
{trend_summary}

Analyze and respond in JSON:
{{
  "top_narrative": "the strongest emerging narrative",
  "confidence": 0.0-1.0,
  "suggested_token_name": "catchy memecoin name if confidence > 0.7",
  "suggested_ticker": "3-5 char ticker",
  "reasoning": "1-2 sentences",
  "action": "DEPLOY_TOKEN | WAIT | MONITOR"
}}

Rules:
- Only recommend DEPLOY_TOKEN if confidence >= 0.75
- Be extremely selective. Most of the time, WAIT is correct.
- Focus on narratives with viral meme potential on Monad.""",
        max_tokens=512,
    )


# ═══════════════════════════════════════════════
#  Tool B: deploy_token_on_nad
# ═══════════════════════════════════════════════

def deploy_token_on_nad(token_name: str, ticker: str, description: str) -> dict:
    """Deploy a token on nad.fun using Playwright (headless Chromium).

    Returns {"success": bool, "token_address": str|None, "error": str|None}
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "success": False, "token_address": None,
            "error": "playwright not installed. Run: pip install playwright && playwright install chromium",
        }

    print(f"  [NAD] Deploying token: {token_name} ({ticker})")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path=os.getenv("CHROMIUM_PATH") or None,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
            )
            page = context.new_page()

            # Navigate to nad.fun token creation page
            page.goto("https://nad.fun/create", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)

            try:
                # Fill token name
                name_input = page.locator(
                    'input[name="name"], input[placeholder*="name" i]'
                ).first
                name_input.fill(token_name)

                # Fill ticker/symbol
                ticker_input = page.locator(
                    'input[name="ticker"], input[name="symbol"], '
                    'input[placeholder*="ticker" i], input[placeholder*="symbol" i]'
                ).first
                ticker_input.fill(ticker)

                # Fill description
                desc_input = page.locator(
                    'textarea[name="description"], textarea[placeholder*="description" i]'
                ).first
                desc_input.fill(description)

                # Click deploy/create button
                deploy_btn = page.locator(
                    'button:has-text("Create"), button:has-text("Deploy"), button:has-text("Launch")'
                ).first
                deploy_btn.click()

                # Wait for on-chain confirmation
                page.wait_for_timeout(10000)

                # Attempt to extract the new token address from URL or page content
                current_url = page.url
                token_address = None
                # nad.fun typically redirects to /token/<address>
                addr_match = re.search(r"0x[a-fA-F0-9]{40}", current_url)
                if addr_match:
                    token_address = addr_match.group()

                print(f"  [NAD] Post-deploy URL: {current_url}")
                browser.close()
                return {
                    "success": True,
                    "token_address": token_address,
                    "url": current_url,
                    "error": None,
                }

            except Exception as e:
                browser.close()
                return {"success": False, "token_address": None, "error": f"DOM interaction failed: {e}"}

    except Exception as e:
        return {"success": False, "token_address": None, "error": str(e)}


# ═══════════════════════════════════════════════
#  Tool C: coordinate_market_making
# ═══════════════════════════════════════════════

def coordinate_market_making(
    ai: AIClient,
    w3,
    account,
    trader_address: str,
    token_address: str,
    amount_mon: float,
) -> dict:
    """Fund the Trader and instruct them to buy.

    Flow:
      1. Manager sends MON to Trader (on-chain tx)
      2. Trader detects deposit in their own loop and acts autonomously
      3. AI generates a brief coordination memo
    """
    max_fund = float(os.getenv("MAX_FUND_AMOUNT_MON", 0.5))
    amount_mon = min(amount_mon, max_fund)

    print(f"  [COORD] Sending {amount_mon:.4f} MON to Trader {trader_address[:10]}...")

    try:
        tx_hash = send_mon(w3, account, trader_address, amount_mon)
        print(f"  [COORD] Funding tx: {tx_hash}")

        memo = ai.think(
            system_prompt="You are a fund manager. Be concise (2-3 lines max).",
            user_message=(
                f"Compose a brief operational memo for the Trader agent:\n"
                f"- Sent {amount_mon} MON to trader wallet\n"
                f"- Target token: {token_address}\n"
                f"- Strategy: Buy on dip, 15% stop-loss, take profit at 2x\n"
                f"- Return profits minus 20% fee to manager"
            ),
            max_tokens=200,
        )

        return {
            "success": True,
            "funding_tx": tx_hash,
            "amount_mon": amount_mon,
            "trader": trader_address,
            "memo": memo,
        }

    except Exception as e:
        print(f"  [COORD] Error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════
#  Main Loop — The Gatekeeper Pattern
# ═══════════════════════════════════════════════

def main():
    banner = """
╔════════════════════════════════════════════════════════╗
║   Manager Agent — The Venture Protocol                ║
║   自律分散型ベンチャーDAO                             ║
║                                                        ║
║   Cost-optimized: API calls only on significant        ║
║   trend changes. Zero-cost polling otherwise.          ║
╚════════════════════════════════════════════════════════╝"""
    print(banner)

    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", 60))
    trader_address = os.getenv("TARGET_TRADER_ADDRESS", "")

    # Initialize components
    ai = AIClient(role="MANAGER")
    w3 = get_web3()
    account = get_account()
    state = load_state()

    print(f"  Wallet  : {account.address}")
    print(f"  Balance : {get_balance_mon(w3, account.address):.4f} MON")
    print(f"  Trader  : {trader_address or '(not configured)'}")
    print(f"  Model   : {ai.model}")
    print(f"  Budget  : {ai.guard.remaining} API calls remaining today")
    print(f"  Interval: {poll_interval}s")
    print("─" * 56)

    cycle = 0
    while RUNNING:
        cycle += 1
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")

        # ── Phase 1: Gather data (FREE — zero API cost) ──
        tokens = fetch_gmgn_trends()
        trends = fetch_x_trends()
        fingerprint = compute_trend_fingerprint(tokens, trends)

        # ── Phase 2: THE GATEKEEPER — skip API if nothing changed ──
        if fingerprint == state.get("last_trend_hash", ""):
            if cycle % 10 == 1:
                print(f"[{now}] Cycle {cycle} — No significant change. Sleeping...")
            time.sleep(poll_interval)
            continue

        # ── Phase 3: Change detected → invoke AI (costs money) ──
        print(f"\n[{now}] Cycle {cycle} — Change detected (fp: {fingerprint[:8]}...)")
        state["last_trend_hash"] = fingerprint

        analysis = analyze_narrative(ai, tokens, trends)
        if not analysis:
            save_state(state)
            time.sleep(poll_interval)
            continue

        action = analysis.get("action", "WAIT")
        confidence = analysis.get("confidence", 0)
        print(f"  Decision : {action} (confidence: {confidence})")
        print(f"  Narrative: {analysis.get('top_narrative', 'N/A')}")
        print(f"  Reasoning: {analysis.get('reasoning', 'N/A')}")

        # ── Phase 4: Execute action ──
        if action == "DEPLOY_TOKEN" and confidence >= 0.75:
            token_name = analysis.get("suggested_token_name", "MonadMeme")
            ticker = analysis.get("suggested_ticker", "MEME")
            reasoning = analysis.get("reasoning", "AI-detected trend")

            print(f"  >>> Deploying: {token_name} ({ticker})")
            result = deploy_token_on_nad(token_name, ticker, reasoning)

            if result["success"]:
                state["deployed_tokens"].append({
                    "name": token_name,
                    "ticker": ticker,
                    "time": datetime.now(timezone.utc).isoformat(),
                    "address": result.get("token_address"),
                    "url": result.get("url"),
                })

                # Coordinate market making if token address is available
                token_addr = result.get("token_address")
                if token_addr and trader_address:
                    balance = get_balance_mon(w3, account.address)
                    fund_amount = min(balance * 0.1, float(os.getenv("MAX_FUND_AMOUNT_MON", 0.5)))
                    if fund_amount > 0.01:
                        coord = coordinate_market_making(
                            ai, w3, account, trader_address, token_addr, fund_amount
                        )
                        state["total_funded_mon"] += fund_amount if coord.get("success") else 0
                        print(f"  Coordination: {'OK' if coord.get('success') else 'FAILED'}")
                    else:
                        print("  Insufficient balance to fund Trader.")
                elif not token_addr:
                    print("  Token address not captured — manual coordination needed.")
            else:
                print(f"  Deploy failed: {result.get('error')}")

        elif action == "MONITOR":
            print("  Monitoring — will re-check next cycle.")
        else:
            print("  Waiting — no action taken.")

        save_state(state)
        time.sleep(poll_interval)

    print("\n[Manager] Stopped.")
    print(f"  Total deployed: {len(state.get('deployed_tokens', []))} tokens")
    print(f"  Total funded:   {state.get('total_funded_mon', 0):.4f} MON")


if __name__ == "__main__":
    main()
