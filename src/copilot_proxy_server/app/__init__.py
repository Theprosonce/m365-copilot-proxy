"""FastAPI application package.

The package keeps application construction, request processing, session handling,
response conversion, and streaming concerns in focused modules.
"""

from .factory import create_app
from .sessions import _conversation_key, _first_real_user_text, _trim_history

__all__ = [
    "create_app",
    "_conversation_key",
    "_first_real_user_text",
    "_trim_history",
]
