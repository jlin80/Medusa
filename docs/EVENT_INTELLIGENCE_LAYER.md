# Medusa — Event Intelligence Layer (V4)

**Documento técnico de diseño. NO es código. NO propone reescritura.**
Evolución **incremental** de Medusa hacia un *Probability Intelligence Engine*.

- Fecha: 2026-07-23
- Autor: diseño técnico (para revisión antes de implementar)
- Ámbito: Polymarket, ejecución SIEMPRE y SOLO en Polymarket, PAPER por defecto.
- Regla madre: **nada de lo existente se reescribe, se rompe ni se elimina.**
  Cada pieza nueva es aditiva, opt-in y con default seguro (apagado).

---

## 0. Tesis del rediseño

Los datos del propio proyecto (sección 3 de `medusa.txt`) ya demostraron, con
muestras y contrastes de hipótesis, que **el edge NO está en el precio**:

| Vía examinada            | Veredicto | Evidencia |
|--------------------------|-----------|-----------|
| Nivel de precio (calibración) | ✗ sin edge | 0/7 y 0/6 cubos con sesgo significativo (Wilson) |
| Momentum (Δ precio)      | ✗ sin edge | delta estratificado +0.012, z=+0.17, IC95 [-0.13,+0.16] |
| Order-book imbalance     | ✗ sin edge | no discrimina resultado tras costes |
| Arbitraje intra-mercado  | ✗ sin edge | 110 mercados, 0 oportunidades, min ask_YES+ask_NO = $1.001 |

**Conclusión asumida como axioma de este diseño:** el precio de Polymarket ya
representa correctamente la probabilidad. El objetivo deja de ser *predecir el
precio* y pasa a ser **estimar la probabilidad REAL de resolución mejor que el
mercado**, usando información **externa al precio** (no necesariamente externa a
Polymarket). El trade es una consecuencia, no el objetivo.

El listón cuantitativo a batir está medido: una ida y vuelta sin movimiento
pierde **~2.2%** (peaje). Cualquier señal externa debe superar ese coste
**out-of-sample**, no in-sample.

---

## 1. Principios arquitectónicos (heredados, no negociables)

Estos principios YA existen en Medusa y este diseño los respeta al pie de la letra:

1. **Un módulo de inteligencia produce FEATURES, nunca decisiones.**
   (`IntelligenceModule.compute() -> list[Feature]`, `Feature.value` siempre float,
   lo textual va en `meta` y jamás llega al camino de decisión.)
2. **El layer no puede bloquear el trading.** Loop propio, `wait_for(timeout)`,
   try/except por módulo, lista blanca por nombre.
3. **Las features se leen del Feature Store en el ciclo, no se calculan.** Si el
   store falla, las estrategias reciben `{}` y siguen. La ausencia nunca degrada.
4. **Triple candado para operar:** fase (research→shadow→paper→live_candidate) +
   señal ejecutable + peso del asignador > 0.
5. **Shadow-first + medición honesta:** doble cota maker/taker, manda la pesimista
   (TAKER). El histórico de `strategy_signals` resueltas 1/0 ES el mecanismo de
   descubrimiento.
6. **Aritmética pura de Python donde se pueda** (CPU Bobcat sin SSE4.2; numpy 2.x
   aborta; catboost descartado; LightGBM viable con numpy<2 + libgomp1).

**El Event Intelligence Layer es una extensión natural del Intelligence Layer V3
ya desplegado**, no un sistema nuevo. Reutiliza `IntelligenceModule`, el
`IntelligenceRunner`, el Feature Store y el ciclo de vida de fases sin tocarlos.

---

## 2. Arquitectura objetivo (visión de conjunto)

