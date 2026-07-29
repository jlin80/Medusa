# Medusa — Market Intelligence Graph (MIG) · V1

**Estado: implementado y aditivo. Apagado por defecto (`MIG_ENABLED=false`).**

- Fecha: 2026-07-28
- Paquete: `medusa/intelligence/mig/`
- Motor: **PostgreSQL y solo PostgreSQL** (sin Neo4j)
- Regla madre heredada: **nada de lo existente se reescribe, se rompe ni se elimina.**

---

## 0. Qué es y qué no es

El MIG construye un **grafo de relaciones** entre las entidades que ya viven
dentro de Medusa. Su única responsabilidad es esa: **describir cómo se
relacionan las cosas**.

Lo que **no** es, y no puede llegar a ser sin reescribir el paquete entero:

| No es | Por qué es imposible aquí |
|---|---|
| Un motor de trading | No importa `medusa.execution` ni `medusa.trading`; no tiene adapter de ejecución |
| Una estrategia | No produce `StrategySignal`; no se registra en el `StrategyManager` |
| Un módulo de ejecución | No conoce `OrderRequest` ni el cliente CLOB |
| Un consumidor del Risk Manager | No importa `medusa.risk`. Ni lo menciona |

Esto **no es una promesa en un docstring**: `tests/test_mig_isolation.py` recorre
el AST de todos los ficheros del paquete y falla si aparece cualquiera de esos
imports, si alguna sentencia escribe en una tabla del sistema de trading, o si
un modelo declara una clave foránea contra él.

Su producto son **objetos de inteligencia reutilizables** (`Discovery`) que
**algún día podrán convertirse en Features** — por decisión explícita de otro
módulo, y con el contrato de siempre (`Feature.value` float, lo textual en
`meta`). El MIG propone el nombre de la feature; **no la crea**.

---

## 1. Por qué PostgreSQL y no Neo4j

Requisito explícito del encargo, y además es la decisión correcta para V1:

1. **Las consultas reales son de un salto.** «Aristas de este nodo», «cuántos
   nodos de cada tipo», «top por grado». Eso en SQL con dos índices es directo.
2. **El hardware manda.** En el CT202 (2 vCPU, 3 GB) ya conviven Postgres,
   Redis, engine, API y nginx. Un motor de grafos es un servicio más a cambio de
   recorridos profundos que hoy nadie hace.
3. **Cero dependencias nuevas.** Ni una línea en `requirements.txt`. Todo el
   paquete es aritmética pura de Python + SQLAlchemy, que ya estaban.

Si algún día hacen falta caminos de longitud arbitraria, esa será la
conversación. No antes.

---

## 2. Vocabulario del grafo

### 2.1 Nodos (9 tipos)

| Tipo | Clave | De dónde sale | `weight` |
|---|---|---|---|
| `market` | `market:<condition_id>` | tabla `markets` (o la propia señal si el mercado ya no está en la ventana) | score del pre-scorer |
| `category` | `category:<nombre>` | `markets.medusa_category` | — |
| `event` | `event:<serie>` | deducido del `slug` (ver 2.3) | nº de mercados |
| `strategy` | `strategy:<nombre>` | `strategy_signals.strategy` | — |
| `trade` | `trade:<id>` | tabla `trades` | PnL |
| `outcome` | `outcome:<market_id>:<YES\|NO>` | resolución real del mercado | 1.0 |
| `feature` | `feature:<módulo>::<nombre>` | tabla `features` | — |
| `experiment` | `experiment:<estrategia>\|<categoría>` | celda de hipótesis con muestra | ROI taker medio |
| `wallet` | `wallet:<address>` | **fuente externa, vacía en V1** (ver 2.4) | ROI |

### 2.2 Relaciones (11 tipos)

