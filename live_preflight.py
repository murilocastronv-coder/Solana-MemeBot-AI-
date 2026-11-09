"""Preflight gates for future live execution. No signing, keys, or swaps."""
from dataclasses import dataclass
import os

@dataclass(frozen=True)
class LivePreflight:
    wallet_is_dedicated: bool
    signer_isolated: bool
    onchain_token_checks: bool
    swap_simulation_passed: bool
    rpc_reliable: bool
    daily_loss_circuit_breaker: bool
    persistence_and_recovery: bool
    explicit_live_approval: bool

    def failures(self):
        return [name for name, value in vars(self).items() if not value]

def live_status():
    # This codebase intentionally cannot submit real trades. An env var must
    # never override missing controls or act as a wallet authorization.
    checks = LivePreflight(False, False, False, False, False, False, False, False)
    return {"enabled": False, "ready": False, "missing": checks.failures(),
            "wallet_address_configured": bool(os.getenv("SOLANA_BOT_PUBLIC_ADDRESS"))}

def require_live_ready():
    raise RuntimeError("Live trading is unavailable: no authorized signer or audited execution pipeline")

def risk_settings():
    return {"order_usd": 10.0, "max_positions": 3, "stop_loss_percent": 8.0,
            "take_profit_percent": 15.0, "daily_loss_limit_usd": 3.0}
