import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config
from core.engine import TradingEngine
from core.logging_store import DecisionLogger, StateStore
from core.scoring import Confidence
from connectors.twitter_watch import TwitterWatcher

CFG = load_config()
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _NullSearchBackend:
    """Aucun appel réseau — les tests ne doivent pas dépendre de Reddit."""

    def search(self, query: str) -> list:
        return []


@dataclass
class FakeRawMarketData:
    price_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    volume_avg_baseline_usd: float
    top_holder_concentration_pct: float
    breakout_detected: bool
    symbol: str = "FAKE"
    has_social_links: bool = True
    paired_with_recognized_quote: bool = True
    mint_authority_renounced: bool = True
    freeze_authority_renounced: bool = True


def _engine(tmp_path, fetchers) -> TradingEngine:
    return TradingEngine(
        dry_run=True,
        market_data_fetchers=fetchers,
        capital_eur=CFG["risk"]["capital_eur"],
        config=CFG,
        twitter_watcher=TwitterWatcher(config=CFG, backend=_NullSearchBackend()),
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


def _zero_liquidity_market_fetcher(_token_address: str) -> FakeRawMarketData:
    """Simule un pool pump.fun tout juste créé : DexPaprika n'a pas encore
    indexé sa vraie liquidité (voir README/config.yaml:market_scan.
    liquidity_watchlist, 15/09/2026 -- observé en dry-run réel : 24 des 38
    rejets Solana d'un même log étaient à liquidité EXACTEMENT 0$)."""
    return FakeRawMarketData(
        price_usd=1.0,
        liquidity_usd=0.0,
        volume_24h_usd=0.0,
        volume_avg_baseline_usd=0.0,
        top_holder_concentration_pct=5,
        breakout_detected=False,
    )


def test_liquidity_rejected_token_is_added_to_watchlist_not_lost(tmp_path):
    engine = _engine(tmp_path, {"solana": _zero_liquidity_market_fetcher})
    engine.on_market_scan_hit("FRESH_TOKEN", "solana", NOW, source="market_scan_new_listing")

    assert engine.candidates["FRESH_TOKEN"].score.rejected_anti_rug
    assert "liquidité" in engine.candidates["FRESH_TOKEN"].score.rejection_reason
    assert "FRESH_TOKEN" in engine.liquidity_watchlist
    assert engine.liquidity_watchlist["FRESH_TOKEN"]["attempts"] == 1


def test_mint_authority_rejection_is_not_added_to_watchlist(tmp_path):
    """Contrairement à la liquidité, l'autorité mint/freeze révoquée ne
    change pas dans le temps -- pas d'intérêt à réessayer."""

    def rugged_fetcher(_token_address: str) -> FakeRawMarketData:
        return FakeRawMarketData(
            price_usd=1.0,
            liquidity_usd=50_000,
            volume_24h_usd=100_000,
            volume_avg_baseline_usd=50_000,
            top_holder_concentration_pct=5,
            breakout_detected=True,
            mint_authority_renounced=False,
        )

    engine = _engine(tmp_path, {"solana": rugged_fetcher})
    engine.on_market_scan_hit("RUGGED_TOKEN", "solana", NOW, source="market_scan_new_listing")

    assert engine.candidates["RUGGED_TOKEN"].score.rejected_anti_rug
    assert "RUGGED_TOKEN" not in engine.liquidity_watchlist


def test_watchlist_recheck_opens_a_position_once_liquidity_catches_up(tmp_path):
    """Reproduit le scénario concret du 15/09/2026 : un token pump.fun
    d'abord vu avec 0$ de liquidité (rejeté), puis réévalué automatiquement
    -- sans attendre qu'il réapparaisse dans un scan -- une fois que sa
    vraie liquidité a été indexée."""
    liquidity_by_call = iter([0.0, 0.0, 3_000.0])  # 3e appel : liquidité enfin indexée

    def catching_up_fetcher(_token_address: str) -> FakeRawMarketData:
        liq = next(liquidity_by_call, 3_000.0)
        return FakeRawMarketData(
            price_usd=1.0,
            liquidity_usd=liq,
            volume_24h_usd=3_000.0 if liq > 0 else 0.0,
            volume_avg_baseline_usd=0.0,
            top_holder_concentration_pct=5,
            breakout_detected=True,
            paired_with_recognized_quote=True,
        )

    engine = _engine(tmp_path, {"solana": catching_up_fetcher})
    engine.on_market_scan_hit("CATCHING_UP_TOKEN", "solana", NOW, source="market_scan_new_listing")
    assert "CATCHING_UP_TOKEN" in engine.liquidity_watchlist

    engine.recheck_liquidity_watchlist(NOW)  # 2e appel : toujours 0$
    assert "CATCHING_UP_TOKEN" in engine.liquidity_watchlist
    assert len(engine.position_manager.positions) == 0

    engine.recheck_liquidity_watchlist(NOW)  # 3e appel : liquidité catchée
    open_positions = [p for p in engine.position_manager.positions.values() if not p.closed]
    assert len(open_positions) == 1
    assert open_positions[0].token_id == "CATCHING_UP_TOKEN"
    assert "CATCHING_UP_TOKEN" not in engine.liquidity_watchlist  # nettoyé une fois passé


def test_watchlist_entry_expires_after_max_age(tmp_path):
    engine = _engine(tmp_path, {"solana": _zero_liquidity_market_fetcher})
    engine.on_market_scan_hit("STALE_TOKEN", "solana", NOW, source="market_scan_new_listing")
    assert "STALE_TOKEN" in engine.liquidity_watchlist

    max_age = CFG["market_scan"]["liquidity_watchlist"]["max_age_minutes"]
    later = NOW + timedelta(minutes=max_age + 1)
    engine.recheck_liquidity_watchlist(later)

    assert "STALE_TOKEN" not in engine.liquidity_watchlist
