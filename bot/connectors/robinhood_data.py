"""Données de marché Robinhood Chain (chain ID 4663) — DexPaprika (principal,
API publique déjà disponible sur cette chaîne) avec repli Bitquery (GraphQL,
sert aussi au décodage des transferts/trades pour le wallet tracker).
"""
from __future__ import annotations

from dataclasses import dataclass

import requests

from core.secrets import get_secret

DEXPAPRIKA_BASE_URL = "https://api.dexpaprika.com"
BITQUERY_GRAPHQL_URL = "https://streaming.bitquery.io/graphql"
REQUEST_TIMEOUT_S = 10

ROBINHOOD_CHAIN_ID = 4663


class MarketDataError(RuntimeError):
    pass


@dataclass
class RawMarketData:
    token_address: str
    price_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    volume_avg_baseline_usd: float
    top_holder_concentration_pct: float
    breakout_detected: bool


class DexPaprikaClient:
    """Client REST DexPaprika. `network_id` doit correspondre à l'identifiant
    réseau attribué par DexPaprika à Robinhood Chain — à confirmer dans leur
    doc/registre réseaux au moment du build (chaîne très récente, l'id peut
    ne pas encore être stabilisé).
    """

    def __init__(self, api_key: str | None = None, network_id: str = "robinhood-chain"):
        self.api_key = api_key or get_secret("dexpaprika_api_key_env", required=False)
        self.network_id = network_id

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def get_pool(self, pool_address: str) -> dict:
        resp = requests.get(
            f"{DEXPAPRIKA_BASE_URL}/networks/{self.network_id}/pools/{pool_address}",
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT_S,
        )
        resp.raise_for_status()
        return resp.json()

    def fetch_raw_market_data(self, pool_address: str, token_address: str) -> RawMarketData:
        pool = self.get_pool(pool_address)
        volume_24h = float(pool.get("volume_usd_24h", 0.0) or 0.0)
        volume_change_pct = float(pool.get("volume_usd_change_24h_pct", 0.0) or 0.0)
        baseline = volume_24h / max(1.0 + volume_change_pct / 100, 0.01)
        price_change_pct = float(pool.get("price_change_24h_pct", 0.0) or 0.0)

        return RawMarketData(
            token_address=token_address,
            price_usd=float(pool.get("price_usd", 0.0) or 0.0),
            liquidity_usd=float(pool.get("liquidity_usd", 0.0) or 0.0),
            volume_24h_usd=volume_24h,
            volume_avg_baseline_usd=baseline,
            top_holder_concentration_pct=0.0,  # DexPaprika ne fournit pas la répartition holders
            breakout_detected=price_change_pct > 0 and volume_change_pct > 0,
        )


class BitqueryClient:
    """GraphQL (API v2) — sert de repli pour les données marché et de source
    principale pour le décodage des transferts/trades (wallet tracker).

    Auth v2 : Authorization: Bearer <token OAuth ory_at_...> sur
    streaming.bitquery.io. Ce token expire (voir leur doc, ~30 jours) — ce
    n'est pas une clé API permanente. Au-delà, il faut soit le renouveler à
    la main depuis le dashboard Bitquery, soit passer par un échange
    client_id/secret (non géré ici, à ajouter si besoin d'un renouvellement
    automatique).
    """

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or get_secret("bitquery_api_key_env")

    def query(self, graphql_query: str, variables: dict | None = None) -> dict:
        resp = requests.post(
            BITQUERY_GRAPHQL_URL,
            json={"query": graphql_query, "variables": variables or {}},
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise MarketDataError(f"Erreur GraphQL Bitquery: {data['errors']}")
        return data["data"]

    def get_top_holder_concentration_pct(self, token_address: str, total_supply: float) -> float:
        query = """
        query ($token: String!, $network: EthereumNetwork!) {
          ethereum(network: $network) {
            address(address: {is: $token}) {
              balances(currency: {is: $token}, orderBy: {descending: value}, limit: {count: 1}) {
                value
              }
            }
          }
        }
        """
        # `network` exact pour Robinhood Chain à confirmer selon le nommage
        # Bitquery une fois la chaîne référencée côté Bitquery.
        data = self.query(query, {"token": token_address, "network": "robinhood"})
        try:
            top_balance = data["ethereum"]["address"][0]["balances"][0]["value"]
        except (KeyError, IndexError, TypeError):
            return 0.0
        if not total_supply:
            return 0.0
        return 100.0 * float(top_balance) / float(total_supply)
