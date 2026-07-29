"""Constructor del grafo: datos crudos de Medusa -> nodos y aristas.

FUNCIONES PURAS. El builder no abre sesiones, no llama a la API de Polymarket y
no conoce Postgres: recibe listas de dicts (las mismas filas que ya devuelven
los repositorios de Medusa) y devuelve un `IntelligenceGraph`. Esa pureza es
lo que permite testear TODA la semantica del grafo sin base de datos, y es
tambien lo que garantiza que este paquete no pueda tener efectos secundarios
sobre el runtime de trading.

Fuentes que entiende hoy (todas OPCIONALES: lo que no llega, no se construye):

    markets     -> filas de `markets`            (Market, Category, Event)
    signals     -> filas de `strategy_signals`   (Strategy, Outcome, Experiment)
    trades      -> filas de `trades`             (Trade)
    features    -> filas de `features`           (Feature)
    wallets     -> fuente EXTERNA opcional       (Wallet)

Sobre Wallet: Medusa no persiste hoy actividad de wallets ajenas (no hay tabla
ni ingesta de la Data API). El grafo SABE representarlas y el builder las
construye si alguien le pasa la fuente, pero en V1 esa fuente llega vacia. Se
documenta asi a proposito: es preferible un tipo de nodo con 0 filas y semantica
clara que inventarse datos para que el dashboard enseñe un numero bonito.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from typing import Any, Iterable

from medusa.intelligence.mig.graph import IntelligenceGraph
from medusa.intelligence.mig.types import EdgeType, NodeType, node_key

# Palabras que no distinguen un mercado de otro: si entran en el parecido, todo
# se parece con todo y `similar_to` deja de significar nada.
_STOPWORDS = frozenset({
    "will", "the", "a", "an", "of", "in", "on", "at", "to", "for", "by", "be",
    "is", "are", "was", "were", "and", "or", "if", "than", "then", "that",
    "this", "with", "before", "after", "any", "who", "what", "when", "how",
    "de", "la", "el", "los", "las", "en", "y", "o", "un", "una", "por", "para",
})
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Sufijo de una serie: fecha ISO, hora HHMM o numero de ronda al final del slug.
_SERIES_TAIL_RE = re.compile(r"^(\d{1,4}|\d{4}-\d{2}-\d{2})$")


def _f(row: dict, key: str, default: float = 0.0) -> float:
    """Float defensivo: los repositorios devuelven None en columnas nullable."""
    try:
        val = row.get(key)
        return default if val is None else float(val)
    except (TypeError, ValueError):
        return default


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower())
            if len(t) > 2 and t not in _STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / float(len(a | b)) if inter else 0.0


def event_key_from_slug(slug: str) -> str:
    """Serie a la que pertenece un mercado, deducida de su slug.

    Polymarket nombra las series repitiendo un prefijo y variando la cola:
    `bnb-updown-5m-2026-07-28-1200` -> serie `bnb-updown-5m`. Se recortan SOLO
    las colas que son fecha, hora o numero de ronda; si no se recorta nada, el
    mercado no pertenece a ninguna serie deducible y devuelve "".

    Es una deduccion, no un dato de Polymarket: Medusa no persiste el event_id
    de Gamma. Por eso `belongs_to(event)` solo se crea cuando la serie agrupa
    DOS mercados o mas (ver `build_graph`): una serie de uno no es una serie.
    """
    parts = [p for p in (slug or "").strip().lower().split("-") if p]
    if len(parts) < 2:
        return ""
    trimmed = 0
    while len(parts) > 1 and _SERIES_TAIL_RE.match(parts[-1]):
        parts.pop()
        trimmed += 1
    return "-".join(parts) if trimmed else ""


def _resolution_of_signal(row: dict) -> str | None:
    """A que resolvio el MERCADO de una señal resuelta: "YES" | "NO" | None.

    Ojo con la distincion: `won` es del lado que apostó la señal, no del
    mercado. Una señal NO ganadora significa que el mercado resolvio NO.
    """
    if row.get("status") != "resolved" or row.get("won") is None:
        return None
    side = str(row.get("outcome") or "YES").upper()
    won = bool(row.get("won"))
    if side == "YES":
        return "YES" if won else "NO"
    return "NO" if won else "YES"


def _resolution_of_trade(row: dict) -> str | None:
    side = str(row.get("outcome") or "YES").upper()
    won = row.get("won")
    if won is None:
        return None
    if side == "YES":
        return "YES" if bool(won) else "NO"
    return "NO" if bool(won) else "YES"


def _mean(vals: Iterable[float]) -> float:
    vals = list(vals)
    return sum(vals) / len(vals) if vals else 0.0


def _stdev(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    mu = _mean(vals)
    return (sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5


# --------------------------------------------------------------- sub-builders --
def add_markets(g: IntelligenceGraph, markets: list[dict]) -> None:
    """Market + Category + Event, con sus `belongs_to`."""
    series: dict[str, list[dict]] = defaultdict(list)

    for m in markets:
        mid = str(m.get("id") or "")
        if not mid:
            continue
        g.node(
            NodeType.MARKET, mid,
            label=str(m.get("question") or "")[:300],
            weight=_f(m, "opportunity_score"),
            slug=str(m.get("slug") or ""),
            category=str(m.get("medusa_category") or m.get("category") or ""),
            volume_24h=_f(m, "volume_24h"),
            liquidity=_f(m, "liquidity"),
        )
        cat = str(m.get("medusa_category") or m.get("category") or "").strip()
        if cat:
            g.node(NodeType.CATEGORY, cat, label=cat)
            g.edge(node_key(NodeType.MARKET, mid), node_key(NodeType.CATEGORY, cat),
                   EdgeType.BELONGS_TO, weight=1.0)
        ev = event_key_from_slug(str(m.get("slug") or ""))
        if ev:
            series[ev].append(m)

    # Una serie de UN mercado no es una serie: seria un nodo Event inventado.
    for ev, rows in series.items():
        if len(rows) < 2:
            continue
        g.node(NodeType.EVENT, ev, label=ev, weight=float(len(rows)), markets=len(rows))
        for m in rows:
            g.edge(node_key(NodeType.MARKET, str(m["id"])),
                   node_key(NodeType.EVENT, ev), EdgeType.BELONGS_TO, weight=1.0)


def add_event_chain(g: IntelligenceGraph, markets: list[dict]) -> None:
    """`preceded` / `followed` entre mercados consecutivos de la misma serie.

    El orden lo da `end_date`. Sin fecha no hay orden, y sin orden no hay
    "antes": esos mercados se quedan sin cadena en vez de encadenarse al azar.
    """
    series: dict[str, list[tuple[dt.datetime, str]]] = defaultdict(list)
    for m in markets:
        ev = event_key_from_slug(str(m.get("slug") or ""))
        end = m.get("end_date")
        if not ev or not end:
            continue
        if isinstance(end, str):
            try:
                end = dt.datetime.fromisoformat(end)
            except ValueError:
                continue
        series[ev].append((end, str(m.get("id") or "")))

    for ev, items in series.items():
        if len(items) < 2 or not g.has_node(node_key(NodeType.EVENT, ev)):
            continue
        items.sort(key=lambda x: x[0])
        for (_, prev_id), (_, next_id) in zip(items, items[1:]):
            a = node_key(NodeType.MARKET, prev_id)
            b = node_key(NodeType.MARKET, next_id)
            g.edge(a, b, EdgeType.PRECEDED, weight=1.0, event=ev)
            g.edge(b, a, EdgeType.FOLLOWED, weight=1.0, event=ev)


def add_similarity(
    g: IntelligenceGraph, markets: list[dict], min_similarity: float = 0.35,
    max_markets: int = 300,
) -> None:
    """`similar_to` entre mercados de la MISMA categoria.

    Coste: O(k^2) DENTRO de cada categoria, con el universo recortado a
    `max_markets` (los primeros que llegan, que vienen ya ordenados por score
    del pre-scorer). Con el default (300 mercados) el peor caso es ~45k
    comparaciones de conjuntos pequeños: milisegundos incluso en la CPU del
    CT202. Subir el tope sin rehacer esta cuenta es como se cuela un builder que
    se come el ciclo de mantenimiento.
    """
    by_cat: dict[str, list[tuple[str, set[str]]]] = defaultdict(list)
    for m in markets[:max_markets]:
        mid = str(m.get("id") or "")
        if not mid:
            continue
        cat = str(m.get("medusa_category") or m.get("category") or "").strip() or "other"
        by_cat[cat].append((mid, _tokens(str(m.get("question") or ""))))

    for pool in by_cat.values():
        for i, (id_a, tok_a) in enumerate(pool):
            for id_b, tok_b in pool[i + 1:]:
                sim = _jaccard(tok_a, tok_b)
                if sim >= min_similarity:
                    g.edge(node_key(NodeType.MARKET, id_a),
                           node_key(NodeType.MARKET, id_b),
                           EdgeType.SIMILAR_TO, weight=round(sim, 4))


def add_signals(g: IntelligenceGraph, signals: list[dict]) -> None:
    """Strategy, Outcome y las relaciones que salen del historial shadow.

        Strategy -participated_in-> Market   (emitio al menos una señal)
        Strategy -predicted-> Market         (peso = edge medio declarado)
        Strategy -won|lost-> Outcome         (señales ya resueltas)
        Strategy -specialized_in-> Category  (peso = ROI taker medio)

    El ROI que se usa es SIEMPRE el taker (`roi`), nunca el maker: es la cota
    pesimista y la unica que decide en este proyecto.
    """
    per_market: dict[tuple[str, str], list[dict]] = defaultdict(list)
    per_category: dict[tuple[str, str], list[dict]] = defaultdict(list)

    for s in signals:
        strat = str(s.get("strategy") or "").strip()
        mid = str(s.get("market_id") or "").strip()
        if not strat or not mid:
            continue
        g.node(NodeType.STRATEGY, strat, label=strat)
        if not g.has_node(node_key(NodeType.MARKET, mid)):
            # El mercado puede no estar en la muestra de `markets` (señal vieja).
            # Se crea el nodo con lo que la propia señal sabe: sin el, todas sus
            # aristas se caerian por extremo inexistente.
            g.node(NodeType.MARKET, mid, label=str(s.get("question") or "")[:300],
                   category=str(s.get("category") or ""))
        cat = str(s.get("category") or "").strip()
        if cat:
            g.node(NodeType.CATEGORY, cat, label=cat)
            g.edge(node_key(NodeType.MARKET, mid), node_key(NodeType.CATEGORY, cat),
                   EdgeType.BELONGS_TO, weight=1.0)
        per_market[(strat, mid)].append(s)
        if cat and s.get("status") == "resolved":
            per_category[(strat, cat)].append(s)

        resolution = _resolution_of_signal(s)
        if resolution is None:
            continue
        out_id = f"{mid}:{resolution}"
        g.node(NodeType.OUTCOME, out_id, label=f"{resolution} @ {mid}",
               weight=1.0, market_id=mid, resolution=resolution)
        g.edge(node_key(NodeType.MARKET, mid), node_key(NodeType.OUTCOME, out_id),
               EdgeType.BELONGS_TO, weight=1.0)
        g.edge(
            node_key(NodeType.STRATEGY, strat), node_key(NodeType.OUTCOME, out_id),
            EdgeType.WON if s.get("won") else EdgeType.LOST,
            weight=_f(s, "roi"), source="signal",
        )

    for (strat, mid), rows in per_market.items():
        a, b = node_key(NodeType.STRATEGY, strat), node_key(NodeType.MARKET, mid)
        g.edge(a, b, EdgeType.PARTICIPATED_IN, weight=float(len(rows)), count=len(rows),
               signals=len(rows))
        g.edge(a, b, EdgeType.PREDICTED, weight=_mean(_f(r, "edge") for r in rows),
               count=len(rows))

    for (strat, cat), rows in per_category.items():
        wins = sum(1 for r in rows if r.get("won"))
        g.edge(
            node_key(NodeType.STRATEGY, strat), node_key(NodeType.CATEGORY, cat),
            EdgeType.SPECIALIZED_IN, weight=_mean(_f(r, "roi") for r in rows),
            count=len(rows), resolved=len(rows), wins=wins,
            win_rate=round(wins / len(rows), 4) if rows else 0.0,
        )


def add_trades(g: IntelligenceGraph, trades: list[dict]) -> None:
    """Trade cerrado -> nodo Trade + su cadena de relaciones.

        Trade -belongs_to-> Market
        Strategy -participated_in-> Trade
        Trade -won|lost-> Outcome
    """
    for t in trades:
        tid = t.get("id")
        mid = str(t.get("market_id") or "").strip()
        if tid is None or not mid:
            continue
        g.node(NodeType.TRADE, str(tid), label=str(t.get("question") or "")[:300],
               weight=_f(t, "pnl"), market_id=mid, mode=str(t.get("mode") or ""),
               roi=_f(t, "roi"), outcome=str(t.get("outcome") or ""))
        if not g.has_node(node_key(NodeType.MARKET, mid)):
            g.node(NodeType.MARKET, mid, label=str(t.get("question") or "")[:300])
        g.edge(node_key(NodeType.TRADE, str(tid)), node_key(NodeType.MARKET, mid),
               EdgeType.BELONGS_TO, weight=1.0)

        strat = str(t.get("strategy") or "").strip()
        if strat:
            g.node(NodeType.STRATEGY, strat, label=strat)
            g.edge(node_key(NodeType.STRATEGY, strat), node_key(NodeType.TRADE, str(tid)),
                   EdgeType.PARTICIPATED_IN, weight=_f(t, "pnl"))

        resolution = _resolution_of_trade(t)
        if resolution is None:
            continue
        out_id = f"{mid}:{resolution}"
        g.node(NodeType.OUTCOME, out_id, label=f"{resolution} @ {mid}", weight=1.0,
               market_id=mid, resolution=resolution)
        g.edge(node_key(NodeType.MARKET, mid), node_key(NodeType.OUTCOME, out_id),
               EdgeType.BELONGS_TO, weight=1.0)
        g.edge(node_key(NodeType.TRADE, str(tid)), node_key(NodeType.OUTCOME, out_id),
               EdgeType.WON if t.get("won") else EdgeType.LOST, weight=_f(t, "pnl"))


def add_features(g: IntelligenceGraph, features: list[dict]) -> None:
    """Feature (modulo::nombre) + `supports` / `contradicts` sobre Outcomes.

    HEURISTICA DE V1, y hay que leerla como lo que es: para cada feature se
    calcula la media y la desviacion de sus valores en la muestra; para cada
    mercado YA RESUELTO se mira si su desviacion va en el mismo sentido que la
    resolucion (por encima de la media + resolvio YES => `supports`).

    Esto es ASOCIACION, no causalidad, y menos aun edge: no ha pasado ningun
    contraste out-of-sample. Por eso vive en el grafo y NO en el Feature Store,
    y por eso el Discovery que sale de aqui propone un nombre de feature en vez
    de crearla. El proyecto ya tiene medido lo que cuesta confundir "se veia
    bien" con "gana dinero".
    """
    latest: dict[tuple[str, str], dict] = {}
    for f in features:
        name = str(f.get("name") or "").strip()
        mid = str(f.get("market_id") or "").strip()
        if not name or not mid:
            continue
        module = str(f.get("module") or "").strip() or "?"
        fkey = f"{module}::{name}"
        g.node(NodeType.FEATURE, fkey, label=name, module=module, feature=name)
        # Se usa la ULTIMA lectura por (feature, mercado): mezclar el historico
        # entero daria mas peso a los mercados mas observados.
        prev = latest.get((fkey, mid))
        if prev is None or str(f.get("ts") or "") >= str(prev.get("ts") or ""):
            latest[(fkey, mid)] = f

    by_feature: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for (fkey, mid), row in latest.items():
        by_feature[fkey].append((mid, _f(row, "value")))

    for fkey, pairs in by_feature.items():
        values = [v for _, v in pairs]
        mu, sd = _mean(values), _stdev(values)
        if sd <= 0:
            continue   # feature constante en la muestra: no discrimina nada
        for mid, value in pairs:
            dev = (value - mu) / sd
            if abs(dev) < 0.5:
                continue   # ruido alrededor de la media: no afirma nada
            for resolution, sign in (("YES", 1.0), ("NO", -1.0)):
                out_key = node_key(NodeType.OUTCOME, f"{mid}:{resolution}")
                if not g.has_node(out_key):
                    continue
                agrees = (dev * sign) > 0
                g.edge(
                    node_key(NodeType.FEATURE, fkey), out_key,
                    EdgeType.SUPPORTS if agrees else EdgeType.CONTRADICTS,
                    weight=round(abs(dev), 4), z=round(dev, 4), value=round(value, 6),
                )


def add_experiments(g: IntelligenceGraph, signals: list[dict], min_samples: int = 30) -> None:
    """Experiment = celda de hipotesis (estrategia x categoria) con muestra.

    Es la unidad con la que Medusa ya descubre que funciona: "¿tiene esta
    estrategia edge EN ESTA categoria?". El grafo la hace explicita:

        Strategy -participated_in-> Experiment
        Experiment -belongs_to-> Category
        Experiment -supports|contradicts-> Strategy

    `supports` exige muestra suficiente (`min_samples`, el mismo umbral que usa
    el asignador de capital) Y ROI taker medio > 0. Sin muestra no se afirma
    nada: la celda existe como nodo pero no emite veredicto.
    """
    cells: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in signals:
        strat = str(s.get("strategy") or "").strip()
        cat = str(s.get("category") or "").strip()
        if strat and cat and s.get("status") == "resolved":
            cells[(strat, cat)].append(s)

    for (strat, cat), rows in cells.items():
        exp_id = f"{strat}|{cat}"
        rois = [_f(r, "roi") for r in rows]
        avg_roi = _mean(rois)
        wins = sum(1 for r in rows if r.get("won"))
        g.node(NodeType.EXPERIMENT, exp_id, label=f"{strat} @ {cat}", weight=avg_roi,
               strategy=strat, category=cat, n=len(rows), wins=wins,
               avg_roi=round(avg_roi, 6))
        g.node(NodeType.STRATEGY, strat, label=strat)
        g.node(NodeType.CATEGORY, cat, label=cat)
        g.edge(node_key(NodeType.STRATEGY, strat), node_key(NodeType.EXPERIMENT, exp_id),
               EdgeType.PARTICIPATED_IN, weight=float(len(rows)), count=len(rows))
        g.edge(node_key(NodeType.EXPERIMENT, exp_id), node_key(NodeType.CATEGORY, cat),
               EdgeType.BELONGS_TO, weight=1.0)
        if len(rows) < min_samples:
            continue
        g.edge(
            node_key(NodeType.EXPERIMENT, exp_id), node_key(NodeType.STRATEGY, strat),
            EdgeType.SUPPORTS if avg_roi > 0 else EdgeType.CONTRADICTS,
            weight=round(abs(avg_roi), 6), count=len(rows), n=len(rows),
            avg_roi=round(avg_roi, 6),
        )


def add_wallets(g: IntelligenceGraph, wallets: list[dict]) -> None:
    """Wallet -participated_in-> Market y -specialized_in-> Category.

    Fuente EXTERNA y opcional (ver cabecera del modulo): en V1 Medusa no la
    ingiere, asi que esta lista llega vacia y no se crea ningun nodo Wallet.
    """
    for w in wallets:
        addr = str(w.get("address") or "").strip()
        if not addr:
            continue
        g.node(NodeType.WALLET, addr, label=addr[:12] + "…" if len(addr) > 12 else addr,
               weight=_f(w, "roi"), win_rate=_f(w, "win_rate"))
        for mid in w.get("markets") or []:
            mkey = node_key(NodeType.MARKET, str(mid))
            if not g.has_node(mkey):
                g.node(NodeType.MARKET, str(mid))
            g.edge(node_key(NodeType.WALLET, addr), mkey, EdgeType.PARTICIPATED_IN,
                   weight=1.0)
        for cat, n in (w.get("categories") or {}).items():
            g.node(NodeType.CATEGORY, str(cat), label=str(cat))
            g.edge(node_key(NodeType.WALLET, addr), node_key(NodeType.CATEGORY, str(cat)),
                   EdgeType.SPECIALIZED_IN, weight=float(n), count=int(n))


# ------------------------------------------------------------------ fachada --
def build_graph(
    markets: list[dict] | None = None,
    signals: list[dict] | None = None,
    trades: list[dict] | None = None,
    features: list[dict] | None = None,
    wallets: list[dict] | None = None,
    *,
    min_samples: int = 30,
    min_similarity: float = 0.35,
    max_markets: int = 300,
) -> IntelligenceGraph:
    """Construye el grafo completo a partir de las fuentes disponibles.

    Todas las fuentes son opcionales: con `build_graph()` sin argumentos sale un
    grafo vacio y valido. El orden importa (mercados antes que señales, señales
    antes que features) porque una arista sin sus dos extremos se descarta.
    """
    g = IntelligenceGraph()
    markets = markets or []
    signals = signals or []

    add_markets(g, markets)
    add_event_chain(g, markets)
    add_similarity(g, markets, min_similarity=min_similarity, max_markets=max_markets)
    add_signals(g, signals)
    add_trades(g, trades or [])
    add_experiments(g, signals, min_samples=min_samples)
    add_features(g, features or [])
    add_wallets(g, wallets or [])
    return g


def build_from_sources(sources: dict[str, Any], **kwargs: Any) -> IntelligenceGraph:
    """Igual que `build_graph` pero desde un dict de fuentes (lo que produce el
    servicio al leer la BD)."""
    return build_graph(
        markets=sources.get("markets"), signals=sources.get("signals"),
        trades=sources.get("trades"), features=sources.get("features"),
        wallets=sources.get("wallets"), **kwargs,
    )
