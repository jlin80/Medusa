"""WALLET INTELLIGENCE — perfilado numerico de wallets de Polymarket.

ESTO NO ES COPY TRADING. Copiar a una wallet significa convertir su movimiento
en una orden; aqui no existe ese camino. El producto es:

    posiciones publicas -> WalletDNA (19 numeros) -> score -> reputacion
                                                  -> clusters / similitud
                                                  -> FEATURES por mercado

Ni una sola funcion del paquete devuelve un lado, un tamaño o un precio. No se
importa `execution`, `trading`, `risk` ni `strategies`, y hay un test que lo
verifica recorriendo el AST (`tests/test_wallet_isolation.py`).

TODO ES NUMERICO. Cero etiquetas cualitativas: ni "smart money", ni "ballena",
ni "novato". Un cluster es un entero y su significado son los 19 numeros de su
centroide. Poner nombre a un grupo es una interpretacion humana, y el momento en
que se mete en el codigo es el momento en que el sistema empieza a heredar la
opinion de alguien como si fuese un dato.

Mapa del paquete:

    types.py             vocabulario: DNA_FEATURES, WalletPosition, WalletDNA
    stats.py             estadistica en Python puro (sin numpy: CPU del CT202)
    ingest.py            JSON de Polymarket -> WalletPosition (puro)
    feed.py              ingesta HTTP (Data API + Gamma), nunca lanza
    wallet_dna/          las 19 metricas y el perfil (puro)
    wallet_scoring/      score relativo a la poblacion + importancia de features
    wallet_reputation/   score castigado por muestra, frescura y estabilidad
    wallet_clusters/     k-means determinista sobre el ADN estandarizado
    wallet_similarity/   coseno entre perfiles
    models.py            tablas ORM (wi_wallets, wi_dna_history, wi_similarity,
                         wi_clusters, wi_runs)
    migrations.py        DDL idempotente
    repository.py        persistencia y consultas
    service.py           orquestacion (descubrir -> ingerir -> perfilar)
    module.py            puente al Intelligence Layer: FEATURES por mercado
    api.py               router HTTP /wallets/*
"""

from medusa.intelligence.wallet.migrations import WALLET_MIGRATIONS
from medusa.intelligence.wallet.service import WalletIntelligenceService
from medusa.intelligence.wallet.types import (
    DNA_DEFINITIONS,
    DNA_FEATURES,
    PopulationStats,
    WalletDNA,
    WalletPosition,
)
from medusa.intelligence.wallet.wallet_clusters import cluster_wallets
from medusa.intelligence.wallet.wallet_dna import build_dna, build_population, population_stats
from medusa.intelligence.wallet.wallet_reputation import reputation_of, reputation_population
from medusa.intelligence.wallet.wallet_scoring import feature_importance, score_wallet
from medusa.intelligence.wallet.wallet_similarity import similar_wallets, similarity_edges

__all__ = [
    "DNA_DEFINITIONS",
    "DNA_FEATURES",
    "PopulationStats",
    "WALLET_MIGRATIONS",
    "WalletDNA",
    "WalletIntelligenceService",
    "WalletPosition",
    "build_dna",
    "build_population",
    "cluster_wallets",
    "feature_importance",
    "population_stats",
    "reputation_of",
    "reputation_population",
    "score_wallet",
    "similar_wallets",
    "similarity_edges",
]
