"""Physical status light for coding agents.

A daemon-free bridge: agent hooks report per-session state to a small file store,
the aggregate state is resolved by priority, and a USB busylight is set to the
matching solid color. See README.md for the design.
"""

from .resolve import State, resolve_color

__all__ = ["State", "resolve_color"]
__version__ = "0.1.2"
