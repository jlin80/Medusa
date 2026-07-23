"""Market Classifier: asigna una categoria de la taxonomia a cada mercado.

Arquitectura de registry: el clasificador es una lista ordenada de reglas
`callable(Market) -> str | None`; la primera regla que responde gana. Para
añadir un clasificador nuevo (p.ej. un modelo ML el dia que exista) se registra
una regla mas con `register()`, sin tocar el nucleo. Para añadir una categoria
nueva se edita la taxonomia (medusa/intelligence/categories.py), que es datos.

Reglas incorporadas, en orden:
  1. Pista de Gamma: si la API ya trae una categoria reconocible, se usa.
  2. Keywords con limite de palabra sobre question+slug: gana la categoria con
     mas matches; empates se resuelven por CATEGORY_PRIORITY (mas especifica
     primero).
"""

from __future__ import annotations

import re
from typing import Callable

from medusa.core.models import Market
from medusa.intelligence.categories import (
    CATEGORY_KEYWORDS,
    CATEGORY_PRIORITY,
    GAMMA_CATEGORY_MAP,
    OTHER,
)

Rule = Callable[[Market], "str | None"]


def _compile_patterns() -> dict[str, re.Pattern]:
    """Un regex por categoria con todas sus keywords unidas por |.

    Compilar una vez y matchear una vez por categoria es lo que hace viable
    clasificar cientos de mercados por ciclo en la CPU debil del CT202.
    """
    patterns: dict[str, re.Pattern] = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        escaped = sorted((re.escape(k) for k in keywords), key=len, reverse=True)
        patterns[category] = re.compile(r"\b(?:" + "|".join(escaped) + r")\b")
    return patterns


class MarketClassifier:
    def __init__(self, log=None) -> None:
        self.log = log
        self._patterns = _compile_patterns()
        self._rules: list[Rule] = [self._by_gamma_hint, self._by_keywords]

    def register(self, rule: Rule, first: bool = False) -> None:
        """Añade una regla de clasificacion sin tocar el nucleo."""
        if first:
            self._rules.insert(0, rule)
        else:
            self._rules.append(rule)

    def classify(self, m: Market) -> str:
        for rule in self._rules:
            try:
                category = rule(m)
            except Exception:  # noqa: BLE001 - una regla rota no rompe el escaneo
                continue
            if category:
                return category
        return OTHER

    # ------------------------------------------------------------- reglas ----
    def _by_gamma_hint(self, m: Market) -> str | None:
        """La categoria de Gamma solo si mapea limpio a nuestra taxonomia.

        Es una pista, no un veredicto: Gamma la deja vacia o usa etiquetas
        genericas en muchos mercados; lo que no reconocemos cae a keywords.
        """
        hint = (m.category or "").strip().lower()
        return GAMMA_CATEGORY_MAP.get(hint) if hint else None

    def _by_keywords(self, m: Market) -> str | None:
        text = f"{m.question} {m.slug.replace('-', ' ')}".lower()
        if not text.strip():
            return None
        counts: dict[str, int] = {}
        for category, pattern in self._patterns.items():
            n = len(pattern.findall(text))
            if n:
                counts[category] = n
        if not counts:
            return None
        best = max(counts.values())
        # Desempate por especificidad: el orden de CATEGORY_PRIORITY decide.
        for category in CATEGORY_PRIORITY:
            if counts.get(category) == best:
                return category
        return None
