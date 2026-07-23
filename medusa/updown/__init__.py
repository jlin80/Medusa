"""Micro-trading de mercados cripto 'Up or Down' de 5 minutos de Polymarket.

Subsistema DESACOPLADO del pipeline principal (scanner -> estrategias -> riesgo),
igual de aislado que el Intelligence Layer: corre en su propio loop, con su propia
tabla, sus propias alertas y su propio flag. Si el flag esta apagado (default) el
engine se comporta EXACTAMENTE como antes de que este modulo existiera.

Por que no cabe en el pipeline normal:
  - El scanner filtra por horas-a-resolucion (>=6h) y rankea por volumen; una
    ventana de 5 min ni aparece.
  - El ciclo de trading corre cada 60s: demasiado lento para un mercado que vive
    300s y donde la decision se toma en los ultimos segundos.
  - El edge NO esta en el libro de Polymarket: esta en un feed de precio EXTERNO
    y mas rapido (spot de Binance como proxy del stream Chainlink que resuelve el
    mercado). Es informacion que el pipeline actual no tiene delante.

Que hace, en una frase: cerca del cierre de cada ventana, si el spot ya se movio
lo bastante como para que el resultado sea casi seguro (varios sigma sobre la
volatilidad residual) Y el ask del lado favorecido deja valor esperado positivo
DESPUES del peaje de ejecucion, apuesta ese lado. Las dos condiciones son
deliberadas: la primera da winrate alto; la segunda evita la trampa de Hermes
(winrate bonito que muere al pagar el spread).

SOLO PAPER: este subsistema no tiene camino a Live por diseño. Simula el fill
contra el libro REAL (mismo motor pesimista que el Paper Engine) y liquida contra
la resolucion REAL del mercado, pero jamas manda una orden on-chain.
"""

from medusa.updown.trader import UpDownTrader, build_updown_trader

__all__ = ["UpDownTrader", "build_updown_trader"]
