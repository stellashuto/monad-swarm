"""On-chain utilities for Monad (EVM-compatible) interactions.

Provides:
  - Web3 connection management
  - Account derivation from private key
  - Native MON transfers
  - Block scanning for incoming transfers
  - Uniswap V2-style DEX swap helpers
"""

import os
import re
import json
import time
import random
import string
import logging
from web3 import Web3
from eth_account import Account

logger = logging.getLogger(__name__)


# ─── Connection & Account ────────────────────────────────

def get_web3(rpc_url: str | None = None) -> Web3:
    """Return a Web3 instance connected to the Monad RPC."""
    url = rpc_url or os.getenv("MONAD_RPC_URL", "https://testnet-rpc.monad.xyz")
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to RPC: {url}")
    return w3


def get_account(private_key: str | None = None) -> Account:
    """Derive an account from the private key in env."""
    pk = private_key or os.getenv("PRIVATE_KEY")
    if not pk or pk.startswith("0x_CHANGE_ME"):
        raise ValueError("PRIVATE_KEY is not configured. Edit your .env file.")
    return Account.from_key(pk)


def get_balance_mon(w3: Web3, address: str) -> float:
    """Return balance in MON (native token, 18 decimals)."""
    balance_wei = w3.eth.get_balance(Web3.to_checksum_address(address))
    return float(Web3.from_wei(balance_wei, "ether"))


def get_chain_id() -> int:
    return int(os.getenv("CHAIN_ID", 143))


# ─── Parameter Validation ────────────────────────────────

MAX_TOKEN_NAME_LENGTH = 20


def sanitize_token_name(name: str) -> str:
    """Truncate token name to MAX_TOKEN_NAME_LENGTH characters.

    If the name exceeds the limit, it is truncated at the last word
    boundary that fits, or hard-truncated if a single word is too long.
    """
    name = name.strip()
    if len(name) <= MAX_TOKEN_NAME_LENGTH:
        return name

    # Try to cut at word boundary
    truncated = name[:MAX_TOKEN_NAME_LENGTH]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated.rstrip()


def validate_ticker(ticker: str) -> str:
    """Validate and sanitize a ticker symbol.

    Rules:
      - Strip leading '$' if present
      - Remove any non-alpha characters (letters only)
      - Uppercase
      - Must be 3–5 characters; pad with 'X' or truncate as needed

    Returns the sanitized ticker.
    Raises ValueError if nothing usable remains after cleaning.
    """
    cleaned = re.sub(r"[^A-Za-z]", "", ticker.strip().lstrip("$")).upper()
    if not cleaned:
        raise ValueError(f"Ticker '{ticker}' contains no valid alpha characters")
    if len(cleaned) < 3:
        cleaned = cleaned.ljust(3, "X")
    if len(cleaned) > 5:
        cleaned = cleaned[:5]
    return cleaned


def make_unique_ticker(ticker: str, existing_tickers: set[str]) -> str:
    """If `ticker` collides with `existing_tickers`, append a random uppercase letter.

    Retries up to 26 times to find a unique variant.
    """
    ticker = validate_ticker(ticker)
    if ticker not in existing_tickers:
        return ticker
    for _ in range(26):
        suffix = random.choice(string.ascii_uppercase)
        candidate = (ticker[:4] + suffix) if len(ticker) >= 5 else (ticker + suffix)
        candidate = candidate[:5]
        if candidate not in existing_tickers:
            logger.info(f"Ticker collision: '{ticker}' → '{candidate}'")
            return candidate
    return ticker


# ─── Native MON Transfers ────────────────────────────────

def send_mon(w3: Web3, sender_account: Account, to_address: str, amount_mon: float) -> str:
    """Send MON from sender to target. Returns tx hash hex."""
    to_addr = Web3.to_checksum_address(to_address)
    sender_addr = sender_account.address
    nonce = w3.eth.get_transaction_count(sender_addr)

    tx = {
        "to": to_addr,
        "value": Web3.to_wei(amount_mon, "ether"),
        "gas": 21000,
        "gasPrice": w3.eth.gas_price,
        "nonce": nonce,
        "chainId": get_chain_id(),
    }
    signed = sender_account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        raise RuntimeError(f"Transaction failed: {tx_hash.hex()}")
    return tx_hash.hex()


