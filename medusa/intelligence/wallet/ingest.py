"""Normalizacion: JSON crudo de Polymarket -> `WalletPosition`.

FUNCIONES PURAS. Aqui no hay red (eso es `feed.py`): entra JSON tal como lo
devuelve la API y sale la lista de posiciones normalizadas de la que se calcula
el ADN. Separarlo asi permite testear la parte fragil -- los nombres de campo de
una API ajena que cambia sin avisar -- con fixtures escritas a mano.

DE DONDE SALE CADA COSA:

    /positions  -> PnL REALIZADO, tamaño, precio medio, si esta cerrada.
                   Emparejar compras y ventas a mano para reconstruir el PnL es
                   justo donde se cuelan los errores de contabilidad.
    /activity   -> CUANDO entro y cuando salio. Sin esto no hay timings.
    Gamma       -> apertura, cierre, liquidez y spread del mercado.

Regla de oro: **lo que no llega, se deja en None**. Un timestamp inventado o un
spread a 0.0 por defecto no son "datos degradados": son datos FALSOS, y las
metricas los promediarian como si fuesen reales. `WalletPosition` y las metricas
estan escritas para excluir lo ausente, no para rellenarlo.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from medusa.intelligence.wallet.types import WalletPosition

UTC = dt.timezone.utc


def _f(row: dict, *keys: str, default: float = 0.0) -> float:
    """Primer campo presente de una lista de alias (la API cambia nombres)."""
    for key in keys:
        val = row.get(key)
        if val is None or val == "":
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return default


def _s(row: dict, *keys: str, default: str = "") -> str:
    for key in keys:
        val = row.get(key)
        if val not in (None, ""):
            return str(val)
    return default


def _ts(value: Any) -> dt.datetime | None:
    """Epoch (seg o ms) o ISO-8601 -> datetime UTC. None si no se puede leer."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:      # milisegundos
            seconds /= 1000.0
        try:
            return dt.datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit():
        return _ts(int(text))
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def market_context(meta: dict) -> dict:
    """Apertura, cierre, liquidez y spread de un mercado de Gamma."""
    return {
        "start": _ts(meta.get("startDate") or meta.get("createdAt")),
        "end": _ts(meta.get("endDate") or meta.get("endDateIso")),
        "liquidity": _f(meta, "liquidityNum", "liquidity"),
        "spread": _f(meta, "spread"),
        "category": _s(meta, "category"),
    }


def activity_windows(activity: list[dict]) -> dict[str, dict]:
    """Primer y ultimo movimiento de cada mercado: {condition_id: {first, last}}.

    Se usa como fuente de los timings. Solo cuentan los eventos de tipo TRADE:
    un `REDEEM` (cobrar el mercado ya resuelto) no es una decision de salida, y
    contarlo como tal empujaria el `exit_timing` de todo el mundo a 1.0.
    """
    out: dict[str, dict] = {}
    for row in activity:
        kind = _s(row, "type", "activityType").upper()
        if kind and kind not in ("TRADE", "BUY", "SELL"):
            continue
        cid = _s(row, "conditionId", "condition_id", "market")
        stamp = _ts(row.get("timestamp") or row.get("ts") or row.get("time"))
        if not cid or stamp is None:
            continue
        window = out.setdefault(cid, {"first": stamp, "last": stamp, "n": 0})
        window["first"] = min(window["first"], stamp)
        window["last"] = max(window["last"], stamp)
        window["n"] += 1
    return out


def _is_closed(row: dict, size: float) -> bool:
    """¿Esta cerrada la posicion?

    Cerrada = ya no queda tamaño vivo, o el mercado resolvio y solo falta
    cobrar. Contar una posicion viva como cerrada es el error clasico que infla
    un track record con ganancias que aun pueden darse la vuelta.
    """
    if size > 1e-9:
        return False
    return True


def normalize_positions(
    wallet: str, positions: list[dict], *, activity: list[dict] | None = None,
    market_meta: dict[str, dict] | None = None,
) -> list[WalletPosition]:
    """Construye las `WalletPosition` de una wallet a partir de las tres fuentes."""
    windows = activity_windows(activity or [])
    meta = market_meta or {}
    out: list[WalletPosition] = []

    for row in positions:
        cid = _s(row, "conditionId", "condition_id", "market")
        if not cid:
            continue
        size = _f(row, "size", "currentSize")
        entry = _f(row, "avgPrice", "averagePrice")
        # Coste comprometido. `initialValue` es el dato directo; si no viene se
        # reconstruye desde el total comprado, y solo como ultimo recurso desde
        # el tamaño vivo (que subestima si ya vendio parte).
        cost = _f(row, "initialValue")
        if cost <= 0:
            bought = _f(row, "totalBought")
            cost = bought * entry if bought > 0 else size * entry
        pnl = _f(row, "realizedPnl", "cashPnl")
        roi = _f(row, "percentRealizedPnl", "percentPnl") / 100.0
        if roi == 0.0 and cost > 0 and pnl != 0.0:
            roi = pnl / cost

        ctx = market_context(meta.get(cid, {}))
        window = windows.get(cid, {})
        closed = _is_closed(row, size)
        out.append(WalletPosition(
            wallet=wallet,
            market_id=cid,
            category=ctx.get("category") or _s(row, "category"),
            outcome=_s(row, "outcome", default="YES").upper(),
            size=_f(row, "totalBought", default=size) or size,
            entry_price=entry,
            exit_price=_f(row, "curPrice"),
            cost=cost,
            pnl=pnl,
            roi=roi,
            opened_at=window.get("first"),
            # Solo hay fecha de cierre si de verdad esta cerrada. Poner el
            # ultimo movimiento en una posicion viva la haria parecer cerrada al
            # resto del sistema.
            closed_at=window.get("last") if closed else None,
            closed=closed,
            won=(pnl > 0) if closed else None,
            market_start=ctx.get("start"),
            market_end=ctx.get("end"),
            liquidity=ctx.get("liquidity", 0.0),
            spread=ctx.get("spread", 0.0),
        ))
    return out


def extract_wallets(holders: list[dict], min_size: float = 0.0) -> list[str]:
    """Direcciones de una respuesta de /holders, deduplicadas y ordenadas.

    El orden es alfabetico y no por tamaño a proposito: quien entra primero en
    la muestra no puede depender de cuanto dinero mueve, o la poblacion contra
    la que se estandariza el ADN quedaria sesgada hacia las wallets grandes.
    """
    found: set[str] = set()
    for row in holders:
        addr = _s(row, "proxyWallet", "wallet", "user", "address", "owner")
        if not addr:
            continue
        if min_size > 0 and _f(row, "amount", "size", "shares") < min_size:
            continue
        found.add(addr.lower())
    return sorted(found)
