"""HYPOTHESIS ENGINE (HE) — V1, PostgreSQL.

Que hace: GENERA HIPOTESIS DE INVESTIGACION a partir de los datos que Medusa ya
observa, las pone a prueba con datos que no pudo ver, y las valida o las rechaza.

    observaciones cosechadas
        v
    variables que los datos sostienen        (cobertura, varianza, cardinalidad)
        v
    gramatica x variables = cientos de contrastes
        v
    los que sobreviven al tamaño de efecto y al FDR -> hipotesis `proposed`
        v
    llegan observaciones NUEVAS (posteriores a `created_at`)
        v
    `testing` -> `validated` | `rejected`

LAS DOS REGLAS QUE DEFINEN EL MOTOR:

  1. NINGUNA HIPOTESIS ESTA ESCRITA EN EL CODIGO. Lo que esta escrito es una
     GRAMATICA de tres formas -- monotona, contraste de grupos y umbral -- y el
     LINEAGE de las fuentes: que columna describe la condicion de un caso y cual
     describe como acabo. El enunciado concreto (que variable, contra que
     resultado, en que direccion, con que corte) sale de los datos en cada pasada.
     Hay un test que recorre el AST del paquete y tumba la suite si aparece una
     frase de hipotesis escrita a mano.

  2. UNA HIPOTESIS SE VALIDA CON DATOS QUE NO PUDO VER. `created_at` es una VALLA:
     `sample_count` cuenta exclusivamente las observaciones posteriores a ella, y
     `confidence` vale 0.0 hasta que llegue la primera. Sin esa valla el motor
     seria una maquina de confirmarse -- propondria la relacion mas llamativa de la
     ventana y la "validaria" con la misma ventana, que es elegir el numero
     despues de ver la ruleta.

QUE NO ES, y es la parte importante:

  - NO ES UN MOTOR DE CAUSALIDAD. Observa ASOCIACION. No hay contrafactual, no hay
    asignacion aleatoria y no hay control de confusores, asi que los enunciados que
    genera dicen "va con" y "se asocia a", y jamas "reduce", "mejora" ni "provoca".
    Que el spread alto aparezca junto a un ROI bajo es una coincidencia medida y
    replicada; por que ocurre es otra pregunta, y el motor no la responde.
  - NO es un motor de trading. No manda ordenes.
  - NO es una estrategia. Una hipotesis validada es una frase con un intervalo de
    confianza: no tiene lado, ni tamaño, ni precio de entrada.
  - NO toca el Risk Manager. Ni lo importa.
  - NO acepta hipotesis escritas a mano. No hay endpoint para crearlas, y su
    ausencia es la funcionalidad.

Aditivo por construccion: cinco tablas nuevas con prefijo `hyp_`, un router HTTP
nuevo, dos paginas nuevas en el panel y un loop opcional en el engine. Apagado por
defecto (`HYPOTHESIS_ENABLED=false`). Nada del sistema existente cambia de
comportamiento tanto si esta encendido como si no.

Mapa del paquete:

    types.py       vocabulario (Observation, Variable, EffectEstimate,
                   Hypothesis) + los cuatro estados y sus transiciones legales
    stats.py       estadistica pura (Spearman, Fisher, Welch, Benjamini-Hochberg)
    features.py    observaciones -> variables utilizables (puro)
    generator.py   gramatica x variables -> hipotesis propuestas (puro)
    evaluator.py   hipotesis + datos posteriores -> veredicto (puro). LA VALLA.
    sources.py     tablas de Medusa -> observaciones (solo lectura)
    models.py      tablas ORM (hyp_observations, hyp_hypotheses, hyp_evidence,
                   hyp_transitions, hyp_snapshots)
    migrations.py  DDL idempotente (indices y unicidad)
    repository.py  persistencia y consultas
    service.py     orquestacion (cosechar -> proponer -> evaluar -> persistir)
    api.py         router HTTP /hypotheses/*
"""

from medusa.intelligence.hypothesis.evaluator import (
    evaluate,
    measure,
    out_of_sample,
)
from medusa.intelligence.hypothesis.features import discover_variables
from medusa.intelligence.hypothesis.generator import Proposal, describe, propose
from medusa.intelligence.hypothesis.migrations import HYPOTHESIS_MIGRATIONS
from medusa.intelligence.hypothesis.service import HypothesisService
from medusa.intelligence.hypothesis.types import (
    FORMS,
    GROUP_CONTRAST,
    MONOTONE,
    PROPOSED,
    REJECTED,
    STATUSES,
    TESTING,
    THRESHOLD,
    VALIDATED,
    EffectEstimate,
    Hypothesis,
    Observation,
    Variable,
    can_transition,
)

__all__ = [
    "EffectEstimate",
    "FORMS",
    "GROUP_CONTRAST",
    "HYPOTHESIS_MIGRATIONS",
    "Hypothesis",
    "HypothesisService",
    "MONOTONE",
    "Observation",
    "PROPOSED",
    "Proposal",
    "REJECTED",
    "STATUSES",
    "TESTING",
    "THRESHOLD",
    "VALIDATED",
    "Variable",
    "can_transition",
    "describe",
    "discover_variables",
    "evaluate",
    "measure",
    "out_of_sample",
    "propose",
]