# ─── Block Scanning ──────────────────────────────────────

def check_incoming_transfers(w3: Web3, address: str, from_block: int) -> list[dict]:
    """Check for incoming native MON transfers to `address` since `from_block`.

    Scans up to 500 blocks (Monad has fast ~1s blocks).
    Returns list of {from, value_mon, block, tx_hash}.
    """
    address = Web3.to_checksum_address(address)
    latest = w3.eth.block_number
    transfers = []

    scan_range = min(latest - from_block, 500)
    start = max(from_block, latest - scan_range)

    for block_num in range(start, latest + 1):
        try:
            block = w3.eth.get_block(block_num, full_transactions=True)
        except Exception:
            continue
        for tx in block.transactions:
            if tx.to and Web3.to_checksum_address(tx.to) == address and tx.value > 0:
                transfers.append({
                    "from": tx["from"],
                    "value_mon": float(Web3.from_wei(tx.value, "ether")),
                    "block": block_num,
                    "tx_hash": tx.hash.hex(),
                })
    return transfers


# ─── DEX Swap Helpers (Uniswap V2 style) ─────────────────

ROUTER_ABI = json.loads("""[
  {
    "name": "swapExactETHForTokens",
    "type": "function",
    "stateMutability": "payable",
    "inputs": [
      {"name": "amountOutMin", "type": "uint256"},
      {"name": "path", "type": "address[]"},
      {"name": "to", "type": "address"},
      {"name": "deadline", "type": "uint256"}
    ],
    "outputs": [{"name": "amounts", "type": "uint256[]"}]
  },
  {
    "name": "swapExactTokensForETH",
    "type": "function",
    "stateMutability": "nonpayable",
    "inputs": [
      {"name": "amountIn", "type": "uint256"},
      {"name": "amountOutMin", "type": "uint256"},
      {"name": "path", "type": "address[]"},
      {"name": "to", "type": "address"},
      {"name": "deadline", "type": "uint256"}
    ],
    "outputs": [{"name": "amounts", "type": "uint256[]"}]
  },
  {
    "name": "WETH",
    "type": "function",
    "stateMutability": "view",
    "inputs": [],
    "outputs": [{"name": "", "type": "address"}]
  }
]""")

ERC20_ABI = json.loads("""[
  {"name":"balanceOf","type":"function","stateMutability":"view",
   "inputs":[{"name":"account","type":"address"}],
   "outputs":[{"name":"","type":"uint256"}]},
  {"name":"approve","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
   "outputs":[{"name":"","type":"bool"}]},
  {"name":"decimals","type":"function","stateMutability":"view",
   "inputs":[],"outputs":[{"name":"","type":"uint8"}]}
]""")


