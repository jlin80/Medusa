"""Risk manager del micro-trader 'Up or Down'.

Los dos candados del modelo (certeza + EV tras el peaje) deciden SI una apuesta
tiene edge. Este modulo decide algo distinto: CUANTO arriesgar y CUANDO parar.
Es el cortafuegos de un bot de alta frecuencia binario, donde el peligro no es
una apuesta mala sino la acumulacion — una mala racha, el sobre-apalancamiento o
seguir apostando con la cuenta a la baja.

Controles (todos configurables, ver config UPDOWN_*):
  - Sizing por bankroll: apuesta = fraccion del balance, acotada a [min, max].
  - Suelo de balance: no operar por debajo de un minimo.
  - Maximo de apuestas abiertas a la vez.
  - Tope de exposicion simultanea (% del balance).
  - Stop de perdida diaria (PnL updown de hoy vs balance de apertura del dia).
  - Tope de apuestas por dia.
  - Cooldown anti-racha: tras N perdidas seguidas, pausa las entradas.

Ademas del kill-switch de cuenta (perdida diaria de TODO el portfolio), que el
loop ya respeta aparte. Funcion PURA: recibe una foto (snapshot) del estado y
devuelve un veredicto; asi se prueba entera con numeros verificables.
"""

from __future__ import annotations

from dataclasses import dataclass

from medusa.config import get_settings


@dataclass(frozen=True)
class UpDownRiskVerdict:
    allow: bool
    stake: float          # importe a arriesgar si allow=True (0 si no)
    reason: str


class UpDownRiskManager:
    def __init__(self, log) -> None:
        self.log = log
        self.s = get_settings()

    def _base_stake(self, balance: float) -> float:
        """Apuesta base: fraccion del balance (si updown_stake_fraction>0) o el
        fijo updown_stake_usd, acotada a [min_stake, max_stake]."""
        s = self.s
        base = balance * s.updown_stake_fraction if s.updown_stake_fraction > 0 else s.updown_stake_usd
        return min(s.updown_max_stake, max(s.updown_min_stake, base))

    def assess(self, snap: dict, now: float) -> UpDownRiskVerdict:
        """Decide si se permite una apuesta nueva y con cuanto, dada la foto del
        estado (balance, exposicion, apuestas del dia, racha...).

        `snap` (de repo.updown_risk_data):
          balance, day_start_equity, open_count, open_exposure, today_bets,
          today_pnl, consecutive_losses, last_loss_epoch.
        """
        s = self.s
        balance = float(snap.get("balance", 0.0))

        def no(reason: str) -> UpDownRiskVerdict:
            return UpDownRiskVerdict(False, 0.0, reason)

        # 1) Suelo de balance: sin caja, no se juega.
        if balance < s.updown_min_balance:
            return no(f"balance ${balance:.0f} < mínimo ${s.updown_min_balance:.0f}")

        # 2) Numero de apuestas abiertas.
        open_count = int(snap.get("open_count", 0))
        if open_count >= s.updown_max_open:
            return no(f"{open_count} apuestas abiertas (máx {s.updown_max_open})")

        # 3) Stop de perdida diaria (PnL updown de hoy vs balance de apertura).
        day_start = float(snap.get("day_start_equity") or balance)
        today_pnl = float(snap.get("today_pnl", 0.0))
        if day_start > 0 and today_pnl <= -s.updown_max_daily_loss_pct * day_start:
            return no(
                f"pérdida del día ${today_pnl:.2f} supera "
                f"{s.updown_max_daily_loss_pct:.0%} del balance de apertura"
            )

        # 4) Tope de apuestas por dia.
        today_bets = int(snap.get("today_bets", 0))
        if today_bets >= s.updown_max_daily_bets:
            return no(f"límite de {s.updown_max_daily_bets} apuestas/día alcanzado")

        # 5) Cooldown anti-racha.
        consec = int(snap.get("consecutive_losses", 0))
        last_loss = float(snap.get("last_loss_epoch", 0.0))
        if consec >= s.updown_max_consecutive_losses and last_loss > 0:
            elapsed = now - last_loss
            if elapsed < s.updown_cooldown_secs:
                left = s.updown_cooldown_secs - elapsed
                return no(
                    f"cooldown anti-racha: {consec} pérdidas seguidas, "
                    f"faltan {left:.0f}s"
                )

        # 6) Exposicion simultanea: cuanto queda del presupuesto de riesgo.
        open_exposure = float(snap.get("open_exposure", 0.0))
        budget = s.updown_max_exposure_pct * balance - open_exposure
        if budget < s.updown_min_stake:
            return no(
                f"exposición al máximo (${open_exposure:.0f} de "
                f"{s.updown_max_exposure_pct:.0%} del balance)"
            )

        # 7) Sizing: la apuesta base, recortada por el presupuesto y la caja.
        stake = min(self._base_stake(balance), budget, balance)
        if stake < s.updown_min_stake:
            return no("stake por debajo del mínimo tras aplicar los límites")

        return UpDownRiskVerdict(True, round(stake, 2), "ok")
