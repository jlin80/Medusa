#!/usr/bin/env python
"""Self-test de Medusa. Se ejecuta DENTRO del contenedor engine:

    docker compose exec engine python scripts/selftest.py

Comprueba lo que de verdad puede arruinar el bot en silencio:
  1. La simulacion de fills del Paper Engine (spread, slippage, fill parcial,
     rechazo por tamaño minimo) con libros sinteticos y numeros verificables.
  2. Que el Live Engine se niega a operar mientras no este desbloqueado.
  3. Que la API de Polymarket sigue respondiendo con el shape que esperamos.

No es un sustituto de pytest; es una verificacion end-to-end del camino critico
en el entorno real donde corre el bot.
"""

import asyncio
import sys

sys.path.insert(0, "/app")

from medusa.config import get_settings                       # noqa: E402
from medusa.core.models import OrderBook, OrderRequest       # noqa: E402
from medusa.data.polymarket.client import PolymarketClient   # noqa: E402
from medusa.execution.paper import PaperExecutionEngine      # noqa: E402

FAILS: list[str] = []


class _Log:
    def _p(self, lvl, event, **kw):
        detail = " ".join(f"{k}={v}" for k, v in kw.items())
        print(f"    [{lvl}] {event} {detail}")

    def info(self, e, **kw): pass
    def debug(self, e, **kw): pass
    def warning(self, e, **kw): self._p("warn", e, **kw)
    def error(self, e, **kw): self._p("error", e, **kw)


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not condition:
        FAILS.append(name)


def test_fill_simulation() -> None:
    print("\n[1] Simulacion de fills del Paper Engine")
    s = get_settings()
    engine = PaperExecutionEngine(client=None, log=_Log())

    # Libro con profundidad de sobra: 1000 shares a 0.50 y 1000 a 0.51.
    deep = OrderBook(
        token_id="t",
        bids=[(0.49, 1000.0), (0.48, 1000.0)],
        asks=[(0.50, 1000.0), (0.51, 1000.0)],
    )

    # --- compra pequeña: cabe entera en el primer nivel ---
    r = engine._simulate(deep, OrderRequest(token_id="t", side="buy", usd_size=50.0))
    check("compra pequeña se llena entera", r.status == "filled", r.status)
    check(
        "paga el ask (0.50) + slippage, nunca el mid (0.495)",
        r.avg_price > 0.50,
        f"avg={r.avg_price:.5f}",
    )
    expected = 0.50 * (1 + s.extra_slippage_bps / 10_000.0)
    check("precio medio = ask con slippage aplicado", abs(r.avg_price - expected) < 1e-9,
          f"{r.avg_price:.6f} vs {expected:.6f}")
    check("no gasta mas del presupuesto", r.cash_out <= 50.0 + 1e-6, f"cash_out={r.cash_out:.4f}")
    check("registra coste de spread > 0", r.fills[0].spread_cost > 0,
          f"{r.fills[0].spread_cost:.4f}")
    check("registra coste de slippage > 0", r.fills[0].slippage_cost > 0,
          f"{r.fills[0].slippage_cost:.4f}")

    # --- compra que barre varios niveles ---
    big = engine._simulate(deep, OrderRequest(token_id="t", side="buy", usd_size=400.0))
    check("orden grande consume mas de un nivel", len(big.fills) >= 2, f"{len(big.fills)} fills")
    check("precio medio empeora al barrer el libro", big.avg_price > r.avg_price,
          f"{big.avg_price:.4f} > {r.avg_price:.4f}")

    # --- libro fino: fill parcial ---
    thin = OrderBook(token_id="t", bids=[(0.49, 40.0)], asks=[(0.50, 40.0)])
    p = engine._simulate(thin, OrderRequest(token_id="t", side="buy", usd_size=500.0))
    check("libro fino produce fill PARCIAL", p.status == "partial", p.status)
    check(
        f"solo consume el {s.book_depth_fraction:.0%} del nivel mostrado",
        abs(p.filled_size - 40.0 * s.book_depth_fraction) < 1e-6,
        f"filled={p.filled_size:.2f}",
    )

    # --- venta: cobra el bid, nunca el mid ---
    sell = engine._simulate(deep, OrderRequest(token_id="t", side="sell", shares=100.0))
    check("la venta cobra por debajo del bid (slippage en contra)", sell.avg_price < 0.49,
          f"avg={sell.avg_price:.5f}")
    check("vender ingresa caja", sell.cash_in > 0, f"cash_in={sell.cash_in:.2f}")

    # --- rechazo por debajo del minimo de Polymarket ---
    tiny = engine._simulate(deep, OrderRequest(token_id="t", side="buy", usd_size=1.0))
    check(f"rechaza fill < {s.min_order_shares} shares (orderMinSize)",
          tiny.status == "rejected", tiny.reason)

    # --- libro vacio ---
    empty = engine._simulate(OrderBook(token_id="t"), OrderRequest(token_id="t", side="buy", usd_size=50.0))
    check("libro vacio se rechaza sin excepcion", empty.status == "rejected", empty.reason)

    # --- round-trip sin movimiento de precio debe PERDER (spread+slippage) ---
    shares = r.filled_size
    back = engine._simulate(deep, OrderRequest(token_id="t", side="sell", shares=shares))
    pnl = back.cash_in - r.cash_out
    check("ida y vuelta sin mover el precio pierde dinero (coste real de operar)",
          pnl < 0, f"PnL={pnl:+.4f} USDC")


