"""Normalizacion: JSON crudo de Polymarket -> `FlowTrade`. FUNCIONES PURAS.

Aqui no hay red (eso es `feed.py`): entra el JSON tal como lo devuelve la Data
API y salen trades normalizados. Separarlo permite testear con fixtures escritas
a mano la parte mas fragil de todo el paquete: los nombres de campo de una API
ajena que cambia sin avisar.

LAS DOS NORMALIZACIONES QUE HACEN QUE EL MOTOR TENGA SENTIDO:

  1. **Lado entrado, no verbo del libro.** Vender SI es posicionarse en NO. Sin
     esto, un vendedor de SI apareceria en la misma cadena que los compradores
     de SI y la cascada mediria lo contrario de lo que paso.

         BUY  Yes -> lado YES        SELL Yes -> lado NO
         BUY  No  -> lado NO         SELL No  -> lado YES

  2. **Precio del lado entrado.** El precio que publica la API es el del token
     de la fila. Si la fila es una venta, la probabilidad implicita del lado en
     el que la wallet queda es 1 - p. Con todo normalizado asi, "el precio subio
     despues de que entrara" significa lo mismo en los dos lados y las metricas
     se pueden agregar.

Regla de oro, la misma que en Wallet Intelligence: **lo que no llega, se
descarta**. Una fila sin wallet, sin timestamp o sin precio no entra con valores
por defecto -- se queda fuera. Un timestamp inventado no es un dato degradado:
es un dato FALSO, y aqui el timestamp ES la medida.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any

from medusa.intelligence.flow.types import FlowTrade

UTC = dt.timezone.utc


def _f(row: dict, *keys: str) -> float | None:
    for key in keys:
        val = row.get(key)
        if val is None or val == "":
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def _s(row: dict, *keys: str, default: str = "") -> str:
    for key in keys:
        val = row.get(key)
        if val not in (None, ""):
            return str(val)
    return default


def _ts(value: Any) -> dt.datetime | None:
    """Epoch (segundos o milisegundos) o ISO-8601 -> datetime UTC. None si no."""
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


def entered_side(outcome: str, side: str) -> str | None:
    """Lado en el que queda la wallet tras el trade. None si no se puede saber.

    Devolver None (en vez de suponer YES) es deliberado: una fila cuyo lado no
    se entiende iria a parar a una cadena equivocada, y una cadena con un
    participante mal colocado corrompe el rango de TODOS los demas.
    """
    out = (outcome or "").strip().upper()
    verb = (side or "").strip().upper()
    if out not in ("YES", "NO"):
        return None
    if verb in ("BUY", "B", "BID"):
        return out
    if verb in ("SELL", "S", "ASK"):
        return "NO" if out == "YES" else "YES"
    return None


def _uid(market_id: str, wallet: str, ts: dt.datetime, side: str,
         price: float, size: float, tx: str) -> str:
    """Huella estable de un trade, para deduplicar entre ingestas solapadas.

    Se prefiere el hash de la transaccion cuando viene, porque es lo unico que
    identifica el evento de verdad. Cuando no viene se compone con los campos
    observables: dos trades identicos de la misma wallet en el mismo segundo son
    indistinguibles desde fuera, y contarlos dos veces seria peor que fundirlos.
    """
    raw = tx or f"{market_id}|{wallet}|{ts.isoformat()}|{side}|{price:.6f}|{size:.6f}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:40]


def normalize_trades(market_id: str, rows: list[dict]) -> list[FlowTrade]:
    """Trades publicos de un mercado -> `FlowTrade`, ordenados por tiempo.

    Se descarta en silencio (no es un error: la API mezcla tipos de fila) todo
    lo que no sea un trade legible: sin wallet, sin timestamp, sin precio valido
    en [0,1], sin tamaño positivo o con un lado que no se entiende.
    """
    out: list[FlowTrade] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        wallet = _s(row, "proxyWallet", "wallet", "user", "maker", "owner").lower()
        stamp = _ts(row.get("timestamp") or row.get("ts") or row.get("time"))
        price = _f(row, "price", "avgPrice")
        size = _f(row, "size", "shares", "amount")
        side = entered_side(
            _s(row, "outcome", "outcomeName"), _s(row, "side", "type", "action")
        )
        if not wallet or stamp is None or price is None or size is None or side is None:
            continue
        if not (0.0 <= price <= 1.0) or size <= 0:
            continue
        # Probabilidad implicita DEL LADO ENTRADO (ver cabecera).
        entered_price = price if side == _s(row, "outcome", "outcomeName").upper() \
            else 1.0 - price
        cid = _s(row, "conditionId", "condition_id", "market", default=market_id)
        uid = _uid(cid, wallet, stamp, side, entered_price, size,
                   _s(row, "transactionHash", "txHash", "hash"))
        if uid in seen:
            continue
        seen.add(uid)
        out.append(FlowTrade(
            market_id=cid, wallet=wallet, side=side, price=entered_price,
            size=size, ts=stamp, uid=uid,
        ))
    out.sort(key=lambda t: (t.ts, t.wallet))
    return out


def resolution_of(meta: dict) -> float | None:
    """Precio final del YES de un mercado de Gamma, o None si sigue vivo.

    Un mercado cerrado pero sin `outcomePrices` legibles tambien devuelve None:
    "cerrado" no es "resuelto", y tratar un cierre sin resultado como una
    resolucion inventaria la verdad contra la que se puntua todo el motor.
    """
    if not isinstance(meta, dict):
        return None
    closed = bool(meta.get("closed")) or str(meta.get("umaResolutionStatus") or "") \
        .lower() == "resolved"
    if not closed:
        return None
    raw = meta.get("outcomePrices")
    if isinstance(raw, str):
        try:
            import json

            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    try:
        value = float(raw[0])
    except (TypeError, ValueError):
        return None
    return value if 0.0 <= value <= 1.0 else None
