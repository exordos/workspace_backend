"""Standalone Workspace provider daemons.

This package intentionally does not import :mod:`workspace`.  Provider
processes communicate with Workspace exclusively through the service API and
store transport state in their own PostgreSQL databases.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