def test_risk_rules() -> None:
    """Las dos reglas que hacen viable un run desatendido de 7-14 dias."""
    print("\n[2] Reglas de riesgo")
    from medusa.core.models import Opportunity
    from medusa.risk.manager import RiskManager

    s = get_settings()
    risk = RiskManager(_Log())

    def opp(edge, spread, price=0.06, liq=5000.0):
        return Opportunity(
            market_id="m", question="q", outcome="YES", market_price=price,
            market_prob=price, medusa_prob=price + edge, edge=edge, confidence=0.9,
            action="buy_yes", token_id="t", spread=spread, liquidity=liq,
        )

    # Un mercado de precio extremo con spread ancho: el spread se traga el edge.
    wide = opp(edge=0.04, spread=0.035)
    check("rechaza entrada donde el spread se come el edge",
          not risk._edge_beats_cost(wide), "edge 0.040 vs spread 0.035")

    # Mismo edge, spread estrecho: aqui si hay margen sobre el coste.
    tight = opp(edge=0.04, spread=0.001)
    check("acepta entrada con spread estrecho", risk._edge_beats_cost(tight),
          "edge 0.040 vs spread 0.001")

    # El filtro tiene que llegar tambien por approve(), no solo por la funcion.
    ins = risk.approve([wide], balance=1000.0, open_positions=[])
    check("approve() descarta la oportunidad cara", len(ins) == 0, f"{len(ins)} instrucciones")
    ins2 = risk.approve([tight], balance=1000.0, open_positions=[])
    check("approve() acepta la oportunidad barata", len(ins2) == 1, f"{len(ins2)} instrucciones")

    # Liquidez por debajo del minimo -> fuera.
    illiquid = opp(edge=0.04, spread=0.001, liq=10.0)
    check("rechaza mercado sin liquidez suficiente",
          len(risk.approve([illiquid], balance=1000.0, open_positions=[])) == 0)

    # Perdida diaria: se mide contra la equity de apertura del dia, no contra el
    # balance inicial del run.
    limit = s.max_daily_loss_pct
    check(f"kill-switch diario salta al perder {limit:.0%} en el dia",
          risk.daily_loss_exceeded(equity=1000 * (1 - limit) - 1, day_start_equity=1000.0))
    check("kill-switch diario NO salta con perdida pequeña del dia",
          not risk.daily_loss_exceeded(equity=990.0, day_start_equity=1000.0))
    check("un mal run acumulado no dispara el corte si HOY no se pierde",
          not risk.daily_loss_exceeded(equity=500.0, day_start_equity=505.0),
          "equity 500 (muy por debajo del inicial 1000) pero el dia abrio en 505")


def test_reconcile() -> None:
    """La comparacion cadena-vs-BD. Es logica pura, asi que se prueba entera sin
    wallet: se le dan payloads sinteticos con la forma real de la Data API."""
    print("\n[3] Reconciliacion on-chain (logica de diff)")
    from medusa.execution.reconcile import diff_positions

    def chain(token, size, redeemable=False):
        return {"asset": token, "size": size, "conditionId": "0xabc",
                "title": "Mercado X", "outcome": "Yes", "avgPrice": 0.5,
                "currentValue": size * 0.5, "redeemable": redeemable}

    def db(token, size, pid=1):
        return {"id": pid, "token_id": token, "size": size, "question": "Mercado X",
                "cost_basis": size * 0.5}

    # Todo cuadra.
    r = diff_positions([chain("t1", 100.0)], [db("t1", 100.0)])
    check("cartera que cuadra se marca limpia", r.clean and len(r.matched) == 1, r.summary())

    # EL CASO PELIGROSO: existe on-chain, no en la BD.
    r = diff_positions([chain("t1", 100.0), chain("t2", 50.0)], [db("t1", 100.0)])
    check("detecta posicion REAL que la BD no conoce",
          not r.clean and len(r.untracked) == 1 and r.untracked[0]["token_id"] == "t2",
          r.summary())

    # La BD cree que tiene algo que on-chain no existe.
    r = diff_positions([], [db("t1", 100.0)])
    check("detecta posicion fantasma (en BD, no en la cadena)",
          not r.clean and len(r.phantom) == 1, r.summary())

    # Mismo token, tamaños distintos.
    r = diff_positions([chain("t1", 100.0)], [db("t1", 80.0)])
    check("detecta desajuste de tamaño", not r.clean and len(r.mismatched) == 1, r.summary())

    # Diferencia por debajo de la tolerancia -> no es descuadre.
    r = diff_positions([chain("t1", 100.0)], [db("t1", 99.7)])
    check("tolera diferencia minima por redondeo", r.clean, r.summary())

    # El polvo on-chain no debe contarse como posicion sin registrar.
    r = diff_positions([chain("t1", 100.0), chain("dust", 0.2)], [db("t1", 100.0)])
    check("ignora polvo on-chain (restos de redondeo)", r.clean, r.summary())

    # Resuelta y cobrable.
    r = diff_positions([chain("t1", 100.0, redeemable=True)], [db("t1", 100.0)])
    check("marca posiciones resueltas como cobrables",
          len(r.redeemable) == 1 and r.redeemable[0]["tracked"], r.summary())

    # Una cartera descuadrada NUNCA puede reportarse como limpia.
    r = diff_positions([chain("t2", 10.0)], [db("t1", 100.0)])
    check("descuadre total no se reporta limpio",
          not r.clean and len(r.untracked) == 1 and len(r.phantom) == 1, r.summary())


async def test_live_gate() -> None:
    print("\n[4] Gate de Live")
    from medusa.execution.live import LiveExecutionEngine, LiveTradingBlocked

    live = LiveExecutionEngine(client=None, log=_Log())
    try:
        await live.ensure_unlocked()
        check("Live bloqueado mientras live_unlocked=False", False, "NO bloqueo la operacion")
    except LiveTradingBlocked as exc:
        check("Live bloqueado mientras live_unlocked=False", True, str(exc)[:60])
    except Exception as exc:  # noqa: BLE001
        check("Live bloqueado mientras live_unlocked=False", False, f"error inesperado: {exc}")


