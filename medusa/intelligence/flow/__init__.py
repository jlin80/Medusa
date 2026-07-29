"""INFORMATION FLOW ENGINE (IFE) — V1, PostgreSQL.

Que mide: COMO SE PROPAGA LA INFORMACION dentro de un mercado de Polymarket.

    la wallet A entra
        v
    la wallet B entra
        v
    la wallet C entra
        v
    el precio se mueve
        v
    el mercado resuelve

De esa cadena el motor extrae siete medidas, todas con su tamaño de muestra:

    information speed        cuanto tarda una wallet en entrar desde que arranca
                             la cadena
    leadership score         con cuanta frecuencia entra en la cabeza
    follow score             con cuanta frecuencia entra en la cola
    consensus delay          cuanto tarda media cadena en estar dentro
    propagation time         cada cuanto entra el siguiente
    early information score  cuando entra pronto, ¿acaba teniendo razon el lado?
    late information score   lo mismo cuando entra tarde

QUE NO ES, y es la parte importante:

  - NO ES UN MOTOR DE CAUSALIDAD. Que B entre despues de A no significa que B
    siga a A, ni que A moviera el precio, ni que ninguno tenga informacion
    privada. No hay contrafactual, no hay asignacion aleatoria y no hay control
    de confusores (un anuncio publico hace entrar a veinte wallets a la vez sin
    que ninguna sepa de las otras). El unico contraste legitimo, y el que se
    publica, es contra el AZAR: bajo intercambiabilidad el rango normalizado
    esperado de cualquier participante es 0.5.
  - NO es un motor de trading. No manda ordenes.
  - NO es una estrategia. No emite señales: nada de lo que produce tiene lado,
    tamaño ni precio de entrada.
  - NO es copy trading. El producto son eventos de propagacion y metricas.
  - NO toca el Risk Manager. Ni lo importa.

Aditivo por construccion: seis tablas nuevas con prefijo `flow_`, un router HTTP
nuevo, una pagina nueva en el Research Lab y un loop opcional en el engine.
Apagado por defecto (`FLOW_ENABLED=false`). Nada del sistema existente cambia de
comportamiento tanto si esta encendido como si no.

Mapa del paquete:

    types.py       vocabulario (FlowTrade, Entry, Cascade, PropagationEvent,
                   WalletFlowMetrics, MarketFlowMetrics)
    metrics.py     estadistica pura (mediana, Wilson, decaimiento, cuantiles)
    ingest.py      JSON de Polymarket -> FlowTrade (puro)
    cascades.py    trades -> cascadas -> eventos de propagacion (puro)
    scoring.py     cascadas y eventos -> metricas por wallet y por mercado (puro)
    feed.py        lectura de la cinta publica (red; nunca lanza)
    models.py      tablas ORM (flow_trades, flow_cascades, flow_events,
                   flow_wallet_metrics, flow_market_metrics, flow_snapshots)
    migrations.py  DDL idempotente (indices y unicidad)
    repository.py  persistencia y consultas
    service.py     orquestacion (ingerir -> detectar -> medir -> persistir)
    api.py         router HTTP /flow/*
"""

from medusa.intelligence.flow.cascades import (
    all_propagation_events,
    annotate_resolutions,
    detect_cascades,
    first_entries,
    propagation_events,
)
from medusa.intelligence.flow.migrations import FLOW_MIGRATIONS
from medusa.intelligence.flow.scoring import (
    flow_summary,
    market_metrics,
    pair_stats,
    wallet_metrics,
)
from medusa.intelligence.flow.service import InformationFlowService
from medusa.intelligence.flow.types import (
    Cascade,
    Entry,
    FlowTrade,
    MarketFlowMetrics,
    PropagationEvent,
    WalletFlowMetrics,
)

__all__ = [
    "Cascade",
    "Entry",
    "FLOW_MIGRATIONS",
    "FlowTrade",
    "InformationFlowService",
    "MarketFlowMetrics",
    "PropagationEvent",
    "WalletFlowMetrics",
    "all_propagation_events",
    "annotate_resolutions",
    "detect_cascades",
    "first_entries",
    "flow_summary",
    "market_metrics",
    "pair_stats",
    "propagation_events",
    "wallet_metrics",
]
