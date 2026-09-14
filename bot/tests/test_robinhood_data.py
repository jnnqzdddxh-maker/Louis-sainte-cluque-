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