async def test_accounting() -> None:
    """La contabilidad tiene que cuadrar: balance + coste de lo abierto + PnL
    realizado == balance inicial. Si no cuadra, en algun sitio se perdio dinero
    (que es exactamente lo que pasaba cuando la entrada no era atomica).
    """
    print("\n[6] Cuadre contable (paper)")
    from medusa.data import repositories as repo

    state = await repo.get_bot_state()
    if state is None:
        check("existe bot_state", False, "no inicializado todavia")
        return

    positions = await repo.get_open_positions("paper")
    trades = await repo.list_trades(limit=10_000, mode="paper")
    balance = float(state["balance"])
    starting = float(state["starting_balance"])
    exposure = sum(p["cost_basis"] for p in positions)
    realized = sum(t["pnl"] for t in trades)

    # balance + capital inmovilizado - PnL ya realizado == balance inicial
    diff = (balance + exposure - realized) - starting
    check(
        "balance + exposicion - PnL realizado == balance inicial",
        abs(diff) < 0.01,
        f"balance={balance:.2f} + exposicion={exposure:.2f} - realizado={realized:.2f} "
        f"= {balance + exposure - realized:.2f} vs inicial={starting:.2f} (desvio {diff:+.2f})",
    )
    check("ninguna posicion abierta sin coste", all(p["cost_basis"] > 0 for p in positions),
          f"{len(positions)} posiciones")


async def test_polymarket() -> None:
    print("\n[5] API de Polymarket (datos reales)")
    client = PolymarketClient()
    try:
        markets = await client.fetch_markets(limit=5)
        check("Gamma API devuelve mercados", len(markets) > 0, f"{len(markets)} mercados")
        if not markets:
            return
        m = markets[0]
        check("el mercado trae token YES", bool(m.yes_token_id), m.yes_token_id[:18] + "…")
        check("precio implicito en rango (0,1)", 0 < m.yes_price < 1, f"{m.yes_price}")

        book = await client.fetch_order_book(m.yes_token_id)
        check("CLOB devuelve order book", bool(book.bids or book.asks),
              f"{len(book.bids)} bids / {len(book.asks)} asks")
        if book.bids and book.asks:
            check("bids ordenados descendente", book.bids[0][0] >= book.bids[-1][0])
            check("asks ordenados ascendente", book.asks[0][0] <= book.asks[-1][0])
            check("spread no negativo", book.spread >= 0, f"{book.spread:.4f}")

        found = await client.fetch_market_by_condition(m.id)
        check("consulta por condition_id (liquidacion) funciona", found is not None)

        # Escaner global: la paginacion debe superar el tamaño de una pagina y
        # deduplicar (Gamma repite mercados entre paginas).
        universe = await client.fetch_all_markets(max_markets=150)
        check("fetch_all_markets pagina mas alla de 100", len(universe) > 100,
              f"{len(universe)} mercados")
        check("universo sin duplicados", len({u.id for u in universe}) == len(universe))

        history = await client.fetch_price_history(m.yes_token_id)
        check("CLOB devuelve historico de precios", len(history) > 0,
              f"{len(history)} puntos")
        if len(history) >= 2:
            check("historico ordenado ascendente", history[0][0] <= history[-1][0])
    finally:
        await client.close()


def test_intelligence() -> None:
    """La capa nueva: clasificador, pre-scorer, estrategias y asignacion.
    Todo con datos sinteticos y verificables (sin BD, sin red)."""
    print("\n[7] Inteligencia multi-estrategia")
    import datetime as dt

    from medusa.allocation import AllocationManager
    from medusa.allocation.manager import wilson_lower
    from medusa.core.models import Market, MarketContext
    from medusa.intelligence import MarketClassifier, OpportunityPreScorer
    from medusa.strategies import StrategyManager, build_default_strategies

    log = _Log()

    def mk(question, **kw):
        end = kw.pop("end_date", dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3))
        return Market(id="0x" + question[:10], question=question, end_date=end,
                      yes_token_id="ty", no_token_id="tn", **kw)

    # --- clasificador: una muestra por categoria + fallback ---
    clf = MarketClassifier(log)
    for q, expected in [
        ("Will Bitcoin reach $150k by December 31?", "crypto"),
        ("Will the Lakers win the NBA Finals?", "sports"),
        ("Will the Fed cut interest rates in September?", "economy"),
        ("Will OpenAI release GPT-6 this year?", "ai"),
        ("Russia Ukraine ceasefire in 2026?", "geopolitics"),
        ("Something completely unrelated xyz?", "other"),
    ]:
        check(f"clasifica '{q[:40]}' como {expected}",
              clf.classify(mk(q)) == expected, clf.classify(mk(q)))

    # --- pre-scorer: liquido y barato > fino y caro; extremos fuera ---
    ps = OpportunityPreScorer(log)
    good = mk("liquid?", yes_price=0.45, liquidity=60000, volume_24h=90000,
              best_bid=0.44, best_ask=0.46, price_change_24h=0.06)
    thin = mk("thin?", yes_price=0.10, liquidity=800, volume_24h=1500,
              best_bid=0.06, best_ask=0.14)
    dead = mk("dead?", yes_price=0.995, liquidity=60000, volume_24h=90000)
    check("mercado operable puntua mas que el fino",
          ps.score(good).score > ps.score(thin).score,
          f"{ps.score(good).score} vs {ps.score(thin).score}")
    check("precio extremo queda fuera del ranking", not ps.is_scannable(dead))

    # --- estrategias: disparan con datos sinteticos y ninguna opera ---
    mgr = StrategyManager(log, build_default_strategies(log))
    check("registro de 6 estrategias", len(mgr.strategies) == 6,
          str([st.name for st in mgr.strategies]))
    check("ninguna estrategia live por defecto (bot plano)",
          not any(mgr.is_live(st.name) for st in mgr.strategies))

    m = mk("Will Bitcoin reach $150k?", yes_price=0.45, no_price=0.55,
           liquidity=60000, volume_24h=90000, price_change_24h=0.08,
           price_change_1h=0.03, spread=0.02)
    m.medusa_category = "crypto"
    ctx = MarketContext(
        book_yes=OrderBook("ty", bids=[(0.44, 3000.0), (0.43, 2000.0)],
                           asks=[(0.46, 300.0), (0.47, 200.0)]),
        book_no=OrderBook("tn", bids=[(0.53, 500.0)], asks=[(0.55, 500.0)]),
    )
    names = {sig.strategy for sig in mgr.evaluate(m, ctx)}
    check("momentum + imbalance + breakout disparan con el escenario sintetico",
          {"momentum", "order_book_imbalance", "liquidity_breakout"} <= names, str(names))
    check("mean_reversion NO dispara (k=0, veredicto de calibracion)",
          "mean_reversion" not in names)

    arb_ctx = MarketContext(
        book_yes=OrderBook("ty", bids=[(0.40, 100.0)], asks=[(0.42, 100.0)]),
        book_no=OrderBook("tn", bids=[(0.55, 100.0)], asks=[(0.56, 100.0)]),
    )
    arb = [sig for sig in mgr.evaluate(m, arb_ctx) if sig.strategy == "stat_arb"]
    check("stat_arb detecta el par YES+NO a 0.98",
          len(arb) == 1 and abs(arb[0].edge - 0.02) < 1e-9)
    check("stat_arb es solo alerta (no operable)", arb and not arb[0].tradeable)

    # --- asignacion: sin historial nadie opera; con edge demostrado, peso>1 ---
    alloc = AllocationManager(log)
    check("sin historial -> peso 0 (no opera)",
          alloc.weight("momentum", "crypto") == 0.0)
    alloc._cells = {
        ("momentum", "crypto"): {"n": 50, "wins": 35, "avg_roi": 0.08, "std_roi": 0.20},
        ("momentum", "sports"): {"n": 50, "wins": 20, "avg_roi": -0.05, "std_roi": 0.30},
    }
    w = alloc.weight("momentum", "crypto")
    check("celda con edge demostrado -> peso > 1 y acotado",
          1.0 < w <= get_settings().alloc_max_weight, f"w={w}")
    check("celda perdedora -> peso 0", alloc.weight("momentum", "sports") == 0.0)
    check("wilson_lower conservador con muestra corta",
          wilson_lower(15, 30) < 0.5, f"{wilson_lower(15, 30):.3f}")