```
                    ┌─────────────────────────────────────────────────────┐
                    │  EVENT INTELLIGENCE LAYER  (loop propio, desacoplado) │
                    │  IntelligenceRunner  (YA EXISTE, sin cambios)         │
                    │                                                       │
  Fuentes externas  │  Módulos nuevos (cada uno = IntelligenceModule):      │
  ─────────────────>│   • news_event      -> features de evento/noticia     │
   (news API, Data  │   • wallet          -> features de reputación wallet   │──┐
    API PM, RSS…)   │   • cross_market    -> features de relación entre mkts │  │
                    │   • temporal        -> features de dinámica temporal    │  │
                    │   • context_memory  -> features de memoria por categoría│  │
                    └─────────────────────────────────────────────────────┘  │
                                          │ list[Feature]                     │
                                          v                                    │
                    ┌─────────────────────────────────────────────────────┐  │
                    │  FEATURE STORE  (tabla `features`, YA EXISTE)         │<─┘
                    │  append-only, (market_id,name,ts DESC)                │
                    └─────────────────────────────────────────────────────┘
                                          │ latest_features()  (en el ciclo)
                                          v
   ┌──────────────────────────────────────────────────────────────────────────┐
   │  CICLO DE TRADING (cada 60s, SIN CAMBIOS en su estructura)                 │
   │                                                                            │
   │  Estrategias existentes ─┐                                                 │
   │  (momentum, wallet…) cada│  P_i, conf_i, expl_i                            │
   │   una produce StrategySig│                                                 │
   │                          v                                                 │
   │                   ┌──────────────┐   P_final + incertidumbre               │
   │                   │  META MODEL  │──────────────────────┐                  │
   │                   │ (nueva Strat)│  (aprende a quién     │                  │
   │                   └──────────────┘   creer por régimen)  v                  │
   │                          │              ┌─────────────────────────┐         │
   │                          │              │ CALIBRATION (aplica el   │         │
   │                          │              │ calibrador activo)       │         │
   │                          v              └─────────────────────────┘         │
   │                 AllocationManager (+ prior bayesiano opt-in)                │
   │                          v                                                  │
   │                 RiskManager (consume incertidumbre)                         │
   │                          v                                                  │
   │                 ExecutionAdapter (paper|live)  ← SIN CAMBIOS                 │
   └──────────────────────────────────────────────────────────────────────────┘

   ┌──────────────────────────────────────────────────────────────────────────┐
   │  CICLO NOCTURNO OFFLINE (nuevo loop, NO toca el trading)                   │
   │  resolver señales → construir dataset → entrenar → walk-forward →          │
   │  validar (gauntlet) → registrar en MODEL REGISTRY → recalibrar →           │
   │  activar SOLO si mejora out-of-sample (fail-closed, humano opcional)       │
   └──────────────────────────────────────────────────────────────────────────┘
```

Todo lo que hay a la derecha de "latest_features()" ya existe. Lo nuevo:
5 módulos de features externas, 1 MetaModel (que es una `Strategy` más), y
un ciclo nocturno offline que **nunca toca el runtime de trading**.

---

## 3. Módulos nuevos (uno por fichero; el núcleo no cambia)

### 3.1 Event Intelligence — módulos de features (contexto `medusa/intelligence_layer/`)

Cada uno hereda de `IntelligenceModule`, devuelve `list[Feature]`, se activa por
nombre en `INTELLIGENCE_MODULES`, tiene su propio `interval` y `timeout`. Ninguno
recibe adapter/repo/engine: por construcción no pueden operar.

#### a) `news_event.py` — EVENT FEATURES
Fuente externa (news API / RSS / Data API de Polymarket para metadatos del evento).
Produce (todas float, texto solo en `meta`):
- `news_recency` — antigüedad de la noticia más reciente relevante (horas, normalizada).
- `news_velocity` — nº de noticias nuevas por hora (aceleración de cobertura).
- `news_sentiment` — sentimiento agregado [-1,1] (léxico ligero; NO LLM en el path).
- `news_consensus` — dispersión de sentimiento entre fuentes (acuerdo).
- `event_importance` — importancia del evento (volumen + nº fuentes + tier de fuente).
- `time_to_close` — proximidad al cierre (ya derivable; se centraliza aquí).
- `event_recent_change` — nº de cambios materiales recientes en el evento.

> **Regla dura anti-LLM:** el titular/resumen va en `meta`, jamás en `value`. Un
> LLM puede *poblar* `meta` offline, pero nunca decide: sólo los escalares entran
> al camino de decisión (mismo candado que ya protege el sistema).

#### b) `wallet.py` — WALLET FEATURES
Data API de Polymarket (sin claves). Produce, por mercado, agregando las wallets
posicionadas en él:
- `wallet_reputation` — reputación media ponderada de las wallets en el lado YES/NO.
- `wallet_roi_hist` — ROI histórico medio de esas wallets.
- `wallet_specialty_match` — cuánto coincide la categoría del mercado con la
  especialidad de las wallets presentes.
- `wallet_sharpe` — Sharpe histórico agregado.
- `wallet_drawdown` — drawdown típico agregado.
- `wallet_hit_rate` — hit rate histórico agregado.
- `wallet_volume` — volumen histórico agregado (tamaño/credibilidad).
- `smart_money_score` — score compuesto (reputación×sharpe×hit_rate×especialidad).
- `whale_concentration` — concentración (Herfindahl) del capital en pocas wallets.
- `wallet_consensus` — grado de acuerdo direccional entre wallets "smart".

> **No es copy-trading.** Produce una feature, jamás una orden. El "smart money"
> es un *prior*, no un gatillo.

#### c) `cross_market.py` — MARKET RELATIONSHIP FEATURES
Relaciones entre mercados del universo ya escaneado (coste de red ≈ 0: reutiliza
lo que el escáner ya trajo).
- `related_markets_count` — nº de mercados relacionados (por entidad/keyword/evento).
- `cross_correlation` — correlación de precio con mercados relacionados.
- `event_dependency` — dependencia lógica (mismo evento madre / condicionales).
- `contradiction_score` — inconsistencia lógica entre mercados (Σ probs > 1 en
  mutuamente excluyentes; YES aquí implica NO allá y no cuadra).
