# Medusa — Information Flow Engine (IFE) · V1

Mide **cómo se propaga la información** dentro de un mercado de Polymarket:

```
wallet A entra
   ↓  (lag)
wallet B entra
   ↓  (lag)
wallet C entra
   ↓
el precio del lado se mueve
   ↓
el mercado resuelve
```

Paquete: `medusa/intelligence/flow/`. Tablas `flow_*`. Router `/flow/*`.
Página `dashboard/html/flow.html`. Apagado por defecto (`FLOW_ENABLED=false`).

---

## 0. Qué es y qué no es

**Es** un motor de investigación: convierte la cinta pública de trades en
cascadas, eslabones de propagación y métricas con evidencia estadística.

**No es**, y esto gobierna todo el diseño:

- **No es un motor de causalidad.** Que B entre después de A no significa que B
  siga a A, ni que A moviera el precio, ni que ninguna tenga información
  privada. No hay contrafactual, no hay asignación aleatoria y no hay control de
  confusores: un anuncio público hace entrar a veinte wallets a la vez sin que
  ninguna sepa de las otras. Los campos se llaman `lag_seconds`, `hop`, `leader`
  y `follower` — orden temporal — y ninguno se llama `caused_by` o `influence`.
  Hay un test (`test_ningun_tipo_publico_afirma_causalidad`) que lo impide.
- **No es un motor de trading.** No manda órdenes.
- **No es una estrategia.** Nada de lo que produce tiene lado, tamaño ni precio
  de entrada.
- **No es copy trading.** El producto son eventos de propagación y métricas.
- **No toca el Risk Manager.** Ni lo importa.

El único contraste legítimo, y el que se publica, es contra el **azar**: bajo
intercambiabilidad el rango normalizado esperado de cualquier participante es
`0.5`. De ahí salen `edge_vs_chance` y la línea gris de cada medidor del panel.

---

## 1. Vocabulario

| Concepto | Definición operativa |
|---|---|
| **Trade** | Trade público normalizado: `(mercado, wallet, lado entrado, precio del lado, tamaño, ts)`. |
| **Entrada** | **Primera** entrada de una wallet en un lado de un mercado. Reforzar no es información nueva. |
| **Cascada** | Entradas consecutivas cuyo hueco no supera `FLOW_WINDOW_SECONDS` (sesionización por huecos), con al menos `FLOW_MIN_PARTICIPANTS`. |
| **Eslabón** | Par ordenado `(leader, follower)` dentro de una cascada a distancia ≤ `FLOW_MAX_HOPS`. |

Dos normalizaciones hacen que la cadena signifique algo:

1. **Lado entrado, no verbo del libro.** Vender SÍ es posicionarse en NO. Sin
   esto, un vendedor de SÍ aparecería en la cadena de los compradores de SÍ.
2. **Precio del lado entrado.** Si la fila es una venta, la probabilidad
   implícita del lado en el que la wallet queda es `1 - p`. Con eso, «el precio
   subió después de que entrara» significa lo mismo en los dos lados.

Los dos lados de un mercado son **cadenas distintas**: mezclarlas convertiría a
un comprador de NO en «seguidor» de un comprador de SÍ.

---

## 2. Las siete métricas

Todas viajan con su **n**, y las proporciones con su **cota inferior de Wilson
al 95 %** (3/3 parece 1.00 y 240/400 parece 0.60; con la cota el orden se
invierte).

| Métrica | Qué mide |
|---|---|
| **Information Speed** | Mediana de segundos entre el inicio de la cascada y la entrada de la wallet. Menos = entra antes. `speed_score` es su forma acotada [0,1] con vida media, comparable entre mercados de escalas distintas. |
| **Leadership Score** | Media de `1 - rango normalizado`. 1.0 = siempre abre; **0.5 = lo esperado por azar**; 0.0 = siempre cierra. |
| **Follow Score** | El complementario exacto (media del rango). Se publica aparte porque es la métrica de «quién llega tarde». |
| **Consensus Delay** | Segundos hasta que ha entrado la fracción de consenso (0.5 por defecto) de una cascada. Métrica de **mercado**. |
| **Propagation Time** | Mediana del salto entre entradas consecutivas. Por wallet: cuánto tarda en entrar el siguiente **después** de ella. |
| **Early Information Score** | De sus entradas tempranas (rango ≤ `FLOW_EARLY_FRACTION`), en qué fracción el lado acabó teniendo razón. |
| **Late Information Score** | Lo mismo para sus entradas tardías. `information_edge = early − late` es **descriptivo**. |

**Jerarquía de evidencia** para «¿acabó teniendo razón el lado?»:

1. **Resolución del mercado** — la verdad.
2. **Movimiento del precio después de la entrada**, dentro de la cascada, si
   supera `FLOW_MIN_PRICE_MOVE`. Es un sustituto y se dice que lo es.
3. **Nada.** Mercado vivo y precio plano ⇒ la observación **no puntúa**. Contar
   un empate como acierto es la forma más silenciosa de fabricar significancia.

Un mercado sin resolver **nunca** recibe un resultado inventado (ni 0.5 ni el
último precio).

---

## 3. Arquitectura del paquete

