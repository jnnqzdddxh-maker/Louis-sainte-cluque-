"""Garde-fous globaux (section 4) : stop catastrophe par position vit dans
position_manager.py. Ici : plafond de perte journalière, nb max de positions
simultanées, et le kill-switch qui exige une validation manuelle explicite.

Ce module ne fait aucun appel réseau et n'exécute aucun trade : il ne fait
que répondre à "le bot a-t-il le droit d'agir maintenant ?".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from core.config import load_config


class ManualResetRequiredError(RuntimeError):
    pass


@dataclass
class HaltEvent:
    reason: str
    triggered_at: datetime
    daily_pnl_eur: float


@dataclass
class RiskGuard:
    config: dict = field(default_factory=load_config)
    capital_eur: float | None = None

    halted: bool = field(init=False, default=False)
    halt_reason: str | None = field(init=False, default=None)
    daily_pnl_eur: float = field(init=False, default=0.0)
    _current_day: date = field(init=False, default_factory=lambda: datetime.now(timezone.utc).date())
    history: list[HaltEvent] = field(init=False, default_factory=list)

    def set_capital(self, capital_eur: float) -> None:
        self.capital_eur = capital_eur

    def _roll_day_if_needed(self, now: datetime) -> None:
        today = now.date()
        if today != self._current_day:
            self._current_day = today
            self.daily_pnl_eur = 0.0
            # Le halt NE se réinitialise PAS automatiquement au changement de
            # jour : require_manual_reset_after_halt impose une intervention
            # humaine explicite, quelle que soit l'heure.

    def record_pnl(self, delta_eur: float, now: datetime | None = None) -> None:
        """À appeler après chaque vente/clôture (partielle ou totale)."""
        now = now or datetime.now(timezone.utc)
        self._roll_day_if_needed(now)
        self.daily_pnl_eur += delta_eur
        self._check_daily_cap(now)

    def _check_daily_cap(self, now: datetime) -> None:
        risk = self.config["risk"]
        cap_eur = risk["daily_loss_cap_eur"]
        if self.daily_pnl_eur <= -abs(cap_eur):
            self._halt(
                f"perte journalière {-self.daily_pnl_eur:.2f}€ a atteint le "
                f"plafond de {cap_eur}€",
                now,
            )
            return

        cap_pct = risk.get("daily_loss_cap_pct_of_capital")
        if cap_pct and self.capital_eur:
            limit_eur = -abs(cap_pct / 100 * self.capital_eur)
            if self.daily_pnl_eur <= limit_eur:
                self._halt(
                    f"perte journalière {-self.daily_pnl_eur:.2f}€ a atteint "
                    f"{cap_pct}% du capital ({self.capital_eur}€)",
                    now,
                )

    def _halt(self, reason: str, now: datetime) -> None:
        if self.halted:
            return
        self.halted = True
        self.halt_reason = reason
        self.history.append(HaltEvent(reason=reason, triggered_at=now, daily_pnl_eur=self.daily_pnl_eur))

    def halt_manual(self, reason: str, now: datetime | None = None) -> None:
        """Arrêt d'urgence déclenché par un opérateur ou une erreur système critique."""
        self._halt(reason, now or datetime.now(timezone.utc))

    def can_open_position(self, open_positions_count: int) -> tuple[bool, str | None]:
        if self.halted:
            return False, self.halt_reason
        max_positions = self.config["risk"]["max_open_positions"]
        if open_positions_count >= max_positions:
            return False, f"nombre max de positions ouvertes atteint ({max_positions})"
        return True, None

    def manual_reset(self, confirmed_by: str) -> None:
        """Seule façon de relever le kill-switch. `confirmed_by` doit
        identifier explicitement l'opérateur humain qui valide le reset —
        jamais appelé automatiquement par le bot lui-même.
        """
        if not confirmed_by or not confirmed_by.strip():
            raise ManualResetRequiredError(
                "manual_reset() exige un identifiant d'opérateur non vide "
                "(ex: adresse email) — aucun reset automatique autorisé."
            )
        self.halted = False
        self.halt_reason = None
