# Hypothesis Engine (HE)

Medusa genera sus propias hipótesis de investigación a partir de los datos que ya
observa, las pone a prueba con datos que **no pudo ver**, y las valida o las
rechaza.

```
observaciones cosechadas
    v
variables que los datos sostienen        (cobertura, varianza, cardinalidad)
    v
gramática x variables = cientos de contrastes
    v
los que sobreviven al tamaño de efecto y al FDR  ->  hipótesis `proposed`
    v
llegan observaciones NUEVAS (posteriores a `created_at`)
    v
`testing`  ->  `validated` | `rejected`
```

Aditivo por construcción y **apagado por defecto** (`HYPOTHESIS_ENABLED=false`).
Encenderlo no cambia una sola decisión del bot: añade filas a cinco tablas nuevas
y dos páginas al panel.

---

## Las dos reglas que definen el motor

### 1. Ninguna hipótesis está escrita en el código

Lo que está programado es una **gramática** de tres formas y el **lineage** de las
fuentes. El enunciado concreto —qué variable, contra qué resultado, en qué
dirección, con qué corte— sale de los datos en cada pasada.

| Forma | Qué relaciona | Estadístico |
|---|---|---|
| `monotone` | predictor numérico × resultado numérico | Spearman + intervalo de Fisher |
| `group_contrast` | un nivel de una etiqueta × resultado | Welch, efecto estandarizado (d) |
| `threshold` | predictor > corte × resultado | Welch, efecto estandarizado (d) |

Las plantillas son la única prosa del motor y no nombran ni una variable del
dominio:

```
"A mayor {predictor}, menor {outcome}"
"Con {predictor} = {level}, {outcome} es más alto que en el resto"
"Con {predictor} por encima de {cut}, {outcome} es más bajo"
```

**Dónde está la frontera.** Declarar «el spread es una condición y el ROI un
resultado» es *lineage*: lo sabe quien escribió la tabla y vive en `sources.py`.
Afirmar «el spread alto va con un ROI bajo» es una *hipótesis*, y eso tiene que
salir de los datos. `test_hypothesis_isolation.py` lo comprueba por
comportamiento: corre el motor sobre dos conjuntos idénticos salvo en el **nombre
de las columnas** y exige que las descripciones cambien con ellos. Si estuvieran
hardcodeadas, saldría la misma frase.

### 2. Una hipótesis se valida con datos que no pudo ver

`created_at` no es decoración: es una **valla**.

- `sample_count` cuenta **exclusivamente** las observaciones posteriores a esa
  valla (comparación estricta: una observación en el instante exacto de la
  creación pudo estar en la ventana de descubrimiento, y en la duda se descarta).
- `confidence` vale **0.00** mientras la hipótesis siga en `proposed`, por brutal
  que sea el efecto que la generó.
- La evidencia del descubrimiento se guarda, pero **no es evidencia**: son los
  mismos datos que eligieron el enunciado entre cientos de candidatos.

Sin esa valla el motor sería una máquina de confirmarse: propondría la relación
más llamativa de una ventana y la «validaría» con la misma ventana, que es elegir
el número después de ver la ruleta.

---

## Estados

```
proposed ──> testing ──> validated
    │           │            │
    └───────────┴──────> rejected  (terminal)
```

| Estado | Significa |
|---|---|
| `proposed` | Encontrada en los datos. Sin una sola observación que el motor no hubiera visto ya. |
| `testing` | Llegan observaciones posteriores a su creación, pero aún no bastan. |
| `validated` | Replicó fuera de muestra: mismo signo, intervalo al margen del nulo, efecto sobre el mínimo. |
| `rejected` | Contradicha (el signo se invirtió con significancia) o sin replicar con muestra de sobra. |

**Dos asimetrías deliberadas:**

- **`rejected` es terminal.** El `id` es el hash del enunciado, así que cuando el
  generador la redescubre cae en la fila cerrada y no abre una nueva. Sin esto
  bastaría con esperar la pasada en la que el ruido saliese a favor: eso es
  *blanquear hipótesis*, y es el fallo más fácil de cometer en un motor que
  propone solo.
