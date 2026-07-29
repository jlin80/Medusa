# Medusa — Wallet Intelligence · V1

**Estado: implementado y aditivo. Apagado por defecto (`WALLET_INTEL_ENABLED=false`).**

- Fecha: 2026-07-28
- Paquete: `medusa/intelligence/wallet/`
- Motor: PostgreSQL (5 tablas `wi_*`) · aritmética pura de Python, **cero dependencias nuevas**
- Regla madre heredada: **nada de lo existente se reescribe, se rompe ni se elimina.**

---

## 0. Esto NO es copy trading

Copiar a una wallet significa convertir su movimiento en una orden. **Ese camino
no existe en este paquete.** Lo que produce es:

```
posiciones públicas → WalletDNA (19 números) → score → reputación
                                             → clusters / similitud
                                             → FEATURES por mercado
```

Ninguna función devuelve un lado, un tamaño o un precio. No se importa
`execution`, `trading`, `risk`, `strategies`, `allocation`, `updown` ni el
cliente CLOB. Y no es una promesa: `tests/test_wallet_isolation.py` recorre el
**AST** de todos los ficheros y falla si aparece cualquiera de esos imports, si
alguna función se llama algo como `copy_trade`/`place_order`/`follow_wallet`, si
se escribe en una tabla del sistema de trading, si un modelo declara una FK
contra ellas, o si el router expone más de un endpoint de escritura.

**Todo es numérico. No hay ni una etiqueta cualitativa.** Ni "smart money", ni
"ballena", ni "novato". Esas etiquetas son juicios disfrazados de datos: alguien
elige el umbral y a partir de ahí el sistema hereda su opinión. Un cluster aquí
es **un entero**, y su significado son los 19 números de su centroide. Hay un
test que barre el AST buscando etiquetas hardcodeadas.

---

## 1. El ADN: 19 métricas

Orden canónico (`DNA_FEATURES`). Es contractual: el clustering, la similitud y
la importancia indexan por posición, y la BD guarda el vector serializado.

| # | Métrica | Qué mide |
|---|---|---|
| 1 | `roi_historical` | ROI medio por posición **cerrada**, todo el historial |
| 2 | `roi_recent` | Igual, restringido a `WALLET_RECENT_DAYS` |
| 3 | `sharpe` | media(ROI)/desviación(ROI) **por posición** (sin anualizar) |
| 4 | `win_rate` | Fracción de cerradas con PnL > 0 |
| 5 | `consistency` | 1/(1+CV). Es **forma, no calidad** |
| 6 | `trade_frequency` | Posiciones/día sobre el periodo **activo** |
| 7 | `entry_timing` | Fracción de la vida del mercado transcurrida al entrar |
| 8 | `exit_timing` | Lo mismo al salir (1.0 = mantiene hasta la resolución) |
| 9 | `liquidity_preference` | log10(1+liquidez media), normalizado |
| 10 | `spread_preference` | Spread medio de los mercados que opera |
| 11 | `category_expertise` | max(peso de categoría × Wilson del win rate en ella) |
| 12 | `conviction` | Gini de los importes: ¿sube el tamaño cuando cree? |
| 13 | `alpha` | ROI − β × ROI de la población |
| 14 | `beta` | cov(wallet, población)/var(población) por cubos temporales |
| 15 | `drawdown` | Máxima caída de la curva de PnL acumulado |
| 16 | `volatility` | Desviación del ROI por posición |
| 17 | `reliability` | **Cota inferior de Wilson** del win rate |
| 18 | `freshness` | exp(−días inactiva / semivida) |
| 19 | `decay` | tanh(ROI reciente − ROI histórico) |

Decisiones que importan y están en el código:

- **Solo cuenta lo cerrado.** Una posición viva tiene un PnL que aún puede
  cambiar de signo; contarla es el error clásico de inflar el track record.
- **Muestra insuficiente ⇒ 0.0, nunca un valor inventado.** Un ADN a ceros dice
  «no se sabe»; uno con números fabricados **miente** y contamina el clustering,
  la similitud y la importancia de **toda** la población.
- **Lo ausente se excluye, no se rellena.** Una posición sin fechas de mercado no
  entra en el promedio de timing: un dato ausente no es un timing temprano.
