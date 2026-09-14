# Bot de trading crypto — surveillance + exécution

Implémentation du cahier des charges : scoring multi-signal, paliers de
sortie en escalier, garde-fous stricts, dashboard de surveillance.

**Le mode dry-run est obligatoire au démarrage. Ce n'est pas négociable —
voir section "Passage en live" ci-dessous.**

## Installation

```bash
cd bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # puis remplir .env (jamais le committer)
```

## Configuration

Tous les seuils vivent dans `config.yaml` — jamais codés en dur dans `core/`
ou `connectors/`. Avant de lancer quoi que ce soit, il faut au minimum
remplir dans `config.yaml` :

- `scoring.wallet_tracker.tracked_wallets` — la liste des wallets performants
  à suivre (point ouvert du cahier des charges, section 8)
- `scoring.twitter.tracked_accounts` / `keywords` — idem
- `risk.daily_loss_cap_eur` / `daily_loss_cap_pct_of_capital` / `capital_eur`
  — le plafond de perte journalière exact reste à trancher (section 8)
- `risk.max_open_positions` — idem

Et dans `.env` (jamais dans `config.yaml`, jamais committé) :
`BIRDEYE_API_KEY`, `HELIUS_API_KEY`, `BITQUERY_API_KEY` au minimum pour que
le dry-run tourne avec des données de marché réelles.

## Lancer le dry-run (obligatoire, plusieurs jours)

```bash
python dry_run.py
```

- Force `dry_run=True` quoi qu'il arrive, quelle que soit `config.yaml:mode`.
- Ne signe ni n'envoie **jamais** de transaction.
- Utilise les vraies APIs de marché en lecture seule, pour que le scoring et
  les paliers de sortie soient validés sur des conditions réelles.
- Dashboard sur http://127.0.0.1:8787 (positions, scores en direct des
  candidats, log des décisions).
- Log lisible des décisions dans `bot/logs/decisions.jsonl`.

## Plan de validation avant argent réel (section 7 du cahier des charges)

1. **Dry-run 3-5 jours minimum** — vérifier que le scoring détecte des
   tokens cohérents (dashboard + `bot/logs/decisions.jsonl`).
2. **Vérifier manuellement chaque décision de sortie simulée par palier** —
   les tests dans `tests/test_position_manager.py` valident la logique en
   théorie, mais seule l'observation sur des vrais mouvements de prix valide
   le comportement en pratique.
3. **Premiers trades réels à la taille minimale (20€), pas 50€** —
   `sizing.force_minimum_size_until_proven: true` dans `config.yaml` force
   déjà cette règle au niveau code ; ne le désactiver qu'après ce palier.
4. **Augmenter progressivement** une fois la confiance acquise sur le
   comportement réel.

Sur Robinhood Chain spécifiquement : prévoir plus de cycles de dry-run
qu'sur Solana (`chains.robinhood.min_dry_run_days_before_live` dans
`config.yaml`) — chaîne très récente, outillage moins mature. Voir aussi la
section "Limites connues" plus bas concernant l'exécution Uniswap v4.

## Passage en live

Le bot refuse de démarrer en mode live tant que **les deux** conditions ne
sont pas réunies :

1. `config.yaml` : `mode: live`
2. Variable d'environnement `BOT_CONFIRM_LIVE=I_UNDERSTAND_THE_RISK` définie
   explicitement au lancement (jamais dans un fichier committé)

```bash
BOT_CONFIRM_LIVE=I_UNDERSTAND_THE_RISK python main.py
```

Sans les deux, `main.py` démarre automatiquement en dry-run par sécurité
(voir `resolve_dry_run()` dans `main.py`).

Il faut en plus renseigner dans `config.yaml` les clés **publiques** des
wallets d'exécution (`chains.solana.wallet_public_key`,
`chains.robinhood.wallet_address`) et dans `.env` les clés **privées**
correspondantes — sans quoi le bot tourne en "mode décision seule" (il log
ce qu'il ferait, sans jamais tenter d'exécuter).

## Architecture

```
bot/
  core/
    config.py           # chargement config.yaml
    secrets.py           # accès secrets via variables d'env uniquement
    scoring.py            # score de confiance 0-100 (3 signaux)
    position_manager.py    # paliers, trailing SL, sizing
    risk_guard.py           # stop catastrophe global, plafond journalier, kill-switch
    engine.py                # orchestration : wallet event OU scan de marché -> score -> position -> exécution
    logging_store.py          # log de décisions (JSONL) + état (SQLite)
  connectors/
    solana_jupiter.py    # exécution Solana (Jupiter)
    solana_data.py        # données marché Solana (Birdeye/Helius)
    robinhood_rpc.py        # RPC direct + Uniswap v4 (Robinhood Chain)
    robinhood_data.py        # données marché Robinhood Chain (DexPaprika/Bitquery)
    wallet_tracker.py         # suivi des wallets performants (polling Helius/Bitquery, fonctionne sur un PC perso sans adresse publique)
    twitter_watch.py           # veille périodique (5 min), pas de streaming
    execution_handlers.py       # branche core.engine sur les connecteurs d'exécution
  dashboard/
    app.py                # FastAPI : positions, scores, décisions, reset manuel
    static/index.html       # page unique, se rafraîchit toutes les 5s
  tests/                   # tests sur la logique critique (scoring, paliers)
  config.yaml               # TOUS les seuils
  dry_run.py                 # mode simulation (voir plus haut)
  main.py                     # dry-run par défaut, live sur confirmation explicite
```

