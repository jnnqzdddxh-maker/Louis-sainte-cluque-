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

- **Bug corrigé : decisions.jsonl trop gros pour être ouvert sous Windows,
  bloquant TOUTE ouverture de position** (`core/logging_store.py`,
  `core/engine.py`, 15/09/2026) — l'utilisateur a signalé "toujours pas une
  position d'ouverte depuis hier soir" ; log réel fourni :
  `OSError: [Errno 22] Invalid argument: 'logs\decisions.jsonl'` sur
  `open()`, à la fois en écriture (`"a"`) ET en lecture, y compris pour une
  seule ligne. Cause : `scoring.py`/`decisions.jsonl` grossissait en continu
  à cause de `liquidity_watchlist` (ajouté le matin même) qui journalisait
  "candidate_scored" à CHAQUE recheck (jusqu'à 30 fois par token sur 15 min,
  toutes les 30s, pour des dizaines de tokens pump.fun détectés par minute)
  même quand le résultat était identique au précédent — le fichier a fini
  par dépasser une limite que Python en mode texte ne peut plus ouvrir sous
  Windows (~2 Go). Conséquence grave, pas juste cosmétique :
  `TradingEngine._maybe_score_candidate` écrit dans ce log AVANT d'atteindre
  la logique d'ouverture de position — donc plus AUCUNE position ne
  pouvait s'ouvrir depuis que le fichier avait dépassé cette taille, quels
  que soient les seuils de scoring. Corrigé par deux mécanismes
  complémentaires :
  - `core/engine.py` : un recheck de liste d'attente qui retombe sur le
    MÊME verdict (encore rejeté pour liquidité) n'est plus écrit sur disque
    (le dashboard reste à jour quand même, via le dict `candidates` en
    mémoire) — seule la détection initiale et les changements de verdict
    sont journalisés.
  - `core/logging_store.py` : rotation automatique du fichier de log
    basée sur `Path.stat()` (jamais sur une ouverture du fichier) dès qu'il
    dépasse 200 Mo — répare donc automatiquement un fichier DÉJÀ trop gros
    pour être ouvert, sans action manuelle. `read_all(limit=...)` (utilisé
    par le dashboard) lit maintenant les dernières lignes en mode binaire
    avec des seeks explicites (`_tail_lines`) au lieu de charger tout le
    fichier en mémoire avec `readlines()`.