| Relación | Semántica exacta | `weight` |
|---|---|---|
| `belongs_to` | market→category, market→event, trade→market, market→outcome, experiment→category | 1.0 |
| `participated_in` | strategy→market (emitió señal), strategy→trade, strategy→experiment, wallet→market | nº de observaciones |
| `predicted` | strategy→market | edge medio declarado |
| `specialized_in` | strategy→category, wallet→category | **ROI taker medio** |
| `won` / `lost` | strategy→outcome, trade→outcome | ROI / PnL |
| `similar_to` | market↔market (**simétrica**, misma categoría) | Jaccard de la pregunta |
| `preceded` / `followed` | market→market dentro de una serie, por `end_date` | 1.0 |
| `supports` / `contradicts` | feature→outcome (asociación), experiment→strategy (veredicto) | \|z\| / \|ROI\| |

`similar_to` se **normaliza** (extremos ordenados): `A~B` y `B~A` son la misma
relación y no pueden contar dos veces. `preceded`/`followed` se guardan **las
dos**, porque «qué vino antes de X» no debería exigir invertir la arista en SQL.

### 2.3 Sobre los nodos `event`

Medusa **no persiste el `event_id` de Gamma**. La serie se deduce del `slug`
recortando la cola de fecha/hora/ronda: `bnb-updown-5m-2026-07-28-1200` →
`bnb-updown-5m`. Dos candados para no inventar entidades:

- si no se recorta nada, **no hay serie** (no todo slug pertenece a una);
- una serie de **un solo mercado no es una serie** y no genera nodo.

### 2.4 Sobre los nodos `wallet`

**Actualizado 2026-07-28: ya hay fuente.** Cuando se escribió el MIG, Medusa no
ingería actividad de wallets ajenas y esta lista llegaba **vacía a propósito**
—preferible un tipo de nodo con 0 filas y semántica clara que fabricar datos
para que el dashboard enseñe un número bonito—. Con
[Wallet Intelligence](WALLET_INTELLIGENCE.md) desplegado, `load_sources` lee los
perfiles persistidos y los nodos `wallet` se crean con su `specialized_in` por
categoría.

Sigue siendo **opcional**: si el paquete falla o no hay perfiles, el grafo se
construye exactamente como antes. Y sigue **sin fabricarse** ninguna arista
`participated_in` hacia mercados concretos: el perfil no guarda en qué mercados
operó cada wallet, e inventarlas sería justo el tipo de dato falso que este
grafo no puede permitirse.

---

## 3. Arquitectura del paquete

```
medusa/intelligence/mig/
    types.py        vocabulario: NodeType, EdgeType, GraphNode, GraphEdge, Discovery
    graph.py        grafo en memoria — PURO, sin I/O
    builder.py      filas de la BD -> nodos y aristas — PURO
    discoveries.py  grafo -> objetos de inteligencia — PURO
    models.py       tablas ORM (mig_nodes, mig_edges, mig_discoveries, mig_snapshots)
    migrations.py   DDL idempotente (índices y unicidad)
    repository.py   persistencia y consultas
    service.py      orquestación: leer -> construir -> persistir
    api.py          router HTTP /mig/*
```

**Los tres módulos que deciden algo son puros.** Reciben listas de dicts y
devuelven objetos; no abren sesiones, no llaman a APIs y no miran el reloj. Por
eso el 100% de la semántica del grafo se testea sin Postgres, sin Redis y sin
red — y por eso este paquete no puede tener efectos secundarios sobre el
runtime.

### Idempotencia

Construir dos veces sobre los mismos datos da **el mismo grafo**: los nodos
tienen clave estable y las aristas se fusionan por el triple `(src, dst, tipo)`
con media ponderada por observaciones. Sin esa idempotencia, el grafo crecería
por repetición y la curva de crecimiento del Research Lab mediría ruido en vez
de conocimiento nuevo.

---

## 4. Esquema en base de datos

Cuatro tablas nuevas, todas con prefijo `mig_`, creadas por el `init_db()` de
siempre (cuelgan del mismo `Base`). Las migraciones (`migrations.py`) son
`CREATE ... IF NOT EXISTS` — idempotentes, aditivas y **solo sobre objetos
`mig_`**.

| Tabla | Contenido | Poda |
|---|---|---|
| `mig_nodes` | PK `key`, tipo, etiqueta, peso, meta, `first_seen`, `last_seen` | **nunca** |
| `mig_edges` | `src`, `dst`, `edge_type` (único), peso, `count`, meta | **nunca** |
| `mig_discoveries` | append-only: una observación por construcción | `MIG_RETENTION_DAYS` |
| `mig_snapshots` | una fila por construcción: la curva de crecimiento | `MIG_RETENTION_DAYS` |

