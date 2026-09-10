"""Suivi des wallets performants — fenêtre glissante d'achats groupés.

Solana : deux modes possibles.
  - Webhooks Helius (push, temps réel) : nécessite une adresse publique
    joignable par Helius (serveur avec IP/domaine public, ou tunnel type
    ngrok) — voir dashboard/app.py:/webhooks/helius.
  - Polling périodique de l'historique Helius (voir poll_solana_wallet_buys
    ci-dessous) : fonctionne depuis n'importe quelle machine, y compris un
    PC perso sans adresse publique — c'est le mode utilisé par défaut par
    dry_run.py/main.py.
Robinhood Chain : alimenté par polling périodique de Bitquery (pas de
webhooks disponibles à ce jour sur une chaîne aussi récente).

Ce module ne fait que maintenir la fenêtre d'événements et exposer
`distinct_wallets_buying(token)`, consommé par core/scoring.py pour
construire un WalletTrackerSignal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from core.config import load_config
from core.scoring import WalletTrackerSignal
from connectors.robinhood_data import BitqueryClient
from connectors.solana_data import HELIUS_BASE_URL, REQUEST_TIMEOUT_S


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


def poll_solana_wallet_buys(
    helius_api_key: str,
    tracked_wallets: list[str],
    since: datetime,
    limit_per_wallet: int = 20,
) -> list[WalletBuyEvent]:
    """Interroge l'historique de transactions "enhanced" de Helius pour
    chaque wallet suivi (même format de données que les webhooks, donc on
    réutilise parse_helius_webhook_payload). Ne nécessite aucune adresse
    publique — fonctionne depuis n'importe quel PC.

    Simplification assumée : Helius pagine par signature, pas par date ; on
    récupère juste les `limit_per_wallet` transactions les plus récentes à
    chaque appel et on filtre par timestamp > since. Un wallet très actif
    au-delà de cette limite entre deux polls pourrait faire manquer un
    achat plus ancien — acceptable pour une fenêtre de poll courte (voir
    execution.candidate_rescan_interval_seconds).
    """
    events: list[WalletBuyEvent] = []
    for wallet in tracked_wallets:
        try:
            resp = requests.get(
                f"{HELIUS_BASE_URL}/v0/addresses/{wallet}/transactions",
                params={"api-key": helius_api_key, "limit": limit_per_wallet},
                timeout=REQUEST_TIMEOUT_S,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException:
            continue  # un wallet en échec ne doit pas bloquer les autres
        for event in parse_helius_webhook_payload(payload, {wallet}):
            if event.timestamp > since:
                events.append(event)
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
