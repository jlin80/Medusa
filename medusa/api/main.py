"""API de Medusa (FastAPI): datos para el dashboard y control del bot.

Sin autenticacion por requisito explicito del usuario. Eso implica que el unico
control de acceso es la red: este servicio SOLO debe exponerse en LAN/VPN, nunca
publicado a Internet. Cualquiera que alcance el puerto puede mover el bot.

El estado durable vive en Postgres (bot_state) y se cachea en Redis para que el
engine lo lea barato. Al escribir se actualizan los dos, siempre en ese orden.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from medusa.allocation import AllocationManager
from medusa.config import get_settings
from medusa.core import events, state
from medusa.core.enums import LogType, Mode
from medusa.data import repositories as repo
from medusa.data.polymarket.client import PolymarketClient
from medusa.execution.reconcile import Reconciler
from medusa.infra.db import check_db, init_db
from medusa.infra.redis_bus import check_redis, get_redis
from medusa.intelligence_layer import IntelligenceRunner, build_default_modules
from medusa.logging_setup import configure_logging
from medusa.notifications.discord import DiscordNotifier
from medusa.portfolio.metrics import compute_metrics, validation_report
from medusa.strategies import StrategyManager, build_default_strategies
from medusa.logging_setup import err

settings = get_settings()
log = configure_logging(settings.log_level, settings.log_dir, settings.log_json, service="api")
notifier = DiscordNotifier(log=log)
_started_at = time.time()

# El registro de estrategias es estatico (sale de la config) y el asignador
# lee de la BD: la API puede reconstruir ambos sin hablar con el engine, que
# corre en otro contenedor.
_strategy_mgr = StrategyManager(log, build_default_strategies(log))
_allocator = AllocationManager(log)
# Solo para DESCRIBIR el registro de modulos en /intelligence/modules. La API
# nunca ejecuta el layer: quien lo corre es el engine, en su propio contenedor
# y su propio loop. Las estadisticas de ejecucion viven en ese proceso, asi que
# aqui se reporta el registro y el estado del store, no los contadores.
_intel = IntelligenceRunner(log, build_default_modules(log))

# Frase exacta que hay que escribir para mover dinero real. Deliberadamente
# incomoda: un toggle de un clic no debe poder poner fondos en riesgo.
CONFIRM_LIVE = "ACTIVAR LIVE"

# Si el engine no late en este tiempo, se considera caido.
HEARTBEAT_TIMEOUT = 90.0


async def _engine_watchdog() -> None:
    """Vigila el heartbeat del engine y avisa por Discord si el servicio cae.

    El engine no puede anunciar su propia muerte, asi que lo hace la API (vive
    en otro contenedor). Reglas anti-ruido: solo se alerta una transicion
    vivo->caido (no cada chequeo), se re-alerta como recordatorio cada 30 min
    mientras siga caido, y al volver se notifica la recuperacion. Para no dar
    una falsa alarma en cada arranque del stack, la primera alerta exige haber
    visto el engine vivo al menos una vez o llevar >3 min de espera.
    """
    check_every = 30.0
    remind_every = 1800.0
    seen_alive = False
    down_since: float | None = None
    last_alert = 0.0

    while True:
        await asyncio.sleep(check_every)
        try:
            hb = await state.get_heartbeat()
            age = (time.time() - hb) if hb else None
            alive = age is not None and age < HEARTBEAT_TIMEOUT

            if alive:
                if down_since is not None:
                    mins = (time.time() - down_since) / 60.0
                    msg = f"Engine RECUPERADO tras {mins:.1f} min caido."
                    log.info("watchdog.engine_recovered", downtime_min=round(mins, 1))
                    await events.publish_log(LogType.SYSTEM, msg)
                    await notifier.risk_alert(msg)
                down_since = None
                seen_alive = True
                continue

            waited = time.time() - _started_at > 180
            if not (seen_alive or waited):
                continue  # arranque del stack: dar margen antes de alertar
            if down_since is None:
                down_since = time.time()
            if time.time() - last_alert >= remind_every or last_alert == 0.0:
                desc = f"sin heartbeat desde hace {age:.0f}s" if age is not None else "sin heartbeat registrado"
                msg = f"CAIDA DEL SERVICIO: el engine no late ({desc})."
                log.error("watchdog.engine_down", heartbeat_age=age)
                await events.publish_log(LogType.ERROR, msg)
                await notifier.error("Caida del servicio", msg)
                last_alert = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - el watchdog no debe morir nunca
            log.warning("watchdog.check_failed", error=err(exc))


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("api.starting")
    try:
        await init_db()   # idempotente; la API puede arrancar antes que el engine
    except Exception as exc:  # noqa: BLE001
        log.warning("api.init_db_failed", error=err(exc))
    watchdog = asyncio.create_task(_engine_watchdog())
    yield
    watchdog.cancel()
    await notifier.close()
    log.info("api.stopping")


app = FastAPI(title="Medusa API", version="1.0.0", lifespan=lifespan)

# Market Intelligence Graph (MIG): router ADITIVO. Todos sus endpoints cuelgan
# de /mig y son de lectura salvo la reconstruccion del grafo. Si el paquete
# fallara al importar, la API arranca igual y simplemente no habra /mig: una
# pieza opcional no puede tumbar el panel de control del bot.
try:
    from medusa.intelligence.mig.api import get_service as _mig_service
    from medusa.intelligence.mig.api import router as mig_router

    _mig_service(log)          # instancia el servicio con el logger de la API
    app.include_router(mig_router)
except Exception as exc:  # noqa: BLE001
    log.warning("api.mig_router_unavailable", error=err(exc))

# Wallet Intelligence: router ADITIVO en /wallets. Mismo blindaje que el MIG:
# si el paquete fallara al importar, la API arranca igual y simplemente no habra
# /wallets. NO es copy trading y no expone ningun endpoint que opere.
try:
    from medusa.intelligence.wallet.api import get_service as _wallet_service
    from medusa.intelligence.wallet.api import router as wallet_router

    _wallet_service(log)
    app.include_router(wallet_router)
except Exception as exc:  # noqa: BLE001
    log.warning("api.wallet_router_unavailable", error=err(exc))

# Sin login por diseño -> solo debe exponerse en LAN/VPN.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------- modelos --
class ModeRequest(BaseModel):
    mode: str
    confirm: str = ""


class UnlockRequest(BaseModel):
    confirm: str = ""


class KillSwitchRequest(BaseModel):
    enabled: bool


class UpDownAssetRequest(BaseModel):
    asset: str
    enabled: bool


class BalanceRequest(BaseModel):
    balance: float


class ResetRequest(BaseModel):
    starting_balance: float | None = None
    confirm: str = ""


# ------------------------------------------------------------------- salud --
@app.get("/health")
async def health() -> dict:
    db_ok = redis_ok = False
    try:
        db_ok = await check_db()
    except Exception as exc:  # noqa: BLE001
        log.warning("health.db_fail", error=err(exc))
    try:
        redis_ok = bool(await check_redis())
    except Exception as exc:  # noqa: BLE001
        log.warning("health.redis_fail", error=err(exc))
    return {"status": "ok" if (db_ok and redis_ok) else "degraded",
            "db": db_ok, "redis": redis_ok}


@app.get("/status")
async def status() -> dict:
    st = await repo.get_bot_state()
    hb = await state.get_heartbeat()
    hb_age = round(time.time() - hb, 1) if hb else None
    return {
        "mode": st["mode"] if st else None,
        "kill_switch": bool(st.get("kill_switch")) if st else False,
        "live_unlocked": bool(st.get("live_unlocked")) if st else False,
        "engine_alive": hb_age is not None and hb_age < HEARTBEAT_TIMEOUT,
        "engine_heartbeat_age_seconds": hb_age,
        "uptime_seconds": round(time.time() - _started_at, 1),
    }


# ---------------------------------------------------------------- metricas --
@app.get("/metrics")
async def metrics(mode: str | None = None) -> dict:
    if mode is None:
        st = await repo.get_bot_state()
        mode = st["mode"] if st else "paper"
    return await compute_metrics(mode)


@app.get("/equity")
async def equity(mode: str | None = None, limit: int = 500) -> list[dict]:
    if mode is None:
        st = await repo.get_bot_state()
        mode = st["mode"] if st else "paper"
    return await repo.equity_curve(limit=limit, mode=mode)


@app.get("/validation")
async def validation() -> dict:
    """Reporte del gate obligatorio de paper trading (7-14 dias)."""
    return await validation_report()


@app.get("/live/reconcile")
async def reconcile() -> dict:
    """Compara las posiciones REALES on-chain contra las de la BD.

    Solo tiene sentido con wallet configurada: en paper no hay nada que
    reconciliar.
    """
    if not (settings.wallet_address or settings.clob_funder):
        raise HTTPException(
            400, "No hay WALLET_ADDRESS / CLOB_FUNDER configurada: nada que reconciliar.",
        )
    client = PolymarketClient()
    try:
        report = await Reconciler(client, log).run(await repo.get_open_positions("live"))
        return report.to_dict()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"No se pudo reconciliar contra la cadena: {exc}")
    finally:
        await client.close()


# --------------------------------------------------------------- operativa --
@app.get("/positions")
async def positions(mode: str | None = None) -> list[dict]:
    if mode is None:
        st = await repo.get_bot_state()
        mode = st["mode"] if st else "paper"
    return await repo.get_open_positions(mode)


@app.get("/trades")
async def trades(mode: str | None = None, limit: int = 100) -> list[dict]:
    return await repo.list_trades(limit=limit, mode=mode)


@app.get("/markets")
async def markets(limit: int = 50) -> list[dict]:
    return await repo.list_markets(limit=limit)


@app.get("/opportunities")
async def opportunities(limit: int = 50) -> list[dict]:
    return await repo.list_opportunities(limit=limit)


@app.get("/logs")
async def logs(limit: int = 200, level: str | None = None) -> list[dict]:
    return await repo.list_events(limit=limit, level=level)


# ------------------------------------------------------------- inteligencia --
@app.get("/intelligence/ranking")
async def intelligence_ranking(limit: int = 25) -> list[dict]:
    """Ranking dinamico de oportunidades: mercados vivos por puntaje del
    pre-scorer (solo lo visto en el ultimo escaneo global)."""
    return await repo.list_ranked_markets(limit=limit)


@app.get("/intelligence/categories")
async def intelligence_categories() -> list[dict]:
    """Distribucion del universo escaneable por categoria."""
    return await repo.category_stats()


@app.get("/strategies")
async def strategies() -> dict:
    """Registro de estrategias + rendimiento medido (señales resueltas) +
    pesos de asignacion de capital."""
    await _allocator.refresh(force=True)
    return {
        "registry": _strategy_mgr.registry_info(),
        "signal_counts": await repo.signal_counts(),
        "performance": _allocator.to_dict(),
        "live_enabled": sorted(
            {st["name"] for st in _strategy_mgr.registry_info() if st["live_enabled"]}
        ),
        "min_samples": settings.alloc_min_samples,
    }


@app.get("/signals")
async def signals(
    limit: int = 100, strategy: str | None = None, status: str | None = None,
) -> list[dict]:
    """Señales de estrategias (shadow y operadas)."""
    return await repo.list_signals(limit=limit, strategy=strategy, status=status)


# --------------------------------------------------- up/down 5 min (paper) --
# Catalogo de mercados Up/Down con ventana de 5 min en Polymarket (cada uno con
# su par spot en Binance). El toggle del dashboard elige cuales opera el bot.
UPDOWN_MARKETS = ["bnb", "btc", "eth", "sol", "xrp"]


@app.get("/updown/assets")
async def updown_assets() -> dict:
    """Mercados Up/Down disponibles, cuales estan activos (toggle) y el estado
    del risk manager (foto en vivo + limites configurados)."""
    active = await repo.get_updown_assets(settings.updown_assets)
    return {
        "master_enabled": settings.updown_enabled,
        "markets": UPDOWN_MARKETS,
        "active": [a for a in active if a in UPDOWN_MARKETS],
        "policy": {
            "entry_window_secs": settings.updown_entry_window_secs,
            "min_prob": settings.updown_min_prob,
            "min_ev": settings.updown_min_ev,
            "max_ask": settings.updown_max_ask,
            "stake_usd": settings.updown_stake_usd,
        },
        "risk": await repo.updown_risk_data(),
        "risk_limits": {
            "stake_fraction": settings.updown_stake_fraction,
            "min_stake": settings.updown_min_stake,
            "max_stake": settings.updown_max_stake,
            "max_open": settings.updown_max_open,
            "max_exposure_pct": settings.updown_max_exposure_pct,
            "min_balance": settings.updown_min_balance,
            "max_daily_bets": settings.updown_max_daily_bets,
            "max_daily_loss_pct": settings.updown_max_daily_loss_pct,
            "max_consecutive_losses": settings.updown_max_consecutive_losses,
            "cooldown_secs": settings.updown_cooldown_secs,
        },
    }


@app.post("/updown/assets")
async def set_updown_asset(req: UpDownAssetRequest) -> dict:
    """Activa o desactiva un mercado Up/Down. El engine lo lee en segundos."""
    asset = req.asset.strip().lower()
    if asset not in UPDOWN_MARKETS:
        raise HTTPException(400, f"Mercado desconocido: {req.asset}. Validos: {UPDOWN_MARKETS}")
    active = set(a for a in await repo.get_updown_assets(settings.updown_assets)
                 if a in UPDOWN_MARKETS)
    if req.enabled:
        active.add(asset)
    else:
        active.discard(asset)
    csv = ",".join(sorted(active))
    await repo.set_updown_assets(csv)
    await events.publish_log(
        LogType.SYSTEM,
        f"Mercado Up/Down {asset.upper()} {'ACTIVADO' if req.enabled else 'desactivado'} "
        f"(activos: {csv or 'ninguno'})",
        source="api",
    )
    return {"active": sorted(active)}


# ------------------------------------------------------ cuenta paper (control) --
@app.post("/paper/balance")
async def set_paper_balance(req: BalanceRequest) -> dict:
    """Ajusta el balance de la cuenta paper (y la referencia del ROI).

    Pensado para hacerse estando plano: si hay posiciones abiertas, su coste ya
    esta descontado del balance, asi que fijar el balance a mano descuadra la
    contabilidad hasta que cierren. El dashboard avisa de ello.
    """
    if req.balance < 0:
        raise HTTPException(400, "El balance no puede ser negativo.")
    await repo.set_paper_balance(req.balance)
    await events.publish_log(
        LogType.SYSTEM, f"Balance paper ajustado a ${req.balance:.2f}", source="api")
    return {"balance": round(req.balance, 2)}


@app.post("/paper/reset")
async def reset_paper(req: ResetRequest) -> dict:
    """WIPE: borra todo el historial de trading de paper y reinicia a cero.

    Se van posiciones, trades, oportunidades, ordenes, fills y equity de paper;
    el balance vuelve al inicial indicado y el reloj de validacion se reinicia.
    NO se toca el gate Paper->Live (14 dias / 30 ops / ROI>0) ni el desbloqueo de
    Live: el gate simplemente vuelve a contar desde cero.
    """
    if req.confirm != "RESET":
        raise HTTPException(400, 'Confirmacion requerida: escribe "RESET".')
    start = req.starting_balance if req.starting_balance is not None else settings.paper_starting_balance
    if start < 0:
        raise HTTPException(400, "El balance inicial no puede ser negativo.")
    deleted = await repo.wipe_paper(start)
    await state.set_kill_switch(False)
    await events.publish_log(
        LogType.SYSTEM,
        f"WIPE de stats paper: cuenta reiniciada a ${start:.2f} (borrado: {deleted})",
        source="api",
    )
    await notifier.risk_alert(f"Stats de paper REINICIADAS a cero (balance ${start:.2f}).")
    return {"reset": True, "starting_balance": round(start, 2), "deleted": deleted}


# ------------------------------------------------- intelligence layer (V3) --
@app.get("/intelligence/modules")
async def intelligence_modules() -> dict:
    """Registro del Intelligence Layer + estado del Feature Store."""
    return {
        "enabled": settings.intelligence_enabled,
        "interval_seconds": settings.intelligence_interval,
        "modules": _intel.info(),
        "feature_store": await repo.feature_stats(),
        "retention_days": settings.feature_retention_days,
    }


@app.get("/features")
async def features(
    limit: int = 100, market_id: str | None = None, name: str | None = None,
    module: str | None = None,
) -> list[dict]:
    """Feature Store: histórico crudo (append-only) para auditoría y ML."""
    return await repo.list_features(
        limit=limit, market_id=market_id, name=name, module=module
    )


@app.get("/features/latest")
async def features_latest(market_id: str) -> dict:
    """Últimas features de un mercado: lo que ve una estrategia al decidir."""
    data = await repo.latest_features([market_id])
    return {"market_id": market_id, "features": data.get(market_id, {})}


# ----------------------------------------------------------------- control --
@app.post("/mode")
async def set_mode(req: ModeRequest) -> dict:
    """Toggle PAPER <-> LIVE.

    Pasar a LIVE exige tres cosas a la vez: que Live este desbloqueado (gate de
    paper superado + desbloqueo manual), la frase de confirmacion exacta, y que
    existan las credenciales de wallet. Si falta cualquiera, se rechaza.
    """
    try:
        new_mode = Mode(req.mode.lower())
    except ValueError:
        raise HTTPException(400, f"Modo invalido: {req.mode}")

    st = await repo.get_bot_state()
    old_mode = st["mode"] if st else "paper"
    if old_mode == new_mode.value:
        return {"mode": old_mode, "changed": False}

    if new_mode == Mode.LIVE:
        if not st or not st.get("live_unlocked"):
            raise HTTPException(
                403,
                "Live esta bloqueado. Supera el gate de paper trading y desbloquea "
                "en /live/unlock antes de cambiar de modo.",
            )
        if req.confirm != CONFIRM_LIVE:
            raise HTTPException(400, f'Confirmacion requerida: escribe "{CONFIRM_LIVE}"')
        if not (settings.private_key and settings.wallet_address):
            raise HTTPException(
                400, "Faltan PRIVATE_KEY / WALLET_ADDRESS en el entorno del engine.",
            )

    await repo.update_bot_state(mode=new_mode.value)
    await state.set_mode(new_mode)
    await events.publish_log(
        LogType.SYSTEM, f"Cambio de modo: {old_mode.upper()} -> {new_mode.value.upper()}",
        source="api",
    )
    await notifier.mode_change(old_mode, new_mode.value)
    log.info("api.mode_changed", old=old_mode, new=new_mode.value)
    return {"mode": new_mode.value, "changed": True}


@app.post("/live/unlock")
async def unlock_live(req: UnlockRequest) -> dict:
    """Desbloquea Live. Solo si el reporte de validacion lo permite."""
    report = await validation_report()
    if not report["eligible_for_live"]:
        failed = [k for k, v in report["checks"].items() if not v["pass"]]
        raise HTTPException(
            403, f"El gate de validacion no se cumple todavia. Pendiente: {', '.join(failed)}",
        )
    if req.confirm != CONFIRM_LIVE:
        raise HTTPException(400, f'Confirmacion requerida: escribe "{CONFIRM_LIVE}"')

    # Antes de habilitar dinero real, la cartera on-chain tiene que cuadrar con
    # la BD. Si hay una posicion real que Medusa no conoce, no la vigila nadie.
    if settings.wallet_address or settings.clob_funder:
        client = PolymarketClient()
        try:
            report = await Reconciler(client, log).run(await repo.get_open_positions("live"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"No se pudo reconciliar contra la cadena: {exc}")
        finally:
            await client.close()
        if not report.clean:
            raise HTTPException(409, f"La cartera on-chain no cuadra con la BD. {report.summary()}")

    await repo.update_bot_state(live_unlocked=True)
    await events.publish_log(LogType.RISK, "Live DESBLOQUEADO manualmente", source="api")
    await notifier.risk_alert("Live ha sido desbloqueado manualmente desde el dashboard.")
    log.warning("api.live_unlocked")
    return {"live_unlocked": True}


@app.post("/live/lock")
async def lock_live() -> dict:
    """Vuelve a bloquear Live y fuerza PAPER. Siempre permitido, sin confirmacion."""
    await repo.update_bot_state(live_unlocked=False, mode=Mode.PAPER.value)
    await state.set_mode(Mode.PAPER)
    await events.publish_log(LogType.RISK, "Live bloqueado, modo forzado a PAPER", source="api")
    return {"live_unlocked": False, "mode": "paper"}


@app.post("/kill-switch")
async def kill_switch(req: KillSwitchRequest) -> dict:
    """Corta (o reanuda) toda la operativa.

    Se marca kill_switch_auto=False: un corte manual es del usuario y el engine
    no puede levantarlo solo al cambiar de dia (a diferencia del automatico por
    perdida diaria).
    """
    await repo.update_bot_state(kill_switch=req.enabled, kill_switch_auto=False)
    await state.set_kill_switch(req.enabled)
    msg = ("KILL-SWITCH activado: operativa detenida" if req.enabled
           else "Kill-switch desactivado: operativa reanudada")
    await events.publish_log(LogType.RISK, msg, source="api")
    await notifier.risk_alert(msg)
    return {"kill_switch": req.enabled}


# ------------------------------------------------------ logs en tiempo real --
@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket) -> None:
    """Terminal de logs en vivo: historico reciente + streaming por Redis."""
    await ws.accept()
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(events.CH_LOGS)
    try:
        recent = await repo.list_events(limit=50)
        for entry in reversed(recent):
            await ws.send_json({
                "ts": entry["ts"], "level": entry["level"],
                "source": entry["source"], "message": entry["message"],
            })
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg.get("data"):
                try:
                    await ws.send_json(json.loads(msg["data"]))
                except (json.JSONDecodeError, TypeError):
                    pass
            await asyncio.sleep(0.05)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("ws.logs_error", error=err(exc))
    finally:
        try:
            await pubsub.unsubscribe(events.CH_LOGS)
            await pubsub.aclose()
        except Exception:  # noqa: BLE001
            pass


@app.get("/")
async def root() -> dict:
    return {"service": "Medusa", "version": "1.0.0"}
