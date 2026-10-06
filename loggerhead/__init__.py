"""Loggerhead aquarium controller.

Loggerhead is intentionally importable on non-Raspberry Pi hosts. Hardware-specific
drivers use optional imports and simulation-safe fallbacks so tests can validate the
control logic without attached equipment.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
