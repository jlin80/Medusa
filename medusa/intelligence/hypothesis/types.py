"""Vocabulario del HYPOTHESIS ENGINE (HE).

Aqui vive lo que el motor sabe representar: una observacion generica, una
variable descubierta, una estimacion de efecto y la hipotesis misma. Es el unico
sitio donde se define ese vocabulario; ni el generador ni el evaluador inventan
campos por su cuenta.

REGLA DEL PAQUETE (la del MIG, la de Wallet Intelligence y la del IFE, mas dos
que son especificas de este motor y no son negociables):

  - NINGUNA HIPOTESIS ESTA ESCRITA EN EL CODIGO. Lo que esta escrito es la
    GRAMATICA (tres formas: monotona, contraste de grupos y umbral) y la
    DECLARACION DE LAS FUENTES (que columna es una condicion y que columna es un
    resultado). El enunciado concreto -- que variable, contra que resultado, en
    que direccion y con que fuerza -- sale de los datos en cada pasada. Un test
    recorre el AST del paquete y tumba la suite si aparece una frase de hipotesis
    escrita a mano.
  - UNA HIPOTESIS SE VALIDA CON DATOS QUE NO PUDO VER. `created_at` no es
    decoracion: es una VALLA. `sample_count` cuenta exclusivamente las
    observaciones posteriores a esa valla. Validar con los mismos datos que
    generaron el enunciado no es ciencia, es el jardin de los senderos que se
    bifurcan con dos decimales.

Y el limite epistemico, que es el que define de verdad el paquete: el motor
observa ASOCIACION. No hay contrafactual, no hay asignacion aleatoria y no hay
control de confusores, asi que las descripciones que genera dicen "va con" y
"se asocia a", jamas "reduce", "mejora" ni "provoca". Que el spread alto aparezca
junto a un ROI bajo es una coincidencia medida y replicada; por que ocurre es
otra pregunta, y el motor no la responde.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field

UTC = dt.timezone.utc


def _utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


# ---------------------------------------------------------------- estados ----
# Los cuatro estados del enunciado. `proposed` es lo que el generador acaba de
# encontrar (evidencia DENTRO de muestra, que no sirve de nada por si sola),
# `testing` es que ya llega evidencia nueva, y los dos ultimos son veredictos.
PROPOSED = "proposed"
TESTING = "testing"
VALIDATED = "validated"
REJECTED = "rejected"

STATUSES: tuple[str, ...] = (PROPOSED, TESTING, VALIDATED, REJECTED)

# Transiciones PERMITIDAS. El grafo no es completo a proposito:
#
#   - `rejected` es TERMINAL. Si una hipotesis rechazada pudiera volver a
#     `proposed`, bastaria con esperar a la pasada en la que el ruido salga a
#     favor para "redescubrirla": eso es blanquear hipotesis, y es el fallo mas
#     facil de cometer en un motor que propone solo.
#   - `validated` NO es terminal. Una replicacion posterior y mas grande que
#     contradiga el efecto tiene que poder tumbarlo; lo contrario seria fijar
#     una conclusion por haber llegado antes.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    PROPOSED: (TESTING, VALIDATED, REJECTED),
    TESTING: (VALIDATED, REJECTED),
    VALIDATED: (REJECTED,),
    REJECTED: (),
}


def can_transition(current: str, target: str) -> bool:
    """¿Es legal este cambio de estado? Quedarse igual siempre lo es."""
    if current == target:
        return True
    return target in TRANSITIONS.get(current, ())


# ------------------------------------------------------------------ formas ----
# Las tres formas de la GRAMATICA. No son tres hipotesis: son tres maneras de
# relacionar una condicion con un resultado, y cada una se instancia con las
# variables que aparezcan en los datos.
#
#   MONOTONE        una condicion numerica y un resultado numerico se ordenan
#                   juntos (correlacion de rangos de Spearman).
#   GROUP_CONTRAST  un nivel de una etiqueta tiene un resultado medio distinto
#                   al del resto (Welch).
#   THRESHOLD       por encima de un corte, el resultado medio cambia. Cubre las
#                   relaciones en escalon, que la monotona no ve.
MONOTONE = "monotone"
GROUP_CONTRAST = "group_contrast"
THRESHOLD = "threshold"

FORMS: tuple[str, ...] = (MONOTONE, GROUP_CONTRAST, THRESHOLD)

# Papel de una variable. Lo declara la FUENTE (es lineage de datos, no una
# hipotesis): una columna que describe la condicion en la que se tomo la
# observacion es un predictor, y una que describe como acabo es un outcome.
PREDICTOR = "predictor"
OUTCOME = "outcome"
LABEL = "label"

NUMERIC = "numeric"
CATEGORICAL = "categorical"


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(float(value), digits)


def humanize(name: str) -> str:
    """`consensus_delay` -> `consensus delay`.

    Des-slugificacion y nada mas. Es deliberado que no haya un diccionario de
    nombres bonitos: un diccionario seria texto de hipotesis escrito a mano
    entrando por la puerta de atras, y el motor tiene que poder hablar de una
    columna que se añada mañana sin que nadie la traduzca.
    """
    return str(name or "").replace("_", " ").strip()


# ------------------------------------------------------------ observaciones ---
@dataclass(frozen=True)
class Observation:
    """El atomo del motor: un caso completo, ya con su resultado conocido.

    Tres diccionarios y un reloj:

        features   condiciones NUMERICAS del caso (spread, liquidez, tamaño de
                   la cascada...). Candidatas a predictor.
        labels     condiciones CATEGORICAS (categoria, estrategia, lado).
        outcomes   como acabo el caso (roi, error de calibracion, retardo de
                   consenso). Candidatas a outcome.

    `ts` es el instante en el que la observacion QUEDO COMPLETA, no el instante
    en el que empezo. Para una señal de estrategia eso es `resolved_at`, no el
    momento en el que se disparo: hasta que el mercado no resuelve, la
    observacion no existe como evidencia y por tanto el generador no pudo verla.
    Confundir las dos fechas rompe la valla de `created_at` en silencio y hace
    que una hipotesis se "valide" con casos que ya conocia.

    `entity` es de quien es el caso (wallet, mercado, estrategia). Solo se usa
    para poder auditar una observacion hacia atras; el motor no agrupa por ella.
    """

    source: str
    entity: str
    ts: dt.datetime
    features: dict[str, float] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    outcomes: dict[str, float] = field(default_factory=dict)

    def numeric(self, name: str) -> float | None:
        """Valor numerico de `name`, venga de features o de outcomes.

        Devuelve None si falta: un hueco NO es un cero. Un spread que no se
        registro (las filas viejas de `strategy_signals` lo tienen NULL) valdria
        "operar aqui era gratis" si se rellenase con 0.0, que es exactamente la
        conclusion falsa que este motor existe para no publicar.
        """
        for bag in (self.features, self.outcomes):
            if name in bag:
                value = bag[name]
                if value is None:
                    return None
                try:
                    out = float(value)
                except (TypeError, ValueError):
                    return None
                # NaN e infinito entran desde la BD con mas facilidad de la que
                # parece (una division por cero guardada como float). Un NaN se
                # propaga por toda la estadistica sin levantar una sola
                # excepcion, asi que se trata como lo que es: un hueco.
                if out != out or out in (float("inf"), float("-inf")):
                    return None
                return out
        return None

    def label(self, name: str) -> str | None:
        value = self.labels.get(name)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def to_dict(self) -> dict:
        return {
            "source": self.source, "entity": self.entity,
            "ts": self.ts.isoformat(),
            "features": self.features, "labels": self.labels,
            "outcomes": self.outcomes,
        }


@dataclass(frozen=True)
class Variable:
    """Una variable que los datos SOSTIENEN, no una que alguien espera.

    Sale de `features.discover_variables`, que mira la cobertura, la varianza y
    la cardinalidad reales de la ventana. Una columna que existe en el esquema
    pero viene vacia, constante o con un solo nivel no llega a ser una Variable:
    proponer sobre ella daria efectos indefinidos o triviales.
    """

    name: str
    kind: str                 # NUMERIC | CATEGORICAL
    role: str                 # PREDICTOR | OUTCOME | LABEL
    source: str
    n: int = 0                # observaciones con valor presente
    coverage: float = 0.0     # n / total de la ventana
    n_distinct: int = 0
    levels: tuple[str, ...] = ()   # solo categoricas

    def to_dict(self) -> dict:
        return {
            "name": self.name, "kind": self.kind, "role": self.role,
            "source": self.source, "n": self.n,
            "coverage": round(self.coverage, 4),
            "n_distinct": self.n_distinct, "levels": list(self.levels),
        }


@dataclass(frozen=True)
class EffectEstimate:
    """Una estimacion con su incertidumbre. Nunca se publica un efecto desnudo.

    `null` es el valor del efecto cuando no pasa nada (0.0 para una correlacion
    de rangos y para una diferencia de medias). `excludes_null` es la unica
    pregunta que importa: ¿el intervalo se queda de un lado del nulo? Un efecto
    de 0.31 con intervalo [-0.10, 0.62] y otro de 0.19 con [0.11, 0.27] se leen
    al reves de como los ordena el ojo.
    """

    n: int = 0
    effect: float = 0.0
    lower: float = 0.0
    upper: float = 0.0
    p_value: float = 1.0
    null: float = 0.0

    @property
    def direction(self) -> int:
        if self.effect > self.null:
            return 1
        if self.effect < self.null:
            return -1
        return 0

    @property
    def excludes_null(self) -> bool:
        return self.lower > self.null or self.upper < self.null

    @property
    def magnitude_lower(self) -> float:
        """Cuanto efecto sostiene la muestra en el peor caso del intervalo.

        Es |efecto| descontada la incertidumbre: si el intervalo cruza el nulo,
        vale 0.0, porque lo que la muestra sostiene entonces es "nada".
        """
        if not self.excludes_null:
            return 0.0
        return min(abs(self.lower - self.null), abs(self.upper - self.null))

    def to_dict(self) -> dict:
        return {
            "n": int(self.n), "effect": _round(self.effect),
            "lower": _round(self.lower), "upper": _round(self.upper),
            "p_value": _round(self.p_value), "null": _round(self.null),
            "direction": self.direction, "excludes_null": self.excludes_null,
            "magnitude_lower": _round(self.magnitude_lower),
        }


EMPTY_EFFECT = EffectEstimate()


# --------------------------------------------------------------- hipotesis ----
@dataclass
class Hypothesis:
    """Una hipotesis de investigacion generada a partir de datos observados.

    Los siete campos del contrato publico van primero (`id`, `description`,
    `status`, `confidence`, `sample_count`, `created_at`, `updated_at`); el resto
    es la trazabilidad que hace auditable a los siete.

    SOBRE `id`: es el hash del ENUNCIADO (forma + fuente + predictor + nivel +
    outcome), no un aleatorio y no un autoincremental. Dos consecuencias
    buscadas:

      1. redescubrir la misma relacion en otra pasada cae en la MISMA fila y
         acumula evidencia, en vez de crear una hipotesis "nueva" cada hora;
      2. la DIRECCION no entra en el hash. "A mayor spread, menor ROI" y "a
         mayor spread, mayor ROI" son la misma hipotesis sobre la misma
         relacion, con signos opuestos. Si la direccion formase parte de la
         identidad, un cambio de signo crearia una hipotesis virgen y el motor
         podria tirar la moneda hasta que saliera cara. Asi no: el signo se
         congela al proponer y un giro fuera de muestra es un RECHAZO.

    SOBRE `confidence`: NO es la probabilidad de que la hipotesis sea cierta.
    Ese numero requeriria un previo que nadie tiene. Es un resumen acotado [0,1]
    y monotono de la FUERZA DE LA EVIDENCIA FUERA DE MUESTRA: crece con el efecto
    que sostiene la cota inferior y con el tamaño de muestra, y vale exactamente
    0.0 mientras la hipotesis siga en `proposed`, porque en `proposed` no hay ni
    una observacion que el motor no hubiera visto ya.

    SOBRE `sample_count`: son las observaciones POSTERIORES a `created_at`. No es
    el total de datos de la fuente ni el tamaño de la ventana de descubrimiento.
    """

    # --- contrato publico ---
    id: str = ""
    description: str = ""
    status: str = PROPOSED
    confidence: float = 0.0
    sample_count: int = 0
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None

    # --- de donde salio (todo descubierto) ---
    form: str = MONOTONE
    source: str = ""
    predictor: str = ""
    outcome: str = ""
    level: str = ""              # solo GROUP_CONTRAST
    direction: int = 0           # signo congelado al proponer: +1 | -1
    # Parametros CONGELADOS en el momento de proponer (p. ej. el corte de la
    # forma umbral). Se congelan porque reajustar el corte sobre los datos de
    # prueba es exactamente la fuga que la valla temporal intenta cerrar: se
    # estaria eligiendo el corte que mejor funciona en el test y llamandolo
    # replicacion.
    params: dict = field(default_factory=dict)

    # --- evidencia de descubrimiento (DENTRO de muestra: no es evidencia) ---
    discovery: EffectEstimate = EMPTY_EFFECT
    # Cuantas relaciones se probaron en la pasada que la propuso. Sin este numero
    # no se puede juzgar nada: encontrar un efecto al 5% habiendo probado 400
    # relaciones es lo que se espera del azar veinte veces.
    tested_in_pass: int = 0

    # --- evidencia fuera de muestra (la unica que cuenta) ---
    test: EffectEstimate = EMPTY_EFFECT

    # --- traza ---
    status_reason: str = ""
    first_tested_at: dt.datetime | None = None
    decided_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        now = _utcnow()
        if self.created_at is None:
            self.created_at = now
        if self.updated_at is None:
            self.updated_at = self.created_at
        if not self.id:
            self.id = self.statement_id()

    # ------------------------------------------------------------ identidad --
    def statement(self) -> str:
        """Forma canonica del enunciado. Sin direccion (ver el docstring)."""
        return "|".join((self.form, self.source, self.predictor,
                         self.level or "-", self.outcome))

    def statement_id(self) -> str:
        digest = hashlib.sha1(self.statement().encode("utf-8")).hexdigest()
        return "h_" + digest[:16]

    @property
    def decided(self) -> bool:
        return self.status in (VALIDATED, REJECTED)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status,
            "confidence": round(float(self.confidence), 4),
            "sample_count": int(self.sample_count),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "form": self.form, "source": self.source,
            "predictor": self.predictor, "outcome": self.outcome,
            "level": self.level, "direction": int(self.direction),
            "params": dict(self.params),
            "discovery": self.discovery.to_dict(),
            "tested_in_pass": int(self.tested_in_pass),
            "test": self.test.to_dict(),
            "status_reason": self.status_reason,
            "first_tested_at":
                self.first_tested_at.isoformat() if self.first_tested_at else None,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }
