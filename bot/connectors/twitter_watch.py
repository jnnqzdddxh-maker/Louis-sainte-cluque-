"""Veille sociale périodique (section 3 du cahier des charges) — recherche
toutes les 5 min, PAS de streaming temps réel (pas d'accès API X payant).

Backend par défaut : Reddit (recherche publique, sans clé API — voir
RedditSearchBackend). Twitter/X exigerait un abonnement API payant, donc
`scoring.twitter.tracked_accounts` (syntaxe "from:@compte") reste prévu pour
un futur backend Twitter mais n'est utilisé par aucun backend pour l'instant.

`SearchBackend` reste une interface branchable : un backend Twitter, un
agrégateur de news crypto (ex: CryptoPanic), ou autre peut remplacer/s'ajouter
à Reddit sans toucher au reste du module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import requests

from core.config import load_config
from core.scoring import TwitterSignal

REQUEST_TIMEOUT_S = 10


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
            "Aucun backend de recherche n'est branché. Fournir une "
            "implémentation de SearchBackend.search(query) à TwitterWatcher."
        )


class RedditSearchBackend:
    """Recherche publique sur des subreddits crypto, sans clé API ni compte.
    Reddit exige un User-Agent identifiable, sinon il répond 429. Moins
    réactif qu'un vrai flux Twitter/X (et Reddit n'a pas forcément un post
    sur chaque micro token), mais fonctionne immédiatement, gratuitement.
    """

    def __init__(
        self,
        subreddits: list[str] | None = None,
        user_agent: str = "trading-bot-dry-run/0.1",
    ):
        self.subreddits = subreddits or [
            "CryptoMoonShots",
            "solana",
            "SatoshiStreetBets",
            "CryptoCurrency",
        ]
        self.user_agent = user_agent

    def search(self, query: str) -> list[Mention]:
        mentions: list[Mention] = []
        for subreddit in self.subreddits:
            try:
                resp = requests.get(
                    f"https://www.reddit.com/r/{subreddit}/search.json",
                    params={"q": query, "restrict_sr": 1, "sort": "new", "limit": 10},
                    headers={"User-Agent": self.user_agent},
                    timeout=REQUEST_TIMEOUT_S,
                )
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException:
                continue  # un subreddit en échec ne doit pas bloquer les autres
            for child in data.get("data", {}).get("children", []):
                post = child.get("data", {})
                mentions.append(
                    Mention(
                        text=post.get("title", ""),
                        author=post.get("author", ""),
                        posted_at=datetime.fromtimestamp(post.get("created_utc", 0), tz=timezone.utc),
                        url=f"https://reddit.com{post.get('permalink', '')}",
                    )
                )
        return mentions


class TwitterWatcher:
    """Interroge periodiquement les mots-clés suivis (+ toujours le symbole
    du token lui-même, même sans mot-clé configuré). L'appelant (dry_run.py /
    main.py) est responsable de respecter poll_interval_minutes entre deux
    appels à `poll()` — ce module ne planifie rien lui-même.
    """

    def __init__(self, config: dict | None = None, backend: SearchBackend | None = None):
        self.cfg = config or load_config()
        tc = self.cfg["scoring"]["twitter"]
        self.keywords: list[str] = tc["keywords"]
        self.max_signal_age_minutes = tc["max_signal_age_minutes"]
        self.backend: SearchBackend = backend or RedditSearchBackend()
        self._last_mentions_by_token: dict[str, list[Mention]] = {}

    def poll(self, token_symbol_or_address: str) -> list[Mention]:
        """Cherche les mentions récentes d'un token donné. Le symbole/l'adresse
        seul est toujours recherché ; les mots-clés suivis (config.yaml) sont
        ajoutés comme contexte supplémentaire s'ils sont renseignés.
        """
        mentions: list[Mention] = []
        queries = [token_symbol_or_address] + [
            f"{kw} {token_symbol_or_address}" for kw in self.keywords
        ]
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
