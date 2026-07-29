"""Deteccion de cascadas y de eventos de propagacion. FUNCIONES PURAS.

Entra una lista de `FlowTrade` (ya normalizados por `ingest.py`) y salen
cascadas y eslabones. Sin BD, sin red, sin reloj: todo lo que decide algo aqui
se puede reproducir con una lista escrita a mano, que es lo que hacen los tests.

LA REGLA QUE ATRAVIESA EL FICHERO: esto mide ORDEN TEMPORAL. La palabra
"cascada" es un nombre para "entradas encadenadas por huecos cortos", no una
afirmacion de contagio. En cuanto una funcion de aqui empiece a decir "A causo
B", el motor habra dejado de medir y habra empezado a inventar.

Las tres decisiones de diseño que mas afectan al resultado, explicitas:

  1. **Solo primeras entradas.** Reforzar una posicion no es informacion nueva
     llegando a esa wallet. Contarlo haria que una wallet activa apareciera
     como seguidora de si misma en cada cascada.
  2. **Sesionizacion por huecos.** Se corta la cascada cuando pasan mas de
     `window_seconds` sin que entre nadie. Es la misma tecnica con la que se
     parten sesiones de navegacion: no impone una duracion fija (que inventaria
     finales) y se adapta a mercados lentos y rapidos.
  3. **Eslabones limitados a `max_hops`.** Emparejar a todos con todos daria
     O(n^2) eventos, y el par que va 40 entradas por detras no describe
     propagacion: describe que los dos estaban en el mismo mercado.
"""

from __future__ import annotations

import datetime as dt

from medusa.intelligence.flow import metrics
from medusa.intelligence.flow.types import Cascade, Entry, FlowTrade, PropagationEvent


def first_entries(trades: list[FlowTrade]) -> dict[tuple[str, str], list[Entry]]:
    """Primera entrada de cada wallet por (mercado, lado), ordenada por tiempo.

    Si la misma wallet aparece varias veces se queda la MAS ANTIGUA. El tamaño
    acumulado no se suma: la entrada describe un instante, y sumarle trades
    posteriores mezclaria dos momentos distintos en una sola observacion.
    """
    seen: dict[tuple[str, str], dict[str, Entry]] = {}
    for t in trades:
        if not t.wallet or not t.market_id or t.side not in ("YES", "NO"):
            continue
        bucket = seen.setdefault((t.market_id, t.side), {})
        prev = bucket.get(t.wallet)
        if prev is None or t.ts < prev.ts:
            bucket[t.wallet] = Entry(
                wallet=t.wallet, ts=t.ts, price=float(t.price), size=float(t.size)
            )
    return {
        key: sorted(entries.values(), key=lambda e: (e.ts, e.wallet))
        for key, entries in seen.items()
    }


def _split_by_gap(entries: list[Entry], window_seconds: float) -> list[list[Entry]]:
    """Parte una serie de entradas alli donde el hueco supera la ventana."""
    if not entries:
        return []
    runs: list[list[Entry]] = [[entries[0]]]
    for prev, cur in zip(entries, entries[1:]):
        if (cur.ts - prev.ts).total_seconds() > window_seconds:
            runs.append([cur])
        else:
            runs[-1].append(cur)
    return runs


def _annotate(cascade: Cascade, consensus_fraction: float) -> Cascade:
    """Rellena `propagation_time` y `consensus_delay` de una cascada.

    - propagation_time: MEDIANA de los saltos entre entradas consecutivas. Es
      "cada cuanto entra el siguiente", y va en mediana porque un solo salto
      largo dentro de la ventana arrastraria la media.
    - consensus_delay: segundos desde la primera entrada hasta que ya ha entrado
      la fraccion de consenso (0.5 por defecto). Es la respuesta a "cuanto
      tarda media cascada en estar dentro".
    """
    start = cascade.started_at
    offsets = [(e.ts - start).total_seconds() for e in cascade.entries]
    cascade.propagation_time = metrics.median(metrics.gaps(offsets))
    cascade.consensus_delay = metrics.quantile_time(offsets, consensus_fraction)
    return cascade


