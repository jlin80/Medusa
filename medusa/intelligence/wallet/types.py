"""Vocabulario de WALLET INTELLIGENCE.

Regla del subsistema, y es la misma que rige el Intelligence Layer V3:

    Wallet Intelligence produce FEATURES. Nunca operaciones.

Esto **no es copy trading**. Copiar a una wallet significa convertir su
movimiento en una orden. Aqui no hay orden posible: el producto final es un
`WalletDNA` -- 19 numeros -- y un puñado de features escalares. No existe una
funcion en todo el paquete que devuelva un lado, un tamaño o un precio de
entrada, y no se importa nada que pueda ejecutar.

TODO ES NUMERICO. No hay una sola etiqueta cualitativa: ni "smart money", ni
"ballena", ni "novato". Esas etiquetas son juicios disfrazados de datos --
alguien elige el umbral y a partir de ahi el sistema hereda su opinion. Un
cluster aqui es un ENTERO, y su significado sale de su centroide, que tambien
son numeros. Si un dia hace falta una etiqueta, que la ponga un humano mirando
el centroide, fuera del camino de decision.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

# Orden CANONICO del vector de ADN. Es contractual: el clustering, la similitud
# y la importancia de features indexan por posicion, y la BD guarda el vector
# serializado. Añadir una metrica va SIEMPRE al final; reordenar esta tupla
# invalida todo lo persistido.
DNA_FEATURES: tuple[str, ...] = (
    "roi_historical",
    "roi_recent",
    "sharpe",
    "win_rate",
    "consistency",
    "trade_frequency",
    "entry_timing",
    "exit_timing",
    "liquidity_preference",
    "spread_preference",
    "category_expertise",
    "conviction",
    "alpha",
    "beta",
    "drawdown",
    "volatility",
    "reliability",
    "freshness",
    "decay",
)

# Que significa cada numero. Se expone en la API para que el dashboard no tenga
# que duplicar las definiciones (y no puedan divergir).
DNA_DEFINITIONS: dict[str, str] = {
    "roi_historical": "ROI medio por posicion cerrada sobre todo el historial disponible.",
    "roi_recent": "El mismo ROI medio, restringido a la ventana reciente (WALLET_RECENT_DAYS).",
    "sharpe": "media(ROI) / desviacion(ROI) por posicion. Rendimiento por unidad de riesgo asumido.",
    "win_rate": "Fraccion de posiciones cerradas con PnL > 0.",
    "consistency": "1/(1+CV) con CV = desviacion/|media| del ROI. 1 = resultados homogeneos, 0 = ruido.",
    "trade_frequency": "Posiciones por dia sobre el periodo activo de la wallet.",
    "entry_timing": "Fraccion media de la vida del mercado ya transcurrida al ENTRAR. 0 = entra al abrir, 1 = al cerrar.",
    "exit_timing": "Lo mismo al SALIR. 1.0 = mantiene hasta la resolucion.",
    "liquidity_preference": "log10(1+liquidez media de los mercados que opera), normalizado.",
    "spread_preference": "Spread medio de los mercados que opera. Alto = tolera mercados caros de operar.",
    "category_expertise": "max sobre categorias de (peso de la categoria x cota inferior de Wilson del win rate en ella).",
    "conviction": "Gini de los tamaños de posicion. 0 = apuesta siempre igual, 1 = concentra todo en una.",
    "alpha": "ROI medio de la wallet menos beta x ROI medio de la poblacion. Exceso no explicado por el mercado.",
    "beta": "cov(ROI wallet, ROI poblacion) / var(ROI poblacion) por cubos temporales.",
    "drawdown": "Maxima caida relativa de la curva de PnL acumulado.",
    "volatility": "Desviacion tipica del ROI por posicion.",
    "reliability": "Cota inferior de Wilson del win rate: win rate penalizado por falta de muestra.",
    "freshness": "exp(-dias desde la ultima operacion / semivida). 1 = activa ahora, 0 = inactiva.",
    "decay": "tanh(ROI reciente - ROI historico). >0 mejora, <0 se degrada.",
}

# Metricas donde MAS ES MEJOR de forma inequivoca. Se usa para orientar el signo
# al agregar el score compuesto. Las que no estan aqui (timings, preferencias,
# frecuencia, beta, conviction) NO tienen direccion buena o mala universal, y
# por eso NO entran en el score: entrarian con una opinion metida a mano.
DNA_HIGHER_IS_BETTER: frozenset[str] = frozenset({
    "roi_historical", "roi_recent", "sharpe", "win_rate", "consistency",
    "alpha", "reliability", "freshness", "decay",
})

# Metricas donde MENOS ES MEJOR.
DNA_LOWER_IS_BETTER: frozenset[str] = frozenset({"drawdown", "volatility"})


@dataclass
class WalletPosition:
    """Una posicion cerrada (o viva) de una wallet, ya normalizada.

    Es el atomo del que sale TODO el ADN. Se construye desde la Data API
    publica de Polymarket; el paquete nunca mira una clave privada ni una
    posicion propia de Medusa.
    """

    wallet: str
    market_id: str
    category: str = ""
    outcome: str = "YES"
    size: float = 0.0             # shares
    entry_price: float = 0.0
    exit_price: float = 0.0
    cost: float = 0.0             # USDC comprometidos (size * entry_price)
    pnl: float = 0.0              # USDC realizados
    roi: float = 0.0              # pnl / cost
    opened_at: dt.datetime | None = None
    closed_at: dt.datetime | None = None
    closed: bool = False
    won: bool | None = None
    # Contexto del MERCADO en el que opero. Sin esto no se pueden calcular las
    # preferencias ni los timings, y son justo las metricas que distinguen a una
    # wallet informada de una que simplemente tuvo suerte.
    market_start: dt.datetime | None = None
    market_end: dt.datetime | None = None
    liquidity: float = 0.0
    spread: float = 0.0

    def duration_fraction(self, at: dt.datetime | None) -> float | None:
        """Fraccion de la vida del mercado transcurrida en el instante `at`.

        None si falta cualquiera de los tres datos: un timing inventado es peor
        que un timing ausente, porque contamina el ADN de toda la wallet.
        """
        if at is None or self.market_start is None or self.market_end is None:
            return None
        total = (self.market_end - self.market_start).total_seconds()
        if total <= 0:
            return None
        elapsed = (at - self.market_start).total_seconds()
        return min(1.0, max(0.0, elapsed / total))


@dataclass
class WalletDNA:
    """Perfil numerico de una wallet: 19 metricas y nada mas.

    `vector()` devuelve los 19 valores en el orden de DNA_FEATURES. Ese vector
    es lo que consumen el clustering y la similitud, y es lo que se persiste.
    """

    wallet: str
    metrics: dict[str, float] = field(default_factory=dict)
    # Muestra sobre la que se calculo. Ninguna metrica se interpreta sin esto:
    # un Sharpe de 3 con n=2 no es un Sharpe, es una anecdota.
    n_positions: int = 0
    n_closed: int = 0
    n_markets: int = 0
    n_categories: int = 0
    first_trade: dt.datetime | None = None
    last_trade: dt.datetime | None = None
    # Detalle por categoria (tambien numerico): {categoria: {n, wins, roi, share}}.
    categories: dict[str, dict] = field(default_factory=dict)
    ts: dt.datetime | None = None

    def vector(self) -> list[float]:
        return [float(self.metrics.get(name, 0.0)) for name in DNA_FEATURES]

    def to_dict(self) -> dict:
        return {
            "wallet": self.wallet,
            "metrics": {k: round(float(v), 6) for k, v in self.metrics.items()},
            "n_positions": self.n_positions,
            "n_closed": self.n_closed,
            "n_markets": self.n_markets,
            "n_categories": self.n_categories,
            "first_trade": self.first_trade.isoformat() if self.first_trade else None,
            "last_trade": self.last_trade.isoformat() if self.last_trade else None,
            "categories": self.categories,
        }


@dataclass
class PopulationStats:
    """Media y desviacion de cada metrica en la POBLACION analizada.

    El ADN se estandariza contra la poblacion, no contra constantes: un Sharpe
    de 1.2 significa una cosa entre wallets de cripto y otra distinta entre las
    de deportes. Fijar umbrales absolutos seria volver a meter una opinion a
    mano por la puerta de atras.
    """

    mean: dict[str, float] = field(default_factory=dict)
    stdev: dict[str, float] = field(default_factory=dict)
    n: int = 0

    def standardize(self, dna: WalletDNA) -> list[float]:
        """Vector z-score de una wallet. Desviacion 0 => componente 0 (esa
        metrica no distingue a nadie en esta poblacion)."""
        out: list[float] = []
        for name in DNA_FEATURES:
            sd = self.stdev.get(name, 0.0)
            if sd <= 0:
                out.append(0.0)
                continue
            out.append((float(dna.metrics.get(name, 0.0)) - self.mean.get(name, 0.0)) / sd)
        return out

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "mean": {k: round(v, 6) for k, v in self.mean.items()},
            "stdev": {k: round(v, 6) for k, v in self.stdev.items()},
        }
