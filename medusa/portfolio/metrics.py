"""Metricas de cartera y reporte de validacion del gate Paper -> Live.

Todas las metricas salen de operaciones CERRADAS y de flujos de caja reales
simulados; nada se estima con precios ideales.
"""

from __future__ import annotations

import datetime as dt

from medusa.config import get_settings
from medusa.data import repositories as repo


def _parse_ts(value) -> dt.datetime | None:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _max_drawdown(equity_points: list[float]) -> float:
    """Maximo drawdown relativo (0..1) sobre la curva de equity."""
    peak = float("-inf")
    max_dd = 0.0
    for value in equity_points:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    return max_dd


async def compute_metrics(mode: str) -> dict:
    """Snapshot completo de rendimiento para el modo indicado."""
    s = get_settings()
    state = await repo.get_bot_state()
    trades = await repo.list_trades(limit=10_000, mode=mode)
    positions = await repo.get_open_positions(mode)

    balance = float(state["balance"]) if state else s.paper_starting_balance
    starting = float(state["starting_balance"]) if state and state.get("starting_balance") \
        else s.paper_starting_balance

    exposure = sum(p["cost_basis"] for p in positions)
    unrealized = sum(p["unrealized_pnl"] for p in positions)
    equity = balance + exposure + unrealized

    realized = sum(t["pnl"] for t in trades)
    wins = [t for t in trades if t["won"]]
    losses = [t for t in trades if not t["won"]]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    curve = await repo.equity_curve(limit=5_000, mode=mode)
    equity_points = [c["equity"] for c in curve] or [equity]

    return {
        "mode": mode,
        "balance": round(balance, 2),
        "equity": round(equity, 2),
        "starting_balance": round(starting, 2),
        "exposure": round(exposure, 2),
        "pnl_realized": round(realized, 2),
        "pnl_unrealized": round(unrealized, 2),
        "pnl_total": round(equity - starting, 2),
        "roi": round((equity - starting) / starting, 4) if starting else 0.0,
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "open_positions": len(positions),
        # Mercados distintos tocados (cerrados + abiertos): pedido en el reporte
        # de validacion original.
        "markets_traded": len({t["market_id"] for t in trades}
                              | {p["market_id"] for p in positions}),
        "avg_edge": round(sum(t["edge"] for t in trades) / len(trades), 4) if trades else 0.0,
        "total_costs": round(sum(t["cost"] for t in trades), 4),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "max_drawdown": round(_max_drawdown(equity_points), 4),
    }


async def validation_report() -> dict:
    """Reporte del gate obligatorio de paper trading (7-14 dias).

    Devuelve las metricas del periodo y si se cumplen las condiciones para
    desbloquear Live. El gate NO desbloquea nada por si mismo: solo informa. El
    desbloqueo real es una accion manual y explicita del usuario.
    """
    s = get_settings()
    state = await repo.get_bot_state()
    metrics = await compute_metrics("paper")

    start = _parse_ts(state.get("paper_start_at")) if state else None
    days = ((dt.datetime.now(dt.timezone.utc) - start).total_seconds() / 86400.0) if start else 0.0

    checks = {
        "dias_minimos": {
            "required": s.paper_validation_min_days,
            "actual": round(days, 2),
            "pass": days >= s.paper_validation_min_days,
        },
        "operaciones_minimas": {
            "required": s.paper_validation_min_trades,
            "actual": metrics["trades"],
            "pass": metrics["trades"] >= s.paper_validation_min_trades,
        },
        "roi_positivo": {
            "required": "> 0",
            "actual": metrics["roi"],
            "pass": metrics["roi"] > 0,
        },
    }
    eligible = all(c["pass"] for c in checks.values())

    return {
        "paper_start_at": state.get("paper_start_at") if state else None,
        "days_running": round(days, 2),
        "window": f"{s.paper_validation_min_days}-{s.paper_validation_max_days} dias",
        "checks": checks,
        "eligible_for_live": eligible,
        "live_unlocked": bool(state.get("live_unlocked")) if state else False,
        "metrics": metrics,
    }