## Découverte de candidats : trois modes en parallèle

- **Wallet tracker** (`scoring.wallet_tracker.tracked_wallets`) — un token
  devient candidat quand assez de wallets suivis l'achètent dans la
  fenêtre configurée. C'est le mode d'origine du cahier des charges.
- **Scan "nouveaux tokens"** (`market_scan.new_listings`) — repère les
  tokens/pools tout juste créés, avant même qu'ils aient du volume. C'est le
  mode prioritaire pour la stratégie x2->x100, qui suppose de rentrer tôt.
  Tourne toutes les `interval_seconds` (60s par défaut).
- **Scan "tendances"** (`market_scan.trending`) — complément qui regarde les
  tokens/pools avec le plus gros volume actuel. Utile en filet de sécurité,
  mais remonte souvent des tokens déjà bien montés (x100 probablement déjà
  raté dessus, x2-x5 peut rester pertinent). Tourne toutes les 5 min par
  défaut. Chaque candidat garde une étiquette `source` (visible dans le
  dashboard et les logs) indiquant lequel des trois modes l'a détecté.

Les trois tournent en même temps et alimentent le même scoring. Différence
importante : un token trouvé uniquement par le scan de marché (aucun wallet
suivi ne l'a acheté) plafonne à un score d'environ 55/100 avec les poids
par défaut (40 marché + 15 twitter max, le wallet tracker pesant 45%) — donc
confiance "moyenne" au mieux, jamais "haute" ni "très haute". C'est voulu :
sans corroboration d'un wallet réputé, le palier x100 (réservé à la
confiance "très haute") ne peut jamais se déclencher sur un token découvert
par le scan seul.

## Vérification de légitimité (bonus, pas un filtre éliminatoire)

En plus du filtre anti-rug (liquidité min / concentration max, lui
éliminatoire), le sous-score marché inclut un bonus de légitimité jusqu'à
20 points (`core/scoring.py:_market_subscore`) :

- **Liens sociaux déclarés** (site web/Twitter/Telegram/Discord dans les
  métadonnées on-chain, via `BirdeyeClient.get_token_overview`) — Solana
  uniquement, DexPaprika ne fournit pas cette info pour Robinhood Chain.
- **Appairé à un actif de référence** (SOL/USDC/USDT sur Solana,
  ETH/WETH/USDC/USDT sur Robinhood Chain — `config.yaml:
  scoring.price_volume_liquidity.recognized_quote_tokens`) plutôt qu'à un
  token obscur, via `BirdeyeClient.get_markets` / les pools DexPaprika.

C'est un bonus, pas une porte : l'absence de liens sociaux ou un appairage
inhabituel ne rejette pas le token (beaucoup de projets légitimes ne
remplissent pas ces métadonnées), ça baisse juste un peu son score.

## Autorités mint/freeze (Solana) — celui-là, éliminatoire