```
types.py       vocabulario (FlowTrade, Entry, Cascade, PropagationEvent,
               WalletFlowMetrics, MarketFlowMetrics)
metrics.py     estadística pura (mediana, Wilson, decaimiento, cuantiles, histograma)
ingest.py      JSON de Polymarket -> FlowTrade                        [puro]
cascades.py    trades -> cascadas -> eventos de propagación           [puro]
scoring.py     cascadas y eventos -> métricas por wallet y mercado    [puro]
feed.py        lectura de la cinta pública (red; nunca lanza)
models.py      6 tablas ORM
migrations.py  DDL idempotente
repository.py  persistencia y consultas
service.py     orquestación (ingerir -> detectar -> medir -> persistir)
api.py         router /flow/*
```

Todo lo que **decide** algo es puro y se testea sin infraestructura. El servicio
es lo único que habla con la red, la BD y el reloj.

**Idempotencia.** `cascade_key = mercado:lado:inicio` es estable entre pasadas:
reanalizar la misma ventana actualiza la cascada en vez de duplicarla. Los
trades y los eslabones se insertan con `DO NOTHING`, así que dos pasadas
solapadas no inflan la muestra.

---

## 4. Esquema en base de datos

| Tabla | Contenido | Poda |
|---|---|---|
| `flow_trades` | Cinta cruda normalizada (PK: huella estable). Lo único irrepetible: permite recalcular con otra definición mañana. | `FLOW_TRADE_RETENTION_DAYS` (45 d) |
| `flow_cascades` | Cada racha, con tiempos, precios y resolución. | `FLOW_RETENTION_DAYS` |
| `flow_events` | **Cada** eslabón de propagación. Sin agregar y sin filtrar por score. | `FLOW_RETENTION_DAYS` |
| `flow_wallet_metrics` | Estado actual por wallet (upsert). Derivada. | nunca |
| `flow_market_metrics` | Estado actual por mercado (upsert). Derivada. | nunca |
| `flow_snapshots` | Una fila por pasada: la serie temporal. | `FLOW_RETENTION_DAYS` |

Ninguna tabla tiene clave foránea contra las tablas de trading: el motor observa
el sistema, no lo ata. `first_seen` nunca se reescribe.

Ojo con el nombre: `trades` es la tabla de operaciones de Medusa y el motor no
la toca. La cinta pública vive en `flow_trades`.

---

## 5. API

Todo lectura salvo `POST /flow/run`.

| Endpoint | Devuelve |
|---|---|
| `GET /flow/info` | Configuración, definiciones de cada métrica y el aviso de no-causalidad. |
| `GET /flow/stats` | Totales, cobertura y wallets con muestra suficiente. |
| `GET /flow/cascades` | Cascadas detectadas. |
| `GET /flow/events` | Eventos de propagación (filtrables por wallet, mercado, cascada, salto). |
| `GET /flow/wallets` | Ranking. Por defecto **solo con muestra suficiente**. |
| `GET /flow/wallets/{wallet}` | Ficha + últimos eslabones. |
| `GET /flow/markets` | Propagación por mercado. |
| `GET /flow/pairs` | Pares que se repiten (**no** es influencia). |
| `GET /flow/lag-histogram` | Distribución de latencias. |
| `GET /flow/timeline` | Serie de pasadas. |
| `POST /flow/run?persist=false` | Pasada ahora; con `persist=false` no escribe nada. |

---

## 6. Dashboard — página Information Flow

`/flow.html`, enlazada desde el panel, el Research Lab y Wallets.

- **Aviso epistémico** arriba del todo: es la pieza más importante de la página.
- **Cadena de propagación**: la cascada elegida dibujada wallet a wallet con su
  latencia, y los dos eslabones finales — el movimiento del precio y la
  resolución — marcados como «lo que ocurrió después», no como un efecto.
- **Histograma de latencias** líder → seguidor (el último tramo acumula la cola).
- **Actividad del motor**: cascadas y eventos por pasada.
- **Wallets**: cada medidor dibuja la **cota inferior** en sólido y el valor
  puntual en translúcido — la diferencia es la incertidumbre — con la línea gris
  del 0.5 del azar.
- **Mercados** y **pares repetidos**, con `n` por delante.

---

## 7. Configuración

Ver el bloque `FLOW_*` de `.env.example`. Los tres parámetros que más cambian el
resultado: `FLOW_WINDOW_SECONDS` (qué es «seguido»), `FLOW_MIN_PARTICIPANTS`
(qué es una cascada) y `FLOW_MIN_PRICE_MOVE` (qué observación puntúa).

---

## 8. Tests

- `tests/test_flow_cascades.py` — normalización (comprar/vender, precio del
  lado, filas incompletas), primeras entradas, sesionización, eslabones,
  resoluciones y estabilidad de la clave.
- `tests/test_flow_scoring.py` — mediana vs media, Wilson, rangos, velocidad,
  temprano/tardío, empates que no puntúan, métricas de mercado y pares.
- `tests/test_flow_isolation.py` — el contrato sobre el **código fuente**: nada
  de `medusa.execution/trading/risk/strategies`, nada escrito fuera de `flow_*`,
  migraciones solo `CREATE ... IF NOT EXISTS ON flow_*`, sin claves foráneas, un
  solo endpoint no-GET, apagado por defecto, ningún campo que afirme causalidad
  y un feed que solo sabe hacer `GET`.
- `tests/test_flow_service.py` — orquestación sin BD ni red, y el SQL de los
  upserts compilado contra el dialecto real.

---

## 9. Qué NO hace este paquete (resumen)

No manda órdenes · no emite señales · no toca el Risk Manager · no escribe fuera
de `flow_*` · no inventa resoluciones · no cuenta empates · no publica
proporciones sin su `n` · **y no afirma causalidad.**
