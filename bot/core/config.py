"""Chargement de config.yaml. Point d'entrée unique pour tous les seuils."""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

# Charge bot/.env dans les variables d'environnement du process dès que
# core.config est importé (donc avant tout appel à core.secrets.get_secret).
# Sans ça, .env existe sur le disque mais rien ne le lit jamais.
load_dotenv(CONFIG_PATH.parent / ".env")


@functools.lru_cache(maxsize=1)
def load_config(path: Path | None = None) -> dict[str, Any]:
    p = path or CONFIG_PATH
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def reload_config(path: Path | None = None) -> dict[str, Any]:
    """Vide le cache — utile en tests ou après une modif manuelle de config.yaml."""
    load_config.cache_clear()
    return load_config(path)