Ajouté le 14/09/2026, sur demande explicite : un token Solana est rejeté si
son **autorité de mint** (le créateur peut créer des tokens à l'infini,
diluer/arnaquer) ou son **autorité de freeze** (le créateur peut geler les
tokens de n'importe quel détenteur) n'est pas révoquée on-chain —
`config.yaml: scoring.price_volume_liquidity.require_renounced_authorities`.
Vérifié via RPC (`HeliusClient.get_mint_authorities`, standard SPL Token,
pas un endpoint REST tiers fragile). **Fail-closed** : si la vérification
échoue (erreur réseau), le token est rejeté par prudence plutôt que laissé
passer sans certitude. Pas d'équivalent standardisé sur Robinhood Chain
(ERC20) — ce filtre ne s'applique qu'à Solana.

## Garde-fous (section 4 du cahier des charges)

- Taille max par position : 20-50€ (`sizing`), forcée à 20€ tant que
  `force_minimum_size_until_proven` est actif.
- Stop-loss catastrophe -60% à -70% avant x2 (`risk.catastrophe_stop_loss_pct`),
  appliqué par `core/position_manager.py` sur **chaque** position, sans
  exception codée.
- Plafond de perte journalière global (`risk.daily_loss_cap_eur` /
  `_pct_of_capital`) : au-delà, `core/risk_guard.py` arrête l'ouverture de
  toute nouvelle position et exige un reset manuel explicite
  (`RiskGuard.manual_reset(confirmed_by=...)`, aussi exposé dans le
  dashboard) — jamais de reset automatique, même le lendemain.
- Nombre max de positions ouvertes simultanément (`risk.max_open_positions`).
- Clés privées : jamais en clair dans le code, jamais committées — lues
  exclusivement via variables d'environnement (`core/secrets.py`), voir
  `.env.example`.

## Limites connues (à traiter avant d'engager du capital réel)

- **Suivi des wallets Robinhood Chain désactivé** (`chains.robinhood.
  wallet_tracking_enabled: false`) — Bitquery a répondu "usage quota
  reached" (14/09/2026), quota gratuit épuisé, pas de plan payant prévu.
  Le scan de marché DexPaprika (gratuit) reste actif sur cette chaîne.
- **Scans "nouveaux tokens"/"tendances" Solana rebasculés sur DexPaprika**
  (14/09/2026) — Birdeye ("Compute units usage limit exceeded") coûtait un
  quota payant pour ces deux scans ; DexPaprika supporte aussi Solana
  nativement, gratuitement, sans clé. Le scoring d'un candidat détecté
  continue d'utiliser Birdeye ensuite (`get_token_overview`, usage bien
  plus léger, non affecté). Simplification à noter :
  `DexPaprikaClient._pick_base_token_address` choisit le token qui n'est
  pas la monnaie de cotation reconnue (SOL/USDC/USDT) dans chaque pool
  remonté — si aucun des deux ne matche (pool exotique), repli sur le
  premier token du pool, pas garanti d'être le bon.
- **Encodage du swap Uniswap v4 sur Robinhood Chain non implémenté**
  (`connectors/robinhood_rpc.py:build_v4_swap_calldata` lève
  volontairement `NotImplementedError`). La chaîne a quelques semaines de
  mainnet ; coder en dur une adresse de Universal Router / un encodage de
  calldata sans les avoir vérifiés sur la chaîne réelle serait le genre
  d'erreur qui fait perdre des fonds. À compléter et tester à montant
  symbolique avant tout usage réel.
- **Résolution token → pool sur Robinhood Chain simplifiée** dans
  `dry_run.py:build_market_data_fetchers` (suppose `token_address ==
  pool_address`) — à remplacer par une vraie résolution avant usage réel.
- **Solde on-chain non branché pour les ventes Solana**
  (`connectors/execution_handlers.py:SolanaExecutionHandler.sell` lève
  `NotImplementedError` en live) — nécessite un lookup de solde réel
  (quantité de tokens détenus, decimals) avant de construire la quote de
  vente Jupiter.
- **Veille sociale sur Reddit par défaut, pas Twitter/X** — pas d'accès API
  X payant (cahier des charges, section 3), donc `TwitterWatcher` utilise
  `connectors/twitter_watch.py:RedditSearchBackend` (recherche publique,
  gratuite, sans clé) sur une poignée de subreddits crypto. Moins réactif
  qu'un vrai flux Twitter, et beaucoup de micro-tokens n'auront tout
  simplement aucun post Reddit — le signal "twitter_news" (15% du poids)
  restera donc souvent à 0, ce qui est normal, pas un bug. Un vrai backend
  Twitter/X ou un agrégateur de news (ex: CryptoPanic) peut remplacer/
  s'ajouter via `SearchBackend` sans toucher au reste du module.
- **Interprétation de deux points non-explicites du tableau de la section 5**
  du cahier des charges (trailing "normal" entre x5→x10 par cohérence avec
  x2→x5 et x10→x20 ; exécution automatique — et non purement manuelle — du
  stop-loss au-delà de x20/x100) — documentée en détail en tête de
  `core/position_manager.py`. À confirmer/ajuster après lecture des logs de
  dry-run réels.
- **Points ouverts du cahier des charges (section 8)** transformés en
  valeurs par défaut prudentes dans `config.yaml`, PAS en décisions
  définitives : plafond de perte journalière, nombre max de positions,
  inclusion des frais dans le breakeven, listes de wallets/comptes Twitter
  à suivre.

## Tests

```bash
python -m pytest tests/ -v
```

Couvre notamment : filtre anti-rug éliminatoire, stop catastrophe avant x2,
paliers x2/x5/x10/x20/x100 (les pourcentages de reliquat obtenus
correspondent aux valeurs approximatives données dans le cahier des
charges — ~45% à x10, ~11% à x20, ~2,75% à x100), trailing stop, sizing
forcé au minimum tant que non éprouvé.