- `info_propagation` — retardo de propagación: un mercado ya movió, este aún no.

> `contradiction_score` e `info_propagation` son las dos con mayor potencial de
> edge REAL: capturan información que el precio de ESTE mercado todavía no refleja.

#### d) `temporal.py` — TEMPORAL FEATURES
Dinámica temporal del propio mercado (usa histórico CLOB ya cacheado).
- `move_velocity` — velocidad de cambio de precio (1ª derivada).
- `move_acceleration` — aceleración (2ª derivada).
- `price_stability` — estabilidad (inversa de la varianza reciente).
- `regime_change` — score de cambio de régimen (ruptura vs base 48h).
- `uncertainty_temporal` — incertidumbre residual del proceso (ancho de banda).

> Ojo: estas son *dinámicas del precio*, no niveles. El veredicto de calibración
> sigue en pie; se incluyen porque **modulan la confianza** del resto de features
> (un evento cambiando de régimen hace las noticias más informativas), no porque
> el movimiento por sí solo tenga edge (ya se descartó).

#### e) `context_memory.py` — CONTEXT MEMORY (features)
Lee la memoria por categoría (tabla `category_memory`, §4) y la expone como
features para que el MetaModel la use como prior:
- `cat_typical_volatility` — volatilidad típica de la categoría.
- `cat_typical_reaction_time` — tiempo típico de reacción a eventos.
- `cat_source_importance` — importancia media de las fuentes de la categoría.
- `cat_hist_calibration` — cómo de calibrado ha estado el mercado en esta categoría.

### 3.2 `medusa/meta/` — META MODEL (contexto nuevo, pero es una `Strategy`)

**Punto de diseño más delicado.** El MetaModel NO es un decisor paralelo que se
salte el pipeline. Es **una `Strategy` más** (hereda de `Strategy`, produce
`StrategySignal`), de modo que pasa por el MISMO triple candado (fase +
ejecutable + peso del asignador). Así se garantiza que **no puede bypassear el
Risk Manager**: es estructuralmente incapaz, igual que el resto.

Entradas del MetaModel (todas ya disponibles, sin red nueva):
- Las señales de las demás estrategias en ese mercado: `P_i, conf_i, expl_i`.
- Las features del Feature Store (`MarketContext.features`), incluidas las nuevas.
- El régimen del mercado (categoría + `regime_change` + `time_to_close`).

Salida: un `StrategySignal` con:
- `signal_prob` = P_final combinada.
- `confidence` = confianza combinada.
- `explanation` = qué estrategias/feature pesaron y por qué (auditable).
- Campos de **incertidumbre** nuevos (§3.4).

Mecánica del "aprende a quién creer según el tipo de mercado":
- **Fase 1 (sin ML, pure Python):** combinación log-lineal ponderada
  (pooling logarítmico de probabilidades) con pesos por (estrategia, régimen)
  derivados del rendimiento histórico calibrado (reutiliza `strategy_performance`).
  Es un *stacking* explicable y baratísimo.
- **Fase 2 (opcional, ML):** stacker LightGBM entrenado offline y exportado a
  **inferencia en Python puro** (recorrer árboles y sumar — microsegundos para
  15 mercados/ciclo, sin numpy en el runtime). Sólo se activa si supera a la
  Fase 1 out-of-sample en el gauntlet.

> El MetaModel arranca en fase `shadow` (registra, no opera) como cualquier
> estrategia. Se gana el paso a `paper`/`live_candidate` con datos.

### 3.3 `medusa/calibration/` — CALIBRATION ENGINE (offline)

Corre en el ciclo nocturno. Sobre las señales `strategy_signals` resueltas 1/0:
- Calcula **Reliability Diagram**, **Brier Score**, **ECE**, **MCE**,
  **Calibration Curve** por estrategia, por categoría y para el MetaModel.
- Ajusta un **calibrador** (isotónico o Platt/logístico — ambos pure Python,
  sin numpy) cuando ECE supera un umbral (`CALIB_ECE_THRESHOLD`).
- Publica el calibrador en el Model Registry como artefacto versionado.
- En el ciclo de trading, la P del MetaModel pasa por el **calibrador activo**
  antes de llegar al asignador. Si no hay calibrador, se usa identidad (no
  degrada nada).

> Ejemplo del enunciado: si el modelo dice 90% pero acierta 76% histórico, el
> calibrador isotónico mapea 0.90→0.76 automáticamente. **No se inflan
> probabilidades.** El calibrador es un artefacto DATO, versionado y con rollback.

