import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.solana_data import HeliusClient

TOKEN = "Hcd8d5DANpxgnXvwYPs37iNY5THJkF51qarFEUdJpump"


def _fake_response(mint_authority=None, freeze_authority=None):
    class FakeResponse:
        status_code = 200
        url = "https://mainnet.helius-rpc.com/?api-key=test"
        text = ""

        def json(self):
            return {
                "result": {
                    "value": {
                        "data": {
                            "parsed": {
                                "info": {
                                    "mintAuthority": mint_authority,
                                    "freezeAuthority": freeze_authority,
                                }
                            }
                        }
                    }
                }
            }

    return FakeResponse()


def test_get_mint_authorities_caches_by_token_and_calls_rpc_only_once():
    """Bug corrigé le 15/09/2026 : sans cache, un token qui ressort dans
    plusieurs cycles de scan (fréquent, listes "nouveaux tokens"/"tendances"
    répétées d'un cycle à l'autre) redéclenchait un appel RPC Helius
    identique à chaque fois -- observé en dry-run réel : quota gratuit
    Helius épuisé ("429 max usage reached") en quelques minutes, rejetant
    tous les candidats Solana par le fail-closed, quel que soit leur score
    marché."""
    client = HeliusClient(api_key="test")
    with patch("connectors.solana_data.requests.post", return_value=_fake_response()) as mock_post:
        first = client.get_mint_authorities(TOKEN)
        second = client.get_mint_authorities(TOKEN)
        third = client.get_mint_authorities(TOKEN)

    assert mock_post.call_count == 1  # un seul vrai appel RPC pour 3 lookups du même token
    assert first == second == third == (True, True)


def test_get_mint_authorities_does_not_cache_on_error():
    """Sur erreur (ex: 429 quota dépassé), on ne connaît pas la vraie
    réponse -- ne PAS mettre en cache, sinon un token serait rejeté pour
    toujours même une fois le quota reconstitué."""
    client = HeliusClient(api_key="test")

    class FakeErrorResponse:
        status_code = 429
        url = "https://mainnet.helius-rpc.com/?api-key=test"
        text = "max usage reached"

    with patch("connectors.solana_data.requests.post", return_value=FakeErrorResponse()):
        try:
            client.get_mint_authorities(TOKEN)
        except Exception:
            pass

    assert TOKEN not in client._mint_authority_cache


def test_get_mint_authorities_different_tokens_each_hit_rpc_once():
    client = HeliusClient(api_key="test")
    with patch("connectors.solana_data.requests.post", return_value=_fake_response()) as mock_post:
        client.get_mint_authorities("token_a")
        client.get_mint_authorities("token_b")
        client.get_mint_authorities("token_a")

    assert mock_post.call_count == 2  # deux tokens distincts, un appel chacun