Dos decisiones que parecen detalles y no lo son:

- **`first_seen` nunca se reescribe** en el upsert. Es lo único que permite
  medir crecimiento real; pisarlo convertiría la curva en una línea plana que
  solo dice «hoy hay N nodos».
- **Sin claves foráneas** contra las tablas de trading. El grafo *observa* el
  sistema, no lo ata: una poda en `markets` jamás puede fallar por culpa del MIG.

Coste en disco: una fila por construcción en `mig_snapshots` (≈8.800/año con el
intervalo por defecto) y nodos/aristas acotados por la ventana
(`MIG_MAX_MARKETS` y compañía). Unos pocos MB al año.

---

## 5. Descubrimientos

Un `Discovery` es un **patrón leído del grafo, con su evidencia y su tamaño de
muestra**. Tipos que produce V1:

| `kind` | Qué afirma | Umbral |
|---|---|---|
| `strategy_specialization` | ROI taker medio de una estrategia en una categoría | `MIG_MIN_SAMPLES` |
| `experiment_verdict` | La celda (estrategia × categoría) apoya o contradice a la estrategia | `MIG_MIN_SAMPLES` |
| `feature_association` | Hacia qué lado se inclina una feature en los mercados resueltos | `MIG_MIN_OBSERVATIONS` |
| `market_cluster` | Familia de mercados relacionados (componente conexa de `similar_to`) | ≥3 miembros |
| `event_series` | Serie temporal de mercados con transiciones ordenadas | ≥3 mercados |
| `hub_market` | Mercado más conectado del grafo | grado ≥4 |

**El `score` (0–1) no es rentabilidad.** Combina cuánta muestra sostiene el
patrón y cuánta magnitud tiene el efecto. Un 0.9 dice «esto está bien soportado
por los datos», jamás «esto gana dinero».

Y hay que decirlo entero: **nada de esto ha pasado un contraste out-of-sample**.
El proyecto ya midió lo que cuesta esa confusión — el peaje de ida y vuelta es
~2.2% y cuatro vías (calibración, momentum, imbalance, arbitraje) quedaron
descartadas por los datos. Por eso los descubrimientos viven en el grafo y **no**
en el Feature Store, y por eso `to_feature_spec()` devuelve una **plantilla**, no
una `Feature`.

También se reporta **lo que pierde**: una especialización con ROI negativo genera
su descubrimiento igual que una positiva. Saber dónde una estrategia pierde vale
exactamente lo mismo, y esconderlo es justo lo que un sistema honesto no puede
permitirse.

---

## 6. API

