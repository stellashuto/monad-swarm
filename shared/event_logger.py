"""Persistent event logger — writes important events to logs/events.log.

Human-readable, timestamped log entries for:
  - AI DEPLOY decisions (ticker + reasoning)
  - Successful token deployments on monad.fun (CA + TxHash)
  - Trader initial swap completions
"""

import logging
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / "events.log"

_logger = logging.getLogger("monad_swarm.events")
_logger.setLevel(logging.INFO)
_logger.propagate = False

if not _logger.handlers:
    _handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    _handler.setFormatter(
        logging.Formatter("%(asctime)s  [%(levelname)s]  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    _logger.addHandler(_handler)


def log_deploy_decision(ticker: str, token_name: str, confidence: float, reasoning: str):
    """Log the moment AI decides to DEPLOY a token."""
    _logger.info(
        "DEPLOY_DECISION  ticker=$%s  name=%s  confidence=%.2f  reason=%s",
        ticker, token_name, confidence, reasoning,
    )


def log_token_deployed(ticker: str, token_address: str, tx_hash: str):
    """Log a successful on-chain token deployment on monad.fun."""
    _logger.info(
        "TOKEN_DEPLOYED  ticker=$%s  CA=%s  tx=%s",
        ticker, token_address, tx_hash,
    )


def log_initial_swap(ticker: str, token_address: str, amount_mon: float, tx_hash: str):
    """Log the Trader's initial buy (swap) completion."""
    _logger.info(
        "INITIAL_SWAP  ticker=$%s  token=%s  amount=%.4f MON  tx=%s",
        ticker, token_address, amount_mon, tx_hash,
    )
