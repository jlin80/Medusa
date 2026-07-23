"""Capa de inteligencia de mercados de Medusa.

Convierte a Medusa de un bot mono-estrategia de cripto en un sistema de
inteligencia para TODO Polymarket:

    classifier  -> a que categoria pertenece cada mercado (data-driven)
    prescorer   -> puntaje preliminar 0-100 por mercado (sin tocar el CLOB)

El principio de diseño: el sistema NO asume que un tipo de mercado o una
estrategia es mejor que otra. Lo descubre con datos (señales en shadow que se
resuelven contra el resultado real del mercado; ver medusa.strategies).
"""

from medusa.intelligence.classifier import MarketClassifier
from medusa.intelligence.prescorer import OpportunityPreScorer

__all__ = ["MarketClassifier", "OpportunityPreScorer"]