async def test_intelligence_layer() -> None:
    """Intelligence Layer (V3): contrato, aislamiento de fallos y fases.

    Lo que se prueba aqui es justo lo que hace segura la capa: que un modulo
    roto no contamine, que un modulo colgado no bloquee, y que una estrategia
    en fase paper NO pueda operar en live.
    """
    print("\n[8] Intelligence Layer (V3)")
    import asyncio as _aio
    import datetime as _dt

    from medusa.core.models import Market, MarketContext, OrderBook
    from medusa.intelligence_layer import (
        Feature,
        IntelligenceModule,
        IntelligenceRunner,
        build_default_modules,
    )
    from medusa.intelligence_layer.microstructure import MicrostructureIntelligence
    from medusa.strategies.phases import Phase, is_tradeable, parse_phases, runs_in_runtime

    log = _Log()

    # --- microstructure: features correctas desde datos ya recolectados ---
    m = Market(
        id="0xfeat", question="Test market?", yes_price=0.45, no_price=0.55,
        liquidity=60_000.0, volume_24h=90_000.0, yes_token_id="ty", no_token_id="tn",
        end_date=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=3),
    )
    book = OrderBook("ty", bids=[(0.44, 3000.0), (0.43, 2000.0)],
                     asks=[(0.46, 800.0), (0.47, 400.0)])
    hist = [(float(i), 0.45 + (0.002 if i % 2 else -0.002)) for i in range(40)]
    ctx = {"books": {m.id: book}, "history": {m.id: hist}}

    micro = MicrostructureIntelligence(log)
    feats = await micro.compute([m], ctx)
    names = {f.name for f in feats}
    for expected in ("liquidity_score", "orderbook_score", "volatility_score",
                     "execution_cost", "book_imbalance"):
        check(f"microstructure produce {expected}", expected in names, str(sorted(names)))
    check("todas las features son float", all(isinstance(f.value, float) for f in feats))
    check("scores normalizados a [0,1]",
          all(0.0 <= f.value <= 1.0 for f in feats
              if f.name.endswith("_score")))
    check("execution_cost > 0 (operar nunca es gratis)",
          next(f.value for f in feats if f.name == "execution_cost") > 0)
    check("cada feature lleva su modulo (trazabilidad)",
          all(f.module == "microstructure" for f in feats))
    check("microstructure no necesita red", not micro.needs_network)

    # --- runner: un modulo roto NO puede tumbar a los demas ---
    class _Boom(IntelligenceModule):
        name = "boom"
        interval = 0.0
        async def compute(self, markets, ctx):
            raise RuntimeError("modulo roto a proposito")

    class _Hang(IntelligenceModule):
        name = "hang"
        interval = 0.0
        timeout = 0.2
        async def compute(self, markets, ctx):
            await _aio.sleep(30)   # se cuelga como lo haria una API muerta
            return []

    class _Good(IntelligenceModule):
        name = "good"
        interval = 0.0
        async def compute(self, markets, ctx):
            return [Feature(market_id=markets[0].id, name="ok", value=1.0, module="good")]

    saved: list = []

    async def _fake_save(features):
        saved.extend(features)
        return len(features)

    from medusa.data import repositories as _repo
    real_save = _repo.save_features
    _repo.save_features = _fake_save
    try:
        s = get_settings()
        real_enabled, real_mods = s.intelligence_enabled, s.intelligence_modules
        object.__setattr__(s, "intelligence_enabled", True)
        object.__setattr__(s, "intelligence_modules", "boom,hang,good")
        runner = IntelligenceRunner(log, [_Boom(log), _Hang(log), _Good(log)])
        written = await runner.run_once([m], ctx)
        check("un modulo que revienta no impide a los demas producir",
              written == 1 and saved and saved[0].name == "ok", f"written={written}")
        info = {i["name"]: i for i in runner.info()}
        check("el fallo queda registrado en el modulo roto", info["boom"]["errors"] == 1)
        check("el modulo colgado se corta por timeout, no bloquea",
              info["hang"]["errors"] == 1 and "timeout" in info["hang"]["last_error"],
              info["hang"]["last_error"])
        check("el modulo sano cuenta su ejecucion", info["good"]["runs"] == 1)
        object.__setattr__(s, "intelligence_enabled", False)
        runner_off = IntelligenceRunner(log, [_Good(log)])
        check("con el layer apagado no corre nada",
              not runner_off.enabled and await runner_off.run_once([m], ctx) == 0)
        object.__setattr__(s, "intelligence_enabled", real_enabled)
        object.__setattr__(s, "intelligence_modules", real_mods)
    finally:
        _repo.save_features = real_save

    # --- fases: el candado que separa paper de dinero real ---
    check("parse_phases lee el csv",
          parse_phases("momentum:paper,stat_arb:research") ==
          {"momentum": Phase.PAPER, "stat_arb": Phase.RESEARCH})
    check("una fase con typo se ignora (no asciende por accidente)",
          parse_phases("momentum:pepper") == {})
    check("research NO corre en el runtime", not runs_in_runtime(Phase.RESEARCH))
    check("shadow corre pero NUNCA opera",
          runs_in_runtime(Phase.SHADOW)
          and not is_tradeable(Phase.SHADOW, "paper")
          and not is_tradeable(Phase.SHADOW, "live"))
    check("PAPER opera en paper", is_tradeable(Phase.PAPER, "paper"))
    check("*** PAPER NO puede operar en LIVE (candado duro) ***",
          not is_tradeable(Phase.PAPER, "live"))
    check("live_candidate opera en ambos modos",
          is_tradeable(Phase.LIVE_CANDIDATE, "paper")
          and is_tradeable(Phase.LIVE_CANDIDATE, "live"))

    # --- DOBLE COTA maker/taker: la leccion de Hermes, con numeros ---
    # Hermes v1.1 solo mide la cota maker y su propia doc la etiqueta
    # "optimista (techo)". Aqui se miden LAS DOS y manda la taker.
    book_dc = OrderBook("ty", bids=[(0.40, 500.0)], asks=[(0.44, 500.0)])
    m_dc = Market(id="0xdc", question="Doble cota?", yes_price=0.42, no_price=0.58,
                  liquidity=50_000.0, volume_24h=50_000.0,
                  yes_token_id="ty", no_token_id="tn",
                  end_date=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=2))

    class _Bull(IntelligenceModule):  # placeholder, no se usa
        name = "_"
        async def compute(self, markets, ctx): return []

    from medusa.strategies.base import Strategy as _Strat

    class _Fake(_Strat):
        name = "fake"
        def evaluate(self, m, ctx):
            return self._directional_signal(m, ctx, prob_yes=0.60, confidence=0.9,
                                            score=50.0, explanation="test",
                                            valid_conditions="test")

    sig_dc = _Fake(log).evaluate(m_dc, MarketContext(book_yes=book_dc))
    check("cota TAKER = ask real (cruzas el spread)", sig_dc.entry_price == 0.44,
          str(sig_dc.entry_price))
    check("cota MAKER = bid real (te llenan sin cruzar)",
          sig_dc.entry_price_maker == 0.40, str(sig_dc.entry_price_maker))
    check("el gap entre cotas ES el spread (0.44-0.40=0.04)",
          abs((sig_dc.entry_price - sig_dc.entry_price_maker) - 0.04) < 1e-9)

    # Si el mercado resuelve YES (settle=1.0), verificable a mano:
    #   taker: (1.00-0.44)/0.44 = +127.3%   maker: (1.00-0.40)/0.40 = +150.0%
    # El techo maker exagera el ROI en 22.7 puntos SOLO por el supuesto de fill.
    roi_taker = (1.0 - sig_dc.entry_price) / sig_dc.entry_price
    roi_maker = (1.0 - sig_dc.entry_price_maker) / sig_dc.entry_price_maker
    check("la cota maker SIEMPRE infla el ROI frente a la taker",
          roi_maker > roi_taker,
          f"maker {roi_maker:+.1%} vs taker {roi_taker:+.1%} (peaje {roi_maker-roi_taker:.1%})")

    # El lado NO sin libro propio se DERIVA del YES y conserva el spread; si
    # cayera al mid, maker==taker y el peaje desapareceria (la ilusion).
    sig_no = _Fake(log).evaluate(
        Market(id="0xno", question="NO side?", yes_price=0.42, no_price=0.58,
               liquidity=50_000.0, volume_24h=50_000.0, yes_token_id="ty",
               no_token_id="tn",
               end_date=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=2)),
        MarketContext(book_yes=OrderBook("ty", bids=[(0.40, 500.0)], asks=[(0.44, 500.0)])),
    )
    # prob_yes=0.60 > 0.42 => sale YES; forzamos NO con una prob baja:
    class _Bear(_Fake):
        name = "bear"
        def evaluate(self, m, ctx):
            return self._directional_signal(m, ctx, prob_yes=0.20, confidence=0.9,
                                            score=50.0, explanation="t", valid_conditions="t")
    sig_bear = _Bear(log).evaluate(m_dc, MarketContext(book_yes=book_dc))
    check("señal NO sin libro propio: se deriva del YES (NO_ask=1-YES_bid)",
          sig_bear.outcome == "NO" and abs(sig_bear.entry_price - 0.60) < 1e-9,
          f"{sig_bear.outcome} @ {sig_bear.entry_price}")
    check("señal NO: el spread SOBREVIVE a la derivacion (no colapsa a mid)",
          abs((sig_bear.entry_price - sig_bear.entry_price_maker) - 0.04) < 1e-9,
          f"gap={sig_bear.entry_price - sig_bear.entry_price_maker:.4f}")

    # --- la señal shadow DEBE llevar su contexto de coste ---
    # Regresion de un bug real (2026-07-16): StrategySignal calculaba spread y
    # liquidity, las estrategias los rellenaban... y save_signals los tiraba.
    # Sin ellos el histórico no puede responder "¿hay edge DESPUES de costes?",
    # que es la unica pregunta que decide si Medusa gana dinero, y el asignador
    # podria ascender una estrategia por un ROI logrado en mercados de spread
    # imposible que jamas se habrian operado.
    import inspect as _insp

    from medusa.data import repositories as _r
    save_src = _insp.getsource(_r.save_signals)
    for campo in ("spread", "liquidity"):
        check(f"save_signals PERSISTE {campo} (contexto de coste)",
              f"{campo}=sig.{campo}" in save_src.replace(" ", ""),
              "sin esto el shadow no puede medir edge tras costes")
    from medusa.data.db_models import StrategySignalRow as _Row
    cols = {c.name for c in _Row.__table__.columns}
    check("la tabla strategy_signals tiene spread y liquidity",
          {"spread", "liquidity"} <= cols)
    check("spread es NULLABLE (NULL='no registrado', no 0.0='fue gratis')",
          _Row.__table__.columns["spread"].nullable,
          "un default 0.0 afirmaria que operar era gratis y contaminaria el analisis")
    perf_src = _insp.getsource(_r.strategy_performance)
    check("strategy_performance filtra por operabilidad (edge > spread*ratio)",
          "edge_cost_ratio" in perf_src and "tradeable_only" in perf_src,
          "el asignador no puede dar peso por señales incobrables")

    # Los repos deben poder RESOLVER todos sus nombres, no solo "leerse bien".
    # Regresion de un bug real (2026-07-16): strategy_performance usaba
    # get_settings() sin importarlo -> NameError. Los checks de arriba miran el
    # TEXTO de la funcion y pasaban tan contentos; el engine la llama en
    # startup() y entro en CRASH LOOP en produccion. Un test que lee codigo no
    # sustituye a uno que lo ejecuta.
    import builtins as _bi
    import dis as _dis

    def _globales_sin_resolver(fn) -> list[str]:
        """Nombres que la funcion cargaria como GLOBAL y no existen.

        Usa LOAD_GLOBAL del bytecode: es exacto. Mirar co_names daria falsos
        positivos porque mezcla globales con nombres de atributo.
        """
        g = fn.__globals__
        return sorted({
            ins.argval for ins in _dis.get_instructions(fn)
            if ins.opname == "LOAD_GLOBAL"
            and ins.argval not in g and not hasattr(_bi, ins.argval)
        })

    rotas = {}
    for _fn_name in ("strategy_performance", "save_signals", "latest_features",
                     "resolve_signals_for_market", "feature_stats", "prune_features",
                     "open_signal_keys", "record_entry", "record_exit"):
        _falta = _globales_sin_resolver(getattr(_r, _fn_name))
        if _falta:
            rotas[_fn_name] = _falta
    check("los repos resuelven TODOS sus globales (sin NameError latente)",
          not rotas, str(rotas) or "ninguno")

    # --- kill-switch: corta ENTRADAS, no salidas ni el auto-levantado ---
    # Regresion de un bug real (2026-07-16): _cycle salia con `return` nada mas
    # ver el kill-switch, asi que (a) no gestionaba las posiciones abiertas
    # (TP/SL/resolucion) justo cuando mas falta hacia y (b) nunca llegaba a
    # _snapshot_and_guard -> _roll_day, que es el UNICO sitio que levanta el
    # kill-switch al cambiar el dia UTC => quedaba permanente. El bug #3 otra
    # vez, por otro camino.
    import inspect

    from medusa.engine import Engine
    src = inspect.getsource(Engine._cycle)
    kill_idx = src.find("kill_switch_active")
    manage_idx = src.find("manage_positions")
    open_idx = src.find("open_positions")
    guard_idx = src.rfind("_snapshot_and_guard")
    check("el kill-switch NO impide gestionar posiciones abiertas",
          manage_idx > 0 and kill_idx > 0 and manage_idx > kill_idx,
          "manage_positions debe correr aunque el kill-switch este activo")
    check("el kill-switch SI impide abrir posiciones nuevas",
          open_idx > 0 and "if killed:" in src and src.find("if killed:", kill_idx) < open_idx,
          "el corte debe estar antes de open_positions")
    check("con kill-switch se sigue midiendo equity (y corre _roll_day)",
          guard_idx > 0 and src.count("_snapshot_and_guard") >= 2,
          "sin esto el auto-levantado del dia UTC es inalcanzable")
    guard_src = inspect.getsource(Engine._snapshot_and_guard)
    check("la alerta de kill-switch solo salta en la transicion (sin spam)",
          "get_kill_switch()" in guard_src,
          "corriendo cada ciclo, sin guarda de transicion escribiria 1 RISK/min")

    # --- integracion con el StrategyManager: default sigue siendo plano ---
    from medusa.strategies import StrategyManager, build_default_strategies
    mgr = StrategyManager(log, build_default_strategies(log))
    check("por defecto TODAS las estrategias estan en shadow",
          all(mgr.phase(st.name) == Phase.SHADOW for st in mgr.strategies),
          str({st.name: mgr.phase(st.name).value for st in mgr.strategies}))
    check("por defecto ninguna opera, ni en paper ni en live",
          not any(mgr.can_trade_in(st.name, "paper") for st in mgr.strategies)
          and not any(mgr.can_trade_in(st.name, "live") for st in mgr.strategies))
    check("modulos por defecto del layer: microstructure",
          [mod.name for mod in build_default_modules(log)] == ["microstructure"])


