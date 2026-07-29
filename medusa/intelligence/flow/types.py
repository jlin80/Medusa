"""Vocabulario del INFORMATION FLOW ENGINE (IFE).

Aqui vive lo que el motor sabe representar: un trade publico normalizado, una
cascada de entradas, un evento de propagacion y las metricas que salen de todo
ello. Es el unico sitio donde se define ese vocabulario; ni el detector de
cascadas ni el scoring inventan campos por su cuenta.

REGLA DEL PAQUETE (la misma del MIG y de Wallet Intelligence, y aqui ademas hay
una regla EPISTEMICA que no es negociable):

  - El IFE MIDE PROPAGACION, jamas causalidad. Que la wallet B entre despues de
    la wallet A no significa que B siga a A: significa exactamente eso, que
    entro despues. Todo el paquete esta escrito para que esa distincion sea
    imposible de perder: los campos se llaman `lag_seconds`, `hop`, `leader`,
    `follower` -- orden temporal -- y ninguno se llama `caused_by`, `influence`
    ni `signal`.
  - Toda metrica viaja con su TAMAÑO DE MUESTRA y con su cota inferior. Un
    `leadership_score` de 1.0 con n=1 y otro de 0.62 con n=400 no son
    comparables, y quien lea el motor tiene que poder distinguirlos sin ir a los
    datos crudos.
  - El producto del paquete son eventos y metricas. No hay lado, no hay tamaño,
    no hay precio de entrada: nada de aqui puede convertirse en una orden.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

UTC = dt.timezone.utc

# Lados sobre los que se mide la propagacion. Un mercado binario tiene dos
# "conversaciones" independientes -- quien apuesta a que SI y quien apuesta a
# que NO -- y mezclarlas convertiria a un comprador de NO en "seguidor" de un
# comprador de SI, que es justo lo contrario de lo que paso.
SIDES: tuple[str, str] = ("YES", "NO")


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(float(value), digits)


@dataclass(frozen=True)
class FlowTrade:
    """Un trade publico normalizado: el atomo del motor.

    `side` es el lado en el que la wallet se POSICIONO, no el verbo del libro:
    vender SI es posicionarse en NO. Y `price` es siempre la probabilidad
    implicita DEL LADO ENTRADO, no la del token YES. Sin esa normalizacion,
    "el precio subio despues de que entrara" significaria cosas distintas segun
    el lado, y la metrica no se podria agregar.
    """

    market_id: str
    wallet: str
    side: str                 # "YES" | "NO" (lado entrado, ya normalizado)
    price: float              # probabilidad implicita del lado entrado [0, 1]
    size: float               # shares
    ts: dt.datetime
    uid: str = ""             # huella estable del trade (dedup entre ingestas)

    @property
    def notional(self) -> float:
        return float(self.size) * float(self.price)

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id, "wallet": self.wallet, "side": self.side,
            "price": round(float(self.price), 6), "size": round(float(self.size), 6),
            "ts": self.ts.isoformat(), "uid": self.uid,
        }


@dataclass(frozen=True)
class Entry:
    """PRIMERA entrada de una wallet en un lado de un mercado.

    La propagacion se mide sobre primeras entradas, no sobre todos los trades:
    reforzar una posicion que ya se tenia no es informacion nueva llegando a esa
    wallet, y contarlo la haria aparecer como "seguidora" de si misma.
    """

    wallet: str
    ts: dt.datetime
    price: float
    size: float


@dataclass
class Cascade:
    """Una racha de entradas encadenadas en el mismo lado de un mercado.

    Definicion OPERATIVA (deliberadamente aburrida): entradas consecutivas cuyo
    hueco temporal no supera `window_seconds`. Es una sesionizacion por huecos,
    la misma tecnica con la que se parten sesiones de navegacion. NO afirma que
    los participantes se esten copiando; afirma que entraron seguidos, que es lo
    unico observable.

    `participants` va ordenada por tiempo: el indice 0 es quien abrio la racha.
    """

    market_id: str
    side: str
    entries: list[Entry] = field(default_factory=list)
    # Rellenados por el analisis (metrics.py), nunca a mano.
    propagation_time: float = 0.0   # mediana de los saltos consecutivos (s)
    consensus_delay: float = 0.0    # s hasta que entra la fraccion de consenso
    resolved: bool = False
    resolution_value: float | None = None   # 1.0 si el lado gano, 0.0 si perdio

    @property
    def key(self) -> str:
        """Clave estable de la cascada: mercado + lado + instante de inicio."""
        return f"{self.market_id}:{self.side}:{int(self.started_at.timestamp())}"

    @property
    def n(self) -> int:
        return len(self.entries)

    @property
    def started_at(self) -> dt.datetime:
        return self.entries[0].ts

    @property
    def ended_at(self) -> dt.datetime:
        return self.entries[-1].ts

    @property
    def span_seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()

    @property
    def price_start(self) -> float:
        return float(self.entries[0].price)

    @property
    def price_end(self) -> float:
        return float(self.entries[-1].price)

    @property
    def price_move(self) -> float:
        """Movimiento del precio DEL LADO a lo largo de la cascada.

        Positivo = el mercado se movio a favor de quien entro. Es una
        coincidencia temporal medida, no un efecto atribuido a nadie.
        """
        return self.price_end - self.price_start

    @property
    def notional(self) -> float:
        return sum(e.size * e.price for e in self.entries)

    def rank(self, index: int) -> float:
        """Posicion normalizada en la cascada: 0.0 = primero, 1.0 = ultimo.

        Con n=1 no hay orden que medir y devuelve 0.5 (el valor neutro bajo
        intercambiabilidad), pero esas cascadas ni siquiera llegan aqui: el
        detector exige un minimo de participantes.
        """
        if self.n <= 1:
            return 0.5
        return index / (self.n - 1)

    def to_dict(self) -> dict:
        return {
            "key": self.key, "market_id": self.market_id, "side": self.side,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "n_participants": self.n,
            "span_seconds": round(self.span_seconds, 3),
            "propagation_time": round(self.propagation_time, 3),
            "consensus_delay": round(self.consensus_delay, 3),
            "price_start": round(self.price_start, 6),
            "price_end": round(self.price_end, 6),
            "price_move": round(self.price_move, 6),
            "notional": round(self.notional, 4),
            "resolved": self.resolved,
            "resolution_value": _round(self.resolution_value),
            "participants": [e.wallet for e in self.entries],
        }


@dataclass(frozen=True)
class PropagationEvent:
    """Un eslabon observado de la cadena: `leader` entro y despues `follower`.

    LEER CON CUIDADO. `leader`/`follower` son etiquetas de ORDEN, no de
    influencia. El evento afirma tres cosas, todas verificables:

        1. las dos wallets entraron en el mismo lado del mismo mercado,
        2. dentro de la misma cascada (hueco <= ventana),
        3. separadas por `lag_seconds`, con `hop` entradas de distancia.

    No afirma que la segunda supiera de la primera, ni que la primera moviera el
    precio. `price_move` es lo que hizo el precio del lado ENTRE las dos
    entradas: contexto, no atribucion.
    """

    market_id: str
    side: str
    cascade_key: str
    leader: str
    follower: str
    hop: int                  # distancia en la cadena (1 = entrada siguiente)
    lag_seconds: float
    price_leader: float
    price_follower: float
    ts: dt.datetime           # instante de la entrada del follower

    @property
    def price_move(self) -> float:
        return self.price_follower - self.price_leader

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id, "side": self.side,
            "cascade_key": self.cascade_key,
            "leader": self.leader, "follower": self.follower,
            "hop": int(self.hop), "lag_seconds": round(float(self.lag_seconds), 3),
            "price_leader": round(float(self.price_leader), 6),
            "price_follower": round(float(self.price_follower), 6),
            "price_move": round(self.price_move, 6),
            "ts": self.ts.isoformat(),
        }


@dataclass
class WalletFlowMetrics:
    """Como se mueve UNA wallet dentro del flujo de informacion.

    Cada numero viene con su n. Los que son proporciones traen ademas una cota
    inferior de Wilson al 95%: es la diferencia entre "acerto 3 de 3" y "acerto
    240 de 400", que sin la cota parecen 1.00 y 0.60 y engañan al ojo.

    `enough_samples` es el unico campo que dice si la fila se puede leer como
    evidencia. Un panel que lo ignore estara mostrando ruido con dos decimales.
    """

    wallet: str
    n_cascades: int = 0
    n_markets: int = 0
    # --- posicion en la cadena ---
    leadership_score: float = 0.5   # media de (1 - rank): 1.0 = siempre abre
    follow_score: float = 0.5       # media de rank: 1.0 = siempre cierra
    leadership_lower: float = 0.0   # cota inferior 95% del leadership
    edge_vs_chance: float = 0.0     # leadership - 0.5 (el nulo bajo intercambio)
    # --- velocidad ---
    information_speed: float = 0.0  # mediana de segundos desde el inicio de la cascada
    speed_score: float = 0.0        # forma acotada [0,1] de lo anterior
    propagation_time: float = 0.0   # mediana de segundos hasta la entrada siguiente
    # --- calidad de la informacion segun el momento de entrada ---
    early_information_score: float = 0.0
    late_information_score: float = 0.0
    n_early: int = 0
    n_late: int = 0
    early_lower: float = 0.0
    late_lower: float = 0.0
    information_edge: float = 0.0   # early - late (descriptivo, no causal)
    enough_samples: bool = False

    def to_dict(self) -> dict:
        return {
            "wallet": self.wallet,
            "n_cascades": self.n_cascades, "n_markets": self.n_markets,
            "leadership_score": round(self.leadership_score, 4),
            "follow_score": round(self.follow_score, 4),
            "leadership_lower": round(self.leadership_lower, 4),
            "edge_vs_chance": round(self.edge_vs_chance, 4),
            "information_speed": round(self.information_speed, 3),
            "speed_score": round(self.speed_score, 4),
            "propagation_time": round(self.propagation_time, 3),
            "early_information_score": round(self.early_information_score, 4),
            "late_information_score": round(self.late_information_score, 4),
            "n_early": self.n_early, "n_late": self.n_late,
            "early_lower": round(self.early_lower, 4),
            "late_lower": round(self.late_lower, 4),
            "information_edge": round(self.information_edge, 4),
            "enough_samples": self.enough_samples,
        }


@dataclass
class MarketFlowMetrics:
    """Como se propaga la informacion dentro de UN mercado."""

    market_id: str
    n_cascades: int = 0
    n_wallets: int = 0
    n_events: int = 0
    consensus_delay: float = 0.0    # mediana entre cascadas
    propagation_time: float = 0.0   # mediana de los saltos consecutivos
    information_speed: float = 0.0  # participantes por hora dentro de cascadas
    avg_cascade_size: float = 0.0
    price_move: float = 0.0         # movimiento medio del lado en sus cascadas
    enough_samples: bool = False

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "n_cascades": self.n_cascades, "n_wallets": self.n_wallets,
            "n_events": self.n_events,
            "consensus_delay": round(self.consensus_delay, 3),
            "propagation_time": round(self.propagation_time, 3),
            "information_speed": round(self.information_speed, 4),
            "avg_cascade_size": round(self.avg_cascade_size, 3),
            "price_move": round(self.price_move, 6),
            "enough_samples": self.enough_samples,
        }
