"""Modelo de probabilidad justa para 'Up or Down' de 5 minutos.

MATEMATICA PURA, sin dependencias externas (nada de numpy/scipy: el CPU del
CT202 no tiene SSE4.2 y numpy 2.x aborta con Illegal instruction). El CDF normal
se calcula con math.erf, que es stdlib y exacto.

Hipotesis del modelo (barrera de difusion sin deriva):

  El mercado resuelve "Up" si el precio de BNB al final de la ventana es >= al
  precio al inicio (S0). Dentro de la ventana el log-precio se comporta como un
  paseo aleatorio ~ Normal(0, sigma^2 * t). Si ahora (a `secs_left` del cierre)
  el precio es S_t, el movimiento que queda es Normal(0, sigma_sec^2 * secs_left).

  El resultado sera "Up" si  ln(S_t/S0) + resto >= 0, es decir  resto >= -ln(S_t/S0).
  Luego:
      P(Up) = P(resto >= -ln(S_t/S0))
            = Phi( ln(S_t/S0) / (sigma_sec * sqrt(secs_left)) )

  Interpretacion: `z` es cuantas desviaciones tipicas del movimiento RESIDUAL
  llevamos ya de ventaja. z grande y positivo -> "Up" casi seguro; z ~ 0 ->
  cara-o-cruz. A medida que secs_left -> 0 con un movimiento a favor, z -> inf y
  P(Up) -> 1: la incertidumbre se agota y el resultado se fija. Eso es justo lo
  que convierte "queda poco y ya me movi a favor" en una apuesta segura.

Deriva: se ignora a proposito. En 5 minutos la deriva esperada de BNB es
despreciable frente a la volatilidad (a estos horizontes domina el ruido), y
meterla solo añadiria un sesgo fragil.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Suelo de volatilidad por segundo: evita divisiones por ~0 y una falsa certeza
# absoluta si la estimacion de sigma colapsa (mercado muerto, pocos datos). Con
# este suelo, un movimiento cualquiera nunca implica P=1 exacta.
_SIGMA_FLOOR = 1e-6
# Cota de z para que erf no desborde ni devuelva literalmente 0.0/1.0 (queremos
# probabilidades acotadas, coherentes con clamp_prob del resto del sistema).
_Z_CAP = 8.0


def norm_cdf(z: float) -> float:
    """CDF de la normal estandar via math.erf (stdlib, sin numpy)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def fair_prob_up(s0: float, st: float, secs_left: float, sigma_sec: float) -> float:
    """Probabilidad de que la ventana resuelva 'Up' dada la foto actual.

    s0        precio de referencia al abrir la ventana (open del minuto en Binance).
    st        precio spot actual.
    secs_left segundos hasta el cierre de la ventana.
    sigma_sec desviacion tipica del log-retorno por segundo (realized vol).
    """
    if s0 <= 0.0 or st <= 0.0:
        return 0.5
    if secs_left <= 0.0:
        # Ventana cerrada: ya no hay incertidumbre, manda el signo del movimiento.
        return 1.0 if st >= s0 else 0.0
    sigma_sec = max(sigma_sec, _SIGMA_FLOOR)
    denom = sigma_sec * math.sqrt(secs_left)
    if denom <= 0.0:
        return 1.0 if st >= s0 else 0.0
    z = math.log(st / s0) / denom
    z = max(-_Z_CAP, min(_Z_CAP, z))
    return norm_cdf(z)


@dataclass(frozen=True)
class EntryPolicy:
    """Umbrales que convierten una probabilidad justa en una decision de apuesta.

    Los dos candados centrales son `min_prob` (winrate) y `min_ev` (que el edge
    sobreviva al peaje). El resto son guardas de sentido comun.
    """

    entry_window_secs: float   # solo se actua en los ultimos N segundos de la ventana
    min_secs_left: float       # ...pero no en el ultimo parpadeo (no da tiempo a nada)
    min_prob: float            # certeza minima del modelo en el lado favorecido (winrate)
    min_ev: float              # valor esperado minimo por share = prob - ask (anti-Hermes)
    max_ask: float             # no pagar por encima de esto (arriba solo hay 1-2% de techo)


