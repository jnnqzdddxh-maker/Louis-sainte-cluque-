"""Suivi des wallets performants — fenêtre glissante d'achats groupés.

Solana : alimenté en temps réel par les webhooks Helius (push).
Robinhood Chain : alimenté par polling périodique de Bitquery (pas de
webhooks disponibles à ce jour sur une chaîne aussi récente).

Ce module ne fait que maintenir la fenêtre d'événements et exposer
`distinct_wallets_buying(token)`, consommé par core/scoring.py pour
construire un WalletTrackerSignal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core.config import load_config
from core.scoring import WalletTrackerSignal
from connectors.robinhood_data import BitqueryClient


@dataclass(frozen=True)
class WalletBuyEvent:
    wallet_address: str
    token_address: str
    chain: str
    amount_usd: float
    timestamp: datetime


class WalletTracker:
    def __init__(self, config: dict | None = None):
        self.cfg = config or load_config()
        self.window_minutes = self.cfg["scoring"]["wallet_tracker"]["window_minutes"]
        self._events: list[WalletBuyEvent] = []

    def ingest(self, event: WalletBuyEvent) -> None:
        self._events.append(event)
        self._prune(event.timestamp)

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(minutes=self.window_minutes)
        self._events = [e for e in self._events if e.timestamp >= cutoff]

    def signal_for_token(self, token_address: str, now: datetime | None = None) -> WalletTrackerSignal:
        now = now or datetime.now(timezone.utc)
        self._prune(now)
        wallets = {e.wallet_address for e in self._events if e.token_address == token_address}
        return WalletTrackerSignal(distinct_wallets_buying=len(wallets))


# -- Solana : Helius webhooks -------------------------------------------

def parse_helius_webhook_payload(payload: list[dict], tracked_wallets: set[str]) -> list[WalletBuyEvent]:
    """Transforme le payload "enhanced transactions" envoyé par un webhook
    Helius (type SWAP) en WalletBuyEvent, en ne gardant que les wallets
    suivis et les swaps où le wallet REÇOIT le token (= achat).
    """
    events: list[WalletBuyEvent] = []
    for tx in payload:
        if tx.get("type") != "SWAP":
            continue
        source_wallet = tx.get("feePayer") or tx.get("source")
        if source_wallet not in tracked_wallets:
            continue
        ts = datetime.fromtimestamp(tx.get("timestamp", 0), tz=timezone.utc)
        for transfer in tx.get("tokenTransfers", []):
            if transfer.get("toUserAccount") == source_wallet:
                events.append(
                    WalletBuyEvent(
                        wallet_address=source_wallet,
                        token_address=transfer.get("mint"),
                        chain="solana",
                        amount_usd=float(transfer.get("tokenAmount", 0) or 0),
                        timestamp=ts,
                    )
                )
    return events


# -- Robinhood Chain : polling Bitquery -----------------------------------

_DEX_TRADES_QUERY = """
query ($wallets: [String!], $since: ISO8601DateTime!, $network: EthereumNetwork!) {
  ethereum(network: $network) {
    dexTrades(
      buyer: {in: $wallets}
      time: {since: $since}
    ) {
      buyer: address(address: {is: "buyer"})
      baseCurrency { address }
      tradeAmount(in: USD)
      block { timestamp { time } }
    }
  }
}
"""


def poll_robinhood_wallet_buys(
    bitquery: BitqueryClient,
    tracked_wallets: list[str],
    since: datetime,
) -> list[WalletBuyEvent]:
    if not tracked_wallets:
        return []
    data = bitquery.query(
        _DEX_TRADES_QUERY,
        {
            "wallets": tracked_wallets,
            "since": since.isoformat(),
            "network": "robinhood",  # nom réseau Bitquery à confirmer
        },
    )
    events = []
    for trade in data.get("ethereum", {}).get("dexTrades", []):
        events.append(
            WalletBuyEvent(
                wallet_address=trade["buyer"],
                token_address=trade["baseCurrency"]["address"],
                chain="robinhood",
                amount_usd=float(trade.get("tradeAmount", 0) or 0),
                timestamp=datetime.fromisoformat(trade["block"]["timestamp"]["time"]),
            )
        )
    return events
