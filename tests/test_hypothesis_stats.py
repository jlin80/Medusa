"""Estadistica del HE, con numeros escritos a mano.

Lo que se fija aqui no es "que la formula sea la de Wikipedia": es que las
decisiones que hacen honesto al motor sigan en pie. Sobre todo tres:

  - los empates se promedian (si no, la correlacion depende del orden en que
    Postgres devolvio las filas),
  - los intervalos se quedan dentro del rango de la magnitud (Fisher),
  - la correccion por multiplicidad esta activa y es la de Benjamini-Hochberg.
"""

from __future__ import annotations

import math

import pytest

from medusa.intelligence.hypothesis import stats


# ------------------------------------------------------------------ rangos ----
def test_los_empates_se_promedian():
    # [10, 20, 20, 30] -> el bloque de dos 20 comparte el rango medio (2+3)/2.
    assert stats.ranks([10, 20, 20, 30]) == [1.0, 2.5, 2.5, 4.0]


def test_el_orden_de_llegada_no_cambia_la_correlacion_con_empates():
    """El fallo que evita promediar los empates: si los rangos se asignaran por
    orden de llegada, permutar filas empatadas moveria rho."""
    xs = [1, 1, 1, 2, 2, 3]
    ys = [5, 7, 6, 9, 8, 12]
    a = stats.spearman(xs, ys)
    # Se permutan las tres primeras (todas con x=1) junto con sus y.
    b = stats.spearman([1, 1, 1, 2, 2, 3], [6, 5, 7, 8, 9, 12])
    assert a == pytest.approx(b, abs=1e-12)


def test_una_serie_constante_no_correlaciona_con_nada():
    """Y no lanza: una feature constante en una ventana es un caso normal."""
    assert stats.spearman([3, 3, 3, 3, 3], [1, 2, 3, 4, 5]) == 0.0


def test_spearman_ve_lo_monotono_no_lineal():
    """La razon de usar rangos: una relacion monotona pero muy curva sale 1.0."""
    xs = [1, 2, 3, 4, 5, 6]
    ys = [1, 4, 9, 100, 10_000, 10 ** 8]
    assert stats.spearman(xs, ys) == pytest.approx(1.0)


def test_spearman_detecta_el_sentido_inverso():
    assert stats.spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)


# ----------------------------------------------------------------- Fisher -----
def test_el_intervalo_de_fisher_se_queda_dentro_del_rango():
    """Con rho pegado a 1 y n pequeño, un intervalo normal se saldria de [-1,1] y
    prometeria una cota imposible."""
    est = stats.correlation_estimate([1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6])
    assert -1.0 <= est["lower"] <= est["upper"] <= 1.0
    assert est["effect"] == pytest.approx(1.0)


def test_con_muestra_ridicula_no_se_estima_nada():
    """n<5: Fisher pide n-3 y devolver "algo" seria inventarse la precision."""
    est = stats.correlation_estimate([1, 2, 3], [3, 2, 1])
    assert est["effect"] == 0.0
    assert est["p_value"] == 1.0


def test_mas_muestra_estrecha_el_intervalo():
    corta = stats.correlation_estimate(list(range(10)), list(range(10)))
    larga = stats.correlation_estimate(list(range(200)), list(range(200)))
    assert (larga["upper"] - larga["lower"]) < (corta["upper"] - corta["lower"])


def test_sin_asociacion_el_intervalo_cruza_el_cero():
    xs = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    ys = [5, 3, 8, 1, 9, 2, 7, 4, 10, 6]
    est = stats.correlation_estimate(xs, ys)
    assert est["lower"] < 0.0 < est["upper"]
    assert est["p_value"] > 0.05


# ------------------------------------------------------------------ Welch -----
def test_la_diferencia_va_estandarizada():
    """El efecto es en desviaciones tipicas, para que un contraste sobre ROI y
    otro sobre segundos se puedan comparar y ordenar juntos."""
    grupo = [10.0, 11.0, 9.0, 10.5, 9.5] * 6
    resto = [12.0, 13.0, 11.0, 12.5, 11.5] * 6
    est = stats.standardized_difference(grupo, resto)
    # Las medias distan 2 y la sd combinada es ~0.72 -> d en torno a -2.8.
    assert est["effect"] < -2.0
    assert est["upper"] < 0.0, "el intervalo tiene que excluir el cero"


def test_escalar_las_unidades_no_cambia_el_efecto_estandarizado():
    """La propiedad que hace comparables los contrastes: multiplicar la columna
    por mil no puede cambiar el tamaño del efecto."""
    a = [1.0, 2.0, 3.0, 4.0, 5.0] * 5
    b = [3.0, 4.0, 5.0, 6.0, 7.0] * 5
    base = stats.standardized_difference(a, b)["effect"]
    escalado = stats.standardized_difference(
        [x * 1000 for x in a], [x * 1000 for x in b])["effect"]
    assert base == pytest.approx(escalado)