### 3.4 UNCERTAINTY — no es un módulo, es un contrato ampliado

Se **añaden campos** a `StrategySignal` (aditivos, con default neutro):
- `ci_low`, `ci_high` — intervalo de confianza de la probabilidad.
- `variance` — varianza de la estimación.
- `n_samples` — nº de muestras que respaldan la estimación.
- `model_quality` — calidad del modelo activo (del registry: Brier/ECE recientes).
- `freshness` — antigüedad de las features usadas (segundos → score).
- `data_quality` — completitud de las features (cuántas de las esperadas había).

El **Risk Manager consume la incertidumbre**: a mayor incertidumbre, menor sizing
(o skip). Esto es aditivo — si los campos vienen con default neutro (CI amplio,
quality=1), el Risk Manager se comporta EXACTAMENTE como hoy.

### 3.5 `medusa/registry/` — MODEL REGISTRY (append-only, con rollback)

Tabla `model_registry`. Cada modelo (MetaModel entrenado, calibrador, o config de
pesos) tiene: `id`, `created_at`, `kind` (meta|calibrator|weights|updown),
`dataset_hash`, `feature_set` (JSON), `params` (JSON/artefacto), y métricas
`roi`, `sharpe`, `brier`, `ece`, `calibration`, `win_rate`, `profit_factor`.
Estados: **active / candidate / historical**. Un solo `active` por `kind`.
`activate(id)` y `rollback()` son operaciones atómicas (cambian el puntero, no
borran nada: append-only, como el Feature Store).

### 3.6 `medusa/learning/` — ONLINE LEARNING (scheduler nocturno)

Un loop nuevo (`_learning_loop`, gemelo de `_intelligence_loop`) que corre cada
noche (`LEARNING_INTERVAL`, default 24h) y **jamás toca el ciclo de trading**:

```
1. resolver señales pendientes (reutiliza resolve_signals_for_market)
2. construir dataset (features del store + label 1/0 del resultado real)
3. entrenar candidato (MetaModel Fase1 pesos, o Fase2 LightGBM offline)
4. walk-forward (ventanas expansivas; nunca mirar el futuro)
5. validar en gauntlet: ¿mejora Brier/logloss OUT-OF-SAMPLE vs modelo activo
   Y vs baseline precio-solo, tras el peaje de 2.2%?
6. registrar candidato en model_registry (kind=meta/calibrator)
7. recalibrar (calibration engine)
8. si mejora de forma estadísticamente significativa -> activar automáticamente;
   si no -> queda como 'candidate', se notifica a Discord, humano decide.
```

**Fail-closed:** si cualquier paso falla, el modelo activo NO cambia. El default
de `LEARNING_AUTO_ACTIVATE` es `false` (activación manual) hasta que el propio
proceso acumule confianza; luego se puede poner `true`.

### 3.7 BAYESIAN ALLOCATION — evolución de `AllocationManager` (aditiva)

Hoy: gate duro `n >= 30` + cota inferior frecuentista del ROI. Problema: hay que
esperar semanas para empezar a aprender.

Nuevo modo **opt-in** `ALLOC_MODE=bayesian` (default sigue siendo `frequentist`,
idéntico a hoy):
- **Prior Beta-Binomial** para win-rate por (estrategia, categoría), con prior
  informado por `category_memory` (§3.1e) — no arranca de cero.
- **Posterior actualizado por evidencia:** con pocas muestras, el posterior está
  dominado por el prior → capital mínimo, no cero. Conforme llegan resoluciones,
  el posterior se estrecha → exposición crece suavemente.
- El sizing sale de un **cuantil conservador** del posterior (p.ej. percentil 10),
  no de la media: mismo espíritu que la cota inferior frecuentista actual.
- Pure Python (funciones Beta/Gamma vía `math.lgamma`; sin scipy/numpy).

> Nunca esperar semanas para empezar a aprender, pero **jamás** relajar los
> límites duros del Risk Manager: el peso sigue siendo sólo un multiplicador de
> sizing en `[0, ALLOC_MAX_WEIGHT]`.

### 3.8 `medusa/memory/` — CONTEXT MEMORY (persistencia)

Tabla `category_memory`. Un registro por categoría con: `history` (agregados
resueltos), `typical_volatility`, `important_sources` (JSON), `relevant_events`
(JSON), `typical_reaction_time`, `hist_calibration`. Se actualiza en el ciclo
nocturno. El módulo `context_memory.py` (§3.1e) la LEE y la expone como features.
Evita recalcular desde cero cada vez.

### 3.9 UP/DOWN — modelos de probabilidad como plugins (refactor aditivo)

`updown/model.py` ya es una función pura `fair_prob_up(...)`. Se **abstrae** tras
una interfaz `ProbabilityModel` con `fair_prob_up(s0, st, secs_left, params) ->
float`, y un registro `UPDOWN_MODEL` (default `brownian` = comportamiento actual,
bit a bit idéntico). Plugins intercambiables:

