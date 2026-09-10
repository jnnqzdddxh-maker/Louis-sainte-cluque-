"""Moteur partagé entre dry_run.py (simulation) et main.py (réel).

Flux : un achat détecté par le wallet tracker peut faire d'un token un
"candidat" -> scoring (wallet + marché + twitter) -> si confiance
suffisante et garde-fous OK, ouverture d'une position -> à chaque tick de
prix, le position manager applique les paliers de sortie -> chaque vente
met à jour le risk guard (plafond de perte journalière).

Ce module ne décide JAMAIS d'exécuter un swap réel : `dry_run` est
propagé jusqu'aux connecteurs d'exécution (voir main.py), qui refusent de
signer/envoyer une transaction tant qu'il vaut True.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from core.config import load_config
from core.logging_store import DecisionLogger, StateStore
from core.position_manager import Action, Position, PositionManager
from core.risk_guard import RiskGuard
from core.scoring import Confidence, MarketSignal, ScoreResult, compute_score
from connectors.twitter_watch import TwitterWatcher
from connectors.wallet_tracker import WalletBuyEvent, WalletTracker

_CONFIDENCE_ORDER = list(Confidence)  # FAIBLE < MOYENNE < HAUTE < TRES_HAUTE


class RawMarketDataLike(Protocol):
    price_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    volume_avg_baseline_usd: float
    top_holder_concentration_pct: float
    breakout_detected: bool


MarketDataFetcher = Callable[[str], RawMarketDataLike]


@dataclass(frozen=True)
class ExecutionResult:
    executed: bool
    note: str
    signature: str | None = None


class ExecutionHandler(Protocol):
    """Point d'extension branché par main.py/dry_run.py vers les connecteurs
    réels (Jupiter / Robinhood RPC). Chaque implémentation DOIT respecter le
    flag dry_run qui lui est passé et ne rien signer/envoyer si True — c'est
    la même règle que dans connectors/solana_jupiter.py et
    connectors/robinhood_rpc.py.
    """

    def buy(self, token_address: str, size_eur: float, price_usd: float, *, dry_run: bool) -> ExecutionResult: ...

    def sell(
        self, token_address: str, pct_of_initial_position: float, price_usd: float, *, dry_run: bool
    ) -> ExecutionResult: ...


@dataclass
class Candidate:
    token_address: str
    chain: str
    score: ScoreResult
    scored_at: datetime


class TradingEngine:
    def __init__(
        self,
        *,
        dry_run: bool,
        market_data_fetchers: dict[str, MarketDataFetcher],
        capital_eur: float,
        config: dict | None = None,
        twitter_watcher: TwitterWatcher | None = None,
        logger: DecisionLogger | None = None,
        state_store: StateStore | None = None,
        execution_handlers: dict[str, ExecutionHandler] | None = None,
    ):
        self.dry_run = dry_run
        self.cfg = config or load_config()
        self.market_data_fetchers = market_data_fetchers
        self.execution_handlers = execution_handlers or {}
        self.wallet_tracker = WalletTracker(self.cfg)
        self.twitter_watcher = twitter_watcher or TwitterWatcher(self.cfg)
        self.position_manager = PositionManager(self.cfg)
        self.risk_guard = RiskGuard(self.cfg, capital_eur=capital_eur)
        self.logger = logger or DecisionLogger(config=self.cfg)
        self.state_store = state_store or StateStore(config=self.cfg)
        self.candidates: dict[str, Candidate] = {}
        self._token_to_position: dict[str, str] = {}

        self.logger.log("engine_start", dry_run=dry_run, capital_eur=capital_eur)

    # -- ingestion d'un achat détecté par le wallet tracker -----------------
    def on_wallet_buy_event(self, event: WalletBuyEvent) -> None:
        self.wallet_tracker.ingest(event)
        self._maybe_score_candidate(event.token_address, event.chain, event.timestamp)

    def _maybe_score_candidate(self, token_address: str, chain: str, now: datetime) -> None:
        wallet_signal = self.wallet_tracker.signal_for_token(token_address, now)
        min_trigger = self.cfg["scoring"]["wallet_tracker"]["min_wallets_to_trigger"]
        if wallet_signal.distinct_wallets_buying < min_trigger:
            return

        fetcher = self.market_data_fetchers.get(chain)
        if fetcher is None:
            self.logger.log("no_market_data_fetcher", chain=chain, token_address=token_address)
            return
        raw = fetcher(token_address)
        market_signal = MarketSignal(
            liquidity_usd=raw.liquidity_usd,
            top_holder_concentration_pct=raw.top_holder_concentration_pct,
            volume_current=raw.volume_24h_usd,
            volume_avg_baseline=raw.volume_avg_baseline_usd,
            breakout_detected=raw.breakout_detected,
        )

        self.twitter_watcher.poll(token_address)
        twitter_signal = self.twitter_watcher.signal_for_token(token_address, now)

        score = compute_score(wallet_signal, market_signal, twitter_signal, self.cfg)
        self.candidates[token_address] = Candidate(token_address, chain, score, now)
        self.logger.log("candidate_scored", token_address=token_address, chain=chain, score=score)

        if score.rejected_anti_rug:
            return

        min_conf = Confidence(self.cfg["execution"]["min_confidence_to_trade"])
        if _CONFIDENCE_ORDER.index(score.confidence) < _CONFIDENCE_ORDER.index(min_conf):
            return  # affiché au dashboard (transparence), jamais tradé

        if token_address in self._token_to_position:
            return  # déjà une position ouverte sur ce token

        self._try_open_position(token_address, chain, raw.price_usd, score.confidence, now)

    def _try_open_position(
        self, token_address: str, chain: str, price_usd: float, confidence: Confidence, now: datetime
    ) -> None:
        open_count = sum(1 for p in self.position_manager.positions.values() if not p.closed)
        can_open, reason = self.risk_guard.can_open_position(open_count)
        if not can_open:
            self.logger.log("position_rejected", token_address=token_address, reason=reason)
            return

        size_eur = self.cfg["sizing"]["position_size_min_eur"]

        handler = self.execution_handlers.get(chain)
        if handler is not None:
            try:
                result = handler.buy(token_address, size_eur, price_usd, dry_run=self.dry_run)
            except Exception as exc:
                self.logger.log(
                    "execution_failed",
                    action="buy",
                    token_address=token_address,
                    chain=chain,
                    error=str(exc),
                )
                if not self.dry_run:
                    self.risk_guard.halt_manual(
                        f"échec d'exécution à l'achat sur {token_address} ({chain}): {exc}", now
                    )
                return
            self.logger.log(
                "execution_result", action="buy", token_address=token_address, chain=chain,
                executed=result.executed, note=result.note, signature=result.signature,
            )

        pos = self.position_manager.open_position(token_address, chain, price_usd, confidence, size_eur, now)
        self._token_to_position[token_address] = pos.position_id
        self.state_store.save_position(pos.position_id, asdict(pos))
        self.logger.log(
            "position_opened",
            position_id=pos.position_id,
            token_address=token_address,
            chain=chain,
            entry_price=price_usd,
            confidence=confidence.value,
            size_eur=size_eur,
            dry_run=self.dry_run,
        )

    # -- boucle de prix -------------------------------------------------------
    def tick_prices(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        for token_address, position_id in list(self._token_to_position.items()):
            pos = self.position_manager.positions[position_id]
            if pos.closed:
                continue
            fetcher = self.market_data_fetchers.get(pos.chain)
            if fetcher is None:
                continue
            raw = fetcher(token_address)
            actions = self.position_manager.update_price(position_id, raw.price_usd, now)
            for action in actions:
                self._handle_action(pos, action)

    def _handle_action(self, pos: Position, action: Action) -> None:
        self.logger.log(
            "position_action",
            position_id=pos.position_id,
            token_address=pos.token_id,
            chain=pos.chain,
            action_type=action.type,
            pct_of_initial_position=action.pct_of_initial_position,
            price=action.price,
            reason=action.reason,
            dry_run=self.dry_run,
        )
        if action.type in ("sell", "close") and action.pct_of_initial_position > 0:
            handler = self.execution_handlers.get(pos.chain)
            if handler is not None:
                try:
                    result = handler.sell(
                        pos.token_id, action.pct_of_initial_position, action.price, dry_run=self.dry_run
                    )
                    self.logger.log(
                        "execution_result", action="sell", position_id=pos.position_id,
                        executed=result.executed, note=result.note, signature=result.signature,
                    )
                except Exception as exc:
                    # La décision de vendre (SL/palier) reste enregistrée dans le
                    # position manager (elle a déjà eu lieu) : l'échec porte sur
                    # l'EXÉCUTION, pas sur la décision. En live, on arrête tout
                    # net pour éviter d'ouvrir de nouvelles positions tant qu'un
                    # humain n'a pas réconcilié l'état on-chain réel avec l'état
                    # interne du bot.
                    self.logger.log(
                        "execution_failed", action="sell", position_id=pos.position_id, error=str(exc),
                    )
                    if not self.dry_run:
                        self.risk_guard.halt_manual(
                            f"échec d'exécution à la vente sur {pos.token_id}: {exc} — "
                            f"réconciliation manuelle requise avant reprise", action.timestamp
                        )

            pnl_eur = (
                pos.size_eur
                * (action.pct_of_initial_position / 100)
                * (action.price / pos.entry_price - 1)
            )
            self.risk_guard.record_pnl(pnl_eur, action.timestamp)
            self.logger.log(
                "pnl_recorded",
                position_id=pos.position_id,
                pnl_eur=pnl_eur,
                daily_pnl_eur=self.risk_guard.daily_pnl_eur,
            )
            if self.risk_guard.halted:
                self.logger.log("risk_halt", reason=self.risk_guard.halt_reason)
        self.state_store.save_position(pos.position_id, asdict(pos))
