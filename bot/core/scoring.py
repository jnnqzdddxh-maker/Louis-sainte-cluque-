"""Moteur de scoring — score de confiance 0-100 (section 3 du cahier des charges).

Fonctions pures : elles ne font aucun appel réseau. Les connecteurs
(bot/connectors/*) sont responsables de peupler les dataclasses de signal ;
ce module se contente de les combiner en un score et un niveau de confiance.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.config import load_config


class Confidence(str, Enum):
    FAIBLE = "faible"
    MOYENNE = "moyenne"
    HAUTE = "haute"
    TRES_HAUTE = "tres_haute"


@dataclass(frozen=True)
class WalletTrackerSignal:
    """Wallets suivis ayant acheté le token dans la fenêtre de temps configurée."""
    distinct_wallets_buying: int


@dataclass(frozen=True)
class MarketSignal:
    """Prix / volume / liquidité — inclut le filtre anti-rug."""
    liquidity_usd: float
    top_holder_concentration_pct: float
    volume_current: float
    volume_avg_baseline: float
    breakout_detected: bool
    has_social_links: bool = False              # site/twitter/telegram déclarés (bonus légitimité)
    paired_with_recognized_quote: bool = False   # appairé à SOL/ETH/USDC/USDT plutôt qu'à un token obscur


@dataclass(frozen=True)
class TwitterSignal:
    """Veille périodique (5 min), pas de streaming temps réel."""
    mention_count: int
    most_recent_mention_age_minutes: float | None


@dataclass(frozen=True)
class ScoreResult:
    total_score: float                 # 0-100
    confidence: Confidence
    wallet_subscore: float
    market_subscore: float
    twitter_subscore: float
    rejected_anti_rug: bool
    rejection_reason: str | None


def _wallet_subscore(signal: WalletTrackerSignal, cfg: dict) -> float:
    wc = cfg["scoring"]["wallet_tracker"]
    trigger = wc["min_wallets_to_trigger"]
    full = wc["min_wallets_for_full_signal"]
    n = signal.distinct_wallets_buying
    if n < trigger:
        return 0.0
    if n >= full:
        return 100.0
    # interpolation linéaire entre le seuil de déclenchement et le seuil "signal plein"
    return 100.0 * (n - trigger) / (full - trigger)


def _anti_rug_filter(signal: MarketSignal, cfg: dict) -> tuple[bool, str | None]:
    mc = cfg["scoring"]["price_volume_liquidity"]
    if signal.liquidity_usd < mc["min_liquidity_usd"]:
        return False, (
            f"liquidité {signal.liquidity_usd:.0f}$ < minimum "
            f"{mc['min_liquidity_usd']}$"
        )
    if signal.top_holder_concentration_pct > mc["max_top_holder_concentration_pct"]:
        return False, (
            f"concentration top holder {signal.top_holder_concentration_pct:.1f}% > "
            f"maximum {mc['max_top_holder_concentration_pct']}%"
        )
    return True, None


def _market_subscore(signal: MarketSignal, cfg: dict) -> float:
    """Momentum (jusqu'à 80 pts) + légitimité (jusqu'à 20 pts). La
    légitimité n'est PAS éliminatoire (contrairement au filtre anti-rug) :
    un token peut ne pas avoir renseigné ses réseaux sociaux sans être une
    arnaque pour autant — ça reste un bonus, pas une porte."""
    mc = cfg["scoring"]["price_volume_liquidity"]
    baseline = signal.volume_avg_baseline or 0.0
    if baseline <= 0:
        volume_ratio_score = 0.0
    else:
        ratio = signal.volume_current / baseline
        volume_ratio_score = min(50.0, (ratio / mc["volume_spike_multiplier"]) * 50.0)
    breakout_score = 30.0 if signal.breakout_detected else 0.0
    legitimacy_score = (10.0 if signal.has_social_links else 0.0) + (
        10.0 if signal.paired_with_recognized_quote else 0.0
    )
    return min(100.0, volume_ratio_score + breakout_score + legitimacy_score)


def _twitter_subscore(signal: TwitterSignal, cfg: dict) -> float:
    tc = cfg["scoring"]["twitter"]
    if signal.mention_count <= 0:
        return 0.0
    if (
        signal.most_recent_mention_age_minutes is not None
        and signal.most_recent_mention_age_minutes > tc["max_signal_age_minutes"]
    ):
        return 0.0
    return min(100.0, signal.mention_count * 25.0)


def _confidence_from_score(score: float, cfg: dict) -> Confidence:
    t = cfg["scoring"]["confidence_thresholds"]
    if score <= t["low_max"]:
        return Confidence.FAIBLE
    if score <= t["medium_max"]:
        return Confidence.MOYENNE
    if score <= t["high_max"]:
        return Confidence.HAUTE
    return Confidence.TRES_HAUTE


def compute_score(
    wallet_signal: WalletTrackerSignal,
    market_signal: MarketSignal,
    twitter_signal: TwitterSignal,
    config: dict | None = None,
) -> ScoreResult:
    """Combine les 3 signaux en un score 0-100 et un niveau de confiance.

    Le filtre anti-rug (liquidité min, concentration top holder max) est
    éliminatoire : s'il échoue, le token est rejeté (score forcé à 0,
    confiance "faible") quels que soient les autres signaux.
    """
    cfg = config or load_config()

    passes, reason = _anti_rug_filter(market_signal, cfg)
    if not passes:
        return ScoreResult(
            total_score=0.0,
            confidence=Confidence.FAIBLE,
            wallet_subscore=0.0,
            market_subscore=0.0,
            twitter_subscore=0.0,
            rejected_anti_rug=True,
            rejection_reason=reason,
        )

    weights = cfg["scoring"]["weights"]
    wallet_sub = _wallet_subscore(wallet_signal, cfg)
    market_sub = _market_subscore(market_signal, cfg)
    twitter_sub = _twitter_subscore(twitter_signal, cfg)

    total = (
        wallet_sub * weights["wallet_tracker"]
        + market_sub * weights["price_volume_liquidity"]
        + twitter_sub * weights["twitter_news"]
    )
    total = max(0.0, min(100.0, total))

    return ScoreResult(
        total_score=total,
        confidence=_confidence_from_score(total, cfg),
        wallet_subscore=wallet_sub,
        market_subscore=market_sub,
        twitter_subscore=twitter_sub,
        rejected_anti_rug=False,
        rejection_reason=None,
    )
