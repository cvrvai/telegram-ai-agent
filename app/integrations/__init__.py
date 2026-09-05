"""Optional business system adapters.

Adapters are disabled until their endpoint and secret are configured. This
keeps the core assistant usable without silently sending business data.
"""

from .google_account import GoogleAccount
from .hub import IntegrationHub

__all__ = ["IntegrationHub", "GoogleAccount"]
