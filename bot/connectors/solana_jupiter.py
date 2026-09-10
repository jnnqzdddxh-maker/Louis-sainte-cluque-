"""Exécution Solana via Jupiter (routing + swap).

En dry-run, `execute_swap` ne fait AUCUN appel réseau d'exécution : la quote
est récupérée pour avoir un prix réaliste, mais le swap n'est jamais signé
ni envoyé. Le passage en signature/envoi réel n'est possible qu'avec
dry_run=False, explicitement, et nécessite la dépendance optionnelle
`solders` (voir requirements.txt).
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

import requests

from core.secrets import get_secret

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
REQUEST_TIMEOUT_S = 10

# Mint de référence pour SOL "wrappé" natif.
SOL_MINT = "So11111111111111111111111111111111111111112"


class JupiterError(RuntimeError):
    pass


@dataclass
class Quote:
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    price_impact_pct: float
    raw: dict


@dataclass
class SwapResult:
    executed: bool
    dry_run: bool
    signature: str | None
    quote: Quote
    note: str


def get_quote(input_mint: str, output_mint: str, amount_lamports: int, slippage_bps: int) -> Quote:
    resp = requests.get(
        JUPITER_QUOTE_URL,
        params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount_lamports,
            "slippageBps": slippage_bps,
        },
        timeout=REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()
    if "outAmount" not in data:
        raise JupiterError(f"Réponse de quote Jupiter inattendue: {data}")
    return Quote(
        input_mint=input_mint,
        output_mint=output_mint,
        in_amount=int(data["inAmount"]),
        out_amount=int(data["outAmount"]),
        price_impact_pct=float(data.get("priceImpactPct", 0.0) or 0.0),
        raw=data,
    )


def _build_swap_transaction(quote: Quote, user_public_key: str) -> str:
    resp = requests.post(
        JUPITER_SWAP_URL,
        json={
            "quoteResponse": quote.raw,
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": True,
        },
        timeout=REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()
    if "swapTransaction" not in data:
        raise JupiterError(f"Réponse de swap Jupiter inattendue: {data}")
    return data["swapTransaction"]


def execute_swap(
    quote: Quote,
    user_public_key: str,
    *,
    dry_run: bool,
    rpc_url: str | None = None,
) -> SwapResult:
    """dry_run=True (par défaut attendu partout dans le bot) : construit la
    quote mais ne signe/n'envoie rien. dry_run=False : signe avec la clé
    privée du wallet d'exécution (lue via core.secrets, jamais en clair) et
    diffuse la transaction sur le RPC fourni.
    """
    if dry_run:
        return SwapResult(
            executed=False,
            dry_run=True,
            signature=None,
            quote=quote,
            note="dry-run: swap simulé, aucune transaction envoyée",
        )

    try:
        from solders.keypair import Keypair  # type: ignore
        from solders.transaction import VersionedTransaction  # type: ignore
        from solana.rpc.api import Client  # type: ignore
    except ImportError as exc:
        raise JupiterError(
            "Exécution live Solana requiert les dépendances optionnelles "
            "'solders' et 'solana' (voir requirements.txt, section live)."
        ) from exc

    if not rpc_url:
        raise JupiterError("rpc_url requis pour l'exécution live")

    private_key_b58 = get_secret("solana_wallet_private_key_env")
    keypair = Keypair.from_base58_string(private_key_b58)

    swap_tx_b64 = _build_swap_transaction(quote, user_public_key)
    raw_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_tx_b64))
    signed_tx = VersionedTransaction(raw_tx.message, [keypair])

    client = Client(rpc_url)
    result = client.send_raw_transaction(bytes(signed_tx))
    signature = str(result.value)

    return SwapResult(
        executed=True,
        dry_run=False,
        signature=signature,
        quote=quote,
        note="swap envoyé",
    )
