"""Modelos de dominio (dataclasses) usados por el pipeline en memoria.

Separados de los modelos ORM (medusa.data.db_models) para no arrastrar el ciclo
de vida de la sesion de SQLAlchemy por todo el codigo.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass
class OrderBook:
    """Snapshot del libro de un token (outcome) de Polymarket."""

    token_id: str
    bids: list[tuple[float, float]] = field(default_factory=list)  # (precio, size) desc
    asks: list[tuple[float, float]] = field(default_factory=list)  # (precio, size) asc

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def mid(self) -> float:
        if self.bids and self.asks:
            return (self.best_bid + self.best_ask) / 2
        return self.best_bid or self.best_ask

    @property
    def spread(self) -> float:
        if self.bids and self.asks:
            return self.best_ask - self.best_bid
        return 0.0

    @property
    def ask_liquidity(self) -> float:
        return sum(p * s for p, s in self.asks)

    @property
    def bid_liquidity(self) -> float:
        return sum(p * s for p, s in self.bids)


@dataclass
class Market:
    id: str                       # condition_id
    question: str = ""
    slug: str = ""
    category: str = ""
    end_date: dt.datetime | None = None
    yes_token_id: str = ""
    no_token_id: str = ""
    yes_price: float = 0.0        # precio implicito YES (0..1)
    no_price: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    volume_24h: float = 0.0
    liquidity: float = 0.0
    active: bool = True
    # Variacion reciente del precio YES (Gamma la trae ya calculada). Alimenta
    # el pre-scorer (componente "move") y las estrategias de momentum/breakout.
    price_change_24h: float = 0.0
    price_change_1h: float = 0.0
    # Clasificacion y puntaje calculados por la capa de inteligencia.
    medusa_category: str = ""
    opportunity_score: float = 0.0

    @property
    def implied_prob(self) -> float:
        """Probabilidad implicita de mercado para YES (precio YES)."""
        return self.yes_price

    def hours_to_resolution(self) -> float | None:
        if not self.end_date:
            return None
        now = dt.datetime.now(dt.timezone.utc)
        delta = self.end_date - now
        return delta.total_seconds() / 3600.0


@dataclass
class MarketContext:
    """Datos de analisis profundo de un mercado (los que cuestan llamadas API).

    Solo se construye para el top del ranking del pre-scorer: pedir order books
    e historicos de TODOS los mercados en cada ciclo seria inviable. Cada
    estrategia declara que necesita (needs_book_no, needs_history) y el scanner
    solo pide eso.
    """

    book_yes: OrderBook | None = None
    book_no: OrderBook | None = None
    history: list[tuple[float, float]] = field(default_factory=list)  # (ts, precio YES) asc
    prescore: float = 0.0
    components: dict = field(default_factory=dict)
    # Features del Intelligence Layer (V3), leidas del Feature Store: {nombre:
    # valor}. Las estrategias consultan ESTO antes que ninguna API externa. Si
    # el layer esta apagado o un modulo fallo, llega vacio y la estrategia debe
    # seguir funcionando igual (usar .get con default, nunca asumir presencia).
    features: dict[str, float] = field(default_factory=dict)


@dataclass
class Opportunity:
    market_id: str
    question: str
    outcome: str                  # "YES" | "NO"
    market_price: float           # precio del outcome elegido (lo que pagas)
    market_prob: float            # probabilidad implicita de mercado
    medusa_prob: float            # probabilidad estimada por Medusa
    edge: float                   # medusa_prob - market_prob (para el outcome)
    confidence: float             # 0..1
    action: str                   # buy_yes | buy_no | hold | skip
    reason: str = ""
    token_id: str = ""            # token del outcome elegido
    spread: float = 0.0           # spread absoluto del libro (coste de operar)
    liquidity: float = 0.0        # liquidez del mercado (USDC)
    # Atribucion multi-estrategia: que estrategia genero la oportunidad, en que
    # categoria de mercado y con que fuerza. Sin esto no se puede medir el
    # rendimiento por estrategia/categoria, que es la base de la asignacion de
    # capital.
    strategy: str = ""
    category: str = ""
    score: float = 0.0


@dataclass
class OrderRequest:
    """Peticion de orden, identica para Paper y Live (mismo ExecutionAdapter)."""

    token_id: str
    side: str                     # buy | sell
    usd_size: float = 0.0         # presupuesto en USDC (BUY)
    shares: float = 0.0           # shares a vender (SELL)
    market_id: str = ""
    outcome: str = "YES"
    ref_price: float = 0.0        # precio de referencia al decidir (para analitica)


@dataclass
class Fill:
    price: float
    size: float                   # shares
    fee: float = 0.0
    slippage_cost: float = 0.0
    spread_cost: float = 0.0


@dataclass
class OrderResult:
    filled_size: float
    avg_price: float
    fills: list[Fill] = field(default_factory=list)
    status: str = "filled"        # filled | partial | rejected
    reason: str = ""
    book_mid: float = 0.0         # mid del libro al ejecutar (referencia de TP/SL)

    @property
    def total_fee(self) -> float:
        return sum(f.fee for f in self.fills)

    @property
    def total_slippage(self) -> float:
        return sum(f.slippage_cost for f in self.fills)

    @property
    def gross(self) -> float:
        """Nocional bruto ejecutado (sin fees)."""
        return sum(f.price * f.size for f in self.fills)

    @property
    def cash_out(self) -> float:
        """USDC que salen de la cuenta al comprar (nocional + fees)."""
        return self.gross + self.total_fee

    @property
    def cash_in(self) -> float:
        """USDC que entran a la cuenta al vender (nocional - fees)."""
        return self.gross - self.total_fee

    @property
    def total_cost(self) -> float:
        """Coste total imputable a la operacion (analitica, no flujo de caja).

        Ojo: fees son dinero real; slippage y spread son el sobrecoste frente a
        un fill ideal al mid. Sumarlos aqui es una metrica de calidad de
        ejecucion, NO se resta al PnL (el PnL sale de los flujos reales).
        """
        return sum(f.fee + f.slippage_cost + f.spread_cost for f in self.fills)