| Plugin           | Qué añade sobre Brownian |
|------------------|--------------------------|
| `brownian`       | actual (barrera de difusión sin deriva) — DEFAULT |
| `student_t`      | colas gruesas (saltos pequeños frecuentes) |
| `jump_diffusion` | saltos discretos (noticias intra-ventana) |
| `heston`         | volatilidad estocástica (vol no constante) |
| `regime_switch`  | conmutación de régimen (calma/estrés) |
| `evt`            | Extreme Value Theory para las colas extremas |

> **No se modifica el Browniano todavía.** Sólo se crea la arquitectura que
> permite cambiarlo por config y compararlos en el gauntlet. Un plugin nuevo se
> gana el puesto batiendo al Browniano en Brier out-of-sample sobre ventanas
> up/down resueltas. `assess()` y `EntryPolicy` no cambian: sólo cambia de dónde
> sale `fair_prob_up`.

---

## 4. Tablas nuevas (migraciones idempotentes, `ADD ... IF NOT EXISTS`)

Todas se crean con el mismo patrón ya usado (init_db idempotente; la BD del CT202
NO se resetea; sin pérdida de datos).

| Tabla              | Propósito | Filas/día aprox. | Poda |
|--------------------|-----------|------------------|------|
| `category_memory`  | memoria por categoría | ~10 filas totales (upsert) | no se poda |
| `model_registry`   | modelos/calibradores versionados | pocas/día | no se poda (append-only) |
| `calibration_reports` | reliability/Brier/ECE/MCE diarios | ~30/día | 365d |
| `training_runs`    | log de cada ciclo nocturno (walk-forward, gauntlet) | ~1/día | 365d |

Columnas nuevas (aditivas) en tablas existentes:
- `strategy_signals`: `ci_low`, `ci_high`, `variance`, `n_samples`, `model_quality`,
  `freshness`, `data_quality`, `meta_prob`, `calibrated_prob` (todas nullable).
- Ninguna columna existente se modifica ni se elimina.

El **Feature Store (`features`) no cambia de esquema**: las features nuevas son
sólo `name`s nuevos en la misma tabla (por eso el diseño de V3 la hizo genérica).

---

## 5. APIs nuevas (todas GET de lectura; el dashboard añade filas)

| Endpoint | Devuelve |
|----------|----------|
| `GET /intelligence/modules` | (ya existe) ahora lista también news/wallet/cross_market/temporal/context_memory |
| `GET /features/event?market_id=` | features de evento/wallet/cross/temporal de un mercado |
| `GET /meta/{market_id}` | descomposición del MetaModel: P_i por estrategia, pesos por régimen, P_final, incertidumbre |
| `GET /calibration` | último reliability diagram, Brier, ECE, MCE por estrategia/categoría |
| `GET /registry` | modelos active/candidate/historical con sus métricas |
| `POST /registry/activate/{id}` | (protegido, LAN) activar/rollback manual de un modelo |
| `GET /memory/categories` | memoria por categoría |
| `GET /learning/runs` | histórico de ciclos nocturnos (walk-forward, gauntlet, decisiones) |

Ningún endpoint existente cambia de contrato. El único no-GET (`activate`) es
opt-in y sólo LAN, como el resto del dashboard sin login.

---

## 6. Flujo de datos (dónde entra cada cosa)

**Ciclo de trading (60s) — estructura sin cambios, sólo lecturas nuevas:**
```
gestionar abiertos → scan → prescore → top-K deep → estrategias →
  [NUEVO] MetaModel (lee señales+features) → [NUEVO] calibrador activo →
  AllocationManager (frequentist|bayesian) → RiskManager (lee incertidumbre) →
  ExecutionAdapter (paper|live)
```

**Loop de inteligencia (propio, desacoplado) — módulos nuevos añadidos a la lista
blanca:** escriben features al store; el ciclo de trading las lee vía
`latest_features()`. Idéntico mecanismo que microstructure hoy.

**Loop nocturno (nuevo, offline) — NO toca el trading:** resolver → dataset →
entrenar → walk-forward → gauntlet → registry → recalibrar → activar-si-mejora.

Si CUALQUIER pieza nueva falla o se apaga, el flujo cae exactamente al de hoy:
MetaModel ausente → se usan las estrategias base; calibrador ausente → identidad;
features ausentes → `{}`; loop nocturno caído → el modelo activo sigue sirviendo.

---

## 7. Dependencias

