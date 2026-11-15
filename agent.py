#!/usr/bin/env python3
"""
Minimal Autonomous Capital Agent for Solana DEX Spread & Risk-Guard Evaluation.

Features:
  - Read-only Solana JSON-RPC health (`getSlot`) and SOL balance (`getBalance`) check.
  - Cross-DEX USD stablecoin pair filtering & spread detection (DexScreener API).
  - Programmable mandate / risk-guard policy (spread floor, liquidity floor, max allocation, daily loss halt).
  - Deterministic 70/30 treasury profit-sweep calculation.
  - Strict dry-run quarantine when signer keys are absent (never submits unsigned or simulated swaps).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from typing import Any, Callable
import urllib.error
import urllib.request

SOL_MINT = "So11111111111111111111111111111111111111112"
DEFAULT_SOLANA_RPC = "https://api.mainnet-beta.solana.com"
DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
USD_STABLE_SYMBOLS = frozenset({"USDC", "USDT", "USD", "USDG", "USDC.E"})
LAMPORTS_PER_SOL = 1_000_000_000


@dataclass(frozen=True)
class RiskGuardPolicy:
    min_spread_pct: float = 0.15
    min_liquidity_usd: float = 25_000.0
    max_position_usdc: float = 100.0
    daily_loss_limit_usdc: float = 20.0
    profit_sweep_ratio: float = 0.70
    dry_run: bool = True


@dataclass(frozen=True)
class SolanaSpreadOpportunity:
    token_mint: str
    symbol: str
    buy_dex: str
    buy_price_usd: float
    sell_dex: str
    sell_price_usd: float
    gross_spread_pct: float
    min_liquidity_usd: float


@dataclass(frozen=True)
class RpcStatus:
    rpc_url: str
    healthy: bool
    slot: int | None
    wallet_address: str
    wallet_sol: float | None
    error: str = ""


@dataclass(frozen=True)
class GuardVerdict:
    approved: bool
    reason: str
    capped_size_usdc: float
    mode: str


HttpJsonFn = Callable[[str, dict[str, Any] | None, float], dict[str, Any]]


def default_http_json(url: str, payload: dict[str, Any] | None = None, timeout: float = 8.0) -> dict[str, Any]:
    """Perform a GET (payload is None) or JSON-RPC POST request and decode the JSON response."""
    headers = {"User-Agent": "SolanaAutonomousCapitalAgent/1.0"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_env(path: Path | None = None) -> dict[str, str]:
    """Load non-empty key=value configuration pairs from `.env` and process environment."""
    env_vars: dict[str, str] = {}
    env_path = path or (Path(__file__).resolve().parent / ".env")
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            val = v.strip().strip("'\"")
            if val:
                env_vars[k.strip()] = val
    for key in (
        "SOLANA_RPC_URL",
        "AGENT_WALLET_PUBLIC_KEY",
        "MASTER_VAULT_ADDRESS",
        "MIN_SPREAD_PCT",
        "MIN_LIQUIDITY_USD",
        "MAX_POSITION_USDC",
        "DAILY_LOSS_LIMIT_USDC",
    ):
        env_val = os.environ.get(key, "").strip()
        if env_val:
            env_vars[key] = env_val
    return env_vars


def solana_json_rpc(
    rpc_url: str,
    method: str,
    params: list[Any],
    http_fn: HttpJsonFn = default_http_json,
    timeout: float = 6.0,
) -> Any:
    """Execute one Solana JSON-RPC call and return its `result`, or raise ValueError on RPC error."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    body = http_fn(rpc_url, payload, timeout)
    if "error" in body and body["error"] is not None:
        raise ValueError(f"RPC error on {method}: {body['error']}")
    return body.get("result")