def swap_mon_for_token(
    w3: Web3,
    account: Account,
    router_address: str,
    token_address: str,
    amount_mon: float,
    slippage_bps: int = 300,
) -> str:
    """Swap native MON for a token via a Uniswap V2-style router."""
    router = w3.eth.contract(
        address=Web3.to_checksum_address(router_address), abi=ROUTER_ABI
    )
    weth = router.functions.WETH().call()
    path = [weth, Web3.to_checksum_address(token_address)]
    value_wei = Web3.to_wei(amount_mon, "ether")
    deadline = int(time.time()) + 300

    tx = router.functions.swapExactETHForTokens(
        0, path, account.address, deadline
    ).build_transaction({
        "from": account.address,
        "value": value_wei,
        "gas": 300000,
        "gasPrice": w3.eth.gas_price,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": get_chain_id(),
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        raise RuntimeError(f"Swap failed: {tx_hash.hex()}")
    return tx_hash.hex()


# ─── Nad.fun (monad.fun) BondingCurveRouter Deployment ───
# Official ABI from https://github.com/Naddotfun/contract-v3-abi
# Mainnet BondingCurveRouter: 0x6F6B8F1a20703309951a5127c45B49b1CD981A22

# Default factory address — used when MONAD_FUN_FACTORY_ADDRESS is not in .env
NADFUN_ROUTER_DEFAULT = "0x6F6B8F1a20703309951a5127c45B49b1CD981A22"

MONAD_FUN_FACTORY_ABI = json.loads("""[
  {
    "type": "function",
    "name": "create",
    "inputs": [
      {
        "name": "params",
        "type": "tuple",
        "internalType": "struct IBondingCurveRouter.TokenCreationParams",
        "components": [
          {"name": "name", "type": "string", "internalType": "string"},
          {"name": "symbol", "type": "string", "internalType": "string"},
          {"name": "tokenURI", "type": "string", "internalType": "string"},
          {"name": "amountOut", "type": "uint256", "internalType": "uint256"},
          {"name": "salt", "type": "bytes32", "internalType": "bytes32"},
          {"name": "actionId", "type": "uint8", "internalType": "uint8"}
        ]
      }
    ],
    "outputs": [
      {"name": "token", "type": "address", "internalType": "address"},
      {"name": "pool", "type": "address", "internalType": "address"}
    ],
    "stateMutability": "payable"
  }
]""")


def _simulate_deployment(w3: Web3, tx: dict) -> dict:
    """Run eth_call simulation before broadcasting.

    Returns {"ok": bool, "error": str|None, "gas_used": int|None}.
    """
    try:
        # eth_call — simulates without sending
        w3.eth.call({
            "from": tx["from"],
            "to": tx["to"],
            "data": tx.get("data", b""),
            "value": tx.get("value", 0),
            "gas": tx.get("gas", 5_000_000),
        })
        return {"ok": True, "error": None}
    except Exception as e:
        err_str = str(e)
        # Try to extract revert reason
        revert_reason = err_str
        if "revert" in err_str.lower():
            revert_reason = err_str
        elif "execution reverted" in err_str.lower():
            revert_reason = err_str
        logger.error(f"[SIMULATE] eth_call REVERTED: {revert_reason}")
        return {"ok": False, "error": revert_reason}


def _estimate_gas_dynamic(w3: Web3, tx_params: dict, multiplier: float = 1.3) -> int:
    """Estimate gas dynamically via eth_estimateGas and apply a safety multiplier.

    Falls back to 3_900_000 if estimation fails.
    """
    try:
        estimated = w3.eth.estimate_gas(tx_params)
        final = int(estimated * multiplier)
        logger.info(f"[GAS] estimateGas={estimated}, ×{multiplier}={final}")
        return final
    except Exception as e:
        logger.warning(f"[GAS] estimateGas failed ({e}), falling back to 3_900_000")
        return 3_900_000


def _get_priority_fee(w3: Web3, boost_pct: float = 0.20) -> int:
    """Get recommended max priority fee and add a boost (default +20%)."""
    try:
        base_fee = w3.eth.max_priority_fee
        boosted = int(base_fee * (1 + boost_pct))
        logger.info(f"[FEE] base_priority_fee={base_fee}, +{int(boost_pct*100)}%={boosted}")
        return boosted
    except Exception:
        # Fallback: use gas_price as-is (legacy chain)
        return 0


def deploy_token_monad_fun(
    w3: Web3,
    account: Account,
    factory_address: str,
    token_name: str,
    ticker: str,
    description: str,
    initial_liquidity_mon: float = 0.1,
    total_supply: int = 1_000_000_000,
) -> dict:
    """Deploy a new token via Nad.fun BondingCurveRouter.create().

    Sends `initial_liquidity_mon` as msg.value (deploy fee + initial buy).
    The `description` is passed as tokenURI metadata.

    Features:
      - Dynamic gas estimation (eth_estimateGas × 1.3)
      - Priority fee boost (+20%)
      - Pre-broadcast simulation via eth_call
      - Initial liquidity default: 0.1 MON

    Returns:
        {"success": bool, "token_address": str|None, "tx_hash": str|None, "error": str|None}
    """
    try:
        # ── Sanitize parameters before on-chain call ──
        token_name = sanitize_token_name(token_name)
        ticker = validate_ticker(ticker)

        router = w3.eth.contract(
            address=Web3.to_checksum_address(factory_address),
            abi=MONAD_FUN_FACTORY_ABI,
        )
        value_wei = Web3.to_wei(initial_liquidity_mon, "ether")

        # Generate a unique salt from token name + current timestamp
        salt = Web3.keccak(text=f"{token_name}-{ticker}-{int(time.time())}")
        # actionId 0 = standard create
        action_id = 0

        nonce = w3.eth.get_transaction_count(account.address)
        chain_id = get_chain_id()

        # Build base tx params for gas estimation
        base_tx = {
            "from": account.address,
            "value": value_wei,
            "nonce": nonce,
            "chainId": chain_id,
        }

        # Try EIP-1559 fee model first, fallback to legacy gasPrice
        priority_fee = _get_priority_fee(w3)
        if priority_fee > 0:
            try:
                latest_block = w3.eth.get_block("latest")
                base_fee_per_gas = latest_block.get("baseFeePerGas", 0)
                max_fee = base_fee_per_gas * 2 + priority_fee
                base_tx["maxPriorityFeePerGas"] = priority_fee
                base_tx["maxFeePerGas"] = max_fee
            except Exception:
                base_tx["gasPrice"] = int(w3.eth.gas_price * 1.2)
        else:
            base_tx["gasPrice"] = int(w3.eth.gas_price * 1.2)

        # Build the function call
        fn_call = router.functions.create(
            (token_name, ticker, description, 0, salt, action_id)
        )
        tx = fn_call.build_transaction({**base_tx, "gas": 5_000_000})

        # ── Dynamic gas estimation (eth_estimateGas × 1.3) ──
        gas_limit = _estimate_gas_dynamic(w3, {
            "from": tx["from"],
            "to": tx["to"],
            "data": tx.get("data", b""),
            "value": tx.get("value", 0),
        })
        tx["gas"] = gas_limit

        # ── Pre-broadcast simulation (eth_call) ──
        sim = _simulate_deployment(w3, tx)
        if not sim["ok"]:
            logger.error(f"[DEPLOY] Simulation FAILED — will NOT broadcast: {sim['error']}")
            print(f"  [SIMULATE] *** REVERT DETECTED *** {sim['error']}")
            return {
                "success": False,
                "token_address": None,
                "tx_hash": None,
                "error": f"Simulation reverted: {sim['error']}",
            }
        logger.info("[DEPLOY] Simulation passed — broadcasting transaction")
        print("  [SIMULATE] Simulation OK — broadcasting...")

        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt.status != 1:
            return {
                "success": False,
                "token_address": None,
                "tx_hash": tx_hash.hex(),
                "error": f"Transaction reverted on-chain: {tx_hash.hex()}",
            }

        # Extract token address from return value or logs
        token_address = None
        try:
            for log in receipt.logs:
                if len(log.topics) >= 2:
                    addr_candidate = "0x" + log.topics[1].hex()[-40:]
                    if Web3.is_address(addr_candidate):
                        token_address = Web3.to_checksum_address(addr_candidate)
                        break
        except Exception:
            pass

        return {
            "success": True,
            "token_address": token_address,
            "tx_hash": tx_hash.hex(),
            "error": None,
        }

    except Exception as e:
        logger.error(f"[DEPLOY] Exception: {e}")
        return {
            "success": False,
            "token_address": None,
            "tx_hash": None,
            "error": str(e),
        }


def swap_token_for_mon(
    w3: Web3,
    account: Account,
    router_address: str,
    token_address: str,
    amount_token: int,
) -> str:
    """Swap ERC20 token back to native MON."""
    token_addr = Web3.to_checksum_address(token_address)
    router_addr = Web3.to_checksum_address(router_address)

    token_contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
    router = w3.eth.contract(address=router_addr, abi=ROUTER_ABI)
    weth = router.functions.WETH().call()

    # Step 1: Approve router to spend tokens
    approve_tx = token_contract.functions.approve(
        router_addr, amount_token
    ).build_transaction({
        "from": account.address,
        "gas": 100000,
        "gasPrice": w3.eth.gas_price,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": get_chain_id(),
    })
    signed = account.sign_transaction(approve_tx)
    approve_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(approve_hash, timeout=60)

    # Step 2: Execute swap
    path = [token_addr, weth]
    deadline = int(time.time()) + 300
    swap_tx = router.functions.swapExactTokensForETH(
        amount_token, 0, path, account.address, deadline
    ).build_transaction({
        "from": account.address,
        "gas": 300000,
        "gasPrice": w3.eth.gas_price,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": get_chain_id(),
    })
    signed = account.sign_transaction(swap_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        raise RuntimeError(f"Sell swap failed: {tx_hash.hex()}")
    return tx_hash.hex()


# ─── Deploy Health Check (Dry-Run) ───────────────────────

def run_deploy_health_check(
    w3: Web3,
    account: Account,
    factory_address: str,
    test_value_mon: float = 0.001,
) -> dict:
    """Perform a dry-run deployment against the BondingCurveRouter to verify
    that chain connectivity, Chain ID, gas settings, and factory ABI are correct.

    Uses eth_call (no actual transaction is sent) so it costs 0 gas.
    Returns {"ok": bool, "details": str}.
    """
    checks: list[str] = []
    try:
        # 1. RPC connectivity
        if not w3.is_connected():
            return {"ok": False, "details": "RPC connection failed"}
        checks.append(f"RPC connected (block #{w3.eth.block_number})")

        # 2. Chain ID verification
        on_chain_id = w3.eth.chain_id
        configured_id = get_chain_id()
        if on_chain_id != configured_id:
            return {
                "ok": False,
                "details": (
                    f"Chain ID mismatch: on-chain={on_chain_id}, "
                    f"configured={configured_id}. Fix CHAIN_ID in .env"
                ),
            }
        checks.append(f"Chain ID OK ({on_chain_id})")

        # 3. Gas price obtainable
        gas_price = w3.eth.gas_price
        if gas_price == 0:
            return {"ok": False, "details": "Gas price is 0 — RPC may be misconfigured"}
        checks.append(f"Gas price OK ({gas_price} wei)")

        # 4. Wallet balance sufficient
        balance = get_balance_mon(w3, account.address)
        if balance < test_value_mon:
            return {
                "ok": False,
                "details": (
                    f"Wallet balance too low for health check: "
                    f"{balance:.6f} MON < {test_value_mon} MON required"
                ),
            }
        checks.append(f"Wallet balance OK ({balance:.4f} MON)")

        # 5. Factory contract exists (has code)
        factory_addr = Web3.to_checksum_address(factory_address)
        code = w3.eth.get_code(factory_addr)
        if code == b"" or code == b"\x00":
            return {
                "ok": False,
                "details": f"No contract code at factory address {factory_addr}",
            }
        checks.append(f"Factory contract found at {factory_addr[:16]}...")

        # 6. Dry-run eth_call of create() — simulates without broadcasting
        router = w3.eth.contract(address=factory_addr, abi=MONAD_FUN_FACTORY_ABI)
        value_wei = Web3.to_wei(test_value_mon, "ether")
        salt = Web3.keccak(text=f"HEALTHCHECK-{int(time.time())}")

        try:
            router.functions.create(
                ("HealthCheck", "HCHK", "deploy-health-check", 0, salt, 0)
            ).call({
                "from": account.address,
                "value": value_wei,
                "gas": 5_000_000,
            })
            checks.append("Dry-run create() call succeeded")
        except Exception as e:
            err_msg = str(e)
            # A contract-level revert (custom error / require failure) means
            # the infrastructure is fine — the ABI encoded correctly and the
            # contract executed. Only flag true infrastructure failures.
            infra_errors = [
                "connection", "timeout", "abi", "encoding",
                "invalid opcode", "could not decode",
            ]
            err_lower = err_msg.lower()
            if any(kw in err_lower for kw in infra_errors):
                return {
                    "ok": False,
                    "details": f"Dry-run create() infrastructure error: {err_msg}",
                }
            # Contract-level revert is expected for test params — pass
            checks.append(f"Dry-run create() reached contract (revert: {err_msg[:80]})")

        return {"ok": True, "details": " | ".join(checks)}

    except Exception as e:
        return {"ok": False, "details": f"Health check exception: {e}"}
