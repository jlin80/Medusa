"""Contrato del INTELLIGENCE LAYER (V3).

Regla number uno, y no es negociable: un modulo de inteligencia produce
FEATURES, nunca DECISIONES.

  - NUNCA ejecuta operaciones.
  - NUNCA modifica posiciones.
  - NUNCA se salta el Risk Manager (ni lo conoce: no tiene forma de llamarlo).
  - NUNCA bloquea el ciclo de trading (corre en su propio loop, con timeout).

El layer esta DESACOPLADO del runtime por construccion, no por disciplina: un
modulo solo puede devolver una lista de Feature. No recibe el adapter de
ejecucion, ni el repositorio de posiciones, ni el engine. Aunque quisiera
operar, no tiene con que. Esa es la diferencia entre "prometemos que no opera"
y "no puede operar".

El flujo es siempre el mismo:

    IntelligenceModule.compute(markets, ctx) -> list[Feature] -> Feature Store

y de ahi las estrategias las leen (MarketContext.features). Una estrategia
sigue siendo quien decide; el layer solo le pone datos delante.

Para AÑADIR un modulo: heredar de IntelligenceModule, implementar compute() y
registrarlo en build_default_modules(). Si falla, el runner lo aisla y el resto
del sistema sigue exactamente igual.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from medusa.config import get_settings
from medusa.core.models import Market


@dataclass
class Feature:
    """Una variable cuantitativa sobre un mercado.

    `value` es SIEMPRE float: es lo único que un modelo o una estrategia puede
    consumir. Todo lo cualitativo (titular de la noticia, wallet de origen,
    explicación) va en `meta`, que es contexto para auditar, no para operar.
    Esta separación es lo que impide que un LLM acabe decidiendo por la puerta
    de atrás: lo textual jamás llega al camino de decisión.
    """

    market_id: str
    name: str
    value: float
    module: str = ""
    meta: dict = field(default_factory=dict)
    ts: dt.datetime | None = None


class IntelligenceModule(ABC):
    """Base de todo modulo del Intelligence Layer."""

    name: str = "base"
    # Cada cuanto tiene sentido recalcular (seg). El runner lo respeta por
    # modulo: la microestructura cambia por minutos, la reputacion de una
    # wallet por dias. Recalcular todo al mismo ritmo seria tirar CPU y
    # llamadas a la API.
    interval: float = 900.0
    # Presupuesto de tiempo. Si un modulo se cuelga contra una API externa, el
    # runner lo corta aqui: el layer nunca puede arrastrar al resto.
    timeout: float = 30.0
    # ¿Necesita red? Los que no, pueden correr aunque no haya conectividad.
    needs_network: bool = False
    # Descripcion corta para /intelligence/modules.
    description: str = ""

    def __init__(self, log) -> None:
        self.log = log
        self.s = get_settings()

    @abstractmethod
    async def compute(self, markets: list[Market], ctx: dict) -> list[Feature]:
        """Devuelve features para los mercados dados. NUNCA decisiones.

        `ctx` trae datos ya recolectados por el runtime (order books, etc.)
        para no volver a pedirlos. Un modulo que no necesite nada de ahi puede
        ignorarlo.

        Contrato de errores: puede lanzar. El runner lo captura, lo registra y
        sigue con los demas modulos. Preferible lanzar a devolver features
        inventadas: un dato falso en el store envenena todos los modelos que
        salgan de el.
        """

    def _feature(self, market_id: str, name: str, value: float, **meta) -> Feature:
        return Feature(
            market_id=market_id, name=name, value=float(value),
            module=self.name, meta=meta or {},
        )
