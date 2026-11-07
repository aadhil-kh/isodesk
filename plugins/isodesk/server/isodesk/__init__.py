"""isodesk - isolated Windows desktop sessions for GUI automation."""

from .session import Session, SessionError, SessionManager

__version__ = "0.1.0"
__all__ = ["Session", "SessionManager", "SessionError"]
