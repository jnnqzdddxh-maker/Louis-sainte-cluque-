import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config
from core.engine import TradingEngine
from core.logging_store import DecisionLogger, StateStore
from core.scoring import Confidence

CFG = load_config()
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass
class FakeRawMarketData:
    price_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    volume_avg_baseline_usd: float
    top_holder_concentration_pct: float
    breakout_detected: bool


def _engine(tmp_path, fetchers) -> TradingEngine:
    return TradingEngine(
        dry_run=True,
        market_data_fetchers=fetchers,
        capital_eur=CFG["risk"]["capital_eur"],
        config=CFG,
        logger=DecisionLogger(path=tmp_path / "decisions.jsonl", config=CFG),
        state_store=StateStore(path=tmp_path / "state.sqlite3", config=CFG),
    )


def _strong_market_fetcher(_token_address: str) -> FakeRawMarketData:
    return FakeRawMarketData(
        price_usd=1.0,
        liquidity_usd=100_000,
        volume_24h_usd=200_000,
        volume_avg_baseline_usd=50_000,
        top_holder_concentration_pct=5,
        breakout_detected=True,
    )


def test_market_scan_hit_never_needs_a_wallet_event(tmp_path):
    engine = _engine(tmp_path, {"solana": _strong_market_fetcher})
    engine.on_market_scan_hit("TOKEN_FROM_SCAN", "solana", NOW)

    candidate = engine.candidates["TOKEN_FROM_SCAN"]
    assert candidate.score.wallet_subscore == 0.0
    assert not candidate.score.rejected_anti_rug


def test_market_scan_hit_caps_below_haute_confidence(tmp_path):
    engine = _engine(tmp_path, {"solana": _strong_market_fetcher})
    engine.on_market_scan_hit("TOKEN_FROM_SCAN", "solana", NOW)

    candidate = engine.candidates["TOKEN_FROM_SCAN"]
    thresholds = CFG["scoring"]["confidence_thresholds"]
    assert candidate.score.total_score <= thresholds["medium_max"]
    assert candidate.score.confidence in (Confidence.FAIBLE, Confidence.MOYENNE)


def test_market_scan_hit_can_still_open_a_position_at_moyenne_confidence(tmp_path):
    engine = _engine(tmp_path, {"solana": _strong_market_fetcher})
    assert CFG["execution"]["min_confidence_to_trade"] == "moyenne"

    engine.on_market_scan_hit("TOKEN_FROM_SCAN", "solana", NOW)

    open_positions = [p for p in engine.position_manager.positions.values() if not p.closed]
    assert len(open_positions) == 1
    assert open_positions[0].token_id == "TOKEN_FROM_SCAN"


def test_wallet_triggered_path_still_requires_the_threshold(tmp_path):
    engine = _engine(tmp_path, {"solana": _strong_market_fetcher})
    engine._maybe_score_candidate(
        "TOKEN_NO_WALLETS", "solana", NOW, require_wallet_trigger=True, source="wallet_tracker"
    )

    assert "TOKEN_NO_WALLETS" not in engine.candidates


def test_excluded_stablecoin_is_never_scored(tmp_path):
    engine = _engine(tmp_path, {"solana": _strong_market_fetcher})
    usdc = CFG["market_scan"]["excluded_tokens"]["solana"][0]

    engine.on_market_scan_hit(usdc, "solana", NOW)

    assert usdc not in engine.candidates
    assert len(engine.position_manager.positions) == 0
