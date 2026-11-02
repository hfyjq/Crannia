"""
Crannia version — single source of truth.

Import this module wherever the app version is needed:
    from core.version import __version__, __version_tuple__, __build__

For the build system (pyproject.toml / hatchling), this file is also the
canonical source.  Keep pyproject.toml `version` field in sync manually
or use [tool.hatch.version] path = "core/version.py".
"""

__version__ = "1.0.0-beta.1"
__version_tuple__ = (1, 0, 0)
__build__ = "beta.1"
