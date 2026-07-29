"""WALLET SIMILARITY: parecido entre wallets por su ADN estandarizado.

Coseno sobre el vector z, no distancia euclidea: interesa el PERFIL (en que
direccion se desvia una wallet de la media de la poblacion), no su magnitud. Dos
wallets que hacen lo mismo, una con el doble de intensidad, son parecidas -- y
el coseno lo dice mientras que la euclidea las separaria.

Que esto NO es: una señal de "haz lo que hace tu parecida". Es una relacion
entre perfiles. El paquete entero no tiene forma de convertirla en una orden.
"""

from __future__ import annotations

import math
from typing import Sequence

from medusa.intelligence.wallet.types import PopulationStats, WalletDNA

__all__ = ["cosine", "similar_wallets", "similarity_edges"]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Coseno en [-1, 1]. 0.0 si algun vector es nulo.

    Un vector nulo es una wallet exactamente en la media de la poblacion en TODO:
    no apunta a ningun sitio, asi que no se parece a nadie en particular. Devolver
    0 es la respuesta honesta; devolver 1 (como haria una implementacion
    descuidada con 0/0) la haria parecida a todo el mundo.
    """
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return max(-1.0, min(1.0, dot / (na * nb)))


def similar_wallets(
    target: str, dnas: Sequence[WalletDNA], pop: PopulationStats, *,
    limit: int = 10, min_similarity: float = 0.0,
) -> list[dict]:
    """Wallets mas parecidas a `target`, de mayor a menor coseno."""
    by_wallet = {d.wallet: d for d in dnas}
    if target not in by_wallet:
        return []
    tv = pop.standardize(by_wallet[target])
    out: list[dict] = []
    for d in dnas:
        if d.wallet == target:
            continue
        sim = cosine(tv, pop.standardize(d))
        if sim >= min_similarity:
            out.append({"wallet": d.wallet, "similarity": round(sim, 6),
                        "n_closed": d.n_closed})
    out.sort(key=lambda r: r["similarity"], reverse=True)
    return out[:limit]


def similarity_edges(
    dnas: Sequence[WalletDNA], pop: PopulationStats, *, min_similarity: float = 0.7,
    top_k: int = 5,
) -> list[dict]:
    """Pares parecidos de toda la poblacion, listos para persistir.

    Cada par aparece UNA sola vez (a < b): la similitud es simetrica y guardar
    las dos direcciones duplicaria el grafo y doblaria cualquier recuento.
    `top_k` acota cuantos vecinos se guardan por wallet, que es lo que impide
    que el resultado crezca como O(n^2) con poblaciones grandes.
    """
    ordered = sorted(dnas, key=lambda d: d.wallet)
    vectors = {d.wallet: pop.standardize(d) for d in ordered}
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for d in ordered:
        vecinos: list[tuple[str, float]] = []
        for other in ordered:
            if other.wallet == d.wallet:
                continue
            sim = cosine(vectors[d.wallet], vectors[other.wallet])
            if sim >= min_similarity:
                vecinos.append((other.wallet, sim))
        vecinos.sort(key=lambda r: r[1], reverse=True)
        for wallet, sim in vecinos[:top_k]:
            pair = (d.wallet, wallet) if d.wallet < wallet else (wallet, d.wallet)
            if pair in seen:
                continue
            seen.add(pair)
            out.append({"wallet_a": pair[0], "wallet_b": pair[1],
                        "similarity": round(sim, 6)})
    out.sort(key=lambda r: r["similarity"], reverse=True)
    return out
