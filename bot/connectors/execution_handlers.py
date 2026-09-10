"""Implémentations concrètes de core.engine.ExecutionHandler.

Chaque handler respecte strictement le flag `dry_run` : en dry-run, aucun
appel qui signe ou envoie une transaction n'est effectué (voir
connectors/solana_jupiter.py et connectors/robinhood_rpc.py — c'est là,
pas ici, que la garde réelle est appliquée).

Conversion EUR -> montant on-chain : approximative dans ce scaffold (EUR
traité comme ~USD, sans gérer le spread de change). À affiner avant tout
usage avec des montants qui comptent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from core.engine import ExecutionResult
from connectors.robinhood_rpc import JsonRpcClient, PoolKey, build_v4_swap_calldata, sign_and_send_transaction
from connectors.solana_jupiter import SOL_MINT, execute_swap, get_quote


@dataclass
class SolanaExecutionHandler:
    rpc_url: str
    wallet_public_key: str
    slippage_bps: int
    get_sol_usd_price: Callable[[], float]

    def buy(self, token_address: str, size_eur: float, price_usd: float, *, dry_run: bool) -> ExecutionResult:
        sol_price = self.get_sol_usd_price()
        amount_lamports = int(size_eur / sol_price * 1_000_000_000)
        quote = get_quote(SOL_MINT, token_address, amount_lamports, self.slippage_bps)
        result = execute_swap(quote, self.wallet_public_key, dry_run=dry_run, rpc_url=self.rpc_url)
        return ExecutionResult(executed=result.executed, note=result.note, signature=result.signature)

    def sell(
        self, token_address: str, pct_of_initial_position: float, price_usd: float, *, dry_run: bool
    ) -> ExecutionResult:
        # Nécessite de connaître le solde réel de tokens détenus pour calculer
        # amount_lamports = solde * pct_of_initial_position / 100 (en unités du
        # token, decimals inclus) — à brancher sur un lookup de solde on-chain
        # réel avant tout usage live (voir connectors/solana_data.py /
        # HeliusClient pour les métadonnées de token, decimals compris).
        if dry_run:
            return ExecutionResult(executed=False, note="dry-run: vente simulée, aucune transaction envoyée")
        raise NotImplementedError(
            "SolanaExecutionHandler.sell nécessite un lookup de solde on-chain réel "
            "(quantité de tokens détenus, decimals) avant de pouvoir construire la "
            "quote Jupiter de vente. Garde-fou volontaire : ne pas deviner un montant."
        )


@dataclass
class RobinhoodExecutionHandler:
    rpc: JsonRpcClient
    wallet_address: str
    universal_router_address: str
    pool_key: PoolKey

    def buy(self, token_address: str, size_eur: float, price_usd: float, *, dry_run: bool) -> ExecutionResult:
        if dry_run:
            return ExecutionResult(executed=False, note="dry-run: achat simulé, aucune transaction envoyée")
        calldata = build_v4_swap_calldata(
            self.universal_router_address, self.pool_key, zero_for_one=True, amount_in=0, min_amount_out=0
        )
        tx_hash = sign_and_send_transaction(
            self.rpc, self.universal_router_address, calldata, 0, dry_run=dry_run
        )
        return ExecutionResult(executed=True, note="swap envoyé", signature=tx_hash)

    def sell(
        self, token_address: str, pct_of_initial_position: float, price_usd: float, *, dry_run: bool
    ) -> ExecutionResult:
        if dry_run:
            return ExecutionResult(executed=False, note="dry-run: vente simulée, aucune transaction envoyée")
        calldata = build_v4_swap_calldata(
            self.universal_router_address, self.pool_key, zero_for_one=False, amount_in=0, min_amount_out=0
        )
        tx_hash = sign_and_send_transaction(
            self.rpc, self.universal_router_address, calldata, 0, dry_run=dry_run
        )
        return ExecutionResult(executed=True, note="swap envoyé", signature=tx_hash)
