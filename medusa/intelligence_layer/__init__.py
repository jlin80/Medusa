"""INTELLIGENCE LAYER (V3, 2026-07-16).

Capa DESACOPLADA del runtime cuya unica responsabilidad es generar informacion
adicional (FEATURES) para que las estrategias decidan mejor. Nunca ejecuta,
nunca toca posiciones, nunca se salta el Risk Manager.

    modulo.compute(markets, ctx) -> [Feature] -> Feature Store -> estrategias

Modulos incorporados:
  microstructure  - salud del libro, liquidez, volatilidad, coste de ejecucion.
                    Cero llamadas externas: reutiliza lo que el scanner ya trajo.

Modulos previstos (cada uno es un fichero mas; el layer no cambia):
  wallet          - reputacion de wallets (win rate, ROI, especialidad,
                    consistencia) -> wallet_score. Data API de Polymarket.
                    NO es copy trading: produce una feature, no una orden.
  cross_market    - relaciones entre mercados -> cross_market_score.
  news            - noticias -> sentimiento/impacto/horizonte (LLM OFFLINE,
                    jamas en el camino de decision).
  event           - Fed, CPI, elecciones, resultados -> variables numericas.
  sports/politics/crypto - features especificas por categoria.

Por que el orden: los dos primeros solo necesitan Polymarket (sin claves, sin
coste, sin dependencias). El edge, si existe, esta en informacion externa AL
PRECIO -- y las wallets informadas son informacion externa al precio que ya
tenemos delante.
"""

from medusa.intelligence_layer.base import Feature, IntelligenceModule
from medusa.intelligence_layer.microstructure import MicrostructureIntelligence
from medusa.intelligence_layer.runner import IntelligenceRunner

__all__ = [
    "Feature",
    "IntelligenceModule",
    "IntelligenceRunner",
    "build_default_modules",
]


def build_default_modules(log) -> list[IntelligenceModule]:
    """Modulos disponibles. Cual corre lo decide INTELLIGENCE_MODULES."""
    modules: list[IntelligenceModule] = [
        MicrostructureIntelligence(log),
    ]
    # Wallet Intelligence: reputacion agregada de los holders de cada mercado.
    # Sigue siendo opt-in por la lista blanca INTELLIGENCE_MODULES (registrarlo
    # aqui no lo enciende). Import perezoso y aislado: un paquete opcional no
    # puede impedir que el layer levante con los modulos de siempre.
    try:
        from medusa.intelligence.wallet.module import WalletIntelligence

        modules.append(WalletIntelligence(log))
    except Exception:  # noqa: BLE001
        pass
    return modules
