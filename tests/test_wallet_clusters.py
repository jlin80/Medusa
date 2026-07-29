"""Tests de clustering y similitud de wallets."""

from __future__ import annotations

import datetime as dt

import pytest

from medusa.intelligence.wallet.types import DNA_FEATURES, WalletDNA
from medusa.intelligence.wallet.wallet_clusters import cluster_wallets, kmeans, sq_distance
from medusa.intelligence.wallet.wallet_dna import population_stats
from medusa.intelligence.wallet.wallet_similarity import cosine, similar_wallets, similarity_edges

NOW = dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.timezone.utc)


def dna(wallet: str, **metrics) -> WalletDNA:
    base = {name: 0.0 for name in DNA_FEATURES}
    base.update(metrics)
    return WalletDNA(wallet=wallet, metrics=base, n_closed=40, n_positions=40, ts=NOW)


def dos_grupos() -> list[WalletDNA]:
    """Dos familias claramente separadas: ROI alto/frecuencia baja frente a
    ROI bajo/frecuencia alta."""
    grupo_a = [dna(f"0xa{i}", roi_historical=0.5 + i / 100.0, sharpe=2.0,
                   trade_frequency=0.5) for i in range(5)]
    grupo_b = [dna(f"0xb{i}", roi_historical=-0.4 + i / 100.0, sharpe=-1.5,
                   trade_frequency=20.0) for i in range(5)]
    return grupo_a + grupo_b


# ------------------------------------------------------------------ kmeans --
def test_kmeans_separa_dos_nubes_evidentes():
    vectores = [[0.0, 0.0], [0.1, 0.1], [10.0, 10.0], [10.1, 9.9]]
    asign, centroides, _ = kmeans(vectores, 2)
    assert asign[0] == asign[1] and asign[2] == asign[3]
    assert asign[0] != asign[2]
    assert len(centroides) == 2


def test_kmeans_es_determinista():
    """Sin esto, el panel de evolucion enseñaria ruido de inicializacion y
    pareceria que las wallets migran solas."""
    vectores = [[float(i), float(i % 3)] for i in range(20)]
    a = kmeans(vectores, 4)
    b = kmeans(vectores, 4)
    assert a[0] == b[0] and a[1] == b[1]


def test_kmeans_con_k_mayor_que_los_puntos_se_recorta():
    asign, centroides, _ = kmeans([[0.0], [1.0]], 10)
    assert len(centroides) <= 2 and len(asign) == 2


def test_kmeans_con_entrada_vacia_no_explota():
    assert kmeans([], 3) == ([], [], 0)


def test_puntos_identicos_no_generan_semillas_falsas():
    asign, centroides, _ = kmeans([[1.0, 1.0]] * 5, 3)
    assert len(centroides) == 1 and set(asign) == {0}


def test_distancia_al_cuadrado():
    assert sq_distance([0.0, 0.0], [3.0, 4.0]) == pytest.approx(25.0)


# ---------------------------------------------------------------- clusters --
def test_muestra_pequena_no_se_agrupa():
    """Partir 4 wallets en 5 grupos produce grupos de uno: ruido en el panel."""
    pocas = [dna(f"0x{i}", roi_historical=float(i)) for i in range(4)]
    out = cluster_wallets(pocas, population_stats(pocas), k=5, min_wallets=6)
    assert out["k"] == 0 and out["clusters"] == []
    assert "insuficiente" in out["reason"]


def test_agrupa_las_dos_familias():
    ds = dos_grupos()
    out = cluster_wallets(ds, population_stats(ds), k=2, min_wallets=6)
    assert out["k"] == 2
    asign = out["assignments"]
    grupo_a = {asign[f"0xa{i}"] for i in range(5)}
    grupo_b = {asign[f"0xb{i}"] for i in range(5)}
    assert len(grupo_a) == 1 and len(grupo_b) == 1
    assert grupo_a != grupo_b


def test_el_cluster_es_un_entero_sin_etiqueta():
    """Ni 'smart money' ni 'ballenas': un entero y su centroide numerico."""
    ds = dos_grupos()
    out = cluster_wallets(ds, population_stats(ds), k=2, min_wallets=6)
    for c in out["clusters"]:
        assert isinstance(c["cluster"], int)
        assert set(c["centroid"]) == set(DNA_FEATURES)
        assert all(isinstance(v, float) for v in c["centroid"].values())
        assert "label" not in c and "name" not in c
        for feat in c["separating_features"]:
            assert set(feat) == {"feature", "z"}


def test_el_centroide_publica_lo_que_separa_al_grupo():
    ds = dos_grupos()
    out = cluster_wallets(ds, population_stats(ds), k=2, min_wallets=6)
    separadoras = {f["feature"] for c in out["clusters"] for f in c["separating_features"]}
    assert {"roi_historical", "trade_frequency", "sharpe"} & separadoras


def test_las_particiones_suman_la_poblacion():
    ds = dos_grupos()
    out = cluster_wallets(ds, population_stats(ds), k=3, min_wallets=6)
    assert sum(c["size"] for c in out["clusters"]) == len(ds)
    assert len(out["assignments"]) == len(ds)


def test_clustering_determinista_entre_pasadas():
    ds = dos_grupos()
    pop = population_stats(ds)
    a = cluster_wallets(ds, pop, k=3, min_wallets=6)
    b = cluster_wallets(ds, pop, k=3, min_wallets=6)
    assert a["assignments"] == b["assignments"]


# -------------------------------------------------------------- similitud --
def test_coseno_basico():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_vector_nulo_no_se_parece_a_todo_el_mundo():
    """Una wallet exactamente en la media no apunta a ningun sitio."""
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_similares_ordenados_y_sin_incluirse_a_si_misma():
    ds = dos_grupos()
    pop = population_stats(ds)
    vecinos = similar_wallets("0xa0", ds, pop, limit=4)
    assert "0xa0" not in [v["wallet"] for v in vecinos]
    assert all(a["similarity"] >= b["similarity"] for a, b in zip(vecinos, vecinos[1:]))
    # Sus parecidas son de su propia familia.
    assert vecinos[0]["wallet"].startswith("0xa")


def test_wallet_desconocida_no_tiene_similares():
    ds = dos_grupos()
    assert similar_wallets("0xzz", ds, population_stats(ds)) == []


def test_los_pares_no_se_duplican():
    """La similitud es simetrica: guardar las dos direcciones doblaria todo."""
    ds = dos_grupos()
    pares = similarity_edges(ds, population_stats(ds), min_similarity=-1.0, top_k=10)
    claves = [(p["wallet_a"], p["wallet_b"]) for p in pares]
    assert len(claves) == len(set(claves))
    assert all(a < b for a, b in claves)


def test_el_umbral_filtra_y_top_k_acota():
    ds = dos_grupos()
    pop = population_stats(ds)
    todos = similarity_edges(ds, pop, min_similarity=-1.0, top_k=10)
    estrictos = similarity_edges(ds, pop, min_similarity=0.9, top_k=10)
    assert len(estrictos) < len(todos)
    assert all(p["similarity"] >= 0.9 for p in estrictos)


def test_las_similares_van_de_mayor_a_menor():
    ds = dos_grupos()
    pares = similarity_edges(ds, population_stats(ds), min_similarity=-1.0, top_k=3)
    assert all(a["similarity"] >= b["similarity"] for a, b in zip(pares, pares[1:]))
