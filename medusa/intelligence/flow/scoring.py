"""De cascadas y eslabones a METRICAS. Funciones PURAS.

Aqui se calculan los siete numeros que el motor promete y nada mas:

    information_speed        cuanto tarda una wallet en entrar desde que arranca
                             la cascada (mediana, segundos). Menos es antes.
    speed_score              lo mismo en forma acotada [0,1] con vida media, para
                             poder comparar y promediar entre mercados de escalas
                             distintas.
    leadership_score         media de (1 - rango normalizado) en sus cascadas.
                             1.0 = siempre abre; 0.5 = lo que se espera por puro
                             azar bajo intercambiabilidad; 0.0 = siempre cierra.
    follow_score             el complementario exacto (media del rango). Se
                             publica aparte a proposito: es la metrica que se
                             mira cuando la pregunta es "quien llega tarde".
    consensus_delay          segundos hasta que ha entrado la fraccion de
                             consenso de una cascada (metrica de MERCADO).
    propagation_time         mediana del salto entre entradas consecutivas.
                             Por wallet: cuanto tarda en entrar el siguiente
                             DESPUES de ella. Por mercado: el ritmo de la cadena.
    early/late_information_score
                             de sus entradas tempranas (o tardias), en que
                             fraccion el lado acabo teniendo razon.

NO CAUSALIDAD, otra vez y en el sitio donde mas tentacion hay. Un
`leadership_score` alto significa "entra pronto en las cadenas en las que
participa". No significa que los demas la sigan, ni que mueva el precio, ni que
tenga informacion privada. Las tres cosas serian afirmaciones causales y este
fichero no tiene con que sostenerlas: no hay contrafactual, no hay asignacion
aleatoria y no hay control de confusores (un anuncio publico hace entrar a
veinte wallets a la vez sin que ninguna sepa de las otras).

El unico contraste que si es legitimo, y por eso se publica, es contra el AZAR:
si el orden de entrada fuese intercambiable, el rango normalizado esperado de
cualquier participante seria 0.5. `edge_vs_chance` y `leadership_lower` dicen
cuanto se separa de ese nulo y con cuanta muestra.
"""

from __future__ import annotations

from collections import defaultdict

from medusa.intelligence.flow import metrics
from medusa.intelligence.flow.types import (
    Cascade,
    MarketFlowMetrics,
    PropagationEvent,
    WalletFlowMetrics,
)


def _outcome(cascade: Cascade, entry_price: float, min_price_move: float) -> float | None:
    """¿Acabo teniendo razon el lado de esta cascada, visto desde una entrada?

    Jerarquia de evidencia, de mas fuerte a mas debil:

      1. **Resolucion del mercado.** Es la verdad. 1.0 si el lado gano.
      2. **Movimiento del precio DESPUES de la entrada**, dentro de la cascada.
         Es un sustituto y se dice que lo es: el mercado puede darse la vuelta
         despues. Solo cuenta si el movimiento supera `min_price_move`.
      3. **Nada.** Si el mercado sigue vivo y el precio apenas se movio, esta
         observacion NO puntua y devuelve None.

    El caso 3 es el importante. Contar un empate como acierto (o como fallo)
    inflaria la muestra con observaciones vacias, que es la forma mas silenciosa
    de fabricar significancia estadistica.
    """
    if cascade.resolved and cascade.resolution_value is not None:
        return 1.0 if cascade.resolution_value >= 0.5 else 0.0
    forward = cascade.price_end - float(entry_price)
    if abs(forward) < float(min_price_move):
        return None
    return 1.0 if forward > 0 else 0.0


def wallet_metrics(
    cascades: list[Cascade], *, min_samples: int = 10, speed_half_life: float = 300.0,
    early_fraction: float = 0.33, late_fraction: float = 0.33,
    min_price_move: float = 0.02,
) -> list[WalletFlowMetrics]:
    """Metricas por wallet, ordenadas por liderazgo (y por muestra a igualdad).

    Las cascadas de un solo participante no llegan aqui (el detector las
    descarta): un rango normalizado sobre n=1 no existe.
    """
    ranks: dict[str, list[float]] = defaultdict(list)
    start_lags: dict[str, list[float]] = defaultdict(list)
    next_lags: dict[str, list[float]] = defaultdict(list)
    markets: dict[str, set[str]] = defaultdict(set)
    early: dict[str, list[float]] = defaultdict(list)
    late: dict[str, list[float]] = defaultdict(list)

    for c in cascades:
        if c.n < 2:
            continue
        start = c.started_at
        for i, entry in enumerate(c.entries):
            w = entry.wallet
            rank = c.rank(i)
            ranks[w].append(rank)
            start_lags[w].append((entry.ts - start).total_seconds())
            markets[w].add(c.market_id)
            if i + 1 < c.n:
                # Cuanto tarda en entrar el SIGUIENTE despues de esta wallet.
                # Coincidencia temporal medida; no se le atribuye la entrada.
                next_lags[w].append((c.entries[i + 1].ts - entry.ts).total_seconds())
            outcome = _outcome(c, entry.price, min_price_move)
            if outcome is None:
                continue
            if rank <= early_fraction:
                early[w].append(outcome)
            if rank >= 1.0 - late_fraction:
                late[w].append(outcome)

    out: list[WalletFlowMetrics] = []
    for wallet, rank_list in ranks.items():
        lead_values = [1.0 - r for r in rank_list]
        leadership = metrics.mean(lead_values)
        early_hits, late_hits = early[wallet], late[wallet]
        early_rate = metrics.mean(early_hits) if early_hits else 0.0
        late_rate = metrics.mean(late_hits) if late_hits else 0.0
        out.append(WalletFlowMetrics(
            wallet=wallet,
            n_cascades=len(rank_list),
            n_markets=len(markets[wallet]),
            leadership_score=leadership,
            follow_score=1.0 - leadership,
            leadership_lower=metrics.mean_lower(lead_values),
            edge_vs_chance=leadership - 0.5,
            information_speed=metrics.median(start_lags[wallet]),
            speed_score=metrics.mean(
                [metrics.decay_score(s, speed_half_life) for s in start_lags[wallet]]
            ),
            propagation_time=metrics.median(next_lags[wallet]),
            early_information_score=early_rate,
            late_information_score=late_rate,
            n_early=len(early_hits),
            n_late=len(late_hits),
            early_lower=metrics.wilson_lower(sum(early_hits), len(early_hits)),
            late_lower=metrics.wilson_lower(sum(late_hits), len(late_hits)),
            # Descriptivo: cuanta de su acierto se concentra en las entradas
            # tempranas. Con poca muestra en cualquiera de los dos lados es
            # ruido, y por eso `enough_samples` viaja pegado.
            information_edge=early_rate - late_rate,
            enough_samples=len(rank_list) >= int(min_samples),
        ))
    out.sort(key=lambda m: (m.leadership_lower, m.n_cascades), reverse=True)
    return out


