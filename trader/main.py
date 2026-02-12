#!/usr/bin/env python3
"""Trader Agent — SUPER AGGRESSIVE Executor (Hackathon Mode)

Responsibilities:
  1. Monitor for incoming MON deposits from the Manager
  2. On deposit: use Claude Haiku 4.5 to decide buy/reject
  3. Execute swaps via DEX, track P&L, take profit / stop loss
  4. Return profits (minus fee) to Manager (Profit Sharing)
  5. [NEW] AUTONOMOUS SNIPE & SCALP: independently discover and trade trending tokens

Cost optimization (The Gatekeeper):
  Only calls Haiku when a deposit arrives, price moves sharply, or autonomous scan finds opportunity.
  All monitoring is pure Python + on-chain reads — zero API cost.
"""

import os
import sys
import time
import json
import signal
import requests
import re
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from shared.ai_client import AIClient
from shared.chain import (
    get_web3, get_account, get_balance_mon,
    send_mon, check_incoming_transfers,
    swap_mon_for_token, swap_token_for_mon,
    ERC20_ABI,
)
from shared.event_logger import log_initial_swap
from web3 import Web3


# ═══════════════════════════════════════════════
#  State tracking
# ═══════════════════════════════════════════════
STATE_FILE = Path("/tmp/trader_state.json")
RUNNING = True

DEX_ROUTER = os.getenv("DEX_ROUTER_ADDRESS", "0x0000000000000000000000000000000000000000")
COORDINATION_FILE = Path("/tmp/manager_trader_coordination.json")


def signal_handler(sig, frame):
    global RUNNING
    print("\n[Trader] Shutting down gracefully...")
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
        "last_scanned_block": 0,
        "positions": [],
        "total_profit_mon": 0.0,
        "total_trades": 0,
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ═══════════════════════════════════════════════
#  Position tracking
# ═══════════════════════════════════════════════

class Position:
    """Tracks a single token position."""

    def __init__(self, token_address: str, entry_mon: float, entry_block: int):
        self.token_address = token_address
        self.entry_mon = entry_mon
        self.entry_block = entry_block
        self.entry_time = datetime.now(timezone.utc).isoformat()
        self.status = "OPEN"

    def to_dict(self) -> dict:
        return vars(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        p = cls(d["token_address"], d["entry_mon"], d["entry_block"])
        p.entry_time = d.get("entry_time", p.entry_time)
        p.status = d.get("status", "OPEN")
        return p


# ═══════════════════════════════════════════════
#  Price monitoring (zero API cost)
# ═══════════════════════════════════════════════

def read_coordination() -> dict | None:
    """Read the Manager's coordination file for target token info."""
    if COORDINATION_FILE.exists():
        try:
            data = json.loads(COORDINATION_FILE.read_text())
            if data.get("status") == "PENDING_BUY" and data.get("token_address"):
                return data
        except (json.JSONDecodeError, KeyError):
            pass
    return None


def mark_coordination_done():
    """Mark the coordination file as consumed."""
    if COORDINATION_FILE.exists():
        try:
            data = json.loads(COORDINATION_FILE.read_text())
            data["status"] = "EXECUTED"
            COORDINATION_FILE.write_text(json.dumps(data, indent=2))
        except Exception:
            pass


# ═══════════════════════════════════════════════
#  Autonomous Snipe & Scalp (SUPER AGGRESSIVE)
# ═══════════════════════════════════════════════

_gmgn_browser_ctx = None


def _get_gmgn_page():
    """Launch (or reuse) a Chromium context for GMGN."""
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


def fetch_trending_tokens_for_snipe() -> list[dict]:
    """Fetch top trending tokens from GMGN for autonomous sniping."""
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
        print(f"  [SNIPE] GMGN fetch failed: {e}")
        _gmgn_browser_ctx = None
        return []


def compute_dynamic_trade_amount(balance_mon: float, ai_suggested_pct: float = 7.5) -> float:
    """Compute dynamic trade amount: 5-10% of balance, AI can adjust within range.

    SUPER AGGRESSIVE: uses 5-10% of total balance per trade.
    Keeps minimum 2 MON as gas reserve.
    """
    gas_reserve = 2.0
    available = max(0, balance_mon - gas_reserve)
    # Clamp AI suggestion to 5-10% range
    pct = max(5.0, min(10.0, ai_suggested_pct))
    amount = available * (pct / 100.0)
    return round(amount, 4)


def ai_decide_snipe(ai: AIClient, trending_tokens: list, balance_mon: float, open_positions: list) -> dict:
    """Ask Haiku whether to snipe a trending token autonomously."""
    open_tickers = [p.get("token_address", "")[:10] for p in open_positions if p.get("status") == "OPEN"]
    token_summary = json.dumps(trending_tokens[:5], indent=2, default=str)[:1500]

    result = ai.think_json(
        system_prompt="You are a SUPER AGGRESSIVE crypto sniper bot in HACKATHON MODE. Respond ONLY with valid JSON. Your goal: TRADE as much as possible to show activity on-chain.",
        user_message=f"""You are an autonomous trader on Monad. Find the BEST token to snipe RIGHT NOW.

TRENDING TOKENS (GMGN top movers, 1h):
{token_summary}

YOUR STATUS:
- Balance: {balance_mon:.4f} MON
- Currently open positions: {len(open_tickers)} ({', '.join(open_tickers) if open_tickers else 'none'})
- Available for trading: {max(0, balance_mon - 2.0):.4f} MON (keeping 2 MON gas reserve)

Respond in JSON:
{{
  "decision": "SNIPE" or "SKIP",
  "token_address": "0x... address of token to buy (from trending list)",
  "ticker": "token ticker if available",
  "reason": "1-2 sentences",
  "trade_pct": 5.0-10.0,
  "conviction": 0.0-1.0
}}

HACKATHON RULES:
- SNIPE if ANY trending token shows strong momentum (volume spike, price increase)
- Prefer tokens with high swap counts and recent price increases
- trade_pct: how much of available balance to use (5-10%)
- Higher conviction = higher trade_pct (up to 10%)
- Only SKIP if no tokens look promising or balance is too low (< 5 MON)
- Do NOT snipe tokens you already have open positions in
- We want MAXIMUM on-chain activity — be aggressive""",
        max_tokens=300,
    )
    return result or {"decision": "SKIP", "reason": "AI unavailable"}


def get_token_balance(w3: Web3, token_address: str, wallet: str) -> int:
    """Get ERC20 token balance (raw units). Zero cost."""
    try:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_ABI,
        )
        return contract.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
    except Exception:
        return 0