| Componente | Dependencia | Runtime o offline | Nota hardware |
|------------|-------------|-------------------|---------------|
| news_event | cliente HTTP (httpx, ya está) + fuente news/RSS | runtime (loop propio) | red, no CPU |
| wallet, cross_market, temporal | ninguna nueva (httpx ya está) | runtime | pure Python |
| MetaModel Fase 1 | ninguna (pure Python) | runtime | trivial |
| MetaModel Fase 2 | LightGBM **sólo para entrenar** | **offline**; inferencia pure Python exportada | numpy<2 + libgomp1 SÓLO en imagen de entrenamiento |
| Calibration | pure Python (isotónica/Platt con math) | offline | trivial |
| Bayesian alloc | pure Python (`math.lgamma`) | runtime | trivial |
| Registry/memory | SQLAlchemy (ya está) | ambos | trivial |

**Decisión clave de hardware:** el runtime del engine (512 MB) **NO** carga
numpy/lightgbm. El entrenamiento LightGBM (si se llega a Fase 2) corre en un
contenedor de entrenamiento aparte (o fuera de la caja), y sólo se exporta el
ensemble a inferencia en Python puro. Coherente con la nota de `medusa.txt`:
"NO se han añadido numpy/lightgbm a la imagen… se añadirán el día que exista un
modelo, con numpy<2 y libgomp1", y sólo a la imagen que entrena.

---

## 8. Impacto sobre módulos existentes

| Módulo | Cambio | Tipo | Compatibilidad hacia atrás |
|--------|--------|------|----------------------------|
| `intelligence_layer/` | +5 módulos nuevos (ficheros) | aditivo | total: no se tocan base/runner |
| `strategies/base.py` | +campos de incertidumbre en `StrategySignal` con default neutro | aditivo | total: código viejo ignora los campos |
| `strategies/` | +`meta` como estrategia registrada en `build_default_strategies` (default fase shadow) | aditivo | total: en shadow no opera |
| `allocation/manager.py` | +modo `bayesian` detrás de `ALLOC_MODE` (default `frequentist` = hoy) | aditivo | total: default idéntico |
| `risk/` | lee incertidumbre si está; si no, comportamiento actual | aditivo | total |
| `updown/model.py` | abstracción `ProbabilityModel` + registro; `brownian` = default idéntico | refactor aditivo | total: mismo resultado bit a bit |
| `engine.py` | +`_learning_loop` (gemelo de `_intelligence_loop`), +llamada opcional MetaModel/calibrador en `_cycle` | aditivo | total: apagado por flags |
| `data/` | +tablas y +columnas nullable | aditivo (migración idempotente) | total: sin reset |
| `api/`, `dashboard/` | +endpoints GET y +filas | aditivo | total |

**Nada se reescribe. Nada se elimina. Nada rompe el `.env` desplegado.** Con
todos los flags nuevos en su default, Medusa se comporta EXACTAMENTE como hoy.

---

## 9. Coste computacional (CPU / RAM), medido contra el presupuesto real

Presupuesto: 2 vCPU Bobcat, 3 GB RAM sin swap, engine limitado a 512 MB, ciclo 60s.

| Pieza | CPU/ciclo | Red | RAM extra | Cuándo |
|-------|-----------|-----|-----------|--------|
| news_event | bajo (parseo léxico) | 1-2 req/interval (300-900s) | ~5 MB caché | loop propio |
| wallet | medio (agregación por mercado) | Data API, top-K | ~10 MB | loop propio |
| cross_market | bajo (reusa scan) | 0 | ~5 MB | loop propio |
| temporal | bajo (reusa histórico) | 0 | ~2 MB | loop propio |
| context_memory | trivial (lee tabla) | 0 | <1 MB | loop propio |
| MetaModel Fase1 | trivial (log-pooling de ~6 números × 15 mkts) | 0 | <1 MB | en ciclo |
| Calibrador (aplicar) | trivial (lookup isotónico) | 0 | <1 MB | en ciclo |
| Bayesian alloc | trivial (`lgamma`) | 0 | <1 MB | refresh throttled |
| **Loop nocturno** | **alto pero 1×/día, offline, no compite con el ciclo** | resolver | pico controlado | 03:00 local |

**Presupuesto de RAM total del layer online: < ~35 MB añadidos** sobre los ~162 MB
actuales → holgado bajo 512 MB. El coste alto (entrenamiento) se aísla al loop
nocturno y, si se usa LightGBM, a un contenedor de entrenamiento separado.

Salvaguardas de coste (heredadas): dos ritmos de escaneo, top-K profundo,
`interval`/`timeout` por módulo, features leídas-no-calculadas, throttling del
asignador. Si un módulo externo (news) se cuelga, el `wait_for` lo corta y el
resto sigue.

---

## 10. Riesgos y mitigaciones

