"""Données de marché Robinhood Chain (chain ID 4663) — DexPaprika (principal,
API publique déjà disponible sur cette chaîne) avec repli Bitquery (GraphQL,
sert aussi au décodage des transferts/trades pour le wallet tracker).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from core.config import load_config
from core.secrets import get_secret

DEXPAPRIKA_BASE_URL = "https://api.dexpaprika.com"
BITQUERY_GRAPHQL_URL = "https://streaming.bitquery.io/graphql"
REQUEST_TIMEOUT_S = 10

ROBINHOOD_CHAIN_ID = 4663


class MarketDataError(RuntimeError):
    pass


def _raise_for_status_with_body(resp: requests.Response) -> None:
    """Comme resp.raise_for_status(), mais garde le corps de la réponse dans
    le message d'erreur — sinon perdu par raise_for_status seul, alors que
    c'est souvent là qu'est la vraie raison (ex: réseau mal identifié,
    quota dépassé)."""
    if resp.status_code >= 400:
        raise MarketDataError(f"{resp.status_code} sur {resp.url}: {resp.text[:500]}")


def _pick_base_token_address(pool: dict, recognized_quotes: set[str]) -> str | None:
    """Dans un résultat de recherche de pools, choisit le token qui n'est
    PAS la monnaie de cotation habituelle (SOL/USDC/... ou ETH/WETH/...) —
    c'est lui le "vrai" candidat, pas le pool lui-même ni sa monnaie de
    référence. Repli sur le premier token si les deux (ou aucun) matchent.

    `recognized_quotes` doit contenir À LA FOIS des symboles ET des adresses
    connues : sur Solana, les entrées de `tokens` n'ont PAS de champ
    "symbol" (juste "id"/"chain"/"has_image", confirmé en dry-run le
    14/09/2026) — sans le matching par adresse en plus, ça retombait
    systématiquement sur le premier token de la liste (souvent SOL lui-même,
    qui est justement exclu des candidats).
    """
    tokens = pool.get("tokens", []) or []
    if not tokens:
        return None
    non_quote = [
        t for t in tokens
        if t.get("symbol", "") not in recognized_quotes and t.get("id", "") not in recognized_quotes
    ]
    chosen = non_quote[0] if non_quote else tokens[0]
    return chosen.get("id")


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
    has_social_links: bool = False               # toujours False ici : DexPaprika ne fournit pas les
                                                  # liens sociaux d'un token (contrairement à Birdeye/Solana)
    paired_with_recognized_quote: bool = False    # pool appairé à ETH/WETH/USDC/USDT plutôt qu'à un token obscur
    mint_authority_renounced: bool = True         # non applicable ici (ERC20) sauf réseau "solana" — voir fetch_raw_market_data_by_token
    freeze_authority_renounced: bool = True


class DexPaprikaClient:
    """Client REST DexPaprika. `network_id` = "robinhood" (confirmé en
    dry-run le 14/09/2026 — "robinhood-chain" renvoyait 410 Gone : ce
    n'était pas le bon identifiant réseau).
    """

    def __init__(self, api_key: str | None = None, network_id: str = "robinhood"):
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
        _raise_for_status_with_body(resp)
        return resp.json()

    def _resolve_addresses(self, pools: list[dict], resolve_base_token: bool) -> list[str]:
        if not resolve_base_token:
            return [p["id"] for p in pools]  # comportement historique (Robinhood Chain) : id de pool
        cfg = load_config()
        pvl = cfg["scoring"]["price_volume_liquidity"]
        # Union symboles (ex: "SOL") + adresses connues (ex: excluded_tokens,
        # qui contient déjà les adresses SOL/USDC/USDT) : sur Solana, les
        # tokens renvoyés par DexPaprika n'ont pas de champ "symbol", donc le
        # matching par symbole seul ne suffit pas — voir _pick_base_token_address.
        recognized_quotes = set(pvl["recognized_quote_tokens"].get(self.network_id, []))
        recognized_quotes |= set(
            cfg.get("market_scan", {}).get("excluded_tokens", {}).get(self.network_id, [])
        )
        return [_pick_base_token_address(p, recognized_quotes) or p["id"] for p in pools]

    def get_new_pools(self, limit: int = 20, resolve_base_token: bool = False) -> list[str]:
        """Pools tout juste créés, avant même d'avoir accumulé du volume —
        permet de rentrer TÔT, contrairement à get_trending_pools qui ne
        remonte que ce qui a déjà du volume (donc probablement déjà monté).

        `resolve_base_token=True` (utilisé pour Solana) : retourne l'adresse
        du token réel plutôt que celle du pool, pour rester compatible avec
        un fetcher de scoring qui attend une vraie adresse de token (ex:
        Birdeye). Robinhood Chain garde le comportement historique (id de
        pool) par défaut — voir la note dans dry_run.py.
        """
        resp = requests.get(
            f"{DEXPAPRIKA_BASE_URL}/networks/{self.network_id}/pools/search",
            params={"order_by": "created_at", "sort": "desc", "limit": limit},
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT_S,
        )
        _raise_for_status_with_body(resp)
        data = resp.json()
        return self._resolve_addresses(data.get("results", []), resolve_base_token)

    def get_trending_pools(self, limit: int = 20, resolve_base_token: bool = False) -> list[str]:
        """Liste des pools les plus actifs (triés par volume), indépendamment
        de tout wallet suivi — sert au scan de marché autonome (voir
        core/engine.py:on_market_scan_hit). Voir get_new_pools pour
        `resolve_base_token`.
        """
        resp = requests.get(
            f"{DEXPAPRIKA_BASE_URL}/networks/{self.network_id}/pools/search",
            params={"order_by": "volume_usd_24h", "sort": "desc", "limit": limit},
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT_S,
        )
        _raise_for_status_with_body(resp)
        data = resp.json()
        return self._resolve_addresses(data.get("results", []), resolve_base_token)

    def get_pools_for_token(self, token_address: str, limit: int = 5) -> list[dict]:
        """Pools où ce token précis est échangé, triés par liquidité
        décroissante (le premier résultat est le pool "principal"). Sert au
        scoring d'un candidat dont on n'a qu'une adresse de token, pas de
        pool précis (ex: wallet tracker Solana). Endpoint confirmé le
        14/09/2026 : /pools/search accepte un paramètre token_address
        (l'ancien /tokens/{address}/pools a été retiré, 410 Gone).
        """
        resp = requests.get(
            f"{DEXPAPRIKA_BASE_URL}/networks/{self.network_id}/pools/search",
            params={
                "token_address": token_address,
                "order_by": "liquidity_usd",
                "sort": "desc",
                "limit": limit,
            },
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT_S,
        )
        _raise_for_status_with_body(resp)
        return resp.json().get("results", [])

    def _pool_to_raw_market_data(self, pool: dict, token_address: str) -> RawMarketData:
        """Champs confirmés en dry-run réel le 14/09/2026 (voir
        get_trending_pools) : pas de "volume_usd_change_24h_pct" ni de
        "price_change_24h_pct" comme précédemment supposé — les vrais noms
        sont volume_usd_7d/30d et price_change_percentage_{5m,1h,6h,24h}.
        Pas de moyenne de volume directe : volume_usd_7d/7 sert de proxy
        pour la moyenne quotidienne "normale", comparée à volume_usd_24h
        pour détecter un pic.

        Bug corrigé le 14/09/2026 (score max observé en dry-run réel : 4/100,
        alors qu'un market_subscore de ~90 aurait dû être atteignable) :
        pour un token sans 7 jours d'historique (volume_usd_7d == 0, le cas
        NORMAL pour un token tout juste créé — exactement ceux que le scan
        "nouveaux tokens" cible), l'ancien code faisait
        `baseline = volume_24h` (repli), puis testait
        `volume_24h > baseline` == `volume_24h > volume_24h` : toujours faux
        par construction, quel que soit le prix. Le breakout était donc
        structurellement indétectable sur les tokens les plus frais. Pour ce
        cas, on se base uniquement sur le momentum de prix (seul signal
        disponible sans historique de volume fiable) plutôt que sur une
        comparaison tautologique.
        """
        volume_24h = float(pool.get("volume_usd_24h", 0.0) or 0.0)
        volume_7d = float(pool.get("volume_usd_7d", 0.0) or 0.0)
        price_change_1h = float(pool.get("price_change_percentage_1h", 0.0) or 0.0)
        price_change_5m = float(pool.get("price_change_percentage_5m", 0.0) or 0.0)
        # Historique fiable seulement si volume_7d dépasse un simple jour de
        # trading répété 7 fois (sinon volume_7d == volume_24h ou proche, ce
        # qui n'apporte aucune info de "moyenne" supplémentaire).
        has_reliable_baseline = volume_7d > volume_24h
        if has_reliable_baseline:
            baseline = volume_7d / 7
            breakout_detected = price_change_1h > 0 and volume_24h > baseline
        else:
            baseline = 0.0
            breakout_detected = price_change_1h > 0 or price_change_5m > 0

        pool_tokens = pool.get("tokens", []) or []
        base_symbol = token_address
        paired_with_recognized = False
        if pool_tokens:
            cfg = load_config()
            recognized_quotes = set(
                cfg["scoring"]["price_volume_liquidity"]["recognized_quote_tokens"].get(self.network_id, [])
            )
            recognized_quotes |= set(
                cfg.get("market_scan", {}).get("excluded_tokens", {}).get(self.network_id, [])
            )
            base_token = next(
                (t for t in pool_tokens if t.get("id") == token_address), pool_tokens[0]
            )
            base_symbol = base_token.get("symbol") or token_address
            paired_with_recognized = any(
                (t.get("symbol", "") in recognized_quotes or t.get("id", "") in recognized_quotes)
                for t in pool_tokens
                if t is not base_token
            )

        return RawMarketData(
            token_address=token_address,
            price_usd=float(pool.get("price_usd", 0.0) or 0.0),
            liquidity_usd=float(pool.get("liquidity_usd", 0.0) or 0.0),
            volume_24h_usd=volume_24h,
            volume_avg_baseline_usd=baseline,
            top_holder_concentration_pct=0.0,  # DexPaprika ne fournit pas la répartition holders
            breakout_detected=breakout_detected,
            symbol=base_symbol,
            paired_with_recognized_quote=paired_with_recognized,
        )

    def fetch_raw_market_data(self, pool_address: str, token_address: str) -> RawMarketData:
        """Utilisé quand on connaît déjà le pool (Robinhood Chain, où le
        "candidat" découvert par le scan EST directement un id de pool)."""
        pool = self.get_pool(pool_address)
        return self._pool_to_raw_market_data(pool, token_address)

    def fetch_raw_market_data_by_token(self, token_address: str) -> RawMarketData:
        """Utilisé quand on a une adresse de TOKEN, pas de pool (Solana :
        wallet tracker et scan de marché résolvent tous les deux une vraie
        adresse de token — voir dry_run.py). Remplace Birdeye pour le
        scoring Solana (quota gratuit épuisé le 14/09/2026)."""
        pools = self.get_pools_for_token(token_address, limit=5)
        if not pools:
            raise MarketDataError(f"Aucun pool DexPaprika trouvé pour le token {token_address}")
        raw = self._pool_to_raw_market_data(pools[0], token_address)

        if self.network_id == "solana":
            # Filtre anti-rug éliminatoire indépendant de Birdeye — RPC
            # Helius, pas concerné par le quota Birdeye.
            from connectors.solana_data import HeliusClient, MarketDataError as SolanaMarketDataError

            try:
                mint_renounced, freeze_renounced = HeliusClient().get_mint_authorities(token_address)
            except (SolanaMarketDataError, requests.RequestException) as exc:
                logging.getLogger("connectors.robinhood_data").warning(
                    "get_mint_authorities a échoué pour %s (fail-closed, rejeté) : %s", token_address, exc
                )
                mint_renounced, freeze_renounced = False, False  # fail-closed, voir core/scoring.py
            raw.mint_authority_renounced = mint_renounced
            raw.freeze_authority_renounced = freeze_renounced

        return raw


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
        _raise_for_status_with_body(resp)
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
