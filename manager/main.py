#!/usr/bin/env python3
"""Manager Agent — Creative Director Mode (Trend Curation)

Responsibilities:
  Tool A: screen_urgency  — Haiku 4.5 scores urgency 0-100 (cheap gate)
  Tool B: analyze_viral   — Sonnet 4.5 deep analysis (only if urgency >= 80)
  Tool C: deploy_token    — Deploy on monad.fun via factory contract
  Tool D: coordinate_market_making — Fund Trader and orchestrate flow

Cost optimization (Two-Stage Gatekeeper):
  Stage 0: Python fingerprint — skip if no data change (zero cost)
  Stage 1: Haiku 4.5 urgency score — skip Sonnet if < 80
  Stage 2: Sonnet 4.5 strategy — only on high-urgency signals
  Dynamic interval: 300s idle / 30s active

Model assignment (STRICT — NO CHANGES ALLOWED):
  Screening : claude-haiku-4-5-latest
  Strategy  : claude-sonnet-4-5-latest
"""

import os
import sys
import re
import time
import json
import signal
import hashlib
import traceback
import requests
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from shared.ai_client import AIClient, SCREENING_MODEL, STRATEGY_MODEL
from shared.chain import (
    get_web3, get_account, get_balance_mon, send_mon,
    deploy_token_monad_fun, NADFUN_ROUTER_DEFAULT,
    sanitize_token_name, validate_ticker, make_unique_ticker,
    run_deploy_health_check,
)
from shared.event_logger import log_deploy_decision, log_token_deployed


