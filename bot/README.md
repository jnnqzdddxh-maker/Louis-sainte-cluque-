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
    engine.py                # orchestration : wallet event -> score -> position -> exécution
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
- **Veille Twitter/X sans backend de recherche branché par défaut**
  (`connectors/twitter_watch.py:NoSearchBackendConfigured`) — pas d'accès
  API X payant (cahier des charges, section 3) ; brancher un backend de
  recherche (service tiers, miroir Nitter, etc.) avant usage réel.
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
