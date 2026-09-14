"""Mode simulation — OBLIGATOIRE plusieurs jours avant tout passage en réel
(section 1 et 7 du cahier des charges).

Usage :
    python dry_run.py

Force dry_run=True quelle que soit la valeur de config.yaml:mode : ce script
ne signera ni n'enverra JAMAIS de transaction, quoi qu'il arrive. Lance le
dashboard de surveillance (positions, scores en direct, log des décisions)
et tourne indéfiniment (Ctrl+C pour arrêter proprement).

IMPORTANT (architecture) : tous les appels réseau (DexPaprika/Helius/
Bitquery) passent par la librairie `requests`, qui est BLOQUANTE. Exécutés
tels quels dans une coroutine asyncio, ils gèleraient tout le programme
(dashboard inclus) pendant leur durée — observé en dry-run réel le
14/09/2026 (plus aucune ligne de log, dashboard qui ne répond plus, alors
qu'un cycle de scan peut déclencher des dizaines d'appels séquentiels).
Chaque boucle ci-dessous encapsule donc son travail réseau dans une
fonction synchrone `_..._once`, appelée via `asyncio.to_thread(...)` — ça
tourne dans un thread séparé, le serveur web reste réactif pendant ce temps.

IMPORTANT (données) : il appelle les vraies APIs de marché (DexPaprika /
Helius / Bitquery) en LECTURE SEULE, pour que le scoring et les paliers de
sortie soient validés sur des conditions de marché réelles. Il faut donc
les clés API de marché (voir .env.example) même en dry-run — seules les
clés privées des wallets d'exécution ne sont pas nécessaires ici.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import uvicorn

from core.config import load_config
from core.engine import TradingEngine
from core.secrets import get_secret
from connectors.robinhood_data import BitqueryClient, DexPaprikaClient
from connectors.wallet_tracker import poll_robinhood_wallet_buys, poll_solana_wallet_buys
from dashboard.app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dry_run")


def build_market_data_fetchers(cfg: dict) -> dict:
    fetchers = {}
    if cfg["chains"]["solana"]["enabled"]:
        # DexPaprika plutôt que Birdeye (quota gratuit Birdeye épuisé, y
        # compris pour token_overview — pas seulement les scans — le
        # 14/09/2026). Le check mint/freeze authority (RPC Helius) reste
        # actif, indépendant de Birdeye — voir fetch_raw_market_data_by_token.
        dexpaprika_solana = DexPaprikaClient(network_id="solana")
        fetchers["solana"] = dexpaprika_solana.fetch_raw_market_data_by_token
    if cfg["chains"]["robinhood"]["enabled"]:
        dexpaprika = DexPaprikaClient()
        # Simplification de ce scaffold : suppose token_address == pool_address
        # (le pool principal). À remplacer par une vraie résolution
        # token -> pool avant tout usage réel — voir connectors/robinhood_data.py.
        fetchers["robinhood"] = lambda token_address: dexpaprika.fetch_raw_market_data(
            token_address, token_address
        )
    return fetchers


# -- Robinhood Chain : suivi des wallets (Bitquery) --------------------------

def _poll_robinhood_wallets_once(
    engine: TradingEngine, bitquery: BitqueryClient, tracked: list[str], since: datetime
) -> datetime:
    events = poll_robinhood_wallet_buys(bitquery, tracked, since)
    now = datetime.now(timezone.utc)
    for event in events:
        engine.on_wallet_buy_event(event)
    return now


async def poll_robinhood_wallets_loop(engine: TradingEngine, cfg: dict) -> None:
    if not cfg["chains"]["robinhood"]["enabled"]:
        return
    if not cfg["chains"]["robinhood"].get("wallet_tracking_enabled", True):
        log.info("suivi des wallets Robinhood Chain désactivé (chains.robinhood.wallet_tracking_enabled)")
        return
    tracked = cfg["scoring"]["wallet_tracker"]["tracked_wallets"]["robinhood"]
    if not tracked:
        log.warning("aucun wallet Robinhood Chain suivi dans config.yaml — poll désactivé")
        return
    bitquery = BitqueryClient()
    interval = cfg["execution"]["candidate_rescan_interval_seconds"]
    since = datetime.now(timezone.utc)
    while True:
        try:
            since = await asyncio.to_thread(_poll_robinhood_wallets_once, engine, bitquery, tracked, since)
        except Exception:
            log.exception("échec du poll wallets Robinhood Chain")
        await asyncio.sleep(interval)


# -- Solana : suivi des wallets (Helius) --------------------------------------

def _poll_solana_wallets_once(
    engine: TradingEngine, helius_api_key: str, tracked: list[str], since: datetime
) -> datetime:
    events = poll_solana_wallet_buys(helius_api_key, tracked, since)
    now = datetime.now(timezone.utc)
    for event in events:
        engine.on_wallet_buy_event(event)
    return now


async def poll_solana_wallets_loop(engine: TradingEngine, cfg: dict) -> None:
    if not cfg["chains"]["solana"]["enabled"]:
        return
    tracked = cfg["scoring"]["wallet_tracker"]["tracked_wallets"]["solana"]
    if not tracked:
        log.warning("aucun wallet Solana suivi dans config.yaml — poll désactivé")
        return
    helius_api_key = get_secret("helius_api_key_env")
    interval = cfg["execution"]["candidate_rescan_interval_seconds"]
    since = datetime.now(timezone.utc)
    while True:
        try:
            since = await asyncio.to_thread(_poll_solana_wallets_once, engine, helius_api_key, tracked, since)
        except Exception:
            log.exception("échec du poll wallets Solana")
        await asyncio.sleep(interval)


# -- Scan de marché : nouveaux tokens/pools -----------------------------------

def _market_scan_new_listings_once(
    engine: TradingEngine,
    now: datetime,
    dexpaprika_solana: DexPaprikaClient | None,
    dexpaprika_robinhood: DexPaprikaClient | None,
    limit: int,
) -> None:
    if dexpaprika_solana is not None:
        try:
            for token_address in dexpaprika_solana.get_new_pools(limit=limit, resolve_base_token=True):
                engine.on_market_scan_hit(token_address, "solana", now, source="market_scan_new_listing")
        except Exception:
            log.exception("échec du scan nouveaux tokens Solana")
    if dexpaprika_robinhood is not None:
        try:
            for pool_address in dexpaprika_robinhood.get_new_pools(limit=limit):
                engine.on_market_scan_hit(pool_address, "robinhood", now, source="market_scan_new_listing")
        except Exception:
            log.exception("échec du scan nouveaux pools Robinhood Chain")


async def market_scan_new_listings_loop(engine: TradingEngine, cfg: dict) -> None:
    """Scan des tokens/pools TOUT JUSTE créés — c'est le mode prioritaire
    pour catcher tôt (la stratégie x2->x100 suppose de rentrer avant que le
    token ait déjà pris son envol). Voir core/engine.py:on_market_scan_hit.
    """
    scan_cfg = cfg.get("market_scan", {}).get("new_listings", {})
    if not scan_cfg.get("enabled"):
        return
    interval = scan_cfg["interval_seconds"]
    limit = scan_cfg["tokens_per_scan"]

    solana_on = cfg["chains"]["solana"]["enabled"] and scan_cfg.get("solana_enabled", True)
    dexpaprika_solana = DexPaprikaClient(network_id="solana") if solana_on else None
    dexpaprika_robinhood = DexPaprikaClient() if cfg["chains"]["robinhood"]["enabled"] else None
    if not solana_on:
        log.info("scan nouveaux tokens Solana désactivé (market_scan.new_listings.solana_enabled)")

    while True:
        now = datetime.now(timezone.utc)
        await asyncio.to_thread(
            _market_scan_new_listings_once, engine, now, dexpaprika_solana, dexpaprika_robinhood, limit
        )
        await asyncio.sleep(interval)


# -- Scan de marché : tendances (volume actuel) -------------------------------

def _market_scan_trending_once(
    engine: TradingEngine,
    now: datetime,
    dexpaprika_solana: DexPaprikaClient | None,
    dexpaprika_robinhood: DexPaprikaClient | None,
    limit: int,
) -> None:
    if dexpaprika_solana is not None:
        try:
            for token_address in dexpaprika_solana.get_trending_pools(limit=limit, resolve_base_token=True):
                engine.on_market_scan_hit(token_address, "solana", now, source="market_scan_trending")
        except Exception:
            log.exception("échec du scan tendances Solana")
    if dexpaprika_robinhood is not None:
        try:
            for pool_address in dexpaprika_robinhood.get_trending_pools(limit=limit):
                engine.on_market_scan_hit(pool_address, "robinhood", now, source="market_scan_trending")
        except Exception:
            log.exception("échec du scan tendances Robinhood Chain")


async def market_scan_trending_loop(engine: TradingEngine, cfg: dict) -> None:
    """Scan des tokens/pools avec le plus gros volume ACTUEL — complément du
    scan "nouveaux tokens", remonte souvent des tokens déjà bien montés.
    """
    scan_cfg = cfg.get("market_scan", {}).get("trending", {})
    if not scan_cfg.get("enabled"):
        return
    interval = scan_cfg["interval_seconds"]
    limit = scan_cfg["tokens_per_scan"]

    solana_on = cfg["chains"]["solana"]["enabled"] and scan_cfg.get("solana_enabled", True)
    dexpaprika_solana = DexPaprikaClient(network_id="solana") if solana_on else None
    dexpaprika_robinhood = DexPaprikaClient() if cfg["chains"]["robinhood"]["enabled"] else None
    if not solana_on:
        log.info("scan tendances Solana désactivé (market_scan.trending.solana_enabled)")

    while True:
        now = datetime.now(timezone.utc)
        await asyncio.to_thread(
            _market_scan_trending_once, engine, now, dexpaprika_solana, dexpaprika_robinhood, limit
        )
        await asyncio.sleep(interval)


# -- Tick de prix sur les positions ouvertes ----------------------------------

async def price_tick_loop(engine: TradingEngine, cfg: dict) -> None:
    interval = cfg["execution"]["price_poll_interval_seconds"]
    while True:
        try:
            await asyncio.to_thread(engine.tick_prices)
        except Exception:
            log.exception("échec du tick de prix")
        await asyncio.sleep(interval)


# -- Réévaluation des tokens rejetés uniquement pour liquidité insuffisante --

async def liquidity_watchlist_loop(engine: TradingEngine, cfg: dict) -> None:
    """Voir config.yaml:market_scan.liquidity_watchlist et
    core/engine.py:TradingEngine.recheck_liquidity_watchlist — un pool
    pump.fun tout juste créé affiche souvent 0$ de liquidité le temps que
    DexPaprika l'indexe, et ne reste que quelques dizaines de secondes dans
    le top "nouveaux tokens" avant d'être remplacé par des tokens encore
    plus récents. Sans cette boucle, un tel token n'aurait jamais de
    seconde chance même si sa vraie liquidité dépasse le seuil peu après.
    """
    wl_cfg = cfg.get("market_scan", {}).get("liquidity_watchlist", {})
    if not wl_cfg.get("enabled"):
        return
    interval = wl_cfg.get("recheck_interval_seconds", 30)
    while True:
        try:
            now = datetime.now(timezone.utc)
            await asyncio.to_thread(engine.recheck_liquidity_watchlist, now)
        except Exception:
            log.exception("échec de la réévaluation de la liste d'attente liquidité")
        await asyncio.sleep(interval)


async def run() -> None:
    cfg = load_config()
    if cfg["mode"] != "dry_run":
        log.warning("config.yaml:mode='%s' ignoré — dry_run.py force toujours le mode simulation", cfg["mode"])

    fetchers = build_market_data_fetchers(cfg)
    engine = TradingEngine(dry_run=True, market_data_fetchers=fetchers, capital_eur=cfg["risk"]["capital_eur"])

    tracked_solana = set(cfg["scoring"]["wallet_tracker"]["tracked_wallets"]["solana"])
    app = create_app(engine, tracked_solana_wallets=tracked_solana)

    server_config = uvicorn.Config(
        app, host=cfg["dashboard"]["host"], port=cfg["dashboard"]["port"], log_level="info"
    )
    server = uvicorn.Server(server_config)

    log.info(
        "Dry-run démarré (AUCUN trade réel) — dashboard sur http://%s:%s",
        cfg["dashboard"]["host"],
        cfg["dashboard"]["port"],
    )
    await asyncio.gather(
        server.serve(),
        poll_solana_wallets_loop(engine, cfg),
        poll_robinhood_wallets_loop(engine, cfg),
        market_scan_new_listings_loop(engine, cfg),
        market_scan_trending_loop(engine, cfg),
        price_tick_loop(engine, cfg),
        liquidity_watchlist_loop(engine, cfg),
    )


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Dry-run arrêté par l'utilisateur.")
