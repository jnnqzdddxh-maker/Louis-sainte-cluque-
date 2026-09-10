"""Accès aux secrets — jamais en clair dans le code ni commit dans le repo.

Toute clé privée ou clé API se lit exclusivement depuis les variables
d'environnement dont le NOM est déclaré dans config.yaml (section `secrets`).
"""
from __future__ import annotations

import os

from core.config import load_config


class MissingSecretError(RuntimeError):
    pass


def get_secret(config_key: str, *, required: bool = True) -> str | None:
    """Récupère un secret via son nom de variable d'env déclaré dans config.yaml.

    Exemple: get_secret("solana_wallet_private_key_env")
    """
    cfg = load_config()
    env_var_name = cfg["secrets"].get(config_key)
    if not env_var_name:
        raise KeyError(f"'{config_key}' absent de la section secrets de config.yaml")

    value = os.environ.get(env_var_name)
    if not value and required:
        raise MissingSecretError(
            f"Variable d'environnement '{env_var_name}' non définie "
            f"(requise pour '{config_key}'). Voir .env.example."
        )
    return value or None
