"""Données de marché Solana — Birdeye (principal) avec repli Helius.

NB: les endpoints publics de Birdeye/Helius évoluent régulièrement — vérifier
la doc officielle avant mise en production, ces clients sont un point de
départ fonctionnel, pas une garantie de compatibilité à long terme.
"""
from __future__ import annotations

from dataclasses import dataclass

import requests

from core.config import load_config
from core.secrets import get_secret

BIRDEYE_BASE_URL = "https://public-api.birdeye.so"
HELIUS_BASE_URL = "https://api.helius.xyz"
REQUEST_TIMEOUT_S = 10


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
    symbol: str = ""                             # utilisé pour la recherche sociale (Reddit/Twitter)
    has_social_links: bool = False               # site web/twitter/telegram déclarés dans les métadonnées
    paired_with_recognized_quote: bool = False    # pool principal appairé à SOL/USDC/USDT plutôt qu'à un token obscur


def _raise_for_status_with_body(resp: requests.Response) -> None:
    """Comme resp.raise_for_status(), mais garde le corps de la réponse dans
    le message d'erreur — l'API y met souvent la vraie raison (ex: "this
    endpoint requires a paid plan"), sinon perdue par raise_for_status seul.
    """
    if resp.status_code >= 400:
        raise MarketDataError(f"{resp.status_code} sur {resp.url}: {resp.text[:500]}")


class BirdeyeClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or get_secret("birdeye_api_key_env")

    def _headers(self) -> dict:
        return {"X-API-KEY": self.api_key, "x-chain": "solana"}

    def get_token_overview(self, token_address: str) -> dict:
        resp = requests.get(
            f"{BIRDEYE_BASE_URL}/defi/token_overview",
            params={"address": token_address},
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT_S,
        )
        _raise_for_status_with_body(resp)
        data = resp.json()
        if not data.get("success"):
            raise MarketDataError(f"Birdeye token_overview a échoué pour {token_address}: {data}")
        return data["data"]

    def get_top_holders(self, token_address: str, limit: int = 10) -> list[dict]:
        resp = requests.get(
            f"{BIRDEYE_BASE_URL}/defi/v3/token/holder",
            params={"address": token_address, "offset": 0, "limit": limit},
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT_S,
        )
        _raise_for_status_with_body(resp)
        data = resp.json()
        if not data.get("success"):
            raise MarketDataError(f"Birdeye holders a échoué pour {token_address}: {data}")
        return data["data"]["items"]

    def get_new_listings(self, limit: int = 20) -> list[str]:
        """Tokens tout juste listés (nouvelle liquidité ajoutée), avant même
        d'avoir accumulé du volume — c'est ce qui permet de rentrer TÔT,
        contrairement à get_trending_tokens qui ne remonte que ce qui a déjà
        du volume (donc probablement déjà bien monté).
        """
        resp = requests.get(
            f"{BIRDEYE_BASE_URL}/defi/v2/tokens/new_listing",
            params={"limit": limit},
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT_S,
        )
        _raise_for_status_with_body(resp)
        data = resp.json()
        if not data.get("success"):
            raise MarketDataError(f"Birdeye new_listing a échoué: {data}")
        return [t["address"] for t in data["data"]["items"]]

    def get_trending_tokens(self, limit: int = 20) -> list[str]:
        """Liste de tokens en tendance (volume/rang), indépendamment de tout
        wallet suivi — sert au scan de marché autonome (voir
        core/engine.py:on_market_scan_hit).
        """
        resp = requests.get(
            f"{BIRDEYE_BASE_URL}/defi/token_trending",
            params={"sort_by": "rank", "sort_type": "asc", "offset": 0, "limit": limit},
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT_S,
        )
        _raise_for_status_with_body(resp)
        data = resp.json()
        if not data.get("success"):
            raise MarketDataError(f"Birdeye token_trending a échoué: {data}")
        return [t["address"] for t in data["data"]["tokens"]]

    def get_markets(self, token_address: str, limit: int = 10) -> list[dict]:
        """Pools/marchés où ce token est échangé — sert à vérifier avec quoi
        il est appairé (SOL/USDC/USDT = infrastructure standard, un token
        obscur en face = signal de prudence supplémentaire).
        """
        resp = requests.get(
            f"{BIRDEYE_BASE_URL}/defi/v2/markets",
            params={"address": token_address, "offset": 0, "limit": limit},
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT_S,
        )
        _raise_for_status_with_body(resp)
        data = resp.json()
        if not data.get("success"):
            raise MarketDataError(f"Birdeye markets a échoué pour {token_address}: {data}")
        return data["data"]["items"]

    def fetch_raw_market_data(self, token_address: str) -> RawMarketData:
        overview = self.get_token_overview(token_address)
        try:
            holders = self.get_top_holders(token_address, limit=1)
            supply = overview.get("supply") or overview.get("totalSupply") or 0
            top_pct = 0.0
            if holders and supply:
                top_pct = 100.0 * float(holders[0].get("uiAmount", 0)) / float(supply)
        except (MarketDataError, requests.RequestException):
            top_pct = 0.0  # anti-rug filter appliqué en aval : ne pas faire échouer le scoring

        volume_24h = float(overview.get("v24hUSD", 0.0) or 0.0)
        # Birdeye ne fournit pas de moyenne historique directe sur cet endpoint ;
        # v24hChangePercent sert de proxy pour détecter un pic de volume.
        vol_change_pct = float(overview.get("v24hChangePercent", 0.0) or 0.0)
        baseline = volume_24h / max(1.0 + vol_change_pct / 100, 0.01)

        price_change_1h = float(overview.get("priceChange1hPercent", 0.0) or 0.0)

        extensions = overview.get("extensions") or {}
        has_social = any(extensions.get(k) for k in ("website", "twitter", "telegram", "discord"))

        recognized_quotes = set(
            load_config()["scoring"]["price_volume_liquidity"]["recognized_quote_tokens"]["solana"]
        )
        paired_with_recognized = False
        try:
            for market in self.get_markets(token_address, limit=10):
                quote_symbol = (market.get("quote", {}) or {}).get("symbol", "")
                if quote_symbol in recognized_quotes:
                    paired_with_recognized = True
                    break
        except (MarketDataError, requests.RequestException):
            paired_with_recognized = False  # signal non bloquant : mieux vaut 0 qu'un crash

        return RawMarketData(
            token_address=token_address,
            price_usd=float(overview.get("price", 0.0) or 0.0),
            liquidity_usd=float(overview.get("liquidity", 0.0) or 0.0),
            volume_24h_usd=volume_24h,
            volume_avg_baseline_usd=baseline,
            top_holder_concentration_pct=top_pct,
            breakout_detected=price_change_1h > 0 and vol_change_pct > 0,
            symbol=overview.get("symbol", "") or token_address,
            has_social_links=has_social,
            paired_with_recognized_quote=paired_with_recognized,
        )


class HeliusClient:
    """Repli / complément : métadonnées de token et données wallets."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or get_secret("helius_api_key_env")

    def get_asset(self, token_address: str) -> dict:
        resp = requests.post(
            f"{HELIUS_BASE_URL}/v0/token-metadata?api-key={self.api_key}",
            json={"mintAccounts": [token_address]},
            timeout=REQUEST_TIMEOUT_S,
        )
        _raise_for_status_with_body(resp)
        results = resp.json()
        if not results:
            raise MarketDataError(f"Helius token-metadata vide pour {token_address}")
        return results[0]
