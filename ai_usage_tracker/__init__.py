"""AI usage tracker package.

Component packages:

- :mod:`ai_usage_tracker.collector` for local OTLP collection and forwarding.
- :mod:`ai_usage_tracker.aggregation_server` for central ingestion,
  client-token administration, and reporting.
"""

from .core import *  # noqa: F401,F403
