"""WALLET CLUSTERS: k-means sobre el ADN estandarizado, en Python puro.

Un cluster aqui es **un entero**. No se llama "smart money", ni "ballenas", ni
"scalpers": esas etiquetas son una interpretacion que alguien pega encima y que
el sistema despues hereda como si fuese un dato. Lo que si se publica es el
CENTROIDE (19 numeros) y las metricas que mas lo separan de la media, para que
un humano pueda mirarlo y sacar sus conclusiones fuera del camino de decision.

DETERMINISTA por construccion, y no es un detalle: la inicializacion es
farthest-first sobre las wallets ordenadas por clave, no aleatoria. Correr el
clustering dos veces sobre los mismos datos tiene que dar exactamente lo mismo,
o la "evolucion de clusters" del dashboard estaria enseñando ruido de
inicializacion y pareceria que las wallets migran solas.

Sin numpy (ver `stats.py`): con unos cientos de wallets y 19 dimensiones, k-means
en listas de Python cuesta milisegundos.
"""

from __future__ import annotations

from typing import Sequence

from medusa.intelligence.wallet.stats import mean
from medusa.intelligence.wallet.types import DNA_FEATURES, PopulationStats, WalletDNA

__all__ = ["cluster_wallets", "kmeans", "sq_distance"]


def sq_distance(a: Sequence[float], b: Sequence[float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b))


def _farthest_first(vectors: list[list[float]], k: int) -> list[list[float]]:
    """Semillas deterministas: la primera wallet, y luego la mas lejana a las ya
    elegidas. Es k-means++ sin el sorteo -- misma idea (separar las semillas),
    cero aleatoriedad."""
    seeds = [list(vectors[0])]
    while len(seeds) < k:
        best_idx, best_dist = 0, -1.0
        for i, v in enumerate(vectors):
            d = min(sq_distance(v, s) for s in seeds)
            if d > best_dist:
                best_idx, best_dist = i, d
        if best_dist <= 0:
            break            # todos los puntos ya coinciden con una semilla
        seeds.append(list(vectors[best_idx]))
    return seeds


def kmeans(
    vectors: list[list[float]], k: int, *, max_iter: int = 50, tol: float = 1e-6,
) -> tuple[list[int], list[list[float]], int]:
    """k-means clasico. Devuelve (asignaciones, centroides, iteraciones).

    Un cluster que se queda vacio CONSERVA su centroide en vez de re-sembrarse
    al azar: re-sembrar rompería el determinismo, que es la propiedad por la que
    este modulo no usa la implementacion de una libreria.
    """
    if not vectors or k <= 0:
        return [], [], 0
    k = min(k, len(vectors))
    centroids = _farthest_first(vectors, k)
    k = len(centroids)
    assign = [0] * len(vectors)
    iterations = 0

    for iterations in range(1, max_iter + 1):
        moved = False
        for i, v in enumerate(vectors):
            best, best_d = 0, float("inf")
            for c, centroid in enumerate(centroids):
                d = sq_distance(v, centroid)
                if d < best_d:
                    best, best_d = c, d
            if assign[i] != best:
                assign[i] = best
                moved = True

        shift = 0.0
        for c in range(k):
            members = [v for v, a in zip(vectors, assign) if a == c]
            if not members:
                continue     # cluster vacio: se conserva el centroide
            new_centroid = [mean(col) for col in zip(*members)]
            shift = max(shift, sq_distance(new_centroid, centroids[c]))
            centroids[c] = new_centroid

        if not moved and shift <= tol:
            break
    return assign, centroids, iterations


def cluster_wallets(
    dnas: Sequence[WalletDNA], pop: PopulationStats, *, k: int = 5,
    min_wallets: int = 6,
) -> dict:
    """Agrupa wallets por ADN estandarizado.

    Devuelve `{"k": ..., "assignments": {wallet: cluster}, "clusters": [...]}`.
    Con menos de `min_wallets` no se agrupa NADA: partir 4 wallets en 5 grupos
    produce grupos de uno, que no describen ningun patron y engordan el
    dashboard con ruido.
    """
    if len(dnas) < max(min_wallets, 2) or k < 2:
        return {"k": 0, "assignments": {}, "clusters": [], "reason": "muestra insuficiente"}

    ordered = sorted(dnas, key=lambda d: d.wallet)
    vectors = [pop.standardize(d) for d in ordered]
    assign, centroids, iterations = kmeans(vectors, k)

    clusters: list[dict] = []
    for c, centroid in enumerate(centroids):
        members = [d.wallet for d, a in zip(ordered, assign) if a == c]
        if not members:
            continue
        # Las metricas que MAS separan a este cluster de la media de la
        # poblacion. En z: el centroide ya esta estandarizado, asi que su propia
        # coordenada ES la separacion. Numeros, sin nombre de fantasia.
        top = sorted(
            ({"feature": name, "z": round(value, 4)}
             for name, value in zip(DNA_FEATURES, centroid)),
            key=lambda r: abs(r["z"]), reverse=True,
        )[:5]
        clusters.append({
            "cluster": c,
            "size": len(members),
            "share": round(len(members) / len(ordered), 4),
            "centroid": {n: round(v, 6) for n, v in zip(DNA_FEATURES, centroid)},
            "separating_features": top,
            "members": members[:50],
        })
    clusters.sort(key=lambda r: r["size"], reverse=True)
    return {
        "k": len(clusters),
        "iterations": iterations,
        "n_wallets": len(ordered),
        "assignments": {d.wallet: a for d, a in zip(ordered, assign)},
        "clusters": clusters,
    }
