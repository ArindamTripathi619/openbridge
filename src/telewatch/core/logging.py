import logging
import sys
from pathlib import Path
from typing import Optional

def setup_logger(log_file: Optional[Path] = None, level=logging.INFO, suppress_console: bool = False):
    """Set up a unified logger for all components."""
    logger = logging.getLogger("telewatch")
    logger.setLevel(level)
    
    # Avoid duplicate handlers
    if logger.handlers:
        return logger
    
    formatter = logging.Formatter(
        '[%(asctime)s] %(levelname)s [%(name)s:%(lineno)d] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Console handler (for foreground)
    if not suppress_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    
    # File handler (if log_file provided)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        
    return logger

def get_logger(name: str):
    """Get a named logger child of the main telewatch logger."""
    return logging.getLogger(f"telewatch.{name}")