- **`validated` no es terminal.** Una muestra mayor que la contradiga puede
  tumbarla. Lo contrario —fijar la conclusión por haber llegado primero— sería un
  archivo de aciertos antiguos. Lo que no existe es `validated -> testing`: una
  validación que se debilita no se borra, se lee en la `confidence`, que baja sola.

### Veredictos, en detalle

| Situación | Resultado |
|---|---|
| n = 0 fuera de muestra | `proposed` |
| signo invertido con intervalo al margen del nulo | `rejected` **sin esperar** más datos |
| n < `MIN_TEST_SAMPLES` | `testing` |
| replica y \|efecto\| ≥ mínimo | `validated` |
| n ≥ `REJECT_AFTER` y el intervalo sigue cruzando el nulo | `rejected` (*no replica*) |
| resto | `testing` |

La asimetría entre las dos últimas es intencional: rechazar pronto por falta de
datos tiraría hipótesis buenas por impaciencia, pero con `REJECT_AFTER`
observaciones, si el efecto propuesto existiera, se vería.

---

## `id`, y por qué no incluye la dirección

`id = sha1(forma | fuente | predictor | nivel | outcome)`. Dos consecuencias
buscadas:

1. redescubrir la misma relación cae en la **misma fila** y acumula evidencia, en
   vez de crear una hipótesis «nueva» cada hora;
2. «a mayor X, mayor Y» y «a mayor X, menor Y» son la **misma** hipótesis sobre la
   misma relación. Si la dirección formase parte de la identidad, un cambio de
   signo crearía una hipótesis virgen y el motor podría tirar la moneda hasta que
   saliera cara. El signo se congela al proponer y un giro fuera de muestra es un
   rechazo.

---

## `confidence` — lo que NO es

**No es la probabilidad de que la hipótesis sea cierta.** Ese número requeriría un
previo que nadie tiene. Es un resumen acotado y monótono de la fuerza de la
evidencia **fuera de muestra**:

```
confianza = min(1, |efecto que sostiene la cota| / referencia)  ×  n / (n + min_n)
```

Propiedades, todas comprobadas por tests:

- `0.0` sin observaciones nuevas (todo `proposed`);
- `0.0` si el intervalo cruza el nulo — sin signo determinado no hay confianza que
  reportar, por grande que sea la muestra;
- `0.0` si el signo observado es el **contrario** al afirmado: una hipótesis con la
  dirección invertida no está «poco confirmada», está contradicha;
- con `n = min_n` el techo es `0.5`: un efecto enorme con muestra ridícula no llega
  arriba.

`referencia` es el efecto que se considera grande en cada escala (0.5 para rho,
0.8 para d). Es una convención **declarada**, y por eso vive en
`REFERENCE_EFFECT` con nombre en vez de escondida en una fórmula.

---

## Multiplicidad: el filtro que hace que esto no sea una máquina de ruido

El motor prueba cientos de relaciones por pasada. Un motor que publica las que dan
p<0.05 publica ~20 hallazgos falsos por pasada, cada hora, para siempre.

- Se aplica **Benjamini-Hochberg** sobre **todos** los contrastes de la pasada
  (BH y no Bonferroni: con 400 contrastes Bonferroni exigiría p<0.000125 y mataría
  también los reales).
- Cada hipótesis guarda `tested_in_pass`: el denominador sin el que su
  significancia no se puede juzgar. Sale en la API y en el panel.
- Los contrastes se estiman **todos** antes de filtrar por tamaño de efecto:
  corregir sobre los supervivientes de otro filtro subestimaría la multiplicidad
  real.

### Tres duplicaciones que se evitan

