"""Orchestrateur principal — dry-run par défaut, live seulement sur
confirmation explicite (section 1 du cahier des charges : "Pas de
négociation sur ce point").

Passage en mode live (les DEUX conditions sont obligatoires) :
  1. config.yaml : mode: live
  2. variable d'environnement BOT_CONFIRM_LIVE=I_UNDERSTAND_THE_RISK au
     lancement (jamais dans un fichier committé)

Sans les deux, le bot démarre automatiquement en dry-run, quoi qu'il arrive.
Voir README.md section "Passage en live" pour la checklist complète
(section 7 du cahier des charges : dry-run 3-5 jours minimum, taille
minimale (20€) sur les premiers trades réels, etc.)
"""
from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

from core.config import load_config
from core.engine import TradingEngine
from connectors.robinhood_rpc import JsonRpcClient, PoolKey
from connectors.execution_handlers import RobinhoodExecutionHandler, SolanaExecutionHandler
from connectors.solana_data import BirdeyeClient
from dashboard.app import create_app
from dry_run import (
    build_market_data_fetchers,
    market_scan_new_listings_loop,
    market_scan_trending_loop,
    poll_robinhood_wallets_loop,
    poll_solana_wallets_loop,
    price_tick_loop,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

LIVE_CONFIRMATION_VALUE = "I_UNDERSTAND_THE_RISK"


def resolve_dry_run(cfg: dict) -> bool:
    if cfg["mode"] != "live":
        return True
    confirm = os.environ.get("BOT_CONFIRM_LIVE")
    if confirm != LIVE_CONFIRMATION_VALUE:
        log.error(
            "config.yaml demande mode='live' mais BOT_CONFIRM_LIVE n'est pas "
            "défini à '%s' — démarrage forcé en DRY-RUN par sécurité.",
            LIVE_CONFIRMATION_VALUE,
        )
        return True
    log.warning("MODE LIVE CONFIRMÉ — des trades réels avec de l'argent réel vont être exécutés.")
    return False


def build_execution_handlers(cfg: dict, dry_run: bool) -> dict:
    """Best-effort : si les clés publiques/adresses nécessaires ne sont pas
    renseignées, le bot continue de tourner en mode "décision seule" (log
    des décisions, pas de tentative d'exécution) plutôt que de planter au
    démarrage — sûr par défaut, y compris en dry-run.
    """
    handlers: dict = {}

    solana_cfg = cfg["chains"]["solana"]
    if solana_cfg["enabled"]:
        wallet_pub = solana_cfg.get("wallet_public_key")
        rpc_url = os.environ.get(solana_cfg["rpc_url_env"])
        if wallet_pub and rpc_url:
            birdeye = BirdeyeClient()

            def get_sol_usd_price(_birdeye=birdeye) -> float:
                overview = _birdeye.get_token_overview(
                    "So11111111111111111111111111111111111111112"
                )
                return float(overview.get("price", 0.0) or 0.0)

            handlers["solana"] = SolanaExecutionHandler(
                rpc_url=rpc_url,
                wallet_public_key=wallet_pub,
                slippage_bps=solana_cfg["slippage_bps"],
                get_sol_usd_price=get_sol_usd_price,
            )
        else:
            log.warning(
                "chains.solana.wallet_public_key ou la variable %s absent(e) — "
                "exécution Solana désactivée, le bot restera en mode décision-seule "
                "sur cette chaîne.",
                solana_cfg["rpc_url_env"],
            )

    robinhood_cfg = cfg["chains"]["robinhood"]
    if robinhood_cfg["enabled"]:
        wallet_addr = robinhood_cfg.get("wallet_address")
        router_addr = robinhood_cfg.get("universal_router_address")
        rpc_url = os.environ.get(robinhood_cfg["rpc_url_env"])
        if wallet_addr and router_addr and rpc_url:
            handlers["robinhood"] = RobinhoodExecutionHandler(
                rpc=JsonRpcClient(rpc_url),
                wallet_address=wallet_addr,
                universal_router_address=router_addr,
                pool_key=PoolKey(currency0="", currency1="", fee=0, tick_spacing=0, hooks="0x0"),
            )
            # Rappel volontaire : cet handler lève toujours NotImplementedError
            # tant que build_v4_swap_calldata (connectors/robinhood_rpc.py)
            # n'a pas été complété et vérifié — voir ce fichier pour les
            # étapes exactes. C'est un garde-fou, pas un bug.
        else:
            log.warning(
                "chains.robinhood wallet_address/universal_router_address ou la "
                "variable %s absent(e) — exécution Robinhood Chain désactivée.",
                robinhood_cfg["rpc_url_env"],
            )

    return handlers


async def run() -> None:
    cfg = load_config()
    dry_run = resolve_dry_run(cfg)

    fetchers = build_market_data_fetchers(cfg)
    execution_handlers = build_execution_handlers(cfg, dry_run)

    engine = TradingEngine(
        dry_run=dry_run,
        market_data_fetchers=fetchers,
        capital_eur=cfg["risk"]["capital_eur"],
        execution_handlers=execution_handlers,
    )

    tracked_solana = set(cfg["scoring"]["wallet_tracker"]["tracked_wallets"]["solana"])
    app = create_app(engine, tracked_solana_wallets=tracked_solana)

    server_config = uvicorn.Config(
        app, host=cfg["dashboard"]["host"], port=cfg["dashboard"]["port"], log_level="info"
    )
    server = uvicorn.Server(server_config)

    log.info(
        "Bot démarré en mode %s — dashboard sur http://%s:%s",
        "DRY-RUN" if dry_run else "LIVE",
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
    )


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Bot arrêté par l'utilisateur.")
