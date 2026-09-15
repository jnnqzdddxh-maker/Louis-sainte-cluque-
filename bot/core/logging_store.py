"""Journalisation des décisions (JSONL lisible) + persistance d'état (SQLite).

Section 7 du cahier des charges : "vérifier manuellement chaque décision de
sortie simulée par palier (logs lisibles)" — le format JSONL est choisi pour
rester grep-able/relisible à la main tout en étant facile à charger dans le
dashboard.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import load_config

# Bug réel observé le 15/09/2026 : decisions.jsonl a grossi jusqu'à dépasser
# une limite que Python en mode texte ne gère pas sous Windows (~2 Go) --
# `open(path, "a", encoding="utf-8")` ET `open(path, encoding="utf-8")`
# échouaient tous les deux avec `OSError: [Errno 22] Invalid argument`, y
# compris pour écrire une seule ligne. Conséquence grave : `TradingEngine.
# _maybe_score_candidate` appelle `logger.log(...)` avant d'atteindre la
# logique d'ouverture de position -- le bot n'ouvrait donc plus AUCUNE
# position depuis que le fichier avait dépassé cette taille, indépendamment
# de tout seuil de scoring. Cause de la croissance : `liquidity_watchlist`
# (ajouté le 15/09/2026 matin) réévalue un token rejeté pour liquidité
# toutes les 30s pendant 15 min, et journalisait "candidate_scored" à
# CHAQUE recheck même quand le résultat était identique au précédent --
# avec des dizaines de tokens pump.fun détectés par minute, ça déborde vite.
# Corrigé par deux mécanismes complémentaires : throttling de ce cas précis
# (core/engine.py) et rotation automatique ici, basée sur la TAILLE du
# fichier (Path.stat(), qui ne souffre pas du bug d'ouverture) plutôt que
# sur son ouverture -- la rotation peut donc se déclencher et réparer un
# fichier déjà trop gros sans jamais avoir besoin de l'ouvrir.
DEFAULT_MAX_LOG_SIZE_BYTES = 200 * 1024 * 1024  # 200 Mo, marge large sous le seuil ~2 Go


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if hasattr(obj, "value"):  # Enum
        return obj.value
    return str(obj)


def _tail_lines(path: Path, n: int) -> list[str]:
    """Lit les n dernières lignes d'un fichier texte sans le charger
    entièrement en mémoire (contrairement à `f.readlines()`) -- lu en mode
    binaire avec des seeks explicites, qui ne souffrent pas du bug
    d'ouverture en mode texte sur un gros fichier (voir plus haut)."""
    chunk_size = 1024 * 1024
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        data = b""
        while pos > 0 and data.count(b"\n") <= n:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            data = f.read(read_size) + data
    text = data.decode("utf-8", errors="replace")
    return text.splitlines(keepends=True)[-n:]


class DecisionLogger:
    def __init__(
        self,
        path: str | Path | None = None,
        config: dict | None = None,
        max_size_bytes: int = DEFAULT_MAX_LOG_SIZE_BYTES,
    ):
        cfg = config or load_config()
        self.path = Path(path or cfg["logging"]["decisions_log_path"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_size_bytes = max_size_bytes

    def _rotate_if_too_large(self) -> None:
        """Renomme le fichier courant (horodaté) et repart d'un fichier
        vierge s'il dépasse `max_size_bytes`. Utilise Path.stat(), qui lit
        la taille sans ouvrir le fichier -- répare donc un fichier déjà
        trop gros pour être ouvert en mode texte, sans jamais avoir besoin
        de l'ouvrir lui-même."""
        try:
            if not self.path.exists() or self.path.stat().st_size < self.max_size_bytes:
                return
            rotated = self.path.with_name(
                f"{self.path.stem}.{datetime.now(timezone.utc):%Y%m%dT%H%M%S}{self.path.suffix}"
            )
            self.path.rename(rotated)
        except OSError:
            pass  # une rotation ratée ne doit jamais empêcher d'essayer d'écrire le log

    def log(self, event_type: str, **fields: Any) -> None:
        self._rotate_if_too_large()
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            **fields,
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=_json_default, ensure_ascii=False) + "\n")

    def read_all(self, limit: int | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        if limit:
            lines = _tail_lines(self.path, limit)
        else:
            with open(self.path, encoding="utf-8") as f:
                lines = f.readlines()
        return [json.loads(line) for line in lines if line.strip()]


class StateStore:
    """Persistance légère de l'état des positions et du risk guard, pour
    pouvoir redémarrer le bot sans perdre le fil (positions ouvertes,
    plafond de perte journalière déjà atteint, etc.)."""

    def __init__(self, path: str | Path | None = None, config: dict | None = None):
        cfg = config or load_config()
        self.path = Path(path or cfg["logging"]["state_db_path"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                position_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS risk_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                halted INTEGER NOT NULL,
                halt_reason TEXT,
                daily_pnl_eur REAL NOT NULL,
                current_day TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def save_position(self, position_id: str, data: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO positions (position_id, data, updated_at) VALUES (?, ?, ?)",
            (position_id, json.dumps(data, default=_json_default), datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def load_open_positions(self) -> list[dict]:
        rows = self._conn.execute("SELECT data FROM positions").fetchall()
        return [json.loads(r[0]) for r in rows]

    def save_risk_state(self, halted: bool, halt_reason: str | None, daily_pnl_eur: float, current_day: str) -> None:
        self._conn.execute(
            """
            INSERT INTO risk_state (id, halted, halt_reason, daily_pnl_eur, current_day)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                halted=excluded.halted,
                halt_reason=excluded.halt_reason,
                daily_pnl_eur=excluded.daily_pnl_eur,
                current_day=excluded.current_day
            """,
            (int(halted), halt_reason, daily_pnl_eur, current_day),
        )
        self._conn.commit()

    def load_risk_state(self) -> dict | None:
        row = self._conn.execute(
            "SELECT halted, halt_reason, daily_pnl_eur, current_day FROM risk_state WHERE id = 1"
        ).fetchone()
        if not row:
            return None
        return {
            "halted": bool(row[0]),
            "halt_reason": row[1],
            "daily_pnl_eur": row[2],
            "current_day": row[3],
        }

    def close(self) -> None:
        self._conn.close()
