"""Test d'intégration bout-en-bout : le VRAI connecteur DexPaprika (Solana)
-> le VRAI moteur de scoring -> le VRAI moteur de trading, pour un token
pump.fun tout juste créé (le cas concret qui a motivé les 3 corrections du
15/09/2026 : breakout tautologique, plafond de score à 16/100, quota Helius
épuisé faute de cache).

Aucun appel réseau réel : seuls `DexPaprikaClient.get_pools_for_token` (HTTP
DexPaprika) et `HeliusClient.get_mint_authorities` (RPC Helius) sont
substitués -- tout le reste (calcul du breakout, du market_subscore, de la
confiance, la décision d'ouvrir une position) passe par le vrai code de
production, pas une version simplifiée pour le test.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config
from core.engine import TradingEngine
from core.logging_store import DecisionLogger, StateStore
from core.scoring import Confidence
from connectors.robinhood_data import DexPaprikaClient
from connectors.twitter_watch import TwitterWatcher

CFG = load_config()
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TOKEN = "Hcd8d5DANpxgnXvwYPs37iNY5THJkF51qarFEUdJpump"


class _NullSearchBackend:
    def search(self, query: str) -> list:
        return []


class _StubHeliusClient:
    """Remplace le RPC Helius réel -- simule une autorité mint/freeze
    révoquée (le cas normal pour un token légitime), sans réseau."""

    def get_mint_authorities(self, token_address: str) -> tuple[bool, bool]:
        return True, True


def _engine(tmp_path, fetchers) -> TradingEngine:
    return TradingEngine(
        dry_run=True,
        market_data_fetchers=fetchers,
        capital_eur=CFG["risk"]["capital_eur"],
        config=CFG,
        twitter_watcher=TwitterWatcher(config=CFG, backend=_NullSearchBackend()),
        logger=DecisionLogger(path=tmp_path / "decisions.jsonl", config=CFG),
        state_store=StateStore(path=tmp_path / "state.sqlite3", config=CFG),
    )


def _dexpaprika_solana_client_for(pool: dict) -> DexPaprikaClient:
    client = DexPaprikaClient(network_id="solana")
    client.get_pools_for_token = lambda token_address, limit=5: [pool]
    client._helius_client = _StubHeliusClient()
    return client


# Pool réaliste d'un token pump.fun tout juste créé : liquidité au-dessus du
# minimum anti-rug (300$ depuis le 15/09/2026, voir config.yaml), volume 24h
# significatif par rapport à cette liquidité (activité réelle), mais AUCUN
# historique 7 jours (le cas normal pour un token qui vient d'être lancé) et
# un vrai momentum de prix positif.
FRESH_ACTIVE_TOKEN_POOL = {
    "id": "3vaJU4tQYUhADSCJCq2eFUDYTk5MF4id6xQT5J7z3kwT",
    "price_usd": 0.0002,
    "liquidity_usd": 1_800.0,
    "volume_usd_24h": 3_000.0,   # 1.67x la liquidité -> momentum plein (seuil 1.5x)
    "volume_usd_7d": 0.0,        # pas d'historique -- tout juste créé
    "price_change_percentage_1h": 18.0,
    "price_change_percentage_5m": 4.0,
    "tokens": [
        {"id": "So11111111111111111111111111111111111111112", "chain": "solana"},
        {"id": TOKEN, "chain": "solana"},
    ],
}

# Même token, mais sans aucune activité réelle (juste de la liquidité
# statique, pas de trading, pas de mouvement de prix) -- ne doit PAS ouvrir
# de position : les fix ne doivent pas rendre le bot permissif au point de
# trader n'importe quoi qui passe juste le filtre anti-rug de liquidité.
DEAD_TOKEN_POOL = {
    **FRESH_ACTIVE_TOKEN_POOL,
    "volume_usd_24h": 0.0,
    "price_change_percentage_1h": 0.0,
    "price_change_percentage_5m": 0.0,
}


def test_fresh_active_pumpfun_token_opens_a_position_end_to_end(tmp_path):
    """Reproduit le scénario concret bloqué avant les 3 fixes du 15/09/2026 :
    un token tout juste créé, sans wallet corroborant, mais avec une vraie
    activité (volume + momentum de prix) doit maintenant pouvoir être tradé."""
    client = _dexpaprika_solana_client_for(FRESH_ACTIVE_TOKEN_POOL)
    engine = _engine(tmp_path, {"solana": client.fetch_raw_market_data_by_token})

    assert CFG["execution"]["min_confidence_to_trade"] == "moyenne"
    engine.on_market_scan_hit(TOKEN, "solana", NOW, source="market_scan_new_listing")

    candidate = engine.candidates[TOKEN]
    assert not candidate.score.rejected_anti_rug, candidate.score.rejection_reason
    assert candidate.score.confidence != Confidence.FAIBLE, candidate.score

    open_positions = [p for p in engine.position_manager.positions.values() if not p.closed]
    assert len(open_positions) == 1
    assert open_positions[0].token_id == TOKEN


def test_dead_fresh_token_still_does_not_open_a_position(tmp_path):
    """Les fixes ne doivent pas rendre le bot permissif au point de trader un
    token sans aucune activité réelle, juste parce qu'il passe le plancher
    de liquidité anti-rug."""
    client = _dexpaprika_solana_client_for(DEAD_TOKEN_POOL)
    engine = _engine(tmp_path, {"solana": client.fetch_raw_market_data_by_token})

    engine.on_market_scan_hit(TOKEN, "solana", NOW, source="market_scan_new_listing")

    candidate = engine.candidates[TOKEN]
    assert not candidate.score.rejected_anti_rug
    assert candidate.score.confidence == Confidence.FAIBLE

    open_positions = [p for p in engine.position_manager.positions.values() if not p.closed]
    assert len(open_positions) == 0


def test_helius_cache_is_reused_across_rescans_of_the_same_token(tmp_path):
    """Le scénario exact qui épuisait le quota Helius (voir README, fix du
    15/09/2026) : le même token ressort dans plusieurs cycles de scan de
    suite. Utilise le VRAI HeliusClient (pas un stub) pour prouver que le
    cache fait effet à travers tout le chemin réel (DexPaprikaClient ->
    engine.on_market_scan_hit), pas seulement en isolation unitaire."""
    from unittest.mock import patch
    from connectors.solana_data import HeliusClient

    def _fake_rpc_response():
        class FakeResponse:
            status_code = 200
            url = "https://mainnet.helius-rpc.com/?api-key=test"
            text = ""

            def json(self):
                return {
                    "result": {
                        "value": {
                            "data": {"parsed": {"info": {"mintAuthority": None, "freezeAuthority": None}}}
                        }
                    }
                }

        return FakeResponse()

    client = _dexpaprika_solana_client_for(FRESH_ACTIVE_TOKEN_POOL)
    client._helius_client = HeliusClient(api_key="test")  # vrai client, vrai cache
    engine = _engine(tmp_path, {"solana": client.fetch_raw_market_data_by_token})

    with patch("connectors.solana_data.requests.post", return_value=_fake_rpc_response()) as mock_post:
        for _ in range(3):
            engine.on_market_scan_hit(TOKEN, "solana", NOW, source="market_scan_new_listing")

    assert mock_post.call_count == 1  # 3 rescans du même token, un seul vrai appel RPC
    open_positions = [p for p in engine.position_manager.positions.values() if not p.closed]
    assert len(open_positions) == 1  # le token a bien pu être tradé malgré les rescans répétés
