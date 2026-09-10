"""Paliers de sortie, trailing stop et sizing (sections 4 et 5).

Interprétation d'un point non-explicite du cahier des charges, à valider
pendant le dry-run (section 7) :
- Le tableau de la section 5 ne mentionne explicitement le "trailing normal"
  qu'entre x2→x5 et x10→x20. On applique le même trailing "normal" entre
  x5→x10 par cohérence (sinon la position n'aurait aucun SL mobile sur ce
  segment). À confirmer/ajuster après lecture des logs de dry-run.
- "Gestion manuelle" au-delà de x20 (confiance faible/moyenne) ou sur le
  dernier reliquat après x100 (confiance très haute) : le bot continue de
  calculer et d'exécuter le stop-loss automatiquement (c'est la fonction
  d'un SL), mais n'invente aucun palier de vente automatique supplémentaire
  au-delà. Le dashboard doit mettre ces positions en évidence pour une
  intervention humaine discrétionnaire avant que le SL ne se déclenche.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from core.config import load_config
from core.scoring import Confidence

ActionType = Literal["sell", "sl_update", "close"]


@dataclass(frozen=True)
class Action:
    type: ActionType
    position_id: str
    pct_of_initial_position: float  # 0 si sl_update pur
    price: float
    reason: str
    timestamp: datetime


@dataclass(frozen=True)
class Tier:
    multiple: float
    sell_pct_of_remaining: float
    on_remainder: str
    requires_confidence: Confidence | None = None


def _load_tiers(cfg: dict) -> list[Tier]:
    tiers = []
    for raw in cfg["exit_strategy"]["tiers"]:
        req = raw.get("requires_confidence")
        tiers.append(
            Tier(
                multiple=raw["multiple"],
                sell_pct_of_remaining=raw["sell_pct_of_remaining"],
                on_remainder=raw["on_remainder"],
                requires_confidence=Confidence(req) if req else None,
            )
        )
    return sorted(tiers, key=lambda t: t.multiple)


@dataclass
class Position:
    position_id: str
    token_id: str
    chain: str
    entry_price: float
    confidence: Confidence
    size_eur: float
    opened_at: datetime

    remaining_pct: float = 100.0          # % de la position initiale encore détenu
    stage_index: int = 0                  # nb de paliers déjà franchis
    peak_price_since_stage: float = field(init=False)
    locked_sl_price: float | None = None  # None => catastrophe SL seul actif
    catastrophe_sl_price: float = field(init=False)
    closed: bool = False
    close_reason: str | None = None

    def __post_init__(self) -> None:
        self.peak_price_since_stage = self.entry_price


class PositionManager:
    def __init__(self, config: dict | None = None):
        self.cfg = config or load_config()
        self.tiers_by_confidence: dict[Confidence, list[Tier]] = {}
        all_tiers = _load_tiers(self.cfg)
        for conf in Confidence:
            self.tiers_by_confidence[conf] = [
                t for t in all_tiers if t.requires_confidence in (None, conf)
            ]
        self.positions: dict[str, Position] = {}

    # -- ouverture -----------------------------------------------------
    def open_position(
        self,
        token_id: str,
        chain: str,
        entry_price: float,
        confidence: Confidence,
        size_eur: float,
        now: datetime | None = None,
    ) -> Position:
        sizing = self.cfg["sizing"]
        size_eur = max(
            sizing["position_size_min_eur"],
            min(sizing["position_size_max_eur"], size_eur),
        )
        if sizing.get("force_minimum_size_until_proven"):
            size_eur = sizing["position_size_min_eur"]

        pos = Position(
            position_id=str(uuid.uuid4()),
            token_id=token_id,
            chain=chain,
            entry_price=entry_price,
            confidence=confidence,
            size_eur=size_eur,
            opened_at=now or datetime.now(timezone.utc),
        )
        cat_pct = self.cfg["risk"]["catastrophe_stop_loss_pct"]
        pos.catastrophe_sl_price = entry_price * (1 + cat_pct / 100)
        self.positions[pos.position_id] = pos
        return pos

    # -- moteur ----------------------------------------------------------
    def _breakeven_price(self, pos: Position) -> float:
        fees_cfg = self.cfg["fees"]
        if not fees_cfg.get("include_fees_in_breakeven"):
            return pos.entry_price
        fee_pct = fees_cfg["estimated_swap_fee_pct"].get(pos.chain, 0.0)
        return pos.entry_price * (1 + 2 * fee_pct / 100)  # frais achat + vente

    def _locked_price_for(self, pos: Position, on_remainder: str) -> float:
        mapping = {
            "breakeven": self._breakeven_price(pos),
            "lock_x2": pos.entry_price * 2,
            "lock_x5": pos.entry_price * 5,
            "lock_x10": pos.entry_price * 10,
            "manual_beyond": pos.entry_price * 20,
        }
        return mapping[on_remainder]

    def _trailing_pct(self, pos: Position, tiers: list[Tier]) -> float:
        ex = self.cfg["exit_strategy"]
        x20_index = next(
            (i for i, t in enumerate(tiers) if t.multiple == 20), None
        )
        if x20_index is not None and pos.stage_index > x20_index:
            if pos.confidence == Confidence.TRES_HAUTE:
                return ex["trailing_pct_beyond_x20_tres_haute"]
            return ex["manual_trailing_pct_beyond_cap"]
        key = pos.confidence.value
        return ex["trailing_pct_by_confidence"].get(key, ex["trailing_pct_by_confidence"]["moyenne"])

    def _effective_sl(self, pos: Position, tiers: list[Tier]) -> float | None:
        if pos.locked_sl_price is None:
            return None  # catastrophe SL only
        trailing_pct = self._trailing_pct(pos, tiers)
        trailing_sl = pos.peak_price_since_stage * (1 - trailing_pct / 100)
        return max(pos.locked_sl_price, trailing_sl)

    def update_price(self, position_id: str, price: float, now: datetime | None = None) -> list[Action]:
        pos = self.positions[position_id]
        if pos.closed:
            return []
        now = now or datetime.now(timezone.utc)
        tiers = self.tiers_by_confidence[pos.confidence]
        actions: list[Action] = []

        pos.peak_price_since_stage = max(pos.peak_price_since_stage, price)

        # 1. Stop-loss catastrophe (uniquement avant le premier palier)
        if pos.stage_index == 0 and price <= pos.catastrophe_sl_price:
            actions.append(self._close(pos, price, now, "stop_catastrophe"))
            return actions

        # 2. Stop-loss courant (verrouillé et/ou trailing) déjà actif
        effective_sl = self._effective_sl(pos, tiers)
        if effective_sl is not None and price <= effective_sl:
            actions.append(self._close(pos, price, now, f"stop_loss_{effective_sl:.8f}"))
            return actions

        # 3. Franchissement d'un nouveau palier (on ne peut en franchir
        #    qu'un seul par appel : dry_run/main appellent update_price à
        #    haute fréquence, donc pas de saut de palier en pratique)
        multiple = price / pos.entry_price
        if pos.stage_index < len(tiers):
            next_tier = tiers[pos.stage_index]
            if multiple >= next_tier.multiple:
                actions.extend(self._execute_tier(pos, next_tier, price, now))

        return actions

    def _execute_tier(self, pos: Position, tier: Tier, price: float, now: datetime) -> list[Action]:
        actions: list[Action] = []
        if tier.sell_pct_of_remaining > 0:
            sell_pct_of_initial = pos.remaining_pct * tier.sell_pct_of_remaining / 100
            pos.remaining_pct -= sell_pct_of_initial
            actions.append(
                Action(
                    type="sell",
                    position_id=pos.position_id,
                    pct_of_initial_position=sell_pct_of_initial,
                    price=price,
                    reason=f"palier_x{tier.multiple:g}",
                    timestamp=now,
                )
            )
        pos.locked_sl_price = self._locked_price_for(pos, tier.on_remainder)
        pos.stage_index += 1
        pos.peak_price_since_stage = price
        actions.append(
            Action(
                type="sl_update",
                position_id=pos.position_id,
                pct_of_initial_position=0.0,
                price=pos.locked_sl_price,
                reason=f"lock_{tier.on_remainder}",
                timestamp=now,
            )
        )
        if pos.remaining_pct <= 1e-9:
            actions.append(self._close(pos, price, now, "position_epuisee"))
        return actions

    def _close(self, pos: Position, price: float, now: datetime, reason: str) -> Action:
        remaining = pos.remaining_pct
        pos.remaining_pct = 0.0
        pos.closed = True
        pos.close_reason = reason
        return Action(
            type="close",
            position_id=pos.position_id,
            pct_of_initial_position=remaining,
            price=price,
            reason=reason,
            timestamp=now,
        )