def test_un_grupo_diminuto_no_produce_contraste():
    assert stats.standardized_difference([1.0], list(range(50)))["effect"] == 0.0


def test_dos_grupos_constantes_no_tienen_escala_en_la_que_medirse():
    """Difieren, pero sin dispersion no hay tamaño de efecto que publicar."""
    est = stats.standardized_difference([5.0] * 20, [7.0] * 20)
    assert est["effect"] == 0.0


# ---------------------------------------------------- Benjamini-Hochberg ------
def test_bh_no_acepta_nada_cuando_todo_es_ruido():
    """400 contrastes con p uniformes: al 5% "crudo" pasarian ~20."""
    ps = [(i + 0.5) / 400 for i in range(400)]
    aceptados = stats.benjamini_hochberg(ps, 0.05)
    assert sum(aceptados) <= 1, sum(aceptados)


def test_bh_conserva_lo_que_de_verdad_destaca():
    ps = [1e-9, 1e-8, 1e-7] + [(i + 0.5) / 100 for i in range(97)]
    aceptados = stats.benjamini_hochberg(ps, 0.05)
    assert aceptados[0] and aceptados[1] and aceptados[2]


def test_bh_es_menos_severo_que_bonferroni():
    """La razon de elegir BH: Bonferroni con 100 contrastes exige p<0.0005 y
    mataria hallazgos reales."""
    ps = [0.0004] * 3 + [0.5] * 97
    aceptados = stats.benjamini_hochberg(ps, 0.05)
    assert sum(aceptados) == 3


def test_bh_respeta_el_orden_de_entrada():
    ps = [0.9, 1e-9, 0.8]
    assert stats.benjamini_hochberg(ps, 0.05) == [False, True, False]


def test_bh_con_lista_vacia():
    assert stats.benjamini_hochberg([], 0.05) == []


# -------------------------------------------------------------- confianza -----
def test_sin_muestra_no_hay_confianza():
    assert stats.evidence_confidence(0.9, 0, 0.5, 60) == 0.0


def test_un_efecto_enorme_con_muestra_ridicula_no_llega_arriba():
    """El peso por muestra es lo que impide que 3 observaciones perfectas
    parezcan una conclusion."""
    poca = stats.evidence_confidence(1.0, 3, 0.5, 60)
    mucha = stats.evidence_confidence(1.0, 600, 0.5, 60)
    assert poca < 0.1 < mucha
    assert mucha > 0.85


def test_con_la_muestra_minima_el_techo_es_la_mitad():
    """Propiedad declarada del peso n/(n+min): con n = min_samples vale 0.5."""
    assert stats.evidence_confidence(10.0, 60, 0.5, 60) == pytest.approx(0.5)


def test_la_confianza_esta_acotada_y_es_monotona():
    previa = -1.0
    for n in (10, 50, 100, 500, 5000):
        c = stats.evidence_confidence(0.4, n, 0.5, 60)
        assert 0.0 <= c <= 1.0
        assert c > previa
        previa = c


def test_sin_efecto_sostenido_la_confianza_es_cero():
    """`magnitude_lower` vale 0 cuando el intervalo cruza el nulo: por muy grande
    que sea la muestra, sin signo determinado no hay confianza que reportar."""
    assert stats.evidence_confidence(0.0, 100_000, 0.5, 60) == 0.0


# ------------------------------------------------------------- utilidades -----
def test_la_mediana_aguanta_una_cola_larga():
    """Por que se usa mediana en los tiempos: un valor a las seis horas no puede
    desplazar el resumen."""
    valores = [10, 11, 12, 13, 21600]
    assert stats.median(valores) == 12
    assert stats.mean(valores) > 4000


def test_el_cuantil_interpola_y_se_recorta():
    valores = [0.0, 10.0]
    assert stats.quantile(valores, 0.5) == pytest.approx(5.0)
    assert stats.quantile(valores, -3) == 0.0
    assert stats.quantile(valores, 9) == 10.0


def test_el_valor_p_bilateral_es_coherente():
    assert stats.two_sided_p(0.0) == pytest.approx(1.0)
    assert stats.two_sided_p(1.959963985) == pytest.approx(0.05, abs=1e-4)
    assert stats.two_sided_p(-1.959963985) == pytest.approx(0.05, abs=1e-4)


def test_la_varianza_muestral_usa_n_menos_uno():
    assert stats.variance([2.0, 4.0]) == pytest.approx(2.0)
    assert stats.variance([5.0]) == 0.0
