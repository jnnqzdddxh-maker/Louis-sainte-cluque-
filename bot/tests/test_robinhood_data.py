import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.robinhood_data import _pick_base_token_address, DexPaprikaClient

# Forme réelle observée en dry-run le 14/09/2026 : les tokens Solana n'ont
# PAS de champ "symbol" (juste "id"/"chain"/"has_image").
REAL_SOLANA_POOL = {
    "id": "AuBT1VYtajjSk83d8J78qFUdjGt2BAazpRtvErYUcM8v",
    "dex_id": "pumpfun",
    "tokens": [
        {"id": "So11111111111111111111111111111111111111112", "chain": "solana", "has_image": True},
        {"id": "BbZ5H8jP369EFzd3gJDb4GkGh77QtGJ2zrRdWBWfpJJd", "chain": "solana", "has_image": True},
    ],
}


def test_pick_base_token_address_matches_solana_by_address_not_symbol():
    """SOL (index 0) n'a pas de "symbol", donc seul le matching par adresse
    permet de l'identifier comme monnaie de cotation à ignorer — sinon on
    retombe systématiquement sur lui (bug corrigé le 14/09/2026)."""
    recognized = {"So11111111111111111111111111111111111111112"}  # adresse SOL
    result = _pick_base_token_address(REAL_SOLANA_POOL, recognized)
    assert result == "BbZ5H8jP369EFzd3gJDb4GkGh77QtGJ2zrRdWBWfpJJd"


def test_pick_base_token_address_falls_back_to_first_token_without_match():
    recognized = {"UNRELATED"}
    result = _pick_base_token_address(REAL_SOLANA_POOL, recognized)
    assert result == "So11111111111111111111111111111111111111112"


def test_resolve_addresses_uses_results_key_not_pools():
    """La réponse DexPaprika /pools/search a sa liste sous "results", pas
    "pools" (confirmé en dry-run le 14/09/2026)."""
    client = DexPaprikaClient(network_id="solana")
    resolved = client._resolve_addresses([REAL_SOLANA_POOL], resolve_base_token=True)
    assert resolved == ["BbZ5H8jP369EFzd3gJDb4GkGh77QtGJ2zrRdWBWfpJJd"]


def test_resolve_addresses_without_resolve_base_token_keeps_pool_id():
    client = DexPaprikaClient(network_id="robinhood")
    resolved = client._resolve_addresses([REAL_SOLANA_POOL], resolve_base_token=False)
    assert resolved == ["AuBT1VYtajjSk83d8J78qFUdjGt2BAazpRtvErYUcM8v"]


# Pool complet, forme réelle observée le 14/09/2026 (via Invoke-RestMethod) :
# pas de "volume_usd_change_24h_pct" ni "price_change_24h_pct" comme
# précédemment supposé.
REAL_POOL_WITH_MARKET_DATA = {
    "id": "3vaJU4tQYUhADSCJCq2eFUDYTk5MF4id6xQT5J7z3kwT",
    "price_usd": 0.0001,
    "liquidity_usd": 4.44,
    "volume_usd_24h": 20.0,   # au-dessus de la moyenne 7j -> pic de volume
    "volume_usd_7d": 70.0,    # moyenne quotidienne proxy = 70/7 = 10
    "price_change_percentage_1h": 5.0,
    "tokens": [
        {"id": "So11111111111111111111111111111111111111112", "chain": "solana"},
        {"id": "Hcd8d5DANpxgnXvwYPs37iNY5THJkF51qarFEUdJpump", "chain": "solana"},
    ],
}


def test_pool_to_raw_market_data_uses_real_field_names():
    client = DexPaprikaClient(network_id="solana")
    raw = client._pool_to_raw_market_data(
        REAL_POOL_WITH_MARKET_DATA, "Hcd8d5DANpxgnXvwYPs37iNY5THJkF51qarFEUdJpump"
    )
    assert raw.price_usd == 0.0001
    assert raw.liquidity_usd == 4.44
    assert raw.volume_24h_usd == 20.0
    assert raw.volume_avg_baseline_usd == 10.0  # 70/7
    assert raw.breakout_detected is True  # price_change_1h > 0 ET volume_24h(20) > baseline(10)
    assert raw.token_address == "Hcd8d5DANpxgnXvwYPs37iNY5THJkF51qarFEUdJpump"


# Token tout juste créé (cas typique pump.fun) : pas encore de volume_usd_7d
# indexé (0), et volume_usd_24h lui-même à 0 (aucun trade encore vu par
# DexPaprika). Bug corrigé le 14/09/2026 : l'ancien code faisait
# baseline = volume_24h (repli), donc "volume_24h > baseline" == comparer
# 0 à lui-même, toujours faux, quel que soit le prix — breakout
# structurellement indétectable sur les tokens les plus frais alors que
# c'est justement le momentum de prix qui doit le signaler ici.
FRESH_POOL_NO_VOLUME_HISTORY = {
    "id": "3vaJU4tQYUhADSCJCq2eFUDYTk5MF4id6xQT5J7z3kwT",
    "price_usd": 0.0002,
    "liquidity_usd": 1800.0,
    "volume_usd_24h": 0.0,
    "volume_usd_7d": 0.0,
    "price_change_percentage_1h": 12.0,
    "price_change_percentage_5m": 3.0,
    "tokens": [
        {"id": "So11111111111111111111111111111111111111112", "chain": "solana"},
        {"id": "Hcd8d5DANpxgnXvwYPs37iNY5THJkF51qarFEUdJpump", "chain": "solana"},
    ],
}


def test_pool_to_raw_market_data_fresh_token_uses_price_momentum_for_breakout():
    client = DexPaprikaClient(network_id="solana")
    raw = client._pool_to_raw_market_data(
        FRESH_POOL_NO_VOLUME_HISTORY, "Hcd8d5DANpxgnXvwYPs37iNY5THJkF51qarFEUdJpump"
    )
    assert raw.volume_avg_baseline_usd == 0.0
    assert raw.breakout_detected is True  # price_change_1h(12) > 0, malgré volume_24h == baseline == 0


def test_pool_to_raw_market_data_fresh_token_without_momentum_is_not_breakout():
    pool = {**FRESH_POOL_NO_VOLUME_HISTORY, "price_change_percentage_1h": 0.0, "price_change_percentage_5m": 0.0}
    client = DexPaprikaClient(network_id="solana")
    raw = client._pool_to_raw_market_data(pool, "Hcd8d5DANpxgnXvwYPs37iNY5THJkF51qarFEUdJpump")
    assert raw.breakout_detected is False


def test_pool_to_raw_market_data_single_day_volume_is_not_a_reliable_baseline():
    """volume_usd_7d == volume_usd_24h (un seul jour de trading vu, pas 7)
    ne doit pas non plus être traité comme un historique fiable — sinon on
    retombe sur la même comparaison tautologique."""
    pool = {**FRESH_POOL_NO_VOLUME_HISTORY, "volume_usd_24h": 20.0, "volume_usd_7d": 20.0}
    client = DexPaprikaClient(network_id="solana")
    raw = client._pool_to_raw_market_data(pool, "Hcd8d5DANpxgnXvwYPs37iNY5THJkF51qarFEUdJpump")
    assert raw.volume_avg_baseline_usd == 0.0
    assert raw.breakout_detected is True  # basé sur le momentum de prix, pas sur volume_24h > lui-même