def check_solana_rpc(
    rpc_url: str = DEFAULT_SOLANA_RPC,
    wallet_address: str = "",
    http_fn: HttpJsonFn = default_http_json,
) -> RpcStatus:
    """Verify live Solana RPC slot progression and optional wallet SOL balance without mutating chain state."""
    try:
        slot_val = solana_json_rpc(rpc_url, "getSlot", [{"commitment": "confirmed"}], http_fn=http_fn)
        slot = int(slot_val) if slot_val is not None else None
        wallet_sol: float | None = None
        if wallet_address:
            bal_res = solana_json_rpc(rpc_url, "getBalance", [wallet_address], http_fn=http_fn)
            if isinstance(bal_res, dict) and "value" in bal_res:
                wallet_sol = round(int(bal_res["value"]) / LAMPORTS_PER_SOL, 6)
        return RpcStatus(
            rpc_url=rpc_url,
            healthy=slot is not None and slot > 0,
            slot=slot,
            wallet_address=wallet_address,
            wallet_sol=wallet_sol,
        )
    except (urllib.error.URLError, TimeoutError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return RpcStatus(
            rpc_url=rpc_url,
            healthy=False,
            slot=None,
            wallet_address=wallet_address,
            wallet_sol=None,
            error=str(exc),
        )


def filter_solana_usd_pairs(
    pairs: list[dict[str, Any]],
    min_liquidity_usd: float = 25_000.0,
) -> list[dict[str, Any]]:
    """Keep liquid Solana pools quoted against canonical USD stablecoins with positive USD price."""
    filtered: list[dict[str, Any]] = []
    for pair in pairs:
        if pair.get("chainId") != "solana":
            continue
        quote_symbol = str(pair.get("quoteToken", {}).get("symbol") or "").upper()
        if quote_symbol not in USD_STABLE_SYMBOLS:
            continue
        liq_usd = float(pair.get("liquidity", {}).get("usd") or 0.0)
        price_usd = float(pair.get("priceUsd") or 0.0)
        if liq_usd >= min_liquidity_usd and price_usd > 0.0:
            filtered.append(pair)
    filtered.sort(key=lambda item: float(item["priceUsd"]))
    return filtered


def find_best_spread(
    pairs: list[dict[str, Any]],
    token_mint: str = SOL_MINT,
    symbol: str = "SOL",
    min_spread_pct: float = 0.15,
    min_liquidity_usd: float = 25_000.0,
) -> SolanaSpreadOpportunity | None:
    """Compute the widest Solana cross-DEX USD spread above `min_spread_pct` and `min_liquidity_usd`."""
    eligible = filter_solana_usd_pairs(pairs, min_liquidity_usd=min_liquidity_usd)
    if len(eligible) < 2:
        return None
    low, high = eligible[0], eligible[-1]
    p_low, p_high = float(low["priceUsd"]), float(high["priceUsd"])
    spread_pct = ((p_high - p_low) / p_low) * 100.0
    if spread_pct < min_spread_pct:
        return None
    min_liq = min(float(low["liquidity"]["usd"]), float(high["liquidity"]["usd"]))
    return SolanaSpreadOpportunity(
        token_mint=token_mint,
        symbol=symbol,
        buy_dex=str(low.get("dexId", "")),
        buy_price_usd=round(p_low, 6),
        sell_dex=str(high.get("dexId", "")),
        sell_price_usd=round(p_high, 6),
        gross_spread_pct=round(spread_pct, 3),
        min_liquidity_usd=round(min_liq, 2),
    )


def evaluate_risk_guard(
    opportunity: SolanaSpreadOpportunity | None,
    policy: RiskGuardPolicy,
    proposed_size_usdc: float,
    realized_loss_today_usdc: float = 0.0,
    signer_configured: bool = False,
) -> GuardVerdict:
    """Enforce programmable capital mandates before any execution route can trigger."""
    mode = "DRY_RUN" if (policy.dry_run or not signer_configured) else "LIVE_ARMED"
    if realized_loss_today_usdc >= policy.daily_loss_limit_usdc:
        return GuardVerdict(False, "daily_loss_limit_reached", 0.0, mode)
    if opportunity is None:
        return GuardVerdict(False, "no_qualifying_spread", 0.0, mode)
    if opportunity.gross_spread_pct < policy.min_spread_pct:
        return GuardVerdict(False, "spread_below_threshold", 0.0, mode)
    if opportunity.min_liquidity_usd < policy.min_liquidity_usd:
        return GuardVerdict(False, "insufficient_pool_liquidity", 0.0, mode)
    if proposed_size_usdc <= 0:
        return GuardVerdict(False, "non_positive_order_size", 0.0, mode)
    capped = round(min(proposed_size_usdc, policy.max_position_usdc), 4)
    return GuardVerdict(True, "mandate_checks_passed", capped, mode)


def calculate_treasury_split(
    seed_usdc: float,
    net_profit_usdc: float,
    sweep_ratio: float = 0.70,
) -> dict[str, float]:
    """Retain working capital (`1 - sweep_ratio`) for compounding and sweep `sweep_ratio` of net profit."""
    if net_profit_usdc <= 0:
        return {"withdraw_to_vault_usdc": 0.0, "retained_working_usdc": round(seed_usdc, 4)}
    retain_ratio = 1.0 - sweep_ratio
    vault_sweep = round(net_profit_usdc * sweep_ratio, 4)
    retained = round((seed_usdc * retain_ratio) + (net_profit_usdc * retain_ratio), 4)
    return {
        "withdraw_to_vault_usdc": vault_sweep,
        "retained_working_usdc": retained,
    }


def run_once(
    token_mint: str = SOL_MINT,
    symbol: str = "SOL",
    proposed_size_usdc: float = 20.0,
    realized_loss_today_usdc: float = 0.0,
    policy: RiskGuardPolicy | None = None,
    rpc_url: str = DEFAULT_SOLANA_RPC,
    wallet_address: str = "",
    http_fn: HttpJsonFn = default_http_json,
) -> dict[str, Any]:
    """Run one deterministic perception -> spread evaluation -> risk-guard cycle."""
    active_policy = policy or RiskGuardPolicy()
    rpc_status = check_solana_rpc(rpc_url=rpc_url, wallet_address=wallet_address, http_fn=http_fn)
    pairs: list[dict[str, Any]] = []
    feed_error = ""
    try:
        feed = http_fn(DEXSCREENER_TOKEN_URL.format(mint=token_mint), None, 8.0)
        raw_pairs = feed.get("pairs", [])
        if isinstance(raw_pairs, list):
            pairs = raw_pairs
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        feed_error = str(exc)

    opp = find_best_spread(
        pairs,
        token_mint=token_mint,
        symbol=symbol,
        min_spread_pct=active_policy.min_spread_pct,
        min_liquidity_usd=active_policy.min_liquidity_usd,
    )
    verdict = evaluate_risk_guard(
        opportunity=opp,
        policy=active_policy,
        proposed_size_usdc=proposed_size_usdc,
        realized_loss_today_usdc=realized_loss_today_usdc,
        signer_configured=False,
    )
    return {
        "rpc_status": asdict(rpc_status),
        "feed_error": feed_error,
        "opportunity": asdict(opp) if opp is not None else None,
        "guard_verdict": asdict(verdict),
        "policy": asdict(active_policy),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Solana Autonomous Capital Agent (Dry-Run & Risk Guard)")
    parser.add_argument("--token-mint", default=SOL_MINT, help="SPL token mint address (default: wrapped SOL)")
    parser.add_argument("--symbol", default="SOL", help="Human-readable symbol label")
    parser.add_argument("--size-usdc", type=float, default=20.0, help="Proposed notional allocation in USDC")
    args = parser.parse_args()

    env = load_env()
    policy = RiskGuardPolicy(
        min_spread_pct=float(env.get("MIN_SPREAD_PCT", "0.15")),
        min_liquidity_usd=float(env.get("MIN_LIQUIDITY_USD", "25000")),
        max_position_usdc=float(env.get("MAX_POSITION_USDC", "100")),
        daily_loss_limit_usdc=float(env.get("DAILY_LOSS_LIMIT_USDC", "20")),
        dry_run=True,
    )
    report = run_once(
        token_mint=args.token_mint,
        symbol=args.symbol,
        proposed_size_usdc=args.size_usdc,
        policy=policy,
        rpc_url=env.get("SOLANA_RPC_URL", DEFAULT_SOLANA_RPC),
        wallet_address=env.get("AGENT_WALLET_PUBLIC_KEY", ""),
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