| Duplicación | Por qué se evita |
|---|---|
| `threshold` sobre una relación ya monótona | Es la misma señal contada dos veces. El umbral existe para relaciones **en escalón**, donde la monótona no llega. |
| k niveles de una etiqueta | Si «sports» está por encima del resto, las otras categorías quedan por debajo por aritmética. Se queda el nivel que más separa. |
| parejas definicionales | `roi` se calcula dividiendo por `entry_price`: sería una división, no un hallazgo. Ver `blocked_pairs`. |

---

## Fuentes y unidad de observación

Las tablas de Medusa son de dos tipos y no se pueden cosechar igual.

| Fuente | Unidad | Qué es una observación |
|---|---|---|
| `signals` | `event` | una señal de estrategia **resuelta** |
| `trades` | `event` | una operación cerrada |
| `flow_wallets` | `entity` | una wallet del IFE, la **primera** vez que se vio |
| `flow_markets` | `entity` | un mercado del IFE, la **primera** vez que se vio |

**La trampa de las tablas de estado.** `flow_wallet_metrics` se reescribe en cada
pasada del IFE. Si se cosechara cada vez, la misma wallet entraría cuarenta veces
al día con valores algo distintos, `sample_count` contaría cuarenta observaciones
donde hay **una sola wallet**, y cualquier intervalo saldría absurdamente
estrecho. Es pseudo-replicación, y hace parecer significativo casi todo. Por eso
el `uid` de una fuente de estado es la **entidad** y la escritura es
`on conflict do nothing`: una entidad, una observación.

**El reloj de una observación** es el instante en el que **quedó completa**: para
una señal, su `resolved_at`, no el momento del disparo. Hasta que el mercado no
resuelve, la observación no existe como evidencia y el generador no pudo verla.
Confundir las dos fechas rompe la valla en silencio.

**Los huecos no se rellenan.** `strategy_signals.spread` es NULL en las filas
anteriores al 2026-07-16. Convertirlo en `0.0` diría «operar aquí era gratis», que
es una afirmación falsa metida en los datos. El hueco se propaga como hueco y el
descarte por parejas se encarga; por eso el `n` que viaja en cada estimación es el
de **pares completos**, no el de la ventana.

---

## Lo que el motor NO es

- **No es un motor de causalidad.** Observa **asociación**. No hay contrafactual,
  no hay asignación aleatoria y no hay control de confusores: cualquier tercera
  variable puede estar moviendo a las dos. Por eso los enunciados dicen «a mayor
  X, menor Y» y **nunca** «X reduce Y» — hay un test que recorre las plantillas
  buscando verbos causales.
- **No es un motor de trading.** No manda órdenes.
- **No es una estrategia.** Una hipótesis validada es una frase con un intervalo
  de confianza: no tiene lado, ni tamaño, ni precio de entrada.
- **No toca el Risk Manager.** Ni lo importa.
- **No acepta hipótesis escritas a mano.** No hay endpoint para crearlas, y su
  ausencia es la funcionalidad: un `POST /hypotheses` sería la puerta por la que
  entraría la primera hipótesis escrita por una persona, y con ella se perdería la
  única garantía que hace interesante a lo guardado.

---

## Tablas (`hyp_*`)

| Tabla | Qué guarda |
|---|---|
| `hyp_observations` | Las observaciones cosechadas. **Inmutable**: `on conflict do nothing`, jamás `do update`. Su inmutabilidad **es** la integridad de la valla — si una segunda cosecha pudiera reescribir un `ts`, una hipótesis podría «adelantarse» a datos que ya conocía sin que quedara registro. |
| `hyp_hypotheses` | Estado actual (upsert por `id`). `created_at`, `discovery_*` y `tested_in_pass` se escriben **una sola vez** y no se vuelven a tocar. |
| `hyp_evidence` | Append-only: un punto por hipótesis y pasada. La curva de cómo la evidencia creció (o no). |
| `hyp_transitions` | Append-only: cada cambio de estado con su motivo. El expediente. |
| `hyp_snapshots` | Una fila por pasada, con `tested` (el denominador de la corrección). |

Ninguna tiene clave foránea contra las tablas de trading: el motor **observa** el
sistema, no lo ata.