| Riesgo | Severidad | Mitigación |
|--------|-----------|------------|
| Overfitting con muestra pequeña (el enemigo nº1) | Alta | walk-forward expansivo; gauntlet exige mejora OUT-OF-SAMPLE tras peaje; cuantil conservador del posterior; nunca activar por media |
| Un LLM/news decide "por la puerta de atrás" | Alta | texto sólo en `meta`, `value` siempre float; MetaModel es una Strategy sujeta al triple candado |
| Fuente de noticias mete look-ahead (timestamp del futuro) | Alta | sólo features con `ts <= ts_señal`; el resolver ya separa señal/resolución; auditar timestamps en el gauntlet |
| Probabilidades infladas | Media | Calibration Engine obligatorio; ECE monitorizado; recalibración automática |
| Módulo externo cuelga el layer | Media | `wait_for(timeout)`, try/except por módulo, lista blanca (mecanismo ya existe) |
| Activar un modelo peor automáticamente | Media | fail-closed; `LEARNING_AUTO_ACTIVATE=false` por defecto; rollback en el registry |
| Coste de red/API (rate limits) | Media | intervalos largos por módulo; caché; top-K; reusar lo ya traído |
| Wallet features = copy-trading encubierto | Media | es una feature, no una orden; ponderada, con `whale_concentration` como freno |
| RAM del engine (512 MB) | Media | numpy/lightgbm NUNCA en runtime; inferencia pure Python; entrenamiento aislado |
| Data quality baja envenena el store | Media | preferir lanzar a inventar (contrato ya existente); `data_quality` como feature que baja el sizing |

---

## 11. Plan de migración (sin downtime, sin reset)

1. **Migración de esquema idempotente** en `init_db` (crea tablas nuevas y añade
   columnas nullable). Verificado con el patrón ya usado en V2/V3 (7 columnas +
   tabla `strategy_signals` migradas solas sin reset). La BD del CT202 no se toca.
2. **Despliegue con TODOS los flags nuevos apagados** → Medusa se comporta
   idéntico a hoy. Verificar selftest + contabilidad cuadrada a $0.00.
3. **Activar módulos de features uno a uno** (`INTELLIGENCE_MODULES`) para empezar
   a **acumular histórico** (shadow puro, cero riesgo). Igual que se hizo con
   microstructure.
4. **Activar MetaModel en fase shadow** → registra P_final sin operar.
5. **Activar loop nocturno** en modo "sólo reporta" (`LEARNING_AUTO_ACTIVATE=false`)
   → genera calibraciones y candidatos, humano revisa.
6. Sólo cuando el gauntlet demuestre edge out-of-sample tras costes: promover el
   MetaModel a `paper` (nunca `live` sin las semanas de evidencia). Triple candado
   intacto.

Rollback en cada paso = apagar el flag. Ninguna fase es irreversible.

---

## 12. Orden exacto de implementación y prioridad

Ordenado por **(valor de edge esperado) × (coste bajo) / (riesgo)**. Cada fase es
desplegable y verificable por sí sola.

| # | Fase | Prioridad | Qué aporta | Bloqueante de |
|---|------|-----------|-----------|---------------|
| 1 | Esquema + registry + memory (tablas, sin lógica) | **P0** | cimientos; todo lo demás cuelga de aquí | todo |
| 2 | Uncertainty (campos en StrategySignal + consumo en Risk) | **P0** | honestidad estadística; barato; habilita el resto | 6,7 |
| 3 | `wallet.py` (features) | **P1** | primera info EXTERNA real, sin claves, sin coste de red alto; "smart money" es el candidato más fuerte a edge | 6 |
| 4 | `cross_market.py` (features) | **P1** | `contradiction`/`info_propagation` = info que el precio aún no refleja; coste de red 0 | 6 |
| 5 | `temporal.py` + `context_memory.py` (features + memoria) | **P2** | modulan confianza; baratos; alimentan priors | 6,7 |
| 6 | MetaModel Fase 1 (log-pooling, pure Python) en shadow | **P1** | fusiona todo; explicable; sin ML | 8,9 |
| 7 | Bayesian allocation (opt-in) | **P2** | empezar a aprender sin esperar semanas | — |
| 8 | Calibration Engine (offline) | **P1** | evita probabilidades infladas; requisito para operar con P externas | 10 |
| 9 | `news_event.py` (features, fuente externa) | **P2** | la vía teórica de mayor edge, pero la de mayor coste/riesgo (fuente, look-ahead) | 10 |
| 10 | Online Learning loop (nocturno) | **P2** | automatiza entrenar/validar/registrar/activar | 11 |
| 11 | MetaModel Fase 2 (LightGBM offline → inferencia pure Python) | **P3** | sólo si Fase 1 se queda corta y hay muestra suficiente | — |
| 12 | Up/Down plugins (`ProbabilityModel` + registro) | **P3** | independiente del resto; mejora el sub-sistema updown | — |

**P0 primero** (cimientos + honestidad). Luego **P1** (wallet, cross_market,
MetaModel Fase1, calibración) — es el núcleo del edge externo con mínimo coste.
**P2/P3** cuando P1 demuestre tracción con datos.

