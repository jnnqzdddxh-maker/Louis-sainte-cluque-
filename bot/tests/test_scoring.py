import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config
from core.scoring import (
    Confidence,
    MarketSignal,
    TwitterSignal,
    WalletTrackerSignal,
    compute_score,
)

CFG = load_config()

GOOD_MARKET = MarketSignal(
    liquidity_usd=50_000,
    top_holder_concentration_pct=10,
    volume_current=100_000,
    volume_avg_baseline=50_000,
    breakout_detected=True,
)
NO_TWITTER = TwitterSignal(mention_count=0, most_recent_mention_age_minutes=None)


def test_anti_rug_filter_rejects_low_liquidity():
    bad_market = MarketSignal(
        liquidity_usd=100,  # sous min_liquidity_usd
        top_holder_concentration_pct=10,
        volume_current=100_000,
        volume_avg_baseline=50_000,
        breakout_detected=True,
    )
    result = compute_score(WalletTrackerSignal(5), bad_market, NO_TWITTER, CFG)
    assert result.rejected_anti_rug is True
    assert result.total_score == 0.0
    assert result.confidence == Confidence.FAIBLE


def test_anti_rug_filter_rejects_high_concentration():
    bad_market = MarketSignal(
        liquidity_usd=50_000,
        top_holder_concentration_pct=90,  # au-dessus du max autorisé
        volume_current=100_000,
        volume_avg_baseline=50_000,
        breakout_detected=True,
    )
    result = compute_score(WalletTrackerSignal(5), bad_market, NO_TWITTER, CFG)
    assert result.rejected_anti_rug is True


def test_anti_rug_filter_rejects_unrenounced_mint_authority():
    bad_market = MarketSignal(
        liquidity_usd=50_000,
        top_holder_concentration_pct=10,
        volume_current=100_000,
        volume_avg_baseline=50_000,
        breakout_detected=True,
        mint_authority_renounced=False,
    )
    result = compute_score(WalletTrackerSignal(5), bad_market, NO_TWITTER, CFG)
    assert result.rejected_anti_rug is True
    assert "mint authority" in result.rejection_reason


def test_anti_rug_filter_rejects_unrenounced_freeze_authority():
    bad_market = MarketSignal(
        liquidity_usd=50_000,
        top_holder_concentration_pct=10,
        volume_current=100_000,
        volume_avg_baseline=50_000,
        breakout_detected=True,
        freeze_authority_renounced=False,
    )
    result = compute_score(WalletTrackerSignal(5), bad_market, NO_TWITTER, CFG)
    assert result.rejected_anti_rug is True
    assert "freeze authority" in result.rejection_reason


def test_wallet_signal_below_trigger_scores_zero_on_wallet_subscore():
    result = compute_score(WalletTrackerSignal(0), GOOD_MARKET, NO_TWITTER, CFG)
    assert result.wallet_subscore == 0.0


def test_wallet_signal_at_full_threshold_scores_max():
    full = CFG["scoring"]["wallet_tracker"]["min_wallets_for_full_signal"]
    result = compute_score(WalletTrackerSignal(full), GOOD_MARKET, NO_TWITTER, CFG)
    assert result.wallet_subscore == 100.0


def test_more_wallets_never_decreases_score():
    r_low = compute_score(WalletTrackerSignal(2), GOOD_MARKET, NO_TWITTER, CFG)
    r_high = compute_score(WalletTrackerSignal(10), GOOD_MARKET, NO_TWITTER, CFG)
    assert r_high.total_score >= r_low.total_score


def test_very_haute_confidence_requires_high_score():
    thresholds = CFG["scoring"]["confidence_thresholds"]
    full_wallets = CFG["scoring"]["wallet_tracker"]["min_wallets_for_full_signal"]
    strong_twitter = TwitterSignal(mention_count=5, most_recent_mention_age_minutes=1)
    result = compute_score(WalletTrackerSignal(full_wallets), GOOD_MARKET, strong_twitter, CFG)
    assert result.total_score > thresholds["high_max"]
    assert result.confidence == Confidence.TRES_HAUTE


def test_stale_twitter_mention_does_not_count():
    old_mention = TwitterSignal(
        mention_count=3,
        most_recent_mention_age_minutes=CFG["scoring"]["twitter"]["max_signal_age_minutes"] + 1,
    )
    result = compute_score(WalletTrackerSignal(0), GOOD_MARKET, old_mention, CFG)
    assert result.twitter_subscore == 0.0


def test_score_is_bounded_0_100():
    huge_wallets = WalletTrackerSignal(1000)
    huge_twitter = TwitterSignal(mention_count=1000, most_recent_mention_age_minutes=0)
    result = compute_score(huge_wallets, GOOD_MARKET, huge_twitter, CFG)
    assert 0.0 <= result.total_score <= 100.0
