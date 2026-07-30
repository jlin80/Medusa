"""Configuracion central de Medusa (12-factor).

Todos los parametros se leen de variables de entorno / .env mediante
pydantic-settings. Nunca se hardcodean secretos en el codigo.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Modo ----
    mode: str = Field(default="paper", description="paper | live")

    # ---- Base de datos ----
    db_user: str = "medusa"
    db_password: str = "medusa"
    db_name: str = "medusa"
    db_host: str = "postgres"
    db_port: int = 5432

    # ---- Redis ----
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db: int = 0

    # ---- Logging ----
    log_level: str = "INFO"
    log_dir: str = "/app/logs"
    log_json: bool = True

    # ---- Engine ----
    heartbeat_interval: int = 15
    scan_interval: int = 60  # segundos entre escaneos de mercado
    data_retention_days: int = 30  # poda de oportunidades/logs/equity (trades NO)

    # ---- Polymarket (endpoints publicos) ----
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_api_url: str = "https://clob.polymarket.com"
    data_api_url: str = "https://data-api.polymarket.com"
    http_timeout: float = 15.0

    # ---- Reconciliacion on-chain (solo Live) ----
    reconcile_interval: float = 300.0        # segundos entre reconciliaciones
    # Tolerancia al comparar shares de la cadena contra la BD. Los fills parciales
    # y el redondeo hacen que un match exacto sea irreal.
    reconcile_size_tolerance: float = 0.01   # 1%
    # Ignorar polvo on-chain (restos de redondeo) al buscar posiciones no
    # registradas: por debajo de esto no es una posicion, es basura.
    reconcile_min_shares: float = 1.0

    # ---- Market Scanner ----
    scan_max_markets: int = 40         # cuantos mercados analizar por ciclo
    min_liquidity: float = 500.0       # USDC minimos de liquidez
    min_volume_24h: float = 1000.0     # USDC minimos de volumen 24h
    min_hours_to_resolution: float = 6.0
    max_hours_to_resolution: float = 24.0 * 30

    # ---- Inteligencia de mercados (escaner global) ----
    # El universo completo se escanea cada global_scan_interval (solo Gamma,
    # barato); el ciclo de trading (scan_interval) trabaja sobre el ultimo
    # ranking y solo pide order books para el top-K. Separar los dos ritmos es
    # lo que permite mirar cientos de mercados sin castigar la API ni la CPU.
    global_scan_interval: float = 300.0
    scan_universe_size: int = 500      # mercados maximos por escaneo global
    prescore_top_k: int = 15           # cuantos pasan al analisis profundo
    history_cache_ttl: float = 900.0   # cache del historico de precios (seg)

    # ---- Estrategias ----
    # CSV de estrategias autorizadas a OPERAR (ademas deben tener peso > 0 en
    # el Capital Allocation Manager). Vacio = ninguna opera: todas corren en
    # "shadow" (registran señales y su resultado se mide contra la resolucion
    # real del mercado, sin arriesgar un dolar). Es el mismo principio que dejo
    # el bot plano tras el estudio de calibracion: sin edge demostrado con
    # datos, no se opera.
    strategies_live_enabled: str = ""
    # CSV de estrategias apagadas del todo (ni siquiera shadow).
    strategies_disabled: str = ""

    # Ciclo de vida por estrategia (V3): "momentum:paper,stat_arb:research".
    # Fases: research | shadow (default) | paper | live_candidate.
    #   research       -> no corre en el runtime
    #   shadow         -> registra señales, NUNCA opera
    #   paper          -> opera SOLO en modo paper y solo con peso > 0
    #   live_candidate -> unica que puede tocar dinero real (con todos los gates)
    # Vacio = todas en shadow = el bot sigue plano. Una estrategia asciende a
    # paper cuando su historial en shadow lo justifica, no antes.
    strategy_phases: str = ""

    # Momentum: continuacion del movimiento 24h. El estudio de momentum
    # (research/momentum_study.py) no encontro señal a horizonte resolucion;
    # corre en shadow para medir horizontes/categorias que el estudio no cubrio.
    momentum_min_move: float = 0.05    # |Δ24h| minimo para disparar
    momentum_beta: float = 0.3         # fraccion del movimiento que se proyecta

    # Order book imbalance: presion compradora/vendedora en el libro.
    imbalance_band: float = 0.05       # banda de precio alrededor del mid
    imbalance_min_ratio: float = 0.55  # desequilibrio minimo |(b-a)/(b+a)|
    imbalance_beta: float = 0.05       # edge maximo que se atribuye al libro

    # Liquidity breakout: movimiento reciente + lado del libro fino.
    breakout_min_move_1h: float = 0.02
    breakout_thin_ratio: float = 0.5   # lado fino < 50% del lado grueso

    # Statistical arbitrage intra-mercado: YES+NO por debajo de $1 (o venta por
    # encima). Riesgo cero cuando aparece; medido 0 veces en 110 mercados, los
    # bots dedicados lo aspiran en segundos. Se registra y alerta por si acaso.
    stat_arb_min_profit: float = 0.005

    # Volatility expansion: vol reciente >> vol base con deriva direccional.
    vol_expansion_ratio: float = 2.0
    vol_min_baseline: float = 0.001    # suelo para no dividir por ~0
    vol_min_drift: float = 0.01        # deriva minima para dar direccion

    # ---- Señales (shadow) y rendimiento ----
    signal_resolve_interval: float = 1800.0  # cada cuanto se resuelven señales
    signal_resolve_batch: int = 40           # mercados por pasada (no castigar Gamma)

    # ---- Intelligence Layer (V3) ----
    # Capa desacoplada que SOLO produce features (nunca decisiones, nunca
    # ordenes). Apagada por defecto: toda funcionalidad nueva es opcional y el
    # runtime debe poder correr exactamente igual sin ella.
    intelligence_enabled: bool = False
    # Lista BLANCA de modulos activos: un modulo solo corre si se le nombra.
    # Disponibles hoy: microstructure.
    intelligence_modules: str = "microstructure"
    # Cada cuanto despierta el loop del layer. Corre en su propio task: el ciclo
    # de trading NUNCA lo espera. Cada modulo tiene ademas su propio `interval`.
    intelligence_interval: float = 300.0
    # Poda del Feature Store. Default largo a proposito (1 año): este histórico
    # es el activo del layer, no ruido de alta frecuencia. Ver la nota de coste
    # en disco en db_models.FeatureRow.
    feature_retention_days: int = 365

    # ---- Wallet Intelligence (medusa.intelligence.wallet) ----
    # Perfilado NUMERICO de wallets publicas de Polymarket: 19 metricas (ADN),
    # score relativo a la poblacion, reputacion, clusters y similitud.
    # NO ES COPY TRADING: produce FEATURES, jamas ordenes. Ninguna funcion del
    # paquete devuelve un lado, un tamaño ni un precio.
    # Apagado por defecto, como toda pieza nueva.
    wallet_intel_enabled: bool = False
    # Cada cuanto recalcula el loop del engine. La reputacion de una wallet se
    # mueve en dias: recalcularla cada minuto solo gasta Data API.
    wallet_intel_interval: float = 3600.0
    wallet_intel_timeout: float = 300.0
    # Tamaño de la poblacion. El ADN se estandariza CONTRA ESTA POBLACION, asi
    # que cambiar estos topes cambia el significado de los scores: no es solo
    # una cuestion de coste.
    wallet_max_markets: int = 25          # mercados de los que se descubren wallets
    wallet_holders_per_market: int = 50   # holders que se piden por mercado
    wallet_max_wallets: int = 200         # tope duro de wallets perfiladas
    wallet_min_holder_size: float = 0.0   # ignora polvo al descubrir
    wallet_max_positions: int = 500       # posiciones por wallet
    wallet_max_activity: int = 500        # eventos de actividad por wallet
    # Ventanas de las metricas temporales.
    wallet_recent_days: float = 30.0      # que cuenta como "reciente" (roi_recent, decay)
    wallet_half_life_days: float = 30.0   # semivida de `freshness`
    wallet_bucket_days: int = 7           # cubo temporal de alpha/beta
    # Muestra minima antes de creerse una reputacion. Alineado con
    # ALLOC_MIN_SAMPLES: no puede haber dos definiciones de "hay muestra".
    wallet_min_samples: int = 30
    # Clustering (k-means determinista) y similitud (coseno).
    wallet_clusters_k: int = 5
    wallet_min_cluster_wallets: int = 6   # por debajo de esto no se agrupa nada
    wallet_min_similarity: float = 0.70
    wallet_similarity_top_k: int = 5
    # Cuantos mercados enriquece el modulo del Intelligence Layer por pasada.
    wallet_feature_max_markets: int = 15
    # Poda del historico y de las pasadas. Los PERFILES no se podan.
    wallet_retention_days: int = 365

    # ---- Market Intelligence Graph (MIG) ----
    # Grafo de relaciones entre entidades de Medusa (mercados, categorias,
    # series, estrategias, trades, resoluciones, features, experimentos).
    # SOLO PostgreSQL, sin Neo4j. NO opera, NO emite señales, NO toca el Risk
    # Manager: su producto son nodos, aristas y objetos de inteligencia
    # (Discovery) que algun dia PODRAN convertirse en Features.
    # Apagado por defecto: igual que el Intelligence Layer, toda pieza nueva es
    # opt-in y el runtime debe correr exactamente igual sin ella.
    mig_enabled: bool = False
    # Cada cuanto se reconstruye el grafo. Una hora es de sobra: las relaciones
    # que describe (especialidad de una estrategia, series de eventos, clusters
    # de mercados) cambian en dias, no en minutos.
    mig_interval: float = 3600.0
    # Presupuesto de tiempo de una construccion. Si se pasa, se corta y se
    # registra: el MIG jamas puede arrastrar al engine.
    mig_timeout: float = 120.0
    # Muestras minimas para que una celda (estrategia x categoria) emita
    # veredicto. Mismo umbral que el asignador de capital a proposito: no puede
    # haber dos definiciones distintas de "hay muestra suficiente".
    mig_min_samples: int = 30
    # Observaciones minimas para afirmar una asociacion de una feature.
    mig_min_observations: int = 20
    # Similitud (Jaccard sobre las palabras de la pregunta) minima para unir dos
    # mercados con `similar_to`. Bajarlo hace que todo se parezca con todo.
    mig_min_similarity: float = 0.35
    # Topes de la ventana de datos por construccion. Existen para no cargar
    # meses de historia en los 3 GB del CT202; acotan, no falsean.
    mig_max_markets: int = 300
    mig_max_signals: int = 5000
    mig_max_trades: int = 2000
    mig_max_features: int = 5000
    # Poda de descubrimientos y snapshots. Nodos y aristas NO se podan: son el
    # grafo, y perderlos rompe el `first_seen` del que sale el crecimiento.
    mig_retention_days: int = 365

    # ---- Information Flow Engine (IFE) ----
    # Mide COMO SE PROPAGA la informacion dentro de un mercado: quien entra
    # antes, cuanto tarda el siguiente, cuanto tarda el consenso y si el lado
    # acabo teniendo razon. MIDE PROPAGACION, JAMAS CAUSALIDAD: que B entre
    # despues de A no dice que B siga a A. No opera, no emite señales y no toca
    # el Risk Manager. Apagado por defecto, como toda pieza nueva.
    flow_enabled: bool = False
    # Cada cuanto corre una pasada. Media hora: la cinta publica se mueve en
    # minutos, pero las cadenas que interesan tardan horas en formarse.
    flow_interval: float = 1800.0
    # Presupuesto de tiempo de una pasada. Si se pasa, se corta y se registra:
    # el motor jamas puede arrastrar al engine.
    flow_timeout: float = 180.0
    # Mercados por pasada y trades que se piden de cada uno. Acotan las
    # peticiones a la API publica y la memoria en una maquina de 3 GB.
    flow_max_markets: int = 40
    flow_trades_per_market: int = 500
    # Ventana de historia que se ANALIZA (sale de la BD, no de la red): con 72 h
    # caben las cadenas lentas sin cargar meses de cinta.
    flow_lookback_hours: float = 72.0
    # Hueco maximo entre dos entradas para seguir en la misma cascada. Subirlo
    # une rachas que no tienen nada que ver; bajarlo parte cadenas reales.
    flow_window_seconds: float = 3600.0
    # Participantes minimos para que una racha cuente como cascada. Con 2,
    # cualquier par de entradas seguidas seria una "cascada".
    flow_min_participants: int = 3
    # Distancia maxima en la cadena para emitir un eslabon. Emparejar a todos
    # con todos daria O(n^2) eventos y el par que va 40 entradas por detras no
    # describe propagacion: describe coincidencia de mercado.
    flow_max_hops: int = 3
    # Fraccion de la cascada que define el CONSENSUS DELAY (mediana por defecto).
    flow_consensus_fraction: float = 0.5
    # Vida media (s) del `speed_score`: convierte una latencia en un numero
    # acotado y comparable entre mercados de escalas muy distintas.
    flow_speed_half_life: float = 300.0
    # Que se considera entrada "temprana" y "tardia" por rango normalizado.
    flow_early_fraction: float = 0.33
    flow_late_fraction: float = 0.33
    # Movimiento minimo del precio para que una cascada sin resolver puntue. Por
    # debajo, la observacion NO cuenta: contar un empate como acierto es la forma
    # mas silenciosa de fabricar significancia.
    flow_min_price_move: float = 0.02
    # Cascadas minimas para que una wallet se lea como evidencia y no como
    # anecdota, y coincidencias minimas para publicar un par (lider, seguidor).
    flow_min_samples: int = 10
    flow_min_pair_observations: int = 5
    # Poda. Dos retenciones: la cinta cruda crece rapido y se poda pronto; las
    # cascadas, los eslabones y los snapshots son el conocimiento del motor.
    flow_retention_days: int = 365
    flow_trade_retention_days: int = 45

    # ---- Hypothesis Engine (HE) ----
    # GENERA hipotesis de investigacion a partir de los datos observados y las
    # pone a prueba con datos que no pudo ver. Ninguna hipotesis esta escrita en
    # el codigo: lo escrito es una gramatica de tres formas (monotona, contraste
    # de grupos y umbral) y el lineage de las fuentes. Observa ASOCIACION, jamas
    # causalidad. No opera, no emite señales y no toca el Risk Manager. Apagado
    # por defecto, como toda pieza nueva.
    hypothesis_enabled: bool = False
    # Cada cuanto corre una pasada. Una hora: las fuentes que lo alimentan
    # (señales resueltas, trades cerrados) producen casos nuevos en horas, no en
    # minutos, y proponer mas a menudo no añadiria observaciones, solo contrastes.
    hypothesis_interval: float = 3600.0
    # Presupuesto de tiempo de una pasada. Si se pasa, se corta y se registra: el
    # motor jamas puede arrastrar al engine.
    hypothesis_timeout: float = 240.0
    # Ventana de DESCUBRIMIENTO: de que historia reciente se proponen hipotesis
    # nuevas. Corta a proposito -- una ventana de un año propondria relaciones de
    # un regimen de mercado que ya no existe.
    hypothesis_discovery_days: float = 30.0
    # Ventana de ANALISIS completa: la que se usa para EVALUAR (necesita historia
    # a los dos lados de la valla temporal de cada hipotesis).
    hypothesis_lookback_days: float = 180.0
    # Topes de lectura por pasada. Acotan la memoria en una maquina de 3 GB.
    hypothesis_max_rows_per_source: int = 5000
    hypothesis_max_observations: int = 50000
    # Filtros de lo que llega a ser una VARIABLE. Una columna presente en menos de
    # `min_coverage` de la ventana describe un subconjunto raro, no la ventana; una
    # numerica con menos de `min_distinct` valores es una categorica disfrazada.
    hypothesis_min_coverage: float = 0.6
    hypothesis_min_distinct: int = 8
    # Niveles maximos de una etiqueta y masa minima de un nivel para contrastarlo.
    # Sin el tope, un `market_id` daria doscientos contrastes de un caso contra el
    # mundo.
    hypothesis_max_levels: int = 12
    hypothesis_min_level_size: int = 15
    # Observaciones minimas de la ventana para proponer algo.
    hypothesis_min_discovery_samples: int = 40
    # Tamaños minimos de efecto, uno por escala: rho para la forma monotona y d
    # (diferencia estandarizada) para las de grupos y umbral. Bajarlos llena el
    # tablero de relaciones ciertas y triviales.
    hypothesis_min_effect_rho: float = 0.15
    hypothesis_min_effect_d: float = 0.25
    # FDR de Benjamini-Hochberg sobre TODOS los contrastes de la pasada. Es el
    # filtro que impide que un motor que prueba cientos de relaciones publique
    # decenas de hallazgos falsos cada hora.
    hypothesis_alpha: float = 0.05
    # Cuantil de la ventana que define el corte de la forma umbral. Se CONGELA en
    # la hipotesis: recalcularlo sobre los datos de prueba seria elegir el corte
    # que mejor queda en el test y llamarlo replicacion.
    hypothesis_cut_quantile: float = 0.5
    # Observaciones FUERA DE MUESTRA para emitir un veredicto, y a partir de
    # cuantas la falta de efecto se lee como "no replica" en vez de "aun no se
    # sabe". La segunda es alta a proposito: rechazar pronto por falta de datos
    # tiraria hipotesis buenas por impaciencia.
    hypothesis_min_test_samples: int = 60
    hypothesis_reject_after: int = 200
    # Topes del tablero. Sin ellos el motor propondria en cada pasada y acabaria
    # con miles de enunciados en `proposed` que nadie va a leer.
    hypothesis_max_proposals_per_source: int = 10
    hypothesis_max_open_per_source: int = 60
    # Poda. Las HIPOTESIS no se podan nunca (una rechazada de hace un año es justo
    # lo que evita volver a proponerla); se podan snapshots y observaciones.
    hypothesis_retention_days: int = 730
    hypothesis_observation_retention_days: int = 365

    # ---- Micro-trader "Up or Down" 5 min (medusa.updown) ----
    # Subsistema desacoplado para las ventanas cripto de 5 min de Polymarket
    # (bnb-updown-5m-*). Apagado por defecto: encenderlo NO cambia nada del
    # pipeline principal, solo levanta su propio loop. SOLO PAPER (sin camino a
    # Live por diseño): simula el fill contra el libro real y liquida contra la
    # resolucion real, pero jamas manda una orden on-chain.
    updown_enabled: bool = False
    # CSV de activos. Cada uno tiene su serie de ventanas de 5 min en Polymarket
    # y su par spot en Binance (bnb->BNBUSDT). Disponibles: bnb, btc, eth, sol, xrp.
    updown_assets: str = "bnb"
    updown_tick_interval: float = 5.0      # segundos entre ticks del loop
    # SOLO se apuesta en los ultimos N segundos de la ventana (cuando el
    # movimiento ya se decanto), y no en el ultimo parpadeo.
    updown_entry_window_secs: float = 90.0
    updown_min_secs_left: float = 8.0
    # CANDADO 1 (winrate): certeza minima del modelo en el lado favorecido. 0.90
    # = solo apostar cuando el resultado ya es casi seguro (varios sigma).
    updown_min_prob: float = 0.90
    # CANDADO 2 (anti-Hermes): valor esperado minimo por share = prob - ask. Un
    # resultado casi seguro que YA cotiza a 0.99 no es negocio: el techo es 1% y
    # el peaje de ejecucion se lo come. Exigir EV real filtra justo eso.
    updown_min_ev: float = 0.02
    updown_max_ask: float = 0.95           # no pagar por encima de esto
    updown_stake_usd: float = 20.0         # importe por apuesta (fijo, si fraction=0)
    # Si el mercado no publica resolucion tras esto, se liquida por el precio de
    # cierre de Binance (open del kline en el borde). Tras updown_void_after sin
    # resolucion de ninguna fuente, la apuesta se marca void.
    updown_settle_timeout: float = 300.0
    updown_void_after: float = 3600.0

    # ---- Risk manager del micro-trader Up/Down ----
    # Estos limites SIEMPRE aplican (ademas del kill-switch de cuenta, que el
    # loop ya respeta). Son el cortafuegos de un bot de alta frecuencia binario:
    # sin ellos, una mala racha o un balance a la baja se comen la cuenta.
    #
    # Sizing por bankroll: la apuesta base es una fraccion del balance, acotada a
    # [min, max]. Con fraction=0 (default) se usa el fijo updown_stake_usd => el
    # comportamiento no cambia hasta que se opte por el %-sizing.
    updown_stake_fraction: float = 0.0     # p.ej. 0.02 = 2% del balance por apuesta
    updown_min_stake: float = 5.0          # nunca menos (>= orderMinSize efectivo)
    updown_max_stake: float = 25.0         # nunca mas
    updown_max_open: int = 3               # apuestas updown abiertas a la vez
    updown_max_exposure_pct: float = 0.20  # % del balance en riesgo updown simultaneo
    updown_min_balance: float = 25.0       # no apostar por debajo de este balance
    updown_max_daily_bets: int = 60        # tope de apuestas por dia UTC
    # Stop del dia si el PnL updown de hoy cae por debajo de -X% del balance de
    # apertura del dia. Es un cortafuegos ADEMAS del kill-switch de cuenta.
    updown_max_daily_loss_pct: float = 0.15
    # Anti-racha (tilt): tras N perdidas seguidas, pausa las entradas un rato.
    updown_max_consecutive_losses: int = 4
    updown_cooldown_secs: float = 900.0    # 15 min de pausa tras la racha

    # ---- Capital Allocation Manager ----
    alloc_refresh_interval: float = 3600.0
    # Muestras minimas (señales resueltas) por estrategia/categoria antes de
    # confiar en su estadistica. Por debajo de esto el peso es 0: una estrategia
    # sin historial no opera, aunque este en strategies_live_enabled.
    alloc_min_samples: int = 30
    alloc_max_weight: float = 1.5      # techo del multiplicador de sizing

    # ---- Umbrales de señal (heredados del antiguo Prediction Engine) ----
    min_confidence: float = 0.55       # confianza minima para considerar oportunidad
    # Fuerza de la reversion a la media del baseline. 0.0 = SIN señal (default):
    # el estudio de calibracion (research/calibration_study.py, 2026-07-16,
    # n=1856 mercados resueltos) mostro que Polymarket esta bien calibrado y la
    # reversion no tiene edge: solo paga spread. Subirlo reactiva el baseline
    # para experimentos, a sabiendas de que los datos dicen que pierde.
    reversion_k: float = 0.0

    # ---- Risk Manager ----
    paper_starting_balance: float = 1000.0  # USDC iniciales (paper)
    min_edge: float = 0.03             # edge minimo (3 puntos de probabilidad)
    max_position_usd: float = 50.0     # tamaño maximo por posicion
    position_fraction: float = 0.05    # fraccion del balance por operacion
    max_exposure_pct: float = 0.50     # exposicion total maxima (% del balance)
    max_open_positions: int = 8
    min_trade_liquidity: float = 500.0
    # El edge debe superar el coste de round-trip (spread+slippage+fees) por este
    # multiplo. 1.5 = exigir un 50% de margen sobre el coste: si el edge solo
    # empata con el coste, la operacion es ruido caro.
    edge_cost_ratio: float = 1.5
    # Kill-switch por perdida DEL DIA (se reinicia al cambiar el dia UTC), no
    # drawdown total: un limite total latcheado mataria un run desatendido.
    max_daily_loss_pct: float = 0.10

    # ---- Costos de simulacion (Paper) ----
    # Polymarket ha cobrado historicamente 0 fees al taker, pero la Gamma API
    # expone takerBaseFee != 0 en algunos mercados. Se deja configurable y en 0
    # por defecto: si Polymarket activa fees, subir este valor. La formula sigue
    # la forma de Polymarket: fee = (fee_bps/10000) * min(p, 1-p) * shares.
    fee_bps: float = 0.0
    extra_slippage_bps: float = 10.0   # colchon extra de slippage sobre el libro
    # Fraccion de cada nivel del libro que asumimos poder consumir. <1 es
    # deliberadamente conservador (parte del tamaño mostrado puede ser stale) y
    # es lo que genera fills parciales en libros finos.
    book_depth_fraction: float = 0.5
    min_order_shares: float = 5.0      # orderMinSize real de Polymarket

    # ---- Gestion de posiciones (salidas) ----
    take_profit_pct: float = 0.20      # +20% sobre cost basis -> cerrar
    stop_loss_pct: float = 0.15        # -15% sobre cost basis -> cerrar
    exit_min_hours_to_resolution: float = 4.0  # cerrar antes de la resolucion

    # ---- Validacion (gate Live) ----
    paper_validation_min_days: int = 7
    paper_validation_max_days: int = 14
    paper_validation_min_trades: int = 30   # nº minimo de operaciones cerradas

    # ---- Notificaciones ----
    discord_webhook_url: str = ""
    # Notificar CADA oportunidad detectada inunda el canal (el scanner corre cada
    # minuto sobre decenas de mercados), y el ruido acaba haciendo que se ignoren
    # las alertas que importan. Por defecto solo se notifican entradas, salidas y
    # eventos de sistema. Ponlo a true si quieres el detalle completo.
    notify_opportunities: bool = False

    # ---- Secretos LIVE (vacios hasta habilitar Live) ----
    private_key: str = ""
    wallet_address: str = ""
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""

    # ---- CLOB (Live) ----
    clob_chain_id: int = 137            # Polygon mainnet
    # 0 = EOA (clave privada = dueña de los fondos)
    # 1 = proxy de email/magic, 2 = proxy de wallet de navegador (Polymarket UI).
    # Con 1 o 2 hay que fijar clob_funder a la direccion del proxy.
    clob_signature_type: int = 0
    clob_funder: str = ""

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache
def get_settings() -> Settings:
    """Instancia unica (cacheada) de la configuracion."""
    return Settings()