Todo cuelga de `/api/mig/`. **Un solo endpoint no-GET**, y hay un test que lo
verifica.

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/mig/info` | Configuración, vocabulario y última construcción |
| GET | `/mig/stats` | Totales, conteos por tipo, densidad, última construcción |
| GET | `/mig/nodes?limit=&node_type=&search=` | Nodos |
| GET | `/mig/edges?limit=&edge_type=&node=` | Aristas |
| GET | `/mig/neighbors?node=` | Vecindario de un nodo (entrada y salida) |
| GET | `/mig/discoveries?limit=&kind=&history=` | Descubrimientos (último de cada patrón por defecto) |
| GET | `/mig/growth?limit=` | Serie de crecimiento |
| POST | `/mig/build?persist=` | Reconstruye el grafo ahora (`persist=false` = solo simular) |

`POST /mig/build` **lee** tablas de trading y **escribe** en `mig_*`. No manda
órdenes, no toca posiciones y no puede cambiar una decisión del bot: es el
equivalente a pulsar «recalcular» en un informe.

Si el paquete fallara al importar, la API arranca igual y simplemente no habrá
`/mig`: una pieza opcional no puede tumbar el panel de control del bot.

---

## 7. Dashboard — página Research Lab

`dashboard/html/research.html`, enlazada desde la barra superior del panel
principal. Muestra:

- **Estadísticas del grafo**: nodos, aristas, tipos, densidad, duración y fecha
  de la última construcción, fecha del primer nodo visto.
- **Conteo de nodos por tipo** y **de aristas por relación**, en barras con un
  color por tipo.
- **Crecimiento en el tiempo**: nodos y aristas por construcción.
- **Últimos descubrimientos**, con su evidencia, su score, la feature que
  proponen y una nota que explica **qué no son**.
- **Nodos más recientes**, con su `first_seen`.
- Botón **«Reconstruir grafo»** (llama a `POST /mig/build`).

Si `MIG_ENABLED=false`, la página lo dice y explica que el botón sigue
funcionando.

---

## 8. Configuración

| Variable | Default | Qué controla |
|---|---|---|
| `MIG_ENABLED` | `false` | Loop automático en el engine |
| `MIG_INTERVAL` | `3600` | Segundos entre reconstrucciones |
| `MIG_TIMEOUT` | `120` | Presupuesto de una construcción |
| `MIG_MIN_SAMPLES` | `30` | Muestra mínima para un veredicto (alineado con `ALLOC_MIN_SAMPLES`) |
| `MIG_MIN_OBSERVATIONS` | `20` | Observaciones mínimas para una asociación de feature |
| `MIG_MIN_SIMILARITY` | `0.35` | Jaccard mínimo de `similar_to` |
| `MIG_MAX_MARKETS` / `_SIGNALS` / `_TRADES` / `_FEATURES` | `300`/`5000`/`2000`/`5000` | Ventana de datos por construcción |
| `MIG_RETENTION_DAYS` | `365` | Poda de descubrimientos y snapshots |

Coste de la similitud: O(k²) **dentro de cada categoría**, con el universo
recortado a `MIG_MAX_MARKETS`. Con el default son ~45k comparaciones de
conjuntos pequeños: milisegundos incluso en la CPU del CT202. **Subir el tope sin
rehacer esta cuenta** es exactamente cómo se cuela un builder que se come el
ciclo de mantenimiento.

---

## 9. Integración con lo que ya existía

Cinco puntos de contacto, todos aditivos:

1. `medusa/config.py` — bloque de ajustes `mig_*` (ninguno existente cambia).
2. `medusa/infra/db.py` — `init_db()` aplica también las migraciones del MIG,
   importadas dentro de un `try` para que un paquete opcional no pueda impedir
   el arranque.
3. `medusa/api/main.py` — `include_router(mig_router)` dentro de un `try`.
4. `medusa/engine.py` — `_mig_loop`, guardado igual que los demás loops, y poda
   del MIG en el mantenimiento diario. Con el flag apagado no hace nada.
5. `dashboard/html/index.html` — un enlace en la barra superior.

Ninguna función, tabla, endpoint o comportamiento existente ha sido modificado
ni eliminado.

---

## 10. Tests

`tests/test_mig_graph.py`, `test_mig_builder.py`, `test_mig_discoveries.py`,
`test_mig_service.py`, `test_mig_isolation.py`.

```bash
python -m pytest tests/ -q
```

Cubren: idempotencia y fusión del grafo, normalización de aristas simétricas,
rechazo de aristas colgantes y bucles, deducción de series, cadena temporal,
similitud, la distinción entre «el lado de la señal ganó» y «el mercado resolvió
YES», umbrales de muestra, tolerancia a filas basura y a `None` en columnas
*nullable*, orquestación del servicio con las escrituras interceptadas,
compilación del SQL del upsert contra el dialecto de PostgreSQL, y el contrato de
aislamiento verificado sobre el AST.

**Lo que los tests NO cubren**, y conviene tenerlo escrito: el comportamiento del
upsert contra un Postgres real con datos. Eso solo lo demuestra correr
`POST /mig/build` en el stack.

---

## 11. Qué NO hace este paquete (resumen)

- No coloca operaciones. **Nunca.**
- No modifica la ejecución de ninguna estrategia.
- No toca el Risk Manager.
- No escribe una sola fila fuera de las tablas `mig_*`.
- No crea Features ni escribe en el Feature Store.
- No afirma tener edge. Describe relaciones y las mide.