- **`reliability` usa Wilson**, la misma función que el Capital Allocation
  Manager. Es lo que convierte «3 de 3» en un número honesto en vez de un 100%.

---

## 2. Score, reputación e importancia

**El score es relativo a la población, no a constantes.** Se estandariza cada
métrica contra la media y desviación de la propia población y se combina en un z
ponderado (recortado a 3σ para que un caso extremo no aplaste la escala) que
pasa por una logística a [0,1]. 0.5 = exactamente la media.

**Solo puntúan las métricas con dirección inequívoca.** Suben: ROI, Sharpe,
win rate, consistencia, alpha, reliability, freshness, decay. Restan: drawdown y
volatilidad. Fuera del score: timings, preferencias, frecuencia, beta y
convicción — describen **estilo**, y decidir que entrar tarde es mejor que
entrar pronto sería inventarse un hallazgo que nadie ha medido.

**Reputación = score × muestra × frescura × estabilidad**, multiplicativo a
propósito: cualquiera de los tres factores cerca de cero debe poder anularla por
su cuenta. El factor de muestra es `n/(n+min_samples)` (0.5 en el umbral, sin
escalón: 29 y 31 no pueden dar veredictos opuestos).

**Feature Importance = dispersión × asociación** con el ROI reciente. Es
**asociación en la muestra, no causalidad ni poder predictivo**: sin validación
fuera de muestra, sin control por categoría ni precio, y con el objetivo dentro
del propio ADN. Sirve para decidir qué mirar, jamás para justificar una
operación. Mismo listón que se aplicó a los descubrimientos del MIG.

---

## 3. Clusters y similitud

- **k-means determinista**: inicialización *farthest-first* sobre las wallets
  ordenadas por clave, no aleatoria. Sin esto, el panel de Evolución enseñaría
  ruido de inicialización y parecería que las wallets migran solas.
- Con menos de `WALLET_MIN_CLUSTER_WALLETS` **no se agrupa nada**: partir 4
  wallets en 5 grupos produce grupos de uno.
- **Similitud por coseno** sobre el vector z: interesa el *perfil* (en qué
  dirección se desvía de la media), no la magnitud. Un vector nulo devuelve 0.0,
  no 1.0: una wallet exactamente en la media no se parece a todo el mundo.
- Cada par se guarda **una sola vez** (a < b).

---

## 4. Ingesta

| Fuente | Qué aporta |
|---|---|
| Data API `/holders` | **Descubrimiento**: qué wallets hay en los mercados que Medusa ya vigila |
| Data API `/positions` | PnL **realizado**, tamaño, precio medio, si está cerrada |
| Data API `/activity` | **Cuándo** entró y salió (sin esto no hay timings) |
| Gamma `/markets` | Apertura, cierre, liquidez y spread del mercado |

`startDate` no lo expone la Data API, y sin él las métricas de timing no se
pueden calcular («¿el 20% de qué vida?»): por eso se pide a Gamma.

Los eventos `REDEEM` se **excluyen** de la actividad: cobrar un mercado ya
resuelto no es una decisión de salida, y contarlo empujaría el `exit_timing` de
todo el mundo a 1.0.

Cliente httpx propio (como `medusa/updown/feed.py`), que **nunca lanza**: ante
cualquier fallo devuelve vacío y registra un warning. Una wallet que falla queda
**fuera** de la pasada en vez de entrar con datos a medias.

---

## 5. El puente a features (`module.py`)

`WalletIntelligence` hereda de `IntelligenceModule`, así que por construcción lo
máximo que puede devolver es `list[Feature]`. Emite por mercado:

`wallet_reputation_mean` · `wallet_reputation_max` ·
`wallet_reputation_weighted` (ponderada por tamaño del holder) ·
`wallet_known_holders` (**el tamaño de muestra**) · `wallet_coverage`.

**Deliberadamente NO se emite el lado agregado de las wallets buenas.** Esa sí
sería una señal de copia disfrazada de número — y además sería mala: el proyecto
ya midió que Polymarket está bien calibrado y que seguir al consenso solo paga
spread. Hay un test que verifica, sobre el AST, que ningún nombre de feature
contiene `side`, `outcome`, `yes`, `buy`, `sell`, `signal` ni `action`.

