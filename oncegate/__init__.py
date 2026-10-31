"""once-gate: make agent side effects happen at most once."""
from .gate import Gate, Indeterminate, key_for

__version__ = "0.1.0"
__all__ = ["Gate", "Indeterminate", "key_for", "__version__"]