def estimate_position_value_mon(w3: Web3, token_address: str, token_balance: int) -> float:
    """Rough estimate of token value in MON.

    In production, query the DEX router.getAmountsOut() for an actual quote.
    Returns 0 if unable to price (placeholder for DEX integration).
    """
    if token_balance == 0:
        return 0.0
    # Placeholder: real impl would call router.getAmountsOut([token, WETH])
    return 0.0


# ═══════════════════════════════════════════════
#  AI decision making (costs money — guarded)
# ═══════════════════════════════════════════════

def ai_decide_on_deposit(ai: AIClient, deposit: dict, balance_mon: float) -> dict:
    """Ask Haiku whether to accept this deposit and buy tokens. SUPER AGGRESSIVE."""
    result = ai.think_json(
        system_prompt="You are a SUPER AGGRESSIVE trading bot in HACKATHON MODE. Respond ONLY with valid JSON. Almost ALWAYS accept deposits.",
        user_message=f"""You are a crypto trader bot on Monad in HACKATHON SPRINT MODE.

DEPOSIT:
- From: {deposit['from']}
- Amount: {deposit['value_mon']:.4f} MON
- Your total balance: {balance_mon:.4f} MON

Decide whether to ACCEPT and execute a buy, or REJECT (return funds).

Respond in JSON:
{{
  "decision": "ACCEPT" or "REJECT",
  "reason": "1 sentence",
  "buy_amount_mon": 0.0
}}

HACKATHON RULES:
- ALMOST ALWAYS ACCEPT — we want maximum on-chain activity
- ACCEPT any amount > 0.001 MON
- Only REJECT if amount is literal dust (< 0.001 MON)
- Use 90% of deposit for buying (keep only 10% gas reserve)
- We're in aggressive mode: bigger buys = better""",
        max_tokens=256,
    )
    return result or {"decision": "REJECT", "reason": "AI unavailable or budget exceeded"}


