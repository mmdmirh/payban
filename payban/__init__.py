"""payban — Iranian payment gateway clients with actionable Persian errors.

Currently supported:
  - Zarinpal v4 REST API (request / verify / connection test)
  - Mellat / Behpardakht Shaparak SOAP (pay request / confirm / connection test)
  - Bank Refah Shaparak refipg REST (purchase / confirm / connection test)

Both PSPs block requests from non-Iranian IPs. When your server is hosted
abroad, route the API calls through a reverse proxy inside Iran and describe
it with a ``ProxyConfig``; the customer-facing redirect URLs always stay
direct (they are opened by the customer's browser, not by your server).
"""

from .errors import GatewayError, MELLAT_RESCODE_FA, ZARINPAL_ERROR_FA
from .proxy import ProxyConfig
from .zarinpal import ZarinpalClient
from .mellat import MellatClient
from .refah import RefahClient

__version__ = "0.2.0"

__all__ = [
    "GatewayError",
    "ProxyConfig",
    "ZarinpalClient",
    "MellatClient",
    "RefahClient",
    "MELLAT_RESCODE_FA",
    "ZARINPAL_ERROR_FA",
]
