import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config
from core.position_manager import PositionManager
from core.scoring import Confidence

CFG = load_config()
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _pm() -> PositionManager:
    return PositionManager(CFG)


def test_catastrophe_stop_closes_position_before_x2():
    pm = _pm()
    pos = pm.open_position("TOK", "solana", 1.0, Confidence.MOYENNE, 20, now=NOW)
    cat_pct = CFG["risk"]["catastrophe_stop_loss_pct"]
    assert -70 <= cat_pct <= -60

    price_just_above = 1.0 * (1 + cat_pct / 100) + 0.001
    assert pm.update_price(pos.position_id, price_just_above, NOW) == []

    price_at_catastrophe = 1.0 * (1 + cat_pct / 100)
    actions = pm.update_price(pos.position_id, price_at_catastrophe, NOW)
    assert len(actions) == 1
    assert actions[0].type == "close"
    assert actions[0].reason == "stop_catastrophe"
    assert actions[0].pct_of_initial_position == pytest.approx(100.0)
    assert pos.closed


def test_x2_sells_40_pct_and_locks_breakeven():
    pm = _pm()
    pos = pm.open_position("TOK", "solana", 1.0, Confidence.MOYENNE, 20, now=NOW)

    actions = pm.update_price(pos.position_id, 2.0, NOW)
    sells = [a for a in actions if a.type == "sell"]
    sl_updates = [a for a in actions if a.type == "sl_update"]

    assert len(sells) == 1
    assert sells[0].pct_of_initial_position == pytest.approx(40.0)
    assert pos.remaining_pct == pytest.approx(60.0)
    assert len(sl_updates) == 1
    assert pos.locked_sl_price is not None
    assert pos.locked_sl_price == pytest.approx(1.0, rel=0.01)  # breakeven ~ entry price
    assert not pos.closed


def test_stop_loss_after_x2_closes_remainder():
    pm = _pm()
    pos = pm.open_position("TOK", "solana", 1.0, Confidence.MOYENNE, 20, now=NOW)
    pm.update_price(pos.position_id, 2.0, NOW)  # atteint x2, SL -> breakeven

    breakeven = pos.locked_sl_price
    actions = pm.update_price(pos.position_id, breakeven - 0.001, NOW)
    assert len(actions) == 1
    assert actions[0].type == "close"
    assert pos.closed
    assert pos.remaining_pct == 0.0


def test_trailing_stop_triggers_on_pullback_between_x2_and_x5():
    pm = _pm()
    pos = pm.open_position("TOK", "solana", 1.0, Confidence.MOYENNE, 20, now=NOW)
    pm.update_price(pos.position_id, 2.0, NOW)  # x2

    trailing_pct = CFG["exit_strategy"]["trailing_pct_by_confidence"]["moyenne"]
    peak = 3.6
    pm.update_price(pos.position_id, peak, NOW)  # monte sans franchir x5, met à jour le peak
    assert not pos.closed

    trailing_sl = peak * (1 - trailing_pct / 100)
    actions = pm.update_price(pos.position_id, trailing_sl - 0.001, NOW)
    assert len(actions) == 1
    assert actions[0].type == "close"
    assert actions[0].reason.startswith("stop_loss")
    assert pos.closed


def test_full_escalator_tres_haute_confidence_matches_spec_percentages():
    """Reproduit la table de la section 5 du cahier des charges : les
    reliquats attendus (~45% à x10, ~11% à x20, ~2.75% à x100) doivent
    tomber directement de la logique des paliers, pas être recalés à la main.
    """
    pm = _pm()
    pos = pm.open_position("TOK", "solana", 1.0, Confidence.TRES_HAUTE, 20, now=NOW)

    pm.update_price(pos.position_id, 2.0, NOW)
    assert pos.remaining_pct == pytest.approx(60.0)

    pm.update_price(pos.position_id, 5.0, NOW)  # pas de vente, juste verrouillage
    assert pos.remaining_pct == pytest.approx(60.0)
    assert pos.locked_sl_price == pytest.approx(2.0, rel=0.01)

    pm.update_price(pos.position_id, 10.0, NOW)
    assert pos.remaining_pct == pytest.approx(15.0)  # 75% de 60% vendu -> ~45% de la position initiale vendus ici

    pm.update_price(pos.position_id, 20.0, NOW)
    assert pos.remaining_pct == pytest.approx(3.75)  # ~11% de la position initiale vendus ici

    actions = pm.update_price(pos.position_id, 100.0, NOW)
    sell = next(a for a in actions if a.type == "sell")
    assert sell.pct_of_initial_position == pytest.approx(2.8125, rel=0.01)  # ~2.75% (spec)
    assert pos.remaining_pct == pytest.approx(0.9375)
    assert not pos.closed  # reliquat géré via SL 50% depuis le plus haut, pas de clôture automatique ici


def test_moyenne_confidence_has_no_x100_tier():
    pm = _pm()
    pos = pm.open_position("TOK", "solana", 1.0, Confidence.MOYENNE, 20, now=NOW)
    for multiple in (2.0, 5.0, 10.0, 20.0):
        pm.update_price(pos.position_id, multiple, NOW)
    remaining_at_x20 = pos.remaining_pct

    # Un token confiance moyenne qui grimpe par accident au-delà de x20 ne
    # déclenche aucun palier de vente supplémentaire (pas de x100).
    pm.update_price(pos.position_id, 100.0, NOW)
    assert pos.remaining_pct == pytest.approx(remaining_at_x20)


def test_sizing_forces_minimum_while_unproven():
    pm = _pm()
    assert CFG["sizing"]["force_minimum_size_until_proven"] is True
    pos = pm.open_position("TOK", "solana", 1.0, Confidence.HAUTE, 50, now=NOW)
    assert pos.size_eur == CFG["sizing"]["position_size_min_eur"]
