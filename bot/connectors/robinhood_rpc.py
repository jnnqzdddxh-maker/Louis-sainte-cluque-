"""Robinhood Chain (chain ID 4663) — RPC JSON direct + router Uniswap v4.

Important : la partie JSON-RPC (lecture de solde, nonce, gas price, envoi de
transaction signée) est un client Ethereum JSON-RPC standard et fonctionne
tel quel sur n'importe quelle chaîne EVM.

En revanche l'encodage exact des appels au Universal Router Uniswap v4
(adresse du router déployé sur Robinhood Chain, encodage des `commands` /
`actions` V4_SWAP, PoolKey) N'EST PAS implémenté en dur ici : la chaîne a
quelques semaines de mainnet et l'outillage/les adresses de contrats ne sont
pas assez stabilisés pour être codés en dur sans risque de perte de fonds
sur un mauvais calldata. `build_v4_swap_calldata` lève NotImplementedError
avec les étapes à suivre — c'est un garde-fou volontaire, pas un oubli.
Cohérent avec la recommandation du cahier des charges de prévoir plus de
tests sur cette chaîne avant d'y engager du capital réel.
"""
from __future__ import annotations

from dataclasses import dataclass

import requests

from core.secrets import get_secret

ROBINHOOD_CHAIN_ID = 4663
REQUEST_TIMEOUT_S = 10


class RpcError(RuntimeError):
    pass


class JsonRpcClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self._id = 0

    def call(self, method: str, params: list) -> dict:
        self._id += 1
        resp = requests.post(
            self.rpc_url,
            json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
            timeout=REQUEST_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RpcError(f"{method} a échoué: {data['error']}")
        return data["result"]

    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def get_balance(self, address: str) -> int:
        return int(self.call("eth_getBalance", [address, "latest"]), 16)

    def get_transaction_count(self, address: str) -> int:
        return int(self.call("eth_getTransactionCount", [address, "pending"]), 16)

    def gas_price(self) -> int:
        return int(self.call("eth_gasPrice", []), 16)

    def call_contract(self, to: str, data: str) -> str:
        return self.call("eth_call", [{"to": to, "data": data}, "latest"])

    def send_raw_transaction(self, signed_tx_hex: str) -> str:
        return self.call("eth_sendRawTransaction", [signed_tx_hex])

    def get_transaction_receipt(self, tx_hash: str) -> dict | None:
        return self.call("eth_getTransactionReceipt", [tx_hash])


@dataclass
class PoolKey:
    currency0: str
    currency1: str
    fee: int
    tick_spacing: int
    hooks: str


def build_v4_swap_calldata(
    universal_router_address: str,
    pool_key: PoolKey,
    zero_for_one: bool,
    amount_in: int,
    min_amount_out: int,
) -> bytes:
    raise NotImplementedError(
        "Encodage du Universal Router V4_SWAP volontairement non implémenté : "
        "1) confirmer l'adresse déployée du Universal Router sur Robinhood Chain "
        "(chain ID 4663) et son ABI exacte, 2) confirmer via un swap de test à "
        "montant symbolique sur un explorateur/testnet avant toute mise en prod, "
        "3) implémenter l'encodage 'commands'/'actions' (V4_SWAP = 0x10) avec "
        "web3.py ou eth_abi une fois ces deux points vérifiés. Voir "
        "core/risk_guard.py + config.yaml:chains.robinhood.min_dry_run_days_before_live."
    )


def sign_and_send_transaction(
    rpc: JsonRpcClient,
    to: str,
    data: bytes,
    value_wei: int,
    *,
    dry_run: bool,
) -> str | None:
    """dry_run=True : ne construit/signe/envoie rien, retourne None.
    dry_run=False : signe avec eth_account (clé lue via core.secrets) et envoie.
    """
    if dry_run:
        return None

    try:
        from eth_account import Account  # type: ignore
    except ImportError as exc:
        raise RpcError(
            "Exécution live Robinhood Chain requiert la dépendance optionnelle "
            "'eth-account' (voir requirements.txt, section live)."
        ) from exc

    private_key = get_secret("robinhood_wallet_private_key_env")
    account = Account.from_key(private_key)

    nonce = rpc.get_transaction_count(account.address)
    gas_price = rpc.gas_price()

    tx = {
        "chainId": ROBINHOOD_CHAIN_ID,
        "to": to,
        "value": value_wei,
        "data": data,
        "nonce": nonce,
        "gasPrice": gas_price,
        "gas": 500_000,  # à affiner via eth_estimateGas avant tout envoi live
    }
    signed = account.sign_transaction(tx)
    return rpc.send_raw_transaction(signed.raw_transaction.hex())
