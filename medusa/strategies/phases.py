"""Ciclo de vida obligatorio de toda estrategia (V3).

    RESEARCH -> SHADOW -> PAPER -> LIVE_CANDIDATE

Ninguna estrategia se salta una fase, y el ascenso no es una opinion: lo tiene
que justificar la evidencia acumulada.

  RESEARCH        Analisis historico e hipotesis. NO corre en el runtime.
                  (research/*.py, backtesting, walk-forward.)

  SHADOW          Corre y registra señales en strategy_signals, que se resuelven
                  contra el resultado REAL del mercado. NUNCA opera. Es el
                  DEFAULT: una estrategia nueva entra aqui y se gana lo demas.

  PAPER           Opera de verdad, pero SOLO contra el motor Paper y solo si el
                  Capital Allocation Manager le da peso > 0 (>=30 señales
                  resueltas con ROI de limite inferior > 0). Genera ROI, DD,
                  profit factor, win rate y costes REALES, y alimenta el
                  Feature Store.
                  *** GARANTIA DURA: una estrategia en PAPER no puede operar en
                  LIVE aunque el bot entero este en modo LIVE. No es una
                  convencion: is_tradeable() lo comprueba contra el modo. ***

  LIVE_CANDIDATE  Unica fase que puede tocar dinero real, y aun asi sigue
                  sujeta a TODOS los gates que ya existian: live_unlocked +
                  frase exacta + credenciales + reconciliacion on-chain + peso
                  del asignador + limites del Risk Manager. Ser candidata no es
                  ser aprobada.

Compatibilidad hacia atras: STRATEGIES_LIVE_ENABLED (V2) sigue funcionando y
mapea a LIVE_CANDIDATE. No hay que tocar el .env desplegado para que todo siga
igual que ayer.
"""

from __future__ import annotations

from enum import Enum


class Phase(str, Enum):
    RESEARCH = "research"
    SHADOW = "shadow"
    PAPER = "paper"
    LIVE_CANDIDATE = "live_candidate"


# Orden de madurez. Sirve para responder "¿esta fase llega al menos a X?".
_ORDER: dict[str, int] = {
    Phase.RESEARCH.value: 0,
    Phase.SHADOW.value: 1,
    Phase.PAPER.value: 2,
    Phase.LIVE_CANDIDATE.value: 3,
}

DEFAULT_PHASE = Phase.SHADOW


def parse_phases(spec: str) -> dict[str, Phase]:
    """Parsea STRATEGY_PHASES: "momentum:paper,stat_arb:research".

    Un nombre de fase invalido se ignora (se queda en el default): un typo en
    el .env jamas debe ascender una estrategia por accidente.
    """
    out: dict[str, Phase] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        name, _, phase_name = item.partition(":")
        try:
            out[name.strip()] = Phase(phase_name.strip().lower())
        except ValueError:
            continue
    return out


def at_least(phase: Phase, minimum: Phase) -> bool:
    return _ORDER[phase.value] >= _ORDER[minimum.value]


def runs_in_runtime(phase: Phase) -> bool:
    """¿Se evalua en el ciclo? Todo menos research."""
    return phase != Phase.RESEARCH


def is_tradeable(phase: Phase, mode: str) -> bool:
    """¿Puede esta fase abrir posiciones en este modo?

    Es el candado central del ciclo de vida:
      - shadow/research: JAMAS, en ningun modo.
      - paper: solo si el bot corre en paper.
      - live_candidate: en paper y en live (sujeta al resto de gates).

    Que PAPER no pueda operar en modo LIVE es deliberado y es lo que hace que
    la fase sea segura de usar: se puede dejar media docena de estrategias en
    paper sin miedo a que un toggle de modo las convierta en dinero real.
    """
    if phase == Phase.PAPER:
        return mode == "paper"
    return phase == Phase.LIVE_CANDIDATE
