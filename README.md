# Solana Autonomous Capital Agent

Minimal, zero-dependency Python agent implementing read-only Solana RPC health/balance telemetry, liquid cross-DEX USD spread screening, programmable mandate guardrails, and a 70/30 treasury profit split.

Built as an open-source reference agent for the **Superteam Earn — Build an Autonomous Capital Agent on Solana ($500 USDC)** challenge ([listing](https://earn.superteam.fun/listing/build-an-autonomous-capital-agent-on-solana-and-compete-for-dollar500-in-rewards)).

## Architecture & Risk Guardrails

1. **Perception (`check_solana_rpc` & `find_best_spread`)**:
   - Queries Solana JSON-RPC (`getSlot`, optional `getBalance`) and fails closed on timeouts or RPC errors.
   - Filters DexScreener pools strictly to `chainId == "solana"`, canonical USD quote tokens (`USDC`, `USDT`, `USD`, `USDG`, `USDC.E`), and pool liquidity `>= $25,000`.
2. **Mandate Enforcement (`evaluate_risk_guard`)**:
   - Blocks execution unless spread `>= min_spread_pct` (`0.15%` default) and liquidity `>= min_liquidity_usd`.
   - Enforces a hard `daily_loss_limit_usdc` circuit breaker (`$20 USDC` default) and caps position notional at `max_position_usdc`.
   - Stays locked in `DRY_RUN` whenever no signer key is attached (never simulates fake fills or submits unsigned swaps).
3. **Treasury Routing (`calculate_treasury_split`)**:
   - Retains `30%` working capital for compounding and sweeps `70%` of realized net profits toward a designated treasury vault (`0` sweep when net profit `<= 0`).

## Quickstart

Zero external Python packages required (Python `3.10+` stdlib only):

```bash
cp .env.example .env
python3 agent.py --size-usdc 25
```

## Offline Unit Tests

```bash
python3 -m unittest -v test_agent.py
```

## Live Challenge Note

This repository ships cleanly in read-only `DRY_RUN` mode with no embedded private keys or live capital deposited. Full prize-track eligibility on **Steve Capital / OOBE Protocol** requires deploying via Steve Capital, funding an agent wallet with at least `20 USDC` (or SOL equivalent), generating verified Arena XP, and sharing public activity tagging `@OOBEonSol` and `@SteveCapitalOS` before **2026-12-10**.
