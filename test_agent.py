#!/usr/bin/env python3
"""Offline unit tests for Solana Autonomous Capital Agent."""

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent


def _sample_pairs() -> list[dict]:
    return [
        {
            "chainId": "solana",
            "dexId": "raydium",
            "quoteToken": {"symbol": "USDC"},
            "liquidity": {"usd": 120_000.0},
            "priceUsd": "180.00",
        },
        {
            "chainId": "solana",
            "dexId": "orca",
            "quoteToken": {"symbol": "USDT"},
            "liquidity": {"usd": 95_000.0},
            "priceUsd": "180.72",
        },
        {
            "chainId": "solana",
            "dexId": "illiquid-dex",
            "quoteToken": {"symbol": "USDC"},
            "liquidity": {"usd": 4_000.0},
            "priceUsd": "250.00",
        },
        {
            "chainId": "ethereum",
            "dexId": "uniswap",
            "quoteToken": {"symbol": "USDC"},
            "liquidity": {"usd": 500_000.0},
            "priceUsd": "185.00",
        },
        {
            "chainId": "solana",
            "dexId": "non-usd-dex",
            "quoteToken": {"symbol": "BONK"},
            "liquidity": {"usd": 200_000.0},
            "priceUsd": "190.00",
        },
    ]


class TestSolanaCapitalAgent(unittest.TestCase):
    def test_filter_and_find_best_spread_only_uses_liquid_solana_usd_pools(self):
        opp = agent.find_best_spread(_sample_pairs(), min_spread_pct=0.15, min_liquidity_usd=25_000.0)
        self.assertIsNotNone(opp)
        assert opp is not None
        self.assertEqual(opp.buy_dex, "raydium")
        self.assertEqual(opp.sell_dex, "orca")
        self.assertAlmostEqual(opp.gross_spread_pct, 0.4, places=3)
        self.assertEqual(opp.min_liquidity_usd, 95_000.0)

    def test_find_best_spread_returns_none_below_threshold(self):
        opp = agent.find_best_spread(_sample_pairs(), min_spread_pct=0.50, min_liquidity_usd=25_000.0)
        self.assertIsNone(opp)

    def test_rpc_health_and_balance_parsing(self):
        def fake_http(url: str, payload: dict | None, timeout: float) -> dict:
            assert payload is not None
            if payload["method"] == "getSlot":
                return {"jsonrpc": "2.0", "id": 1, "result": 300_123_456}
            if payload["method"] == "getBalance":
                return {"jsonrpc": "2.0", "id": 1, "result": {"value": 2_500_000_000}}
            return {}

        status = agent.check_solana_rpc(
            rpc_url="https://mock.solana.rpc",
            wallet_address="AN9Wgpx31Ue9G8bVqXbyXzTCa6PVLt6ZbQxhtCp4fu6K",
            http_fn=fake_http,
        )
        self.assertTrue(status.healthy)
        self.assertEqual(status.slot, 300_123_456)
        self.assertEqual(status.wallet_sol, 2.5)

    def test_rpc_error_fails_closed(self):
        def failing_http(url: str, payload: dict | None, timeout: float) -> dict:
            raise TimeoutError("simulated timeout")

        status = agent.check_solana_rpc(rpc_url="https://mock.solana.rpc", http_fn=failing_http)
        self.assertFalse(status.healthy)
        self.assertIn("simulated timeout", status.error)

    def test_risk_guard_enforces_caps_halt_and_dry_run(self):
        opp = agent.find_best_spread(_sample_pairs())
        assert opp is not None
        policy = agent.RiskGuardPolicy(
            min_spread_pct=0.15,
            min_liquidity_usd=25_000.0,
            max_position_usdc=50.0,
            daily_loss_limit_usdc=20.0,
            dry_run=True,
        )

        # Caps oversized order to max_position_usdc while keeping DRY_RUN mode.
        ok_verdict = agent.evaluate_risk_guard(opp, policy, proposed_size_usdc=200.0, realized_loss_today_usdc=5.0)
        self.assertTrue(ok_verdict.approved)
        self.assertEqual(ok_verdict.capped_size_usdc, 50.0)
        self.assertEqual(ok_verdict.mode, "DRY_RUN")

        # Halts immediately when daily loss threshold is reached.
        halted = agent.evaluate_risk_guard(opp, policy, proposed_size_usdc=20.0, realized_loss_today_usdc=20.0)
        self.assertFalse(halted.approved)
        self.assertEqual(halted.reason, "daily_loss_limit_reached")
        self.assertEqual(halted.capped_size_usdc, 0.0)

    def test_treasury_split_70_30_and_zero_profit(self):
        pos = agent.calculate_treasury_split(seed_usdc=100.0, net_profit_usdc=20.0)
        self.assertEqual(pos["withdraw_to_vault_usdc"], 14.0)
        self.assertEqual(pos["retained_working_usdc"], 36.0)

        zero = agent.calculate_treasury_split(seed_usdc=100.0, net_profit_usdc=0.0)
        self.assertEqual(zero["withdraw_to_vault_usdc"], 0.0)
        self.assertEqual(zero["retained_working_usdc"], 100.0)

    def test_run_once_end_to_end_offline(self):
        def fake_http(url: str, payload: dict | None, timeout: float) -> dict:
            if payload is None:
                return {"pairs": _sample_pairs()}
            if payload["method"] == "getSlot":
                return {"jsonrpc": "2.0", "id": 1, "result": 300_999_000}
            return {"jsonrpc": "2.0", "id": 1, "result": {"value": 0}}

        res = agent.run_once(proposed_size_usdc=150.0, http_fn=fake_http)
        self.assertTrue(res["rpc_status"]["healthy"])
        self.assertEqual(res["feed_error"], "")
        self.assertEqual(res["opportunity"]["buy_dex"], "raydium")
        self.assertTrue(res["guard_verdict"]["approved"])
        self.assertEqual(res["guard_verdict"]["capped_size_usdc"], 100.0)
        self.assertEqual(res["guard_verdict"]["mode"], "DRY_RUN")


if __name__ == "__main__":
    unittest.main()