# ═══════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════
URGENCY_THRESHOLD = 60          # SUPER AGGRESSIVE: lowered from 80 → 60
DEPLOY_CONFIDENCE = 0.68        # SUPER AGGRESSIVE: lowered from 0.85 → 0.68
INTERVAL_IDLE = 120             # SUPER AGGRESSIVE: faster idle polling (was 300)
INTERVAL_ACTIVE = 15            # SUPER AGGRESSIVE: faster active polling (was 30)
MONAD_FUN_FACTORY = os.getenv(
    "MONAD_FUN_FACTORY_ADDRESS",
    NADFUN_ROUTER_DEFAULT,
)
INITIAL_LIQUIDITY_MON = float(os.getenv("INITIAL_LIQUIDITY_MON", "0.1"))
COORDINATION_FILE = Path("/tmp/manager_trader_coordination.json")


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
    return {
        "last_trend_hash": "",
        "deployed_tokens": [],
        "total_funded_mon": 0.0,
        "last_interval": INTERVAL_IDLE,
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ═══════════════════════════════════════════════
#  Data Collection (zero API cost)
# ═══════════════════════════════════════════════

_gmgn_browser_ctx = None


def _get_gmgn_page():
    """Launch (or reuse) a Chromium context that has passed Cloudflare."""
    global _gmgn_browser_ctx
    if _gmgn_browser_ctx is not None:
        try:
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
    page.goto("https://gmgn.ai/", timeout=30000)
    page.wait_for_selector("body", timeout=30000)
    page.wait_for_timeout(5000)
    _gmgn_browser_ctx = {"pw": pw, "browser": browser, "page": page}
    return page, True


def fetch_gmgn_trends() -> list[dict]:
    """Fetch trending tokens from GMGN via headless Chromium."""
    global _gmgn_browser_ctx
    url = os.getenv("GMGN_API_URL", "https://gmgn.ai/defi/quotation/v1/rank/monad/swaps/1h")
    try:
        page, _ = _get_gmgn_page()
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
        _gmgn_browser_ctx = None
        return []


def fetch_viral_trends() -> list[str]:
    """Fetch viral/trending topics from multiple sources.

    SUPER AGGRESSIVE: captures ALL viral words from every source.
    Genre-agnostic: sports, politics, entertainment, memes, weather — ALL valid.
    Zero AI cost.
    """
    trends = []

    # Source 1: Google Trends (US)
    try:
        resp = requests.get(
            "https://trends.google.com/trending/rss?geo=US",
            headers={"User-Agent": "MonadSwarm/2.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            titles = re.findall(r"<title>(.+?)</title>", resp.text)
            trends.extend(t for t in titles[1:30] if t != "Daily Search Trends")
    except Exception as e:
        print(f"  [Trends] Google Trends US failed: {e}")

    # Source 2: Google Trends (Global) — SUPER AGGRESSIVE: more sources
    try:
        resp = requests.get(
            "https://trends.google.com/trending/rss?geo=",
            headers={"User-Agent": "MonadSwarm/2.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            titles = re.findall(r"<title>(.+?)</title>", resp.text)
            trends.extend(t for t in titles[1:20] if t != "Daily Search Trends")
    except Exception as e:
        print(f"  [Trends] Google Trends Global failed: {e}")

    # Source 3: Twitter/X trending topics
    try:
        resp = requests.get(
            "https://trends24.in/united-states/",
            headers={"User-Agent": "MonadSwarm/2.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            hashtags = re.findall(r'<a[^>]*class="trend-link"[^>]*>([^<]+)</a>', resp.text)
            if not hashtags:
                hashtags = re.findall(r"#(\w+)", resp.text)
            trends.extend(h.strip("#").strip() for h in hashtags[:20])
    except Exception as e:
        print(f"  [Trends] X/Twitter trends failed: {e}")

    # Source 4: Twitter/X worldwide — SUPER AGGRESSIVE: even more data
    try:
        resp = requests.get(
            "https://trends24.in/",
            headers={"User-Agent": "MonadSwarm/2.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            hashtags = re.findall(r'<a[^>]*class="trend-link"[^>]*>([^<]+)</a>', resp.text)
            trends.extend(h.strip("#").strip() for h in hashtags[:15])
    except Exception as e:
        print(f"  [Trends] X/Twitter worldwide failed: {e}")

    # Deduplicate while preserving order — SUPER AGGRESSIVE: return more results
    seen = set()
    unique = []
    for t in trends:
        key = t.lower().strip()
        if key not in seen and len(key) > 1:
            seen.add(key)
            unique.append(t)
    return unique[:30]  # Increased from 20 → 30 for more coverage


def compute_trend_fingerprint(tokens: list, trends: list) -> str:
    """Create a hash to detect changes. Pure Python, zero cost."""
    raw = json.dumps(
        {"tokens": [str(t)[:50] for t in tokens[:5]], "trends": trends[:5]},
        sort_keys=True,
    )
    return hashlib.md5(raw.encode()).hexdigest()


def derive_ticker_candidates(trends: list[str]) -> list[str]:
    """Derive potential $TICKER symbols from viral words. Zero AI cost.

    SUPER AGGRESSIVE: generates more candidates from each trend.
    """
    tickers = []
    for word in trends:
        clean = re.sub(r"[^a-zA-Z0-9]", "", word).upper()
        if len(clean) >= 3:
            tickers.append(clean[:5])
        if len(clean) > 5:
            # Abbreviation: first letters of each word
            parts = word.split()
            if len(parts) >= 2:
                abbrev = "".join(p[0] for p in parts if p).upper()
                if 3 <= len(abbrev) <= 5:
                    tickers.append(abbrev)
            # SUPER AGGRESSIVE: also try first 3 letters as ticker
            tickers.append(clean[:3])
    # Deduplicate — return more candidates (was 10, now 20)
    seen = set()
    return [t for t in tickers if not (t in seen or seen.add(t))][:20]


# ═══════════════════════════════════════════════
#  Stage 1: Urgency Screening (Haiku 4.5)
# ═══════════════════════════════════════════════

def screen_urgency(ai: AIClient, tokens: list, trends: list, ticker_candidates: list) -> dict | None:
    """Stage 1: Haiku 4.5 scores urgency (0-100).

    If urgency < 80, the pipeline stops here. No Sonnet call.
    """
    token_summary = json.dumps(tokens[:5], indent=2, default=str)[:1500]
    trend_summary = ", ".join(trends[:15]) if trends else "(no trends detected)"
    ticker_summary = ", ".join(f"${t}" for t in ticker_candidates[:8])

    return ai.screen_json(
        system_prompt=(
            "You are a creative director who analyzes market trends and provides culturally valuable memes to the Monad ecosystem. "
            "Your mission is to tokenize the excitement of trending topics in their purest form and deliver them to the community. "
            "Respond ONLY with a pure JSON object."
        ),
        user_message=f"""Evaluate the cultural relevance and viral potential of current trends for meme token creation on Monad.
Our goal is to capture trending cultural moments and bring them on-chain for the community.

TRENDING TOKENS (GMGN, 1h):
{token_summary}

VIRAL TOPICS (Twitter/X + Google):
{trend_summary}

PRE-GENERATED TICKER CANDIDATES:
{ticker_summary}

Respond in JSON:
{{
  "urgency": 0-100,
  "top_viral_word": "the most culturally resonant trending word/phrase",
  "viral_potential": "HIGH | MEDIUM | LOW",
  "best_ticker": "$XXXXX from the candidates or a new one",
  "reason": "1 sentence explaining the cultural momentum behind this trend"
}}

Guidelines:
- Score 60+ for trends with clear cultural relevance or community resonance
- Score 80+ for trends showing strong viral momentum across multiple platforms
- Score 90+ for trends that are genuinely dominating public conversation
- All genres are valid — sports, politics, entertainment, memes, weather, celebrities, food trends
- Niche and emerging trends can be valuable as fresh cultural narratives
- Evaluate whether the trend has enough momentum to sustain community interest
- Non-crypto cultural moments are especially interesting as untapped on-chain narratives""",
        max_tokens=256,
    )


# ═══════════════════════════════════════════════
#  Stage 2: Strategy Analysis (Sonnet 4.5)
# ═══════════════════════════════════════════════

def analyze_strategy(ai: AIClient, tokens: list, trends: list, screening: dict) -> dict | None:
    """Stage 2: Sonnet 4.5 deep analysis.

    ONLY called when Haiku screening urgency >= 80.
    """
    token_summary = json.dumps(tokens[:5], indent=2, default=str)[:2000]
    trend_summary = ", ".join(trends[:15]) if trends else "(none)"

    return ai.strategize_json(
        system_prompt=(
            "You are a creative director for the Monad meme token ecosystem. "
            "You analyze cultural trends and decide whether to tokenize them, "
            "capturing the purest form of trending excitement for the community. "
            "Respond ONLY with a pure JSON object."
        ),
        user_message=f"""You are the Creative Director for Monad meme token launches.
Analyze the current cultural landscape and decide the best course of action.

SCREENING RESULT:
- Urgency: {screening.get('urgency', 'N/A')}/100
- Top viral word: {screening.get('top_viral_word', 'N/A')}
- Viral potential: {screening.get('viral_potential', 'N/A')}
- Suggested ticker: {screening.get('best_ticker', 'N/A')}
- Reason: {screening.get('reason', 'N/A')}

TRENDING TOKENS (GMGN):
{token_summary}

VIRAL TOPICS:
{trend_summary}

Decide the optimal action. Respond in JSON:
{{
  "action": "DEPLOY | MONITOR | WAIT",
  "confidence": 0.0-1.0,
  "token_name": "catchy memecoin name with viral appeal",
  "ticker": "3-5 char ticker (e.g. $DOGE, $PEPE)",
  "description": "1-2 sentence token description for monad.fun listing",
  "narrative": "the core narrative driving this token",
  "reasoning": "2-3 sentences on why this action",
  "viral_score": 0.0-1.0,
  "estimated_hype_window_hours": 1-48
}}

Guidelines:
- Output action=DEPLOY if confidence > 0.68 and cultural momentum is strong
- Prefer DEPLOY when a trend has clear community resonance and viral potential
- Moderate viral signals with strong cultural narratives are worth deploying
- If urgency was scored 60+, the trend likely has enough momentum to deploy
- Output WAIT only if there is genuinely no cultural moment worth capturing
- Token name should be catchy, memeable, and immediately recognizable
- Ticker must be 3-5 uppercase letters
- Focus on capturing cultural moments that the community will rally around""",
        max_tokens=768,
    )


# ═══════════════════════════════════════════════
#  Tool: deploy_token (monad.fun on-chain)
# ═══════════════════════════════════════════════

def _get_deployed_tickers(state: dict) -> set[str]:
    """Collect all tickers already deployed to avoid collisions."""
    return {t.get("ticker", "") for t in state.get("deployed_tokens", [])}


def deploy_token(w3, account, token_name: str, ticker: str, description: str, state: dict | None = None) -> dict:
    """Deploy a token on Nad.fun via BondingCurveRouter.create().

    Uses wallet MON for initial liquidity seeding.
    Applies proactive deduplication: always appends YY+2random to ticker
    (e.g., HINT → HINT26XY) to avoid 0x8d6be2a7 symbol duplicate revert.
    Returns {"success": bool, "token_address": str|None, "tx_hash": str|None, "error": str|None}
    """
    # ── Sanitize parameters ──
    original_name = token_name
    token_name = sanitize_token_name(token_name)
    if token_name != original_name:
        print(f"  [DEPLOY] Token name truncated: '{original_name}' → '{token_name}'")

    try:
        existing = _get_deployed_tickers(state) if state else set()
        original_ticker = ticker
        ticker = make_unique_ticker(ticker, existing)
        print(f"  [DEPLOY] Ticker uniquified: ${original_ticker} → ${ticker}")
    except ValueError as e:
        return {"success": False, "token_address": None, "tx_hash": None, "error": str(e)}

    factory_addr = MONAD_FUN_FACTORY
    if not factory_addr or factory_addr == "0x0000000000000000000000000000000000000000":
        print("  [DEPLOY] Nad.fun factory address not configured. Set MONAD_FUN_FACTORY_ADDRESS in .env")
        return {"success": False, "token_address": None, "tx_hash": None, "error": "Factory not configured"}

    balance = get_balance_mon(w3, account.address)
    liquidity = min(INITIAL_LIQUIDITY_MON, balance * 0.1)
    if liquidity < 0.001:
        return {"success": False, "token_address": None, "tx_hash": None, "error": f"Insufficient MON ({balance:.4f})"}

    print(f"  [DEPLOY] Token: {token_name} (${ticker})")
    print(f"  [DEPLOY] Factory: {factory_addr[:16]}...")
    print(f"  [DEPLOY] Initial liquidity: {liquidity:.4f} MON")

    result = deploy_token_monad_fun(
        w3, account, factory_addr,
        token_name, ticker, description,
        initial_liquidity_mon=liquidity,
    )

    if result["success"]:
        print(f"  [DEPLOY] SUCCESS! Token: {result.get('token_address', 'pending')}")
        print(f"  [DEPLOY] Tx: {result.get('tx_hash', 'N/A')}")
    else:
        print(f"  [DEPLOY] FAILED: {result.get('error', 'unknown')}")

    return result


# ═══════════════════════════════════════════════
#  Tool: coordinate_market_making
# ═══════════════════════════════════════════════

def coordinate_market_making(
    ai: AIClient,
    w3,
    account,
    trader_address: str,
    token_address: str,
    amount_mon: float,
) -> dict:
    """Fund the Trader and instruct initial liquidity support (buy)."""
    max_fund = float(os.getenv("MAX_FUND_AMOUNT_MON", 0.5))
    amount_mon = min(amount_mon, max_fund)

    print(f"  [COORD] Sending {amount_mon:.4f} MON to Trader {trader_address[:10]}...")

    try:
        tx_hash = send_mon(w3, account, trader_address, amount_mon)
        print(f"  [COORD] Funding tx: {tx_hash}")

        memo = ai.screen(
            system_prompt="You are a fund manager. Be concise (2-3 lines max).",
            user_message=(
                f"Compose a brief operational memo for the Trader agent:\n"
                f"- Sent {amount_mon} MON to trader wallet\n"
                f"- Target token: {token_address}\n"
                f"- Strategy: Buy immediately for initial liquidity support, 15% stop-loss, take profit at 2x\n"
                f"- Return profits minus 20% fee to manager"
            ),
            max_tokens=200,
        )

        return {
            "success": True,
            "funding_tx": tx_hash,
            "amount_mon": amount_mon,
            "trader": trader_address,
            "token_address": token_address,
            "memo": memo,
        }

    except Exception as e:
        print(f"  [COORD] Error: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════
#  Startup Test Deployment
# ═══════════════════════════════════════════════

def _generate_test_ticker() -> str:
    """Generate a unique test ticker using the YY+2random convention.

    Example: TST → TST26AB (ensures uniqueness via make_unique_ticker).
    """
    return make_unique_ticker("TST")


# Known revert selectors that indicate a duplicate / already-exists condition
_DUPLICATE_REVERT_SELECTORS = {"0x8d6be2a7"}


def _is_duplicate_revert(error_msg: str) -> bool:
    """Check whether a revert error message is likely caused by symbol duplication."""
    if not error_msg:
        return False
    err_lower = error_msg.lower()
    # Check for known selectors
    for sel in _DUPLICATE_REVERT_SELECTORS:
        if sel in err_lower:
            return True
    # Also match common duplicate/exists patterns
    duplicate_hints = ["already exists", "duplicate", "symbol exists", "token exists"]
    return any(hint in err_lower for hint in duplicate_hints)


def test_deployment(w3, account) -> bool:
    """Execute a one-time test token deployment at startup.

    Deploys a dummy token on monad.fun with a randomized ticker
    ($TSxxxxx) to verify the full pipeline end-to-end.
    If the deployment reverts due to a duplicate symbol, this is
    treated as non-fatal (logged and returns True).
    Returns True if the test deployment succeeded or was a known duplicate.
    """
    test_ticker = _generate_test_ticker()
    test_name = f"Monad Swarm Test {test_ticker}"

    sep = "=" * 60
    print(f"\n{sep}")
    print("  [TEST DEPLOY] Starting startup test deployment...")
    print(f"  [TEST DEPLOY] Token : {test_name} (${test_ticker})")
    print(f"  [TEST DEPLOY] Factory: {MONAD_FUN_FACTORY[:16]}...")
    print(sep)

    result = deploy_token_monad_fun(
        w3=w3,
        account=account,
        factory_address=MONAD_FUN_FACTORY,
        token_name=test_name,
        ticker=test_ticker,
        description="Startup test deployment by Monad Swarm Manager. This token verifies the deployment pipeline is operational.",
        initial_liquidity_mon=INITIAL_LIQUIDITY_MON,
    )

    print(f"\n{sep}")
    if result["success"]:
        print("  [TEST DEPLOY] *** SUCCESS ***")
        print(f"  [TEST DEPLOY] Token Address : {result.get('token_address', 'pending log extraction')}")
        print(f"  [TEST DEPLOY] Tx Hash       : {result.get('tx_hash', 'N/A')}")
        print(sep + "\n")
        return True

    # Check if the failure is a known duplicate revert — treat as non-fatal
    error_msg = result.get("error", "")
    if _is_duplicate_revert(error_msg):
        print("  [TEST DEPLOY] *** DUPLICATE DETECTED (non-fatal) ***")
        print(f"  [TEST DEPLOY] The ticker ${test_ticker} or a similar symbol already exists on-chain.")
        print(f"  [TEST DEPLOY] Error: {error_msg}")
        print(f"  [TEST DEPLOY] Pipeline connectivity is confirmed — proceeding.")
        print(sep + "\n")
        return True

    print("  [TEST DEPLOY] *** FAILED ***")
    print(f"  [TEST DEPLOY] Error         : {error_msg}")
    if result.get("tx_hash"):
        print(f"  [TEST DEPLOY] Tx Hash       : {result['tx_hash']}")
    print(sep + "\n")

    return False


# ═══════════════════════════════════════════════
#  Main Loop — Two-Stage Gatekeeper + Aggressive Issuer
# ═══════════════════════════════════════════════

def main():
    banner = f"""
╔════════════════════════════════════════════════════════════╗
║   Manager Agent — Creative Director Mode                   ║
║   自律分散型ベンチャーDAO v2 — トレンドキュレーター       ║
║                                                            ║
║   Two-Stage AI:                                            ║
║     Stage 1: {SCREENING_MODEL:<30s}        ║
║     Stage 2: {STRATEGY_MODEL:<30s}        ║
║   Dynamic Interval: {INTERVAL_ACTIVE}s active / {INTERVAL_IDLE}s idle             ║
║   Deploy threshold: confidence > {DEPLOY_CONFIDENCE}                      ║
║   Urgency gate    : {URGENCY_THRESHOLD}/100                                ║
╚════════════════════════════════════════════════════════════╝"""
    print(banner)

    trader_address = os.getenv("TARGET_TRADER_ADDRESS", "")

    ai = AIClient(role="MANAGER")
    w3 = get_web3()
    account = get_account()
    state = load_state()

    print(f"  Wallet     : {account.address}")
    print(f"  Balance    : {get_balance_mon(w3, account.address):.4f} MON")
    print(f"  Trader     : {trader_address or '(not configured)'}")
    print(f"  Screening  : {ai.screening_model}")
    print(f"  Strategy   : {ai.strategy_model}")
    print(f"  Budget     : {ai.guard.remaining} API calls remaining today")
    print(f"  Factory    : {MONAD_FUN_FACTORY[:16]}...")
    print(f"  Liquidity  : {INITIAL_LIQUIDITY_MON} MON per deploy")
    print("─" * 60)

    # ── Deploy Health Check (runs once at startup) ────────
    print("\n[HEALTH CHECK] Running deployment dry-run...")
    hc = run_deploy_health_check(w3, account, MONAD_FUN_FACTORY)
    if hc["ok"]:
        print(f"[HEALTH CHECK] PASSED: {hc['details']}")
    else:
        print(f"[HEALTH CHECK] FAILED: {hc['details']}")
        print("[HEALTH CHECK] Aborting — fix the issue above and restart.")
        sys.exit(1)
    print("─" * 60)

    # ── Startup Test Deployment (runs once) ────────────────
    test_ok = test_deployment(w3, account)
    if test_ok:
        print("[STARTUP] Test deployment verified — pipeline is LIVE.")
    else:
        print("[STARTUP] Test deployment failed — check logs above.")
        print("[STARTUP] Continuing to main loop (test failure is non-fatal).")
    print("─" * 60)

    cycle = 0
    current_interval = state.get("last_interval", INTERVAL_IDLE)

    while RUNNING:
        cycle += 1
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")

        try:
            # ── Phase 0: Gather data (FREE) ──────────────────────
            tokens = fetch_gmgn_trends()
            trends = fetch_viral_trends()
            ticker_candidates = derive_ticker_candidates(trends)
            fingerprint = compute_trend_fingerprint(tokens, trends)

            # ── Phase 1: Fingerprint gate (zero cost) ────────────
            if fingerprint == state.get("last_trend_hash", ""):
                current_interval = min(current_interval + 30, INTERVAL_IDLE)
                if cycle % 5 == 1:
                    print(f"[{now}] Cycle {cycle} — No data change. Interval: {current_interval}s")
                time.sleep(current_interval)
                continue

            # Change detected → accelerate polling
            current_interval = INTERVAL_ACTIVE
            state["last_trend_hash"] = fingerprint
            state["last_interval"] = current_interval

            print(f"\n[{now}] ═══ Cycle {cycle} — CHANGE DETECTED (fp: {fingerprint[:8]}) ═══")
            print(f"  Viral topics  : {', '.join(trends[:8]) if trends else '(none)'}")
            print(f"  GMGN tokens   : {len(tokens)} trending")
            print(f"  Ticker candidates: {', '.join('$'+t for t in ticker_candidates[:6])}")

            # ── Phase 2: Stage 1 — Haiku Screening ──────────────
            print(f"\n  [STAGE 1] Urgency screening via {SCREENING_MODEL}...")
            screening = screen_urgency(ai, tokens, trends, ticker_candidates)

            if not screening:
                print("  [STAGE 1] Screening failed (budget or parse error). Skipping.")
                save_state(state)
                time.sleep(current_interval)
                continue

            urgency = screening.get("urgency", 0)
            viral_potential = screening.get("viral_potential", "LOW")
            best_ticker = screening.get("best_ticker", "N/A")
            top_word = screening.get("top_viral_word", "N/A")

            print(f"  [STAGE 1] Urgency    : {urgency}/100")
            print(f"  [STAGE 1] Viral      : {viral_potential}")
            print(f"  [STAGE 1] Top word   : {top_word}")
            print(f"  [STAGE 1] Best ticker: {best_ticker}")
            print(f"  [STAGE 1] Reason     : {screening.get('reason', 'N/A')}")

            if urgency < URGENCY_THRESHOLD:
                print(f"  [GATE] Urgency {urgency} < {URGENCY_THRESHOLD}. Sonnet NOT invoked. Cost saved.")
                print(f"  [VIRAL] Potential: {viral_potential} | ${best_ticker} from '{top_word}'")
                current_interval = min(INTERVAL_ACTIVE * 3, INTERVAL_IDLE)
                save_state(state)
                time.sleep(current_interval)
                continue

            # ── Phase 3: Stage 2 — Sonnet Strategy ──────────────
            print(f"\n  [STAGE 2] Deep analysis via {STRATEGY_MODEL}...")
            strategy = analyze_strategy(ai, tokens, trends, screening)

            if not strategy:
                print("  [STAGE 2] Strategy failed. Skipping.")
                save_state(state)
                time.sleep(current_interval)
                continue

            action = strategy.get("action", "WAIT")
            confidence = strategy.get("confidence", 0)
            viral_score = strategy.get("viral_score", 0)
            token_name = sanitize_token_name(strategy.get("token_name", "MonadMeme"))
            try:
                ticker = validate_ticker(strategy.get("ticker", "MEME"))
            except ValueError:
                ticker = "MEME"
            narrative = strategy.get("narrative", "N/A")
            hype_window = strategy.get("estimated_hype_window_hours", "?")

            print(f"  [STAGE 2] Action     : {action}")
            print(f"  [STAGE 2] Confidence : {confidence}")
            print(f"  [STAGE 2] Viral score: {viral_score}")
            print(f"  [STAGE 2] Token      : {token_name} (${ticker})")
            print(f"  [STAGE 2] Narrative  : {narrative}")
            print(f"  [STAGE 2] Hype window: ~{hype_window}h")
            print(f"  [STAGE 2] Reasoning  : {strategy.get('reasoning', 'N/A')}")

            # ── Phase 4: Execute action ──────────────────────────
            if action == "DEPLOY" and confidence >= DEPLOY_CONFIDENCE:
                description = strategy.get("description", f"AI-minted token inspired by: {narrative}")

                print(f"\n  >>> DEPLOY TRIGGERED: {token_name} (${ticker})")
                print(f"  >>> Confidence: {confidence} | Viral: {viral_score}")
                log_deploy_decision(ticker, token_name, confidence, strategy.get("reasoning", "N/A"))

                result = deploy_token(w3, account, token_name, ticker, description, state=state)

                if result["success"]:
                    token_addr = result.get("token_address")
                    log_token_deployed(ticker, token_addr or "unknown", result.get("tx_hash", "N/A"))
                    state["deployed_tokens"].append({
                        "name": token_name,
                        "ticker": ticker,
                        "narrative": narrative,
                        "confidence": confidence,
                        "viral_score": viral_score,
                        "time": datetime.now(timezone.utc).isoformat(),
                        "address": token_addr,
                        "tx_hash": result.get("tx_hash"),
                    })

                    # Coordinate with Trader for initial liquidity support
                    if token_addr and trader_address:
                        balance = get_balance_mon(w3, account.address)
                        # SUPER AGGRESSIVE: fund up to 15% of balance (was 10%), higher cap (was 0.5)
                        fund_amount = min(balance * 0.15, float(os.getenv("MAX_FUND_AMOUNT_MON", 5.0)))
                        if fund_amount > 0.01:
                            # Write coordination file BEFORE funding so Trader knows what to buy
                            coord_data = {
                                "token_address": token_addr,
                                "token_name": token_name,
                                "ticker": ticker,
                                "narrative": narrative,
                                "fund_amount_mon": fund_amount,
                                "deploy_time": datetime.now(timezone.utc).isoformat(),
                                "status": "PENDING_BUY",
                            }
                            COORDINATION_FILE.write_text(json.dumps(coord_data, indent=2))
                            print(f"  [COORD] Coordination file written: {token_addr}")

                            coord = coordinate_market_making(
                                ai, w3, account, trader_address, token_addr, fund_amount
                            )
                            if coord.get("success"):
                                state["total_funded_mon"] += fund_amount
                                print(f"  [COORD] Trader funded: {fund_amount:.4f} MON for initial buy support")
                            else:
                                print(f"  [COORD] Funding failed: {coord.get('error', 'unknown')}")
                        else:
                            print("  [COORD] Insufficient balance to fund Trader.")
                    elif not token_addr:
                        print("  [COORD] Token address not captured — manual coordination needed.")
                else:
                    print(f"  [DEPLOY] Failed: {result.get('error')}")

            elif action == "MONITOR":
                print(f"  [ACTION] MONITOR — Watching '{top_word}' (${ticker}) for escalation")
                print(f"  [VIRAL] Potential: {viral_potential} | Confidence: {confidence}")
            else:
                print(f"  [ACTION] WAIT — Confidence {confidence} below {DEPLOY_CONFIDENCE} threshold")
                print(f"  [VIRAL] Tracking: '{top_word}' | Potential: ${ticker}")

            save_state(state)

        except Exception as exc:
            print(f"\n[{now}] [ERROR] Cycle {cycle} — Unhandled exception: {exc}")
            traceback.print_exc()
            print(f"  Recovering... will resume in {current_interval}s")
            save_state(state)

        time.sleep(current_interval)

    # Shutdown summary
    print("\n[Manager] Stopped.")
    print(f"  Total deployed : {len(state.get('deployed_tokens', []))} tokens")
    print(f"  Total funded   : {state.get('total_funded_mon', 0):.4f} MON")
    print(f"  API calls used : {ai.guard.count}/{ai.guard.max_daily}")


if __name__ == "__main__":
    main()
