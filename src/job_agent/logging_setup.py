"""Configuration des logs : RichHandler en console + fichier rotatif quotidien."""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

_LOGGER_NAME = "job_agent"
_CONFIGURED = False


def setup(logs_dir: Path, verbose: bool = False, console: Console | None = None) -> logging.Logger:
    """Initialise le logger racine du projet.

    Args:
        logs_dir: répertoire où écrire les fichiers de logs (créé si absent).
        verbose: si True, niveau DEBUG ; sinon INFO.
        console: rich.Console à utiliser pour la sortie terminal.

    Returns:
        Le logger 'job_agent' configuré. Réappels idempotents.
    """
    global _CONFIGURED
    logger = logging.getLogger(_LOGGER_NAME)
    if _CONFIGURED:
        logger.setLevel(logging.DEBUG if verbose else logging.INFO)
        return logger

    logs_dir.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    rich_handler = RichHandler(
        console=console or Console(stderr=True),
        rich_tracebacks=True,
        markup=False,
        show_path=False,
        show_time=True,
    )
    rich_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    rich_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(rich_handler)

    file_handler = TimedRotatingFileHandler(
        logs_dir / "agent.log",
        when="midnight",
        interval=1,
        backupCount=14,
        encoding="utf-8",
        utc=True,
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(file_handler)

    _CONFIGURED = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Renvoie un logger enfant du logger racine job_agent."""
    if name:
        return logging.getLogger(f"{_LOGGER_NAME}.{name}")
    return logging.getLogger(_LOGGER_NAME)
