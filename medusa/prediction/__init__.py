"""LEGACY (2026-07-16): el Prediction Engine fue absorbido por el sistema
multi-estrategia (medusa.strategies). Este paquete queda como shim de
compatibilidad; no lo usa ningun modulo del pipeline actual.
"""

from medusa.prediction.engine import PredictionEngine

__all__ = ["PredictionEngine"]