def ai_decide_exit(ai: AIClient, position: dict, current_pnl_pct: float) -> dict:
    """Ask Haiku whether to take profit or cut losses."""
    result = ai.think_json(
        system_prompt="You are a trading bot. Respond ONLY with valid JSON.",
        user_message=f"""You are a crypto trader bot. Evaluate this position:

POSITION:
- Token: {position.get('token_address', 'unknown')[:20]}...
- Entry: {position.get('entry_mon', 0):.4f} MON
- Current P&L: {current_pnl_pct:+.1f}%
- Time held: since {position.get('entry_time', 'unknown')}

Decide: HOLD, TAKE_PROFIT, or STOP_LOSS.

Respond in JSON:
{{
  "decision": "HOLD" or "TAKE_PROFIT" or "STOP_LOSS",
  "reason": "1 sentence"
}}

Rules:
- TAKE_PROFIT if P&L > +80%
- STOP_LOSS if P&L < -15%
- HOLD otherwise, unless you have strong conviction""",
        max_tokens=200,
    )

    if result:
        return result

    # Fallback: safety stop-loss if AI is unavailable
    if current_pnl_pct < -15:
        return {"decision": "STOP_LOSS", "reason": "AI unavailable, safety stop-loss triggered"}
    return {"decision": "HOLD", "reason": "AI unavailable, defaulting to HOLD"}


# ═══════════════════════════════════════════════
#  Profit sharing (Agent-to-Agent Tx)
# ═══════════════════════════════════════════════

def return_profits(w3, account, boss_address: str, profit_mon: float, fee_pct: float = 20.0):
    """Send profits back to Manager, keeping fee_pct as Trader's commission.

    fee_pct = Trader's cut (e.g. 20% means Trader keeps 20%, Manager gets 80%)
    """
    if profit_mon <= 0:
        print("  [PROFIT] No profit to return.")
        return None

    trader_fee = profit_mon * (fee_pct / 100.0)
    return_amount = profit_mon - trader_fee

    if return_amount < 0.001:
        print(f"  [PROFIT] Return amount too small ({return_amount:.6f} MON). Skipping.")
        return None

    print(f"  [PROFIT] Total: {profit_mon:.4f} MON | Trader fee: {trader_fee:.4f} | Returning: {return_amount:.4f}")
    tx_hash = send_mon(w3, account, boss_address, return_amount)
    print(f"  [PROFIT] Return tx: {tx_hash}")
    return tx_hash


# ═══════════════════════════════════════════════
#  Main Loop — The Gatekeeper Pattern
# ═══════════════════════════════════════════════

