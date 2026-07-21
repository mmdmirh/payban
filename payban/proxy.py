"""Reverse-proxy description for reaching Iran-only PSP endpoints.

Both Shaparak (Mellat) and api.zarinpal.com refuse non-Iranian source IPs.
A server hosted abroad reaches them through a reverse proxy inside Iran
(e.g. nginx on an Iranian VPS). The proxy is expected to:

  - require a shared-secret header and reject requests without it, and
  - (for Mellat) serve the WSDL with the SOAP endpoint rewritten to itself.

The proxy commonly terminates TLS with a self-signed certificate — it is an
IP endpoint you operate, so ``verify_tls`` defaults to ``False`` for the
proxy leg only; direct calls to the PSPs always verify.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProxyConfig:
    #: Base URL of the proxy, e.g. ``https://203.0.113.7``.
    base_url: str
    #: Shared secret expected by the proxy.
    secret: str = ''
    #: Header the secret is sent in.
    secret_header: str = 'X-Proxy-Secret'
    #: Verify the proxy's TLS certificate (self-signed proxies -> False).
    verify_tls: bool = False

    @property
    def base(self):
        return (self.base_url or '').rstrip('/')

    def __bool__(self):
        return bool(self.base)