**Poda.** Las hipótesis **no se podan nunca**, ni su expediente: una rechazada de
hace un año es justo lo que evita volver a proponerla. Se podan snapshots y
observaciones viejas.

---

## API

Todo de lectura salvo `POST /hypotheses/run`.

```
GET  /hypotheses/info          configuración, gramática, definiciones y avisos
GET  /hypotheses/stats         tablero, observaciones y contrastes acumulados
GET  /hypotheses/board         cuántas hay en cada estado
GET  /hypotheses/sources       lineage: unidad y parejas bloqueadas
GET  /hypotheses/coverage      hipótesis por fuente y estado
GET  /hypotheses/timeline      serie temporal del motor
GET  /hypotheses/transitions   últimos cambios de estado
GET  /hypotheses               listado con filtros (status, source, form, orden)
GET  /hypotheses/{id}          expediente: estado, evidencia y transiciones
POST /hypotheses/run           una pasada ahora (`persist=false` no escribe nada)
```

Una pasada con `persist=false` es una vista previa útil, pero **no puede validar
nada**: sus hipótesis nacen con la valla puesta en ese instante.

---

## Panel

- `/hypotheses.html` — tablero por estado, listado con filtros, lineage de las
  fuentes, expediente reciente y la evolución del tablero.
- `/hypothesis.html?id=…` — el expediente de una hipótesis: la valla dibujada
  (n antes / n después), el hallazgo y la replicación con sus intervalos y el cero
  marcado, la curva de evidencia y el histórico de transiciones.

---

## Configuración

Todas las claves en `.env.example` bajo *Hypothesis Engine*. Las que más cambian el
comportamiento:

| Clave | Default | Efecto |
|---|---|---|
| `HYPOTHESIS_ENABLED` | `false` | Con `false` el engine no lo ejecuta solo; el botón del panel sigue funcionando. |
| `HYPOTHESIS_DISCOVERY_DAYS` | `30` | Ventana de la que se **proponen** hipótesis. Larga = relaciones de un régimen que ya no existe. |
| `HYPOTHESIS_LOOKBACK_DAYS` | `180` | Ventana de **análisis**: necesita historia a los dos lados de la valla. |
| `HYPOTHESIS_ALPHA` | `0.05` | FDR de BH. Subirlo llena el tablero de ruido. |
| `HYPOTHESIS_MIN_EFFECT_RHO` / `_D` | `0.15` / `0.25` | Bajarlos llena el tablero de relaciones ciertas y triviales. |
| `HYPOTHESIS_MIN_TEST_SAMPLES` | `60` | Muestra fuera de muestra para emitir veredicto. |
| `HYPOTHESIS_REJECT_AFTER` | `200` | A partir de aquí, la falta de efecto se lee como «no replica». |

---

## Tests

| Fichero | Qué fija |
|---|---|
| `test_hypothesis_isolation.py` | El contrato, sobre el AST y por comportamiento: sin imports de ejecución, solo escribe en `hyp_*`, plantillas ciegas al dominio y sin verbos causales, el enunciado cambia si cambian los nombres de las columnas, ninguna hipótesis nace con evidencia, el `id` no depende de la dirección. |
| `test_hypothesis_stats.py` | Empates promediados, Fisher dentro de rango, efectos estandarizados invariantes a la escala, BH activo y menos severo que Bonferroni, propiedades de `confidence`. |
| `test_hypothesis_generator.py` | Filtros de variables (cobertura, varianza, cardinalidad, niveles), propone lo evidente y **no** propone ruido, parejas bloqueadas, sin duplicar formas ni niveles. |
| `test_hypothesis_evaluator.py` | **La valla**, el grafo de estados, `rejected` terminal, giro de signo = rechazo, corte congelado. |
| `test_hypothesis_service.py` | Coordinación: una rechazada no se blanquea, topes por fuente, ciclo completo propone-espera-valida con el reloj avanzando, unidad de observación por fuente. |