def detect_cascades(
    trades: list[FlowTrade], *, window_seconds: float = 3600.0,
    min_participants: int = 3, consensus_fraction: float = 0.5,
) -> list[Cascade]:
    """Cascadas de un conjunto de trades, ordenadas por instante de inicio.

    `min_participants` es el filtro de ruido mas importante del motor: con 2
    participantes cualquier par de entradas seguidas seria una "cascada" y la
    tabla se llenaria de coincidencias. Tres es el minimo con el que la palabra
    cadena significa algo (hay un medio).
    """
    out: list[Cascade] = []
    for (market_id, side), entries in first_entries(trades).items():
        for run in _split_by_gap(entries, window_seconds):
            if len(run) < max(2, int(min_participants)):
                continue
            out.append(_annotate(
                Cascade(market_id=market_id, side=side, entries=run),
                consensus_fraction,
            ))
    out.sort(key=lambda c: (c.started_at, c.market_id, c.side))
    return out


def propagation_events(
    cascade: Cascade, *, max_hops: int = 3, max_lag_seconds: float | None = None,
) -> list[PropagationEvent]:
    """Eslabones (leader -> follower) de una cascada.

    Un eslabon por cada par ordenado a distancia <= `max_hops` en la cadena.
    `leader` y `follower` son etiquetas de ORDEN: leader entro antes, nada mas.
    """
    events: list[PropagationEvent] = []
    entries = cascade.entries
    hops = max(1, int(max_hops))
    for i, lead in enumerate(entries):
        for j in range(i + 1, min(i + hops + 1, len(entries))):
            follow = entries[j]
            lag = (follow.ts - lead.ts).total_seconds()
            if max_lag_seconds is not None and lag > max_lag_seconds:
                break   # los siguientes solo pueden estar mas lejos
            if lead.wallet == follow.wallet:
                continue   # no deberia pasar (primeras entradas), pero nunca
                           # se emite un eslabon de una wallet consigo misma
            events.append(PropagationEvent(
                market_id=cascade.market_id, side=cascade.side,
                cascade_key=cascade.key, leader=lead.wallet, follower=follow.wallet,
                hop=j - i, lag_seconds=lag,
                price_leader=float(lead.price), price_follower=float(follow.price),
                ts=follow.ts,
            ))
    return events


def all_propagation_events(
    cascades: list[Cascade], *, max_hops: int = 3,
    max_lag_seconds: float | None = None,
) -> list[PropagationEvent]:
    """Todos los eslabones de una lista de cascadas (se persisten TODOS)."""
    out: list[PropagationEvent] = []
    for c in cascades:
        out.extend(propagation_events(
            c, max_hops=max_hops, max_lag_seconds=max_lag_seconds
        ))
    return out


def annotate_resolutions(
    cascades: list[Cascade], resolutions: dict[str, float | None],
) -> list[Cascade]:
    """Marca cada cascada con el resultado de su mercado, si lo hay.

    `resolutions` es {market_id: precio final del YES en [0,1]} o None si el
    mercado sigue vivo. Para una cascada del lado NO el valor se invierte.

    Lo que NO se hace: rellenar los mercados sin resolucion con 0.5 ni con el
    ultimo precio. Un mercado vivo no tiene resultado, y fabricarlo meteria
    ruido con apariencia de dato en el `early_information_score` de todo el
    mundo. Sin resolucion, la cascada simplemente no puntua por resolucion.
    """
    for c in cascades:
        final = resolutions.get(c.market_id)
        if final is None:
            c.resolved = False
            c.resolution_value = None
            continue
        value = float(final)
        c.resolved = True
        c.resolution_value = value if c.side == "YES" else 1.0 - value
    return cascades


def cascade_window(cascades: list[Cascade]) -> tuple[dt.datetime | None, dt.datetime | None]:
    """Primer y ultimo instante cubiertos por las cascadas (para telemetria)."""
    if not cascades:
        return None, None
    return (min(c.started_at for c in cascades), max(c.ended_at for c in cascades))
