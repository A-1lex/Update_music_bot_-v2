"""Compatibility imports for the OS-backed single-instance lock."""
from utils.process_lock import ProcessLock as SingleInstanceLock, SingleInstanceError

__all__ = ["SingleInstanceLock", "SingleInstanceError"]