def main():
    banner = """
╔════════════════════════════════════════════════════════╗
║   Trader Agent — SUPER AGGRESSIVE Executor             ║
║   自律分散型ベンチャーDAO — 超・攻撃モード            ║
║                                                        ║
║   HACKATHON MODE:                                      ║
║     + Autonomous Snipe & Scalp (GMGN scanning)         ║
║     + Dynamic trade sizing (5-10% of balance)          ║
║     + Aggressive deposit acceptance                    ║
╚════════════════════════════════════════════════════════╝"""
    print(banner)

    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", 10))
    boss_address = os.getenv("BOSS_WALLET_ADDRESS", "")
    stop_loss_pct = float(os.getenv("STOP_LOSS_PCT", 15))
    trader_fee_pct = 100.0 - float(os.getenv("PROFIT_SHARE_PCT", 80))
    # SUPER AGGRESSIVE: autonomous snipe interval (every N cycles)
    snipe_interval_cycles = int(os.getenv("SNIPE_INTERVAL_CYCLES", 30))

    # Initialize
    ai = AIClient(role="TRADER")
    w3 = get_web3()
    account = get_account()
    state = load_state()

    my_address = account.address

    print(f"  Wallet    : {my_address}")
    print(f"  Balance   : {get_balance_mon(w3, my_address):.4f} MON")
    print(f"  Boss      : {boss_address or '(not configured)'}")
    print(f"  Model     : {ai.model}")
    print(f"  Budget    : {ai.guard.remaining} API calls remaining today")
    print(f"  Interval  : {poll_interval}s")
    print(f"  Stop-loss : {stop_loss_pct}%")
    print(f"  Trader fee: {trader_fee_pct}%")
    print(f"  DEX Router: {DEX_ROUTER[:10]}...")
    print("─" * 56)

    if state["last_scanned_block"] == 0:
        try:
            state["last_scanned_block"] = w3.eth.block_number
        except Exception:
            state["last_scanned_block"] = 0
        save_state(state)

    cycle = 0
    while RUNNING:
        cycle += 1
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")

        # ── Phase 1: Check for deposits (FREE — zero API cost) ──
        try:
            current_block = w3.eth.block_number
        except Exception as e:
            print(f"  [RPC] Block fetch failed: {e}")
            time.sleep(poll_interval)
            continue

        from_block = state["last_scanned_block"]
        deposits = []

        if current_block > from_block:
            deposits = check_incoming_transfers(w3, my_address, from_block)
            # Filter: only from boss wallet
            if boss_address:
                deposits = [
                    d for d in deposits
                    if d["from"].lower() == boss_address.lower()
                ]
            state["last_scanned_block"] = current_block

        # ── Phase 2: Check open positions for P&L (FREE) ──
        sharp_moves = []
        open_positions = [p for p in state["positions"] if p.get("status") == "OPEN"]

        for pos in open_positions:
            token_bal = get_token_balance(w3, pos["token_address"], my_address)
            estimated_value = estimate_position_value_mon(w3, pos["token_address"], token_bal)
            entry_mon = pos.get("entry_mon", 0)

            if entry_mon > 0 and estimated_value > 0:
                pnl_pct = ((estimated_value - entry_mon) / entry_mon) * 100
                if pnl_pct > 80 or pnl_pct < -stop_loss_pct:
                    sharp_moves.append({"position": pos, "pnl_pct": pnl_pct, "value": estimated_value})

        # ── Phase 2.5: AUTONOMOUS SNIPE SCAN (SUPER AGGRESSIVE) ──
        autonomous_snipes = []
        if cycle % snipe_interval_cycles == 0:
            balance = get_balance_mon(w3, my_address)
            if balance > 5.0:  # Only snipe if we have enough balance
                print(f"\n[{now}] [SNIPE SCAN] Cycle {cycle} — Scanning GMGN for autonomous snipe opportunities...")
                try:
                    trending = fetch_trending_tokens_for_snipe()
                    if trending:
                        open_positions = [p for p in state["positions"] if p.get("status") == "OPEN"]
                        snipe_decision = ai_decide_snipe(ai, trending, balance, open_positions)
                        print(f"  [SNIPE] Decision: {snipe_decision.get('decision')} — {snipe_decision.get('reason', 'N/A')}")

                        if snipe_decision.get("decision") == "SNIPE":
                            token_addr = snipe_decision.get("token_address")
                            trade_pct = snipe_decision.get("trade_pct", 7.5)
                            trade_amount = compute_dynamic_trade_amount(balance, trade_pct)

                            if token_addr and trade_amount > 0.1:
                                autonomous_snipes.append({
                                    "token_address": token_addr,
                                    "ticker": snipe_decision.get("ticker", "???"),
                                    "amount_mon": trade_amount,
                                    "conviction": snipe_decision.get("conviction", 0.5),
                                })
                                print(f"  [SNIPE] TARGET: {token_addr[:16]}... | Amount: {trade_amount:.4f} MON ({trade_pct:.1f}% of balance)")
                            else:
                                print(f"  [SNIPE] Trade amount too low ({trade_amount:.4f} MON) or no token address. Skipping.")
                    else:
                        print(f"  [SNIPE] No trending tokens found.")
                except Exception as e:
                    print(f"  [SNIPE] Autonomous scan error: {e}")

        # ── Phase 3: THE GATEKEEPER — skip API if nothing happened ──
        if not deposits and not sharp_moves and not autonomous_snipes:
            if cycle % 30 == 0:
                print(f"[{now}] Cycle {cycle} — No deposits, no sharp moves, no snipes. Sleeping...")
            time.sleep(poll_interval)
            save_state(state)
            continue

        # ── Phase 4: Events detected → invoke AI (costs money) ──
        print(f"\n[{now}] Cycle {cycle} — Events detected!")

        # Handle incoming deposits
        for deposit in deposits:
            print(f"  [DEPOSIT] {deposit['value_mon']:.4f} MON from {deposit['from'][:10]}... (block {deposit['block']})")
            balance = get_balance_mon(w3, my_address)
            decision = ai_decide_on_deposit(ai, deposit, balance)

            print(f"  [AI] Decision: {decision.get('decision')} — {decision.get('reason')}")

            if decision.get("decision") == "ACCEPT":
                buy_amount = decision.get("buy_amount_mon", deposit["value_mon"] * 0.9)

                # Read coordination file for target token address
                coord = read_coordination()
                target_token = coord["token_address"] if coord else None

                if target_token and DEX_ROUTER != "0x0000000000000000000000000000000000000000":
                    try:
                        print(f"  [TRADE] Buying ${coord.get('ticker', '???')}: {buy_amount:.4f} MON → {target_token[:16]}...")
                        tx = swap_mon_for_token(w3, account, DEX_ROUTER, target_token, buy_amount)
                        print(f"  [TRADE] Swap tx: {tx}")
                        log_initial_swap(coord.get("ticker", "???"), target_token, buy_amount, tx)
                        mark_coordination_done()
                    except Exception as e:
                        print(f"  [TRADE] Swap failed: {e}")
                elif target_token:
                    print(f"  [TRADE] Target token: {target_token[:16]}... (DEX not configured)")
                else:
                    print(f"  [TRADE] No target token from Manager. Holding {buy_amount:.4f} MON.")

                # Record position
                state["positions"].append({
                    "token_address": target_token,
                    "entry_mon": buy_amount,
                    "entry_block": deposit["block"],
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "status": "OPEN",
                    "funding_tx": deposit["tx_hash"],
                    "ticker": coord.get("ticker") if coord else None,
                })
                state["total_trades"] += 1

            elif decision.get("decision") == "REJECT":
                # Return funds to boss
                print(f"  [REJECT] Returning {deposit['value_mon']:.4f} MON to Manager")
                try:
                    return_amount = deposit["value_mon"] * 0.99  # keep tiny gas reserve
                    return_tx = send_mon(w3, account, boss_address, return_amount)
                    print(f"  [REJECT] Return tx: {return_tx}")
                except Exception as e:
                    print(f"  [REJECT] Return failed: {e}")

        # Handle autonomous snipes (SUPER AGGRESSIVE)
        for snipe in autonomous_snipes:
            token_addr = snipe["token_address"]
            amount = snipe["amount_mon"]
            ticker = snipe["ticker"]
            print(f"  [SNIPE EXEC] Sniping ${ticker}: {amount:.4f} MON → {token_addr[:16]}...")

            if DEX_ROUTER != "0x0000000000000000000000000000000000000000":
                try:
                    tx = swap_mon_for_token(w3, account, DEX_ROUTER, token_addr, amount)
                    print(f"  [SNIPE EXEC] Swap tx: {tx}")
                    log_initial_swap(ticker, token_addr, amount, tx)
                except Exception as e:
                    print(f"  [SNIPE EXEC] Swap failed: {e}")
                    continue
            else:
                print(f"  [SNIPE EXEC] DEX not configured — would snipe {amount:.4f} MON → {token_addr[:16]}...")

            # Record autonomous position
            state["positions"].append({
                "token_address": token_addr,
                "entry_mon": amount,
                "entry_block": current_block,
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "status": "OPEN",
                "funding_tx": "autonomous_snipe",
                "ticker": ticker,
                "source": "AUTONOMOUS_SNIPE",
            })
            state["total_trades"] += 1
            print(f"  [SNIPE EXEC] Position recorded: ${ticker} | {amount:.4f} MON")

        # Handle sharp P&L moves
        for move in sharp_moves:
            pos = move["position"]
            pnl = move["pnl_pct"]
            print(f"  [P&L] Token {pos['token_address'][:10]}... P&L: {pnl:+.1f}%")

            decision = ai_decide_exit(ai, pos, pnl)
            print(f"  [AI] Exit decision: {decision.get('decision')} — {decision.get('reason')}")

            if decision.get("decision") in ("TAKE_PROFIT", "STOP_LOSS"):
                # Execute sell if DEX configured
                if DEX_ROUTER != "0x0000000000000000000000000000000000000000":
                    try:
                        token_bal = get_token_balance(w3, pos["token_address"], my_address)
                        if token_bal > 0:
                            print(f"  [SELL] Selling {token_bal} tokens")
                            # tx = swap_token_for_mon(w3, account, DEX_ROUTER, pos["token_address"], token_bal)
                            # print(f"  [SELL] Sell tx: {tx}")
                    except Exception as e:
                        print(f"  [SELL] Sell failed: {e}")
                else:
                    print(f"  [SELL] Would sell position (configure DEX_ROUTER_ADDRESS)")

                pos["status"] = "CLOSED"

                # If profitable, return profit to Manager
                if pnl > 0 and boss_address:
                    estimated_profit = pos["entry_mon"] * (pnl / 100)
                    try:
                        return_profits(w3, account, boss_address, estimated_profit, trader_fee_pct)
                    except Exception as e:
                        print(f"  [PROFIT] Return failed: {e}")

        save_state(state)
        time.sleep(poll_interval)

    # Final stats
    open_count = len([p for p in state["positions"] if p.get("status") == "OPEN"])
    print("\n[Trader] Final Statistics:")
    print(f"  Total trades     : {state['total_trades']}")
    print(f"  Open positions   : {open_count}")
    print(f"  Total profit     : {state['total_profit_mon']:.4f} MON")
    print(f"  Current balance  : {get_balance_mon(w3, my_address):.4f} MON")
    print("[Trader] Stopped.")


if __name__ == "__main__":
    main()
