"""Veille Twitter/X périodique (section 3) — recherche web toutes les 5 min,
PAS de streaming temps réel (pas d'accès API X payant).

Ce module ne code aucun backend de recherche en dur : il expose une
interface `SearchBackend` que l'orchestrateur (dry_run.py / main.py) doit
brancher sur un moteur de recherche réel disponible dans l'environnement
d'exécution (API de recherche web, scraping d'un miroir Nitter, service
tiers, etc.). Un moteur non branché lève NotImplementedError plutôt que de
simuler silencieusement des résultats.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from core.config import load_config
from core.scoring import TwitterSignal


@dataclass(frozen=True)
class Mention:
    text: str
    author: str
    posted_at: datetime
    url: str


class SearchBackend(Protocol):
    def search(self, query: str) -> list[Mention]: ...


class NoSearchBackendConfigured:
    def search(self, query: str) -> list[Mention]:
        raise NotImplementedError(
            "Aucun backend de recherche Twitter/X n'est branché. Fournir une "
            "implémentation de SearchBackend.search(query) à TwitterWatcher "
            "(ex: wrapper autour d'un outil de recherche web, d'une API tierce, "
            "ou d'un miroir Nitter) — voir README section veille Twitter."
        )


class TwitterWatcher:
    """Interroge periodiquement les comptes/mots-clés suivis. L'appelant
    (dry_run.py / main.py) est responsable de respecter poll_interval_minutes
    entre deux appels à `poll()` — ce module ne planifie rien lui-même.
    """

    def __init__(self, config: dict | None = None, backend: SearchBackend | None = None):
        self.cfg = config or load_config()
        tc = self.cfg["scoring"]["twitter"]
        self.tracked_accounts: list[str] = tc["tracked_accounts"]
        self.keywords: list[str] = tc["keywords"]
        self.max_signal_age_minutes = tc["max_signal_age_minutes"]
        self.backend: SearchBackend = backend or NoSearchBackendConfigured()
        self._last_mentions_by_token: dict[str, list[Mention]] = {}

    def poll(self, token_symbol_or_address: str) -> list[Mention]:
        """Cherche les mentions récentes d'un token donné parmi les comptes
        et mots-clés suivis. `token_symbol_or_address` est injecté dans les
        requêtes pour cibler les mentions pertinentes à CE token précis.
        """
        mentions: list[Mention] = []
        queries = [f"from:{acct} {token_symbol_or_address}" for acct in self.tracked_accounts]
        queries += [f"{kw} {token_symbol_or_address}" for kw in self.keywords]
        for query in queries:
            mentions.extend(self.backend.search(query))
        self._last_mentions_by_token[token_symbol_or_address] = mentions
        return mentions

    def signal_for_token(self, token_symbol_or_address: str, now: datetime | None = None) -> TwitterSignal:
        now = now or datetime.now(timezone.utc)
        mentions = self._last_mentions_by_token.get(token_symbol_or_address, [])
        recent = [
            m for m in mentions
            if (now - m.posted_at).total_seconds() / 60 <= self.max_signal_age_minutes
        ]
        most_recent_age = None
        if recent:
            most_recent_age = min((now - m.posted_at).total_seconds() / 60 for m in recent)
        return TwitterSignal(
            mention_count=len(recent),
            most_recent_mention_age_minutes=most_recent_age,
        )
