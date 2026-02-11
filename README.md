# Monad Swarm

**Autonomous multi-agent system for on-chain coordination on Monad.**

Two AI agents — a **Manager** and a **Trader** — collaborate via on-chain MON transfers. The Manager scouts narratives and deploys tokens; the Trader executes swaps and returns profits. A shared **Gatekeeper** pattern keeps API costs near zero by filtering out noise before any LLM call.

---

## Architecture

```
                        ┌──────────────────────────────────┐
                        │         Google Trends /           │
                        │           GMGN API               │
                        └──────────┬───────────────────────┘
                                   │ trend data (free)
                                   ▼
┌──────────────────────────────────────────────────────────────┐
│  MANAGER AGENT  (Claude Sonnet 4.5)                          │
│                                                              │
│  Tool A: analyze_narrative   ← Gatekeeper: skip if no Δ     │
│  Tool B: deploy_token_on_nad ← Playwright headless Chromium  │
│  Tool C: coordinate_market_making                            │
│           │                                                  │
│           │  on-chain MON transfer                           │
│           ▼                                                  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  MONAD BLOCKCHAIN (Testnet)                            │  │
│  │  ┌──────────┐          ┌──────────┐                    │  │
│  │  │ Manager  │── MON ──▶│ Trader   │                    │  │
│  │  │ Wallet   │◀── MON ──│ Wallet   │                    │  │
│  │  └──────────┘  profit  └──────────┘                    │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│           ▲  profit return                                   │
│           │                                                  │
│  TRADER AGENT  (Claude Haiku 4.5)                            │
│                                                              │
│  • Detect deposits via block scanning (free)                 │
│  • AI buy/reject decision (guarded)                          │
│  • DEX swap execution (Uniswap V2)                           │
│  • P&L monitoring → take profit / stop loss                  │
│  • Profit sharing back to Manager                            │
└──────────────────────────────────────────────────────────────┘
```

## Key Features

- **The Gatekeeper Pattern** — Python pre-filters run *before* every LLM call. Trend fingerprinting, block-level deposit detection, and P&L thresholds ensure Claude is only invoked when something meaningful changes. No change = no API call = zero cost.
- **Agent-to-Agent On-Chain Coordination** — Manager funds the Trader with native MON transfers. Trader detects deposits autonomously, executes trades, and returns profits on-chain. No off-chain messaging required.
- **Cost Optimization** — Daily API call budgets enforced by `CostGuard`. Manager uses Sonnet 4.5 for high-stakes narrative analysis; Trader uses Haiku 4.5 for fast, cheap execution decisions.
- **Cloudflare Bypass** — GMGN trend data fetched via Playwright with cookie persistence, avoiding Cloudflare challenges on subsequent requests.

## Project Structure

```
monad-swarm/
├── manager/
│   ├── main.py            # Manager agent — narrative analysis, token deploy, coordination
│   ├── .env               # Manager secrets (git-ignored)
│   └── .env.example       # Template
├── trader/
│   ├── main.py            # Trader agent — deposit detection, swaps, P&L, profit sharing
│   ├── .env               # Trader secrets (git-ignored)
│   └── .env.example       # Template
├── shared/
│   ├── __init__.py
│   ├── ai_client.py       # Cost-guarded Anthropic API wrapper
│   ├── chain.py           # Web3 utilities — transfers, block scanning, DEX swaps
│   └── cost_guard.py      # The Gatekeeper — daily API budget enforcement
├── setup.sh               # One-command setup (venv + deps + Playwright)
├── start_manager.sh       # Launch Manager agent
├── start_trader.sh        # Launch Trader agent
├── requirements.txt
└── README.md
```

## Quick Start

### 1. Clone & Setup

```bash
git clone <repo-url> && cd monad-swarm
chmod +x setup.sh start_manager.sh start_trader.sh
./setup.sh
```

### 2. Configure

```bash
cp manager/.env.example manager/.env
cp trader/.env.example  trader/.env
```

Edit both `.env` files:
- Set your `ANTHROPIC_API_KEY`
- Set wallet `PRIVATE_KEY` for each agent
- Set `TARGET_TRADER_ADDRESS` (Manager) and `BOSS_WALLET_ADDRESS` (Trader) to each other's wallet
- Optionally configure `DEX_ROUTER_ADDRESS`, cost limits, and polling intervals

### 3. Run

```bash
# Terminal 1 — Manager
./start_manager.sh

# Terminal 2 — Trader
./start_trader.sh
```

## How It Works

### Manager Flow

1. **Poll** — Fetch trending tokens from GMGN and Google Trends (zero cost)
2. **Fingerprint** — Hash the trend data; skip LLM call if unchanged
3. **Analyze** — On change, ask Claude Sonnet 4.5 to evaluate narrative potential
4. **Deploy** — If confidence >= 0.75, deploy a token on nad.fun via headless Chromium
5. **Fund** — Send MON to the Trader wallet to seed market making

### Trader Flow

1. **Scan** — Watch blocks for incoming MON from Manager (zero cost)
2. **Decide** — On deposit, ask Claude Haiku 4.5: accept or reject?
3. **Execute** — Swap MON for tokens via DEX router
4. **Monitor** — Track P&L per position (zero cost); trigger AI exit decision only on sharp moves
5. **Return** — On take-profit or stop-loss, sell tokens and send profits back to Manager

## Tech Stack

| Component | Technology |
|-----------|-----------|
| LLM | Claude Sonnet 4.5 (Manager), Claude Haiku 4.5 (Trader) |
| Blockchain | Monad Testnet (EVM-compatible) |
| On-chain | web3.py, eth-account |
| DEX | Uniswap V2-style router |
| Scraping | Playwright (headless Chromium) |
| API | Anthropic Python SDK |

## License

MIT