---

## 13. Cómo validar estadísticamente que CADA módulo agrega edge

Regla de oro: **ningún módulo se cree hasta que bata al baseline precio-solo
OUT-OF-SAMPLE y tras el peaje de 2.2%.** El mecanismo de medición ya existe
(`strategy_signals` resueltas 1/0 + doble cota maker/taker). Se formaliza así:

### 13.1 Métrica primaria: poder predictivo incremental
Para cada feature/módulo, medir la mejora en predecir el resultado 1/0 **por
encima del precio de mercado**:
- **ΔBrier** y **Δlog-loss** del modelo *con* la feature vs *sin* ella (precio-solo),
  en ventanas walk-forward. Debe ser negativo (mejora) y significativo.
- **ΔAUC** como control (dirección), con IC bootstrap.
- **Calibración:** ECE/MCE no deben empeorar (una feature que mejora Brier pero
  descalibra no sirve para sizing).

### 13.2 Contraste de hipótesis (mismo rigor que el estudio de calibración)
- **Wilson** para proporciones (win-rate), no Wald (colapsa en extremos).
- **Estratificar por cubos de precio y por categoría**; resumen tipo
  Mantel-Haenszel. Nunca celebrar una celda aislada: con ~18 celdas, 1 "significativa"
  es lo que produce el azar (lección ya aprendida con momentum).
- **Corrección por comparaciones múltiples** (Benjamini-Hochberg) cuando se testean
  muchas features a la vez.

### 13.3 Prueba económica (la que de verdad importa)
- **ROI net-of-cost** de las señales del módulo en shadow, con la **cota TAKER**
  (realista), no la maker. Cota inferior (frecuentista o cuantil bayesiano) > 0.
- **Profit Factor** y **max drawdown** en shadow.
- Comparar contra: (a) no operar, (b) baseline precio-solo, (c) MetaModel sin ese
  módulo (ablación). Un módulo sólo "agrega edge" si mejora (c).

### 13.4 Ablación y walk-forward obligatorios
- **Ablación:** entrenar el MetaModel con y sin el grupo de features del módulo;
  la diferencia OUT-OF-SAMPLE es la contribución real del módulo.
- **Walk-forward expansivo:** entrenar en [t0,t], validar en (t,t+Δ], avanzar.
  Jamás optimizar y reportar sobre la misma ventana.
- **Registro:** cada corrida (dataset_hash, feature_set, métricas) queda en
  `training_runs` y `model_registry` → reproducible y auditable.

### 13.5 Criterio de promoción (gauntlet, fail-closed)
Un módulo/modelo pasa a `paper` sólo si, out-of-sample:
1. ΔBrier < 0 significativo (vs modelo activo Y vs precio-solo), y
2. ECE/MCE no empeoran, y
3. ROI-TAKER con cota inferior > 0 (bate el peaje de 2.2%), y
4. mejora la ablación (aporta sobre el MetaModel sin él).
Si falla cualquiera → se queda en `candidate`, se notifica, humano decide.
**Worse never deployed.**

---

## 14. Qué NO hace este diseño (límites explícitos)

- No reescribe ningún módulo existente.
- No elimina ningún módulo.
- No cambia el pipeline de trading (sólo añade lecturas opcionales).
- No toca el Risk Manager salvo para *darle* más información (incertidumbre) que
  puede ignorar.
- No pone a Medusa en `live`: el triple candado y la ventana de evidencia siguen.
- No mete numpy/lightgbm en el runtime del engine.
- No deja que ningún texto (noticia/LLM) entre al camino de decisión.
- No modifica el modelo Browniano todavía: sólo crea la arquitectura de plugins.

---

## 15. Resumen ejecutivo

Medusa ya tiene, desplegada, la mitad de la infraestructura que este rediseño
necesita: el Intelligence Layer desacoplado, el Feature Store append-only, el
ciclo de vida de fases, el tracking shadow con doble cota y el asignador por
evidencia. El Event Intelligence Layer es la **continuación natural** de esa V3:

- **5 módulos de features externas al precio** (wallet, cross_market, temporal,
  context_memory, news_event) que se enchufan al runner ya existente.
- **Un MetaModel** que fusiona estrategias con pesos por régimen — y que, por ser
  una `Strategy` más, no puede saltarse el Risk Manager.
- **Calibración, Model Registry, Online Learning y Bayesian Allocation** que
  convierten el descubrimiento manual en un ciclo automático, honesto y con rollback.
- **Incertidumbre** propagada hasta el sizing.
- **Up/Down como plugins** intercambiables sin tocar el Browniano.

Todo aditivo, todo opt-in, todo desplegable fase por fase, y todo sujeto al
mismo estándar estadístico que ya mató tres falsos edges: **si no bate al precio
out-of-sample y tras costes, no se despliega.**