- **Liquidité minimale anti-rug re-baissée de 1500$ à 300$**
  (`scoring.price_volume_liquidity.min_liquidity_usd`, 15/09/2026) —
  malgré la liste d'attente (recheck automatique pendant 15 min, voir plus
  bas), les logs dry-run réels montrent que la liquidité des tokens
  pump.fun ne "rattrape" quasiment jamais 1500$ dans cette fenêtre : des
  tokens rechecked à 10-15 minutes d'écart restaient à 0-13$ pour la
  plupart, un 832$ et un 1011$ n'ont pas franchi 1500$ non plus. Hypothèse
  la plus probable (pas juste un délai d'indexation, contrairement à ce
  qu'on pensait le 15/09/2026 matin) : le bonding curve pump.fun ne
  contient tout simplement pas 1500$ de liquidité réelle tant que le token
  n'a pas "gradué" vers un vrai pool AMM (Raydium) — ce qui n'arrive qu'à
  une minorité de tokens, souvent après une hausse de prix déjà bien
  entamée (donc l'entrée précoce y est déjà ratée). Décision explicite de
  l'utilisateur, qui accepte le risque supplémentaire ("on garde l'autre
  [la liste d'attente] mais on baisse aussi le seuil ... on est en test
  autant voir si il finira en positif ou pas") : 300$ est BEAUCOUP plus
  facile à manipuler/rug qu'à 1500$ — le stop-loss catastrophe
  (`risk.catastrophe_stop_loss_pct`) reste la protection de dernier recours
  si un rug passe quand même ce filtre plus permissif.
- **Seuil de déclenchement wallet baissé de 2 à 1**
  (`scoring.wallet_tracker.min_wallets_to_trigger`, 15/09/2026, demande
  explicite de l'utilisateur : "j'en ai pas beaucoup pour l'instant") —
  avec un nombre encore modeste de wallets suivis (indépendants, pas un
  groupe coordonné), exiger que 2 wallets distincts achètent le MÊME token
  dans la même fenêtre de 10 minutes (`window_minutes`) est une coïncidence
  rare : la plupart des achats de wallets suivis n'étaient donc jamais
  scorés ni affichés (`require_wallet_trigger` dans core/engine.py:
  on_wallet_buy_event ignore l'événement en dessous du seuil). À 1, un seul
  wallet suivi qui achète suffit à déclencher l'évaluation complète du
  token (marché + Twitter) — le sous-score WALLET lui-même reste à 0 pour
  un achat solo (`min_wallets_for_full_signal` reste à 3 pour la pleine
  confiance), ça débloque juste l'évaluation au lieu de l'ignorer
  totalement. Compromis assumé : signal plus faible par achat solo qu'une
  convergence de plusieurs wallets — à remonter si la liste de wallets
  suivis grandit beaucoup.
- **Toute l'échelle de confiance baissée** (`scoring.confidence_thresholds`,
  14/09/2026) — le bot n'a ouvert aucune position en plusieurs heures de
  dry-run réel : à 40, un candidat sans wallet (plafonné à ~55/100)
  devait avoir un score marché quasi parfait pour devenir tradable. Demande
  explicite de l'utilisateur ("il faut que le bot prenne un peu plus de
  risque", puis "baisser chaque palier un peu") :
  - `low_max` 39 → 19 (moyenne dès 20 au lieu de 40)
  - `medium_max` 64 → 50, `high_max` 84 → 70 (haute/très haute plus
    accessibles aussi)
  Propriété intentionnellement préservée malgré la baisse : le plafond
  d'un candidat sans wallet reste ~55/100, en dessous de 71 (nouveau seuil
  "très haute") — le palier x100 reste donc réservé aux tokens corroborés
  par un wallet suivi, pas à un simple pic de volume. Plus de trades, sur
  des signaux plus faibles — à surveiller de près sur les prochains jours.
- **Bug corrigé : breakout structurellement indétectable sur les tokens tout
  juste créés** (`connectors/robinhood_data.py:_pool_to_raw_market_data`,
  14/09/2026) — score max observé en dry-run réel : 4/100, signalé par
  l'utilisateur comme suspect ("le max score que j'ai eu c'est 4 c'est pas
  normal"), et il avait raison. Cause : pour un token sans 7 jours
  d'historique (`volume_usd_7d == 0`, le cas NORMAL pour un token qui vient
  d'être créé — exactement ceux que le scan "nouveaux tokens" cible),
  l'ancien code faisait `baseline = volume_24h` (repli), puis testait
  `volume_24h > baseline`, soit comparer une valeur à elle-même : toujours
  faux, quel que soit le prix. Le breakout (30 pts sur 100 du score marché)
  était donc à 0 sur tous les tokens les plus frais, et le bonus de
  légitimité (`has_social_links`) est toujours `False` avec DexPaprika — il
  ne restait souvent que les 10 pts de `paired_with_recognized_quote`, soit
  `10 × 0.40 = 4` de score total. Corrigé : historique fiable seulement si
  `volume_usd_7d > volume_usd_24h` (plus qu'un simple jour de trading) ;
  sinon le breakout se base sur le momentum de prix seul
  (`price_change_percentage_1h`/`5m`), le seul signal réellement disponible
  sur un token sans historique de volume.
- **Bug corrigé : un token frais ne pouvait de toute façon jamais atteindre
  le seuil "moyenne"** (`core/scoring.py:_market_subscore`, 14/09/2026) —
  signalé par l'utilisateur juste après le fix du breakout ci-dessus ("il a
  pas ouvert une seule position encore"), et il avait de nouveau raison :
  le fix du breakout était réel mais insuffisant. Sans historique 7j
  (`volume_avg_baseline<=0`, cas normal pour un token tout juste créé),
  `volume_ratio_score` retombait à 0 quel que soit le momentum de prix —
  plafonnant le market_subscore à `30 (breakout) + 10 (appairage) = 40/100`,
  donc le score total à `40 × 0.40 = 16/100`. Sous le seuil "moyenne" (20,
  voir plus haut) : un token frais sans corroboration wallet ne pouvait
  DONC JAMAIS être tradé via le signal marché seul, indépendamment de son
  momentum de prix. Corrigé : quand il n'y a pas d'historique 7j mais que
  la liquidité est connue, le momentum se base sur le ratio volume 24h /
  liquidité (`scoring.price_volume_liquidity.
  fresh_token_volume_to_liquidity_multiplier`, 1.5 par défaut = volume 24h
  >= 1.5x la liquidité pour le score plein) — une métrique d'activité
  réelle valable dès le jour de création du token, contrairement à une
  moyenne 7 jours qui n'existe pas encore.
- **Bug corrigé : quota Helius épuisé en quelques minutes, faute de cache**
  (`connectors/solana_data.py:HeliusClient`, `connectors/robinhood_data.py:
  DexPaprikaClient.fetch_raw_market_data_by_token`, 15/09/2026) — logs
  dry-run réels : "429 sur .../getAccountInfo: max usage reached" sur
  quasiment CHAQUE candidat Solana, quelques minutes après le démarrage.
  `get_mint_authorities` rejette par fail-closed sur erreur (voulu, c'est le
  filtre anti-rug) — mais l'ancien code recréait un `HeliusClient()` tout
  neuf à chaque appel, sans aucun cache, et rappelait Helius pour un token
  déjà vérifié à chaque fois qu'il ressortait dans un cycle de scan suivant
  (fréquent : les listes "nouveaux tokens"/"tendances" se répètent). Pour du
  20 tokens/60s (scan nouveaux tokens) + 20/300s (tendances), ça fait vite
  des centaines d'appels Helius par heure pour une donnée (mint/freeze
  authority) qui ne change quasiment jamais une fois vérifiée. Résultat :
  quota gratuit épuisé très vite, TOUS les candidats Solana rejetés par le
  fail-closed, quel que soit leur score marché (indépendamment des deux
  bugs de scoring ci-dessus). Corrigé : `HeliusClient` met en cache
  (mint_renounced, freeze_renounced) par adresse de token dès la première
  vérification réussie (pas mis en cache sur erreur — un 429 ne doit pas
  rejeter un token pour toujours), et `DexPaprikaClient` réutilise la MÊME
  instance de `HeliusClient` sur toute la durée du run au lieu d'en créer
  une neuve à chaque appel.
- **Liste d'attente pour les tokens rejetés uniquement pour liquidité
  insuffisante** (`core/engine.py:TradingEngine.liquidity_watchlist` /
  `recheck_liquidity_watchlist`, `dry_run.py:liquidity_watchlist_loop`,
  `config.yaml:market_scan.liquidity_watchlist`, 15/09/2026) — log dry-run
  réel fourni par l'utilisateur après les 3 fixes ci-dessus : toujours
  aucune position ouverte. Analyse : sur 38 candidats Solana, 34 rejetés
  pour liquidité insuffisante, dont **24 à EXACTEMENT 0$** (pas juste sous
  le seuil de 1500$). Cause : le scan "nouveaux tokens" capture chaque pool
  pump.fun à la seconde de sa création, avant que DexPaprika ait indexé sa
  vraie liquidité (le bonding curve a pourtant du SOL derrière dès le
  premier achat) — et comme pump.fun crée des dizaines de tokens/minute, un
  token ne reste dans le top 20 "plus récents" qu'un seul cycle de scan
  avant d'être poussé hors de la liste par des tokens encore plus récents :
  il n'était donc jamais réévalué, même si sa liquidité réelle dépassait le
  seuil deux minutes plus tard. Baisser encore `min_liquidity_usd` n'aurait
  presque rien changé (la plupart des rejets sont à 0$ pile, pas "presque
  assez"). Corrigé : un token rejeté UNIQUEMENT pour liquidité insuffisante
  (jamais pour mint/freeze authority révoquée, qui ne change pas dans le
  temps) est gardé en mémoire et réévalué automatiquement toutes les 30s
  pendant 15 minutes maximum (`liquidity_watchlist.max_age_minutes`),
  indépendamment de sa présence dans les scans suivants.
- **Suivi des wallets Robinhood Chain désactivé** (`chains.robinhood.
  wallet_tracking_enabled: false`) — Bitquery a répondu "usage quota
  reached" (14/09/2026), quota gratuit épuisé, pas de plan payant prévu.
  Le scan de marché DexPaprika (gratuit) reste actif sur cette chaîne.
- **Birdeye entièrement retiré du chemin critique Solana, remplacé par
  DexPaprika** (14/09/2026) — le quota gratuit épuisé bloquait en fait
  TOUTES les routes Birdeye, y compris `token_overview` utilisé pour
  scorer chaque candidat (pas seulement les 2 scans, comme cru dans un
  premier temps). `market_data_fetchers["solana"]` pointe maintenant vers
  `DexPaprikaClient.fetch_raw_market_data_by_token`
  (`/networks/solana/pools/search?token_address=...`, endpoint confirmé
  via test réel). Le check mint/freeze authority (RPC Helius) reste actif,
  indépendant de Birdeye. `BirdeyeClient` reste dans le code
  (`connectors/solana_data.py`) mais n'est plus appelé nulle part par
  défaut — à réactiver seulement avec un plan payant.
  Simplification à noter : `DexPaprikaClient._pick_base_token_address`
  choisit le token qui n'est pas la monnaie de cotation reconnue
  (SOL/USDC/USDT, par symbole ET par adresse) dans chaque pool remonté —
  si aucun des deux ne matche (pool exotique), repli sur le premier token
  du pool, pas garanti d'être le bon.
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
