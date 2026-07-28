"""Sistema de logs estructurados (structlog).

Salida dual: stdout (lo captura Docker) + fichero rotado en el volumen de logs.
Formato JSON por defecto (parseable por el dashboard y por herramientas).
"""

import logging
import logging.handlers
import os
import sys

import structlog


def err(exc: BaseException) -> str:
    """Describe una excepcion para un log, SIEMPRE con algo dentro.

    `str(exc)` a secas devuelve cadena VACIA en las excepciones que se lanzan sin
    mensaje, que son justo las mas comunes en la capa de red: httpx.ReadTimeout,
    httpx.ConnectError, asyncio.CancelledError... El resultado eran lineas como
    `{"event": "scan.history_fail", "error": ""}`: el log avisa de que algo
    fallo pero no dice QUE, que es la mitad inutil de una alerta. Se antepone
    siempre el tipo, que nunca esta vacio.
    """
    name = type(exc).__name__
    msg = str(exc).strip()
    return f"{name}: {msg}" if msg else name


def configure_logging(
    level: str = "INFO",
    log_dir: str = "/app/logs",
    json_logs: bool = True,
    service: str = "medusa",
) -> structlog.stdlib.BoundLogger:
    os.makedirs(log_dir, exist_ok=True)
    level_num = getattr(logging, level.upper(), logging.INFO)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level_num),
        cache_logger_on_first_use=True,
    )

    renderer = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer()
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processor=renderer,
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level_num)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, f"{service}.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Silenciar loggers ruidosos de librerias. httpx registra CADA peticion a
    # INFO: con un escaneo por minuto sobre decenas de libros son ~17k lineas al
    # dia que entierran los eventos reales y llenan el disco sin aportar nada.
    for noisy in ("uvicorn.access", "asyncio", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(level_num, logging.WARNING))

    return structlog.get_logger(service)