Si ningún holder de un mercado tiene perfil, **no se emite feature**: un 0.0 se
leería como «aquí hay wallets malas», que es una afirmación distinta de «no
sabemos nada de ellas».

Está registrado en `build_default_modules()` pero **sigue siendo opt-in**: solo
corre si se le nombra en `INTELLIGENCE_MODULES`.

---

## 6. Base de datos

| Tabla | Contenido | Poda |
|---|---|---|
| `wi_wallets` | Perfil vigente: ADN, score, reputación, cluster | **nunca** |
| `wi_dna_history` | Append-only, una fila por pasada → panel Evolución | `WALLET_RETENTION_DAYS` |
| `wi_similarity` | Pares (único por a<b) | — |
| `wi_clusters` | Centroides por pasada | `WALLET_RETENTION_DAYS` |
| `wi_runs` | Telemetría + población + importancia de features | `WALLET_RETENTION_DAYS` |

`first_seen` **nunca se reescribe** en el upsert, y **no hay claves foráneas**
contra las tablas de trading (el subsistema observa, no ata). Guardar la
población en `wi_runs` es imprescindible: un score de 0.8 solo significa algo
contra la media y desviación con las que se calculó.

---

## 7. API y dashboard

`/api/wallets/`: `info` · `dna/definitions` · `stats` · `` (explorer) ·
`reputation` · `clusters` · `feature-importance` · `runs` · `{wallet}` ·
`{wallet}/history` · `{wallet}/similar` (GET) + **`POST /wallets/build`**
(único endpoint de escritura).

**Página `dashboard/html/wallets.html`** con los 7 paneles pedidos:
1. Wallet Explorer (filtrable y ordenable) · 2. Wallet DNA (19 barras con
definición al pasar el ratón + desglose por categoría) · 3. Wallet Reputation ·
4. Wallet Similarity · 5. Wallet Clusters (enteros + features separadoras en σ) ·
6. Wallet Evolution (reputación/score/muestra en el tiempo) · 7. Feature
Importance. Enlazada desde el panel principal y desde el Research Lab.

---

## 8. Integración (6 puntos de contacto, todos aditivos)

`config.py` (bloque `wallet_*`) · `infra/db.py` (`_extra_migrations` ahora suma
también las de wallet, cada paquete en su propio `try`) · `api/main.py`
(`include_router` dentro de `try`) · `engine.py` (`_wallet_loop` guardado, poda y
cierre del feed) · `intelligence_layer/__init__.py` (registro opt-in del módulo) ·
`dashboard/html/index.html` y `research.html` (enlaces).

Además, **el MIG ya tiene fuente de wallets**: `mig/repository.load_sources`
lee los perfiles si el paquete está presente, así que los nodos `wallet` del
grafo dejan de llegar vacíos. Sigue sin fabricarse ninguna arista
`participated_in` hacia mercados: el perfil no guarda en qué mercados concretos
operó cada wallet, y inventarlas sería justo el tipo de dato falso que el grafo
no puede permitirse.

---

## 9. Tests

`tests/test_wallet_dna.py`, `test_wallet_scoring.py`, `test_wallet_clusters.py`,
`test_wallet_ingest.py`, `test_wallet_service.py`, `test_wallet_isolation.py`.

```bash
python -m pytest tests/ -q
```

**Lo que NO cubren**: el comportamiento de los upserts contra un Postgres real
con datos, y la forma exacta que devuelve hoy la Data API de Polymarket (las
fixtures son escritas a mano y contemplan varios alias de campo, pero solo una
llamada real lo confirma). Eso solo lo demuestra correr
`POST /api/wallets/build` en el stack.

---

## 10. Qué NO hace este paquete (resumen)

- No coloca operaciones. **Nunca.**
- No es copy trading: no mira el lado de nadie, no propone tamaño ni precio.
- No modifica la ejecución de ninguna estrategia. No toca el Risk Manager.
- No escribe una sola fila fuera de las tablas `wi_*`.
- No usa etiquetas cualitativas: todo es numérico.
- No afirma tener edge. Describe wallets y mide la confianza que merece cada
  número.
