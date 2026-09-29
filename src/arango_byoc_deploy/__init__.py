"""Shared BYOC deployment for the Arango Container Manager.

The platform-shaped half of a deploy, extracted from seven hand-copied
``scripts/byoc_deploy.py`` files across the estate. Each repository supplies
its own defaults, pre-flight requirements and verification probes through an
``arango-byoc.toml`` (or ``[tool.arango-byoc]``); everything else lives here.
"""

__version__ = "0.1.0"