@dataclass(frozen=True)
class UpDownDecision:
    """Resultado de evaluar una ventana. `act` dice si se apuesta."""

    fair_prob_up: float
    favored: str               # "Up" | "Down"
    fair_prob: float           # probabilidad del lado favorecido
    ask: float                 # ask REAL del lado favorecido (lo que se pagaria)
    ev: float                  # fair_prob - ask (valor esperado por share, en prob.)
    act: bool
    reason: str
    z_sigmas: float            # cuantos sigma de ventaja lleva el movimiento (diagnostico)

    @property
    def favored_is_up(self) -> bool:
        return self.favored == "Up"


def _z_sigmas(s0: float, st: float, secs_left: float, sigma_sec: float) -> float:
    if s0 <= 0 or st <= 0 or secs_left <= 0:
        return 0.0
    denom = max(sigma_sec, _SIGMA_FLOOR) * math.sqrt(max(secs_left, 0.0))
    if denom <= 0:
        return 0.0
    return math.log(st / s0) / denom


def favored_side(p_up: float) -> str:
    """Lado que el modelo favorece dada P(Up). Empate -> Up (arbitrario, no se
    apuesta en empates: no pasan el candado de certeza)."""
    return "Up" if p_up >= 0.5 else "Down"


def assess(
    *,
    s0: float,
    st: float,
    secs_left: float,
    sigma_sec: float,
    favored_ask: float,
    policy: EntryPolicy,
) -> UpDownDecision:
    """Decide si apostar y en que lado, dado el estado y el ask REAL del lado
    favorecido.

    Funcion PURA (sin red, sin BD): recibe el ask ya leido del libro y devuelve
    la decision. Asi el selftest la ejercita entera con numeros verificables. El
    lado favorecido lo determina el propio modelo (P(Up)), asi que el trader solo
    tiene que pedir el libro de ESE lado.

    Orden de los candados (cada uno tapa un fallo distinto):
      1. ventana temporal: solo en los ultimos `entry_window_secs`, y no en el
         ultimo `min_secs_left` (sin tiempo para llenar/liquidar con sentido).
      2. certeza (winrate): la prob del lado favorecido debe superar `min_prob`.
      3. precio (anti-Hermes): el ask debe dejar EV = prob - ask >= `min_ev` y no
         superar `max_ask`. Un resultado casi seguro que ya cotiza a 0.99 no es
         negocio: el techo es 1% y el peaje se lo come.
    """
    p_up = fair_prob_up(s0, st, secs_left, sigma_sec)
    z = _z_sigmas(s0, st, secs_left, sigma_sec)

    favored = favored_side(p_up)
    p_fav = p_up if favored == "Up" else 1.0 - p_up
    ask = favored_ask

    ev = p_fav - ask if ask > 0 else -1.0

    def no(reason: str) -> UpDownDecision:
        return UpDownDecision(round(p_up, 4), favored, round(p_fav, 4),
                              round(ask, 4), round(ev, 4), False, reason, round(z, 2))

    if secs_left > policy.entry_window_secs:
        return no(f"aun lejos del cierre ({secs_left:.0f}s > {policy.entry_window_secs:.0f}s)")
    if secs_left < policy.min_secs_left:
        return no(f"demasiado cerca del cierre ({secs_left:.0f}s < {policy.min_secs_left:.0f}s)")
    if p_fav < policy.min_prob:
        return no(f"sin certeza suficiente (P={p_fav:.3f} < {policy.min_prob:.2f})")
    if ask <= 0.0:
        return no("sin ask en el lado favorecido (libro vacio)")
    if ask > policy.max_ask:
        return no(f"ask demasiado alto ({ask:.3f} > {policy.max_ask:.2f}): solo queda techo")
    if ev < policy.min_ev:
        return no(f"EV insuficiente tras el peaje ({ev:+.3f} < {policy.min_ev:.2f})")

    return UpDownDecision(
        round(p_up, 4), favored, round(p_fav, 4), round(ask, 4), round(ev, 4),
        True,
        f"{favored} casi seguro (P={p_fav:.3f}, {z:+.1f}sigma) y ask {ask:.3f} deja EV {ev:+.3f}",
        round(z, 2),
    )
