import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("whatsapp_gateway")
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.StreamHandler(), RotatingFileHandler(data_dir / "app.log", maxBytes=10_000_000, backupCount=5)):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
