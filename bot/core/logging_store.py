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


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if hasattr(obj, "value"):  # Enum
        return obj.value
    return str(obj)


class DecisionLogger:
    def __init__(self, path: str | Path | None = None, config: dict | None = None):
        cfg = config or load_config()
        self.path = Path(path or cfg["logging"]["decisions_log_path"])
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event_type: str, **fields: Any) -> None:
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
        with open(self.path, encoding="utf-8") as f:
            lines = f.readlines()
        if limit:
            lines = lines[-limit:]
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