def test_updown() -> None:
    """Micro-trader 'Up or Down' 5 min: el modelo de probabilidad y los dos
    candados (certeza=winrate, EV=anti-Hermes). Matematica pura, verificable a
    mano y sin red."""
    print("\n[9] Micro-trader Up/Down (5 min)")
    from medusa.updown.model import (
        EntryPolicy,
        assess,
        fair_prob_up,
        favored_side,
        norm_cdf,
    )

    # --- CDF normal: valores conocidos ---
    check("norm_cdf(0) = 0.5", abs(norm_cdf(0.0) - 0.5) < 1e-9, f"{norm_cdf(0.0)}")
    check("norm_cdf(1.645) ~ 0.95 (una cola al 5%)",
          abs(norm_cdf(1.645) - 0.95) < 1e-3, f"{norm_cdf(1.645):.4f}")

    # --- fair_prob_up: forma y limites ---
    sig = 0.00003  # ~ vol/seg realista de BNB (medido en vivo)
    # Sin movimiento -> cara o cruz.
    check("sin movimiento => P(Up) = 0.5",
          abs(fair_prob_up(500.0, 500.0, 60, sig) - 0.5) < 1e-9)
    # Movimiento a favor con poco tiempo -> casi seguro.
    p_up_strong = fair_prob_up(500.0, 500.5, 20, sig)   # +0.1% con 20s
    check("movimiento a favor + poco tiempo => P(Up) alta",
          p_up_strong > 0.95, f"{p_up_strong:.4f}")
    # Mismo movimiento pero con MUCHO tiempo por delante -> menos seguro.
    p_up_early = fair_prob_up(500.0, 500.5, 280, sig)
    check("mas tiempo por delante => menos certeza (misma ventaja)",
          p_up_early < p_up_strong, f"{p_up_early:.4f} < {p_up_strong:.4f}")
    # Simetria: una caida da P(Up) < 0.5 y elige Down.
    p_dn = fair_prob_up(500.0, 499.5, 20, sig)
    check("movimiento en contra => P(Up) baja y favored=Down",
          p_dn < 0.05 and favored_side(p_dn) == "Down", f"{p_dn:.4f}")
    # Ventana cerrada: manda el signo del movimiento, sin incertidumbre.
    check("ventana cerrada (secs_left<=0) resuelve por el signo",
          fair_prob_up(500.0, 500.1, 0, sig) == 1.0
          and fair_prob_up(500.0, 499.9, 0, sig) == 0.0)

    pol = EntryPolicy(entry_window_secs=90, min_secs_left=8, min_prob=0.90,
                      min_ev=0.02, max_ask=0.95)

    # CANDADO temporal: aun lejos del cierre -> no se apuesta.
    far = assess(s0=500.0, st=500.5, secs_left=200, sigma_sec=sig,
                 favored_ask=0.80, policy=pol)
    check("no apuesta lejos del cierre (fuera de la ventana de entrada)",
          not far.act and "lejos" in far.reason, far.reason)

    # CANDADO certeza (winrate): cerca del cierre pero sin ventaja clara.
    unsure = assess(s0=500.0, st=500.02, secs_left=60, sigma_sec=sig,
                    favored_ask=0.55, policy=pol)
    check("no apuesta sin certeza suficiente (protege el winrate)",
          not unsure.act and "certeza" in unsure.reason,
          f"P={unsure.fair_prob} · {unsure.reason}")

    # CANDADO anti-Hermes: resultado casi seguro PERO el ask ya se comio el edge.
    # P(Up) ~ 0.99 pero ask 0.99 => EV ~ 0 => se descarta (winrate caro).
    overpriced = assess(s0=500.0, st=500.5, secs_left=15, sigma_sec=sig,
                        favored_ask=0.99, policy=pol)
    check("descarta el winrate CARO (casi seguro pero ask ya lo cotiza)",
          not overpriced.act and overpriced.fair_prob >= 0.95,
          f"P={overpriced.fair_prob} ask=0.99 EV={overpriced.ev} · {overpriced.reason}")

    # ENTRADA valida: casi seguro Y el ask deja EV positivo tras el peaje.
    good = assess(s0=500.0, st=500.5, secs_left=15, sigma_sec=sig,
                  favored_ask=0.80, policy=pol)
    check("apuesta cuando hay certeza Y EV positivo (winrate + margen)",
          good.act and good.favored == "Up" and good.ev > 0.02,
          f"P={good.fair_prob} ask=0.80 EV={good.ev:+.3f} · {good.reason}")
    check("el EV declarado es prob - ask (coherente)",
          abs(good.ev - (good.fair_prob - good.ask)) < 1e-6,
          f"{good.ev} vs {good.fair_prob - good.ask}")

    # --- symbol map ---
    from medusa.updown.trader import _SYMBOLS
    check("mapa de simbolos incluye bnb->BNBUSDT", _SYMBOLS.get("bnb") == "BNBUSDT")

    # --- risk manager del micro-trader (sizing + cortafuegos) ---
    from medusa.updown.risk import UpDownRiskManager
    rm = UpDownRiskManager(_Log())
    s = get_settings()
    now = 1_000_000.0

    def snap(**kw):
        base = dict(balance=1000.0, day_start_equity=1000.0, open_count=0,
                    open_exposure=0.0, today_bets=0, today_pnl=0.0,
                    consecutive_losses=0, last_loss_epoch=0.0)
        base.update(kw)
        return base

    ok = rm.assess(snap(), now)
    check("permite apostar en condiciones normales", ok.allow and ok.stake > 0,
          f"stake={ok.stake} · {ok.reason}")
    check("stake por defecto = fijo acotado a [min,max]",
          ok.stake == min(s.updown_max_stake, max(s.updown_min_stake, s.updown_stake_usd)),
          str(ok.stake))
    check("bloquea por balance bajo el suelo",
          not rm.assess(snap(balance=s.updown_min_balance - 1), now).allow)
    check("bloquea al alcanzar el máx de apuestas abiertas",
          not rm.assess(snap(open_count=s.updown_max_open), now).allow)
    check("bloquea por stop de pérdida diaria",
          not rm.assess(snap(today_pnl=-(s.updown_max_daily_loss_pct * 1000.0) - 1), now).allow)
    check("bloquea al superar el tope de apuestas del día",
          not rm.assess(snap(today_bets=s.updown_max_daily_bets), now).allow)
    check("bloquea por exposición al máximo",
          not rm.assess(snap(open_exposure=s.updown_max_exposure_pct * 1000.0), now).allow)
    # Cooldown anti-racha: bloquea dentro de la ventana, permite pasada esta.
    hot = snap(consecutive_losses=s.updown_max_consecutive_losses, last_loss_epoch=now - 1)
    cold = snap(consecutive_losses=s.updown_max_consecutive_losses,
                last_loss_epoch=now - s.updown_cooldown_secs - 1)
    check("cooldown anti-racha bloquea recién perdida la racha",
          not rm.assess(hot, now).allow)
    check("cooldown expira pasado el tiempo", rm.assess(cold, now).allow)
    # Sizing por bankroll (fraction>0): recorta por exposición restante.
    object.__setattr__(s, "updown_stake_fraction", 0.10)   # 10% -> 100 sobre 1000
    try:
        frac = rm.assess(snap(), now)
        check("con fraction>0 la apuesta escala con el balance",
              abs(frac.stake - min(s.updown_max_stake, 100.0)) < 1e-6, str(frac.stake))
        tight = rm.assess(snap(open_exposure=190.0), now)  # budget=200-190=10
        check("el sizing se recorta al presupuesto de exposición restante",
              tight.allow and tight.stake <= 10.0 + 1e-9, str(tight.stake))
    finally:
        object.__setattr__(s, "updown_stake_fraction", 0.0)

    # --- BUG REAL encontrado en produccion (2026-07-20): el candado max_ask se
    #     evaluaba en assess() contra book.best_ask, pero el FILL real (que
    #     camina niveles mas caros si el libro esta fino + slippage) puede
    #     superarlo. Medido en las 26 apuestas reales del CT202: 7 (27%) se
    #     llenaron por encima del UPDOWN_MAX_ASK=0.95 configurado, hasta 0.974
    #     -- exactamente lo que la politica "no pagar donde solo queda 1-2% de
    #     techo" existe para evitar. ---
    from medusa.execution.paper import PaperExecutionEngine
    from medusa.core.models import OrderBook as _OB, OrderRequest as _OReq

    thin_near_ceiling = _OB(
        token_id="t", bids=[(0.80, 100.0)],
        # best_ask=0.94 pasa el gate (<= max_ask=0.95), pero solo hay 5 shares
        # ahi: un stake de $20 tiene que caminar hasta el segundo nivel (0.98).
        asks=[(0.94, 5.0), (0.98, 100.0)],
    )
    paper_probe = PaperExecutionEngine(client=None, log=_Log())
    fill = paper_probe._simulate(
        thin_near_ceiling, _OReq(token_id="t", side="buy", usd_size=20.0)
    )
    check("libro fino: el fill medio SUPERA el best_ask que paso el gate de la decision",
          fill.status != "rejected" and fill.avg_price > thin_near_ceiling.best_ask,
          f"avg={fill.avg_price:.4f} vs best_ask={thin_near_ceiling.best_ask}")
    check("*** ese fill medio rompe max_ask=0.95 aunque la decision se aprobo con 0.94 ***",
          fill.avg_price > 0.95, f"avg_price={fill.avg_price:.4f}")

    import inspect as _insp2

    from medusa.updown.trader import UpDownTrader as _UDT
    open_src = _insp2.getsource(_UDT._open)
    check("FIX: _open comprueba el precio MEDIO real contra max_ask antes de registrar la entrada",
          "result.avg_price > self.s.updown_max_ask" in open_src,
          "sin esto, un fill que camina el libro puede superar el techo anti-Hermes sin ser detectado")
    check("la comprobacion va ANTES de record_entry (rechaza sin comprometer la posicion)",
          open_src.find("result.avg_price > self.s.updown_max_ask") < open_src.find("record_entry"))

    # --- integracion con el ledger real: manage_positions IGNORA las posiciones
    #     updown (las gestiona su propio loop, que es el unico settler) ---
    import inspect as _insp

    from medusa.trading.engine import TradingEngine
    from medusa.updown.trader import UpDownTrader
    mp_src = _insp.getsource(TradingEngine._manage_one)
    check("manage_positions ignora las posiciones updown (su loop las gestiona)",
          'startswith("updown")' in mp_src)
    check("manage_positions descarta updown ANTES de tocar el mercado (return al inicio)",
          mp_src.index('startswith("updown")') < mp_src.index("fetch_market_by_condition"),
          "el return de updown debe ir antes de toda la logica de gestion")
    # El settler unico vive en el trader: liquida a 1/0 en la resolucion.
    settle_src = _insp.getsource(UpDownTrader._settle_one)
    check("el trader liquida updown contra la resolucion (record_exit a 1/0)",
          "record_exit" in settle_src and '("YES", "Up")' in settle_src)

    # --- los repos nuevos resuelven todos sus globales (sin NameError latente) ---
    import builtins as _bi
    import dis as _dis

    from medusa.data import repositories as _r

    def _globs(fn):
        g = fn.__globals__
        return sorted({
            ins.argval for ins in _dis.get_instructions(fn)
            if ins.opname == "LOAD_GLOBAL"
            and ins.argval not in g and not hasattr(_bi, ins.argval)
        })

    rotas = {}
    for name in ("has_open_updown_bet", "get_updown_assets", "set_updown_assets",
                 "set_paper_balance", "wipe_paper", "updown_risk_data"):
        falta = _globs(getattr(_r, name))
        if falta:
            rotas[name] = falta
    check("los repos de updown/cuenta resuelven todos sus globales", not rotas,
          str(rotas) or "ninguno")


async def main() -> int:
    print("=" * 68)
    print("MEDUSA — SELF-TEST")
    print("=" * 68)
    test_fill_simulation()
    test_risk_rules()
    test_reconcile()
    test_intelligence()
    await test_intelligence_layer()
    test_updown()
    await test_live_gate()
    await test_polymarket()
    await test_accounting()

    print("\n" + "=" * 68)
    if FAILS:
        print(f"RESULTADO: {len(FAILS)} FALLO(S)")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("RESULTADO: todo OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