def market_metrics(
    cascades: list[Cascade], events: list[PropagationEvent], *, min_samples: int = 3,
) -> list[MarketFlowMetrics]:
    """Metricas por mercado: como se propaga la informacion dentro de el."""
    by_market: dict[str, list[Cascade]] = defaultdict(list)
    for c in cascades:
        by_market[c.market_id].append(c)
    event_counts: dict[str, int] = defaultdict(int)
    for e in events:
        event_counts[e.market_id] += 1

    out: list[MarketFlowMetrics] = []
    for market_id, group in by_market.items():
        wallets = {e.wallet for c in group for e in c.entries}
        # Participantes por hora dentro de las cascadas. Las de span 0 (todo el
        # mundo dentro del mismo segundo) se excluyen en vez de dividir por cero
        # o de inventar un span minimo: no aportan ritmo, aportan simultaneidad.
        rates = [c.n / (c.span_seconds / 3600.0) for c in group if c.span_seconds > 0]
        out.append(MarketFlowMetrics(
            market_id=market_id,
            n_cascades=len(group),
            n_wallets=len(wallets),
            n_events=event_counts.get(market_id, 0),
            consensus_delay=metrics.median([c.consensus_delay for c in group]),
            propagation_time=metrics.median([c.propagation_time for c in group]),
            information_speed=metrics.median(rates),
            avg_cascade_size=metrics.mean([float(c.n) for c in group]),
            price_move=metrics.mean([c.price_move for c in group]),
            enough_samples=len(group) >= int(min_samples),
        ))
    out.sort(key=lambda m: (m.n_cascades, m.n_wallets), reverse=True)
    return out


def pair_stats(events: list[PropagationEvent], *, min_observations: int = 5) -> list[dict]:
    """Pares (leader, follower) que se repiten, con su latencia tipica.

    ESTO NO ES UNA RELACION DE INFLUENCIA. Es un par que ha coincidido en ese
    orden `n` veces. Se publica con `n` delante justamente para que nadie lea
    tres coincidencias como un patron: en un mercado con doscientas wallets
    activas, muchos pares coinciden varias veces sin que medie nada.
    """
    buckets: dict[tuple[str, str], list[PropagationEvent]] = defaultdict(list)
    for e in events:
        buckets[(e.leader, e.follower)].append(e)
    out: list[dict] = []
    for (leader, follower), group in buckets.items():
        if len(group) < int(min_observations):
            continue
        out.append({
            "leader": leader, "follower": follower, "n": len(group),
            "median_lag": round(metrics.median([e.lag_seconds for e in group]), 3),
            "median_hop": round(metrics.median([float(e.hop) for e in group]), 2),
            "markets": len({e.market_id for e in group}),
            "median_price_move": round(metrics.median([e.price_move for e in group]), 6),
        })
    out.sort(key=lambda r: (r["n"], -r["median_lag"]), reverse=True)
    return out


def flow_summary(
    cascades: list[Cascade], events: list[PropagationEvent],
) -> dict:
    """Resumen global de una pasada (lo que se guarda en el snapshot)."""
    wallets = {e.wallet for c in cascades for e in c.entries}
    resolved = [c for c in cascades if c.resolved]
    return {
        "cascades": len(cascades),
        "events": len(events),
        "wallets": len(wallets),
        "markets": len({c.market_id for c in cascades}),
        "resolved_cascades": len(resolved),
        "median_cascade_size": round(metrics.median([float(c.n) for c in cascades]), 3),
        "median_propagation_time": round(
            metrics.median([c.propagation_time for c in cascades]), 3),
        "median_consensus_delay": round(
            metrics.median([c.consensus_delay for c in cascades]), 3),
        "median_lag": round(metrics.median([e.lag_seconds for e in events]), 3),
    }
