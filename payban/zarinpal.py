"""Zarinpal v4 REST client.

Flow (stateless, no gateway-side records needed):
  request_payment -> redirect the customer to the returned StartPay URL ->
  Zarinpal returns the customer to your callback (?Authority=..&Status=OK) ->
  verify(amount, authority).
"""

import logging

import requests

from .errors import GatewayError, ZARINPAL_ERROR_FA, flag_on
from .proxy import ProxyConfig

logger = logging.getLogger(__name__)

ZARINPAL_URLS = {
    'request': 'https://api.zarinpal.com/pg/v4/payment/request.json',
    'verify': 'https://api.zarinpal.com/pg/v4/payment/verify.json',
    'startpay': 'https://www.zarinpal.com/pg/StartPay/',
}
ZARINPAL_SANDBOX_URLS = {
    'request': 'https://sandbox.zarinpal.com/pg/v4/payment/request.json',
    'verify': 'https://sandbox.zarinpal.com/pg/v4/payment/verify.json',
    'startpay': 'https://sandbox.zarinpal.com/pg/StartPay/',
}


class ZarinpalClient:
    """Zarinpal v4 client with optional Iran-proxy routing.

    ``proxy`` reroutes only the server-to-server API calls (request/verify);
    the StartPay URL stays direct because the customer's browser opens it.
    Sandbox mode always calls sandbox.zarinpal.com directly — the sandbox is
    itself Iran-only, so from foreign hosting the connection test will say so.
    """

    def __init__(self, merchant_id, sandbox=False, currency='IRR',
                 proxy: ProxyConfig = None, timeout=20):
        if not merchant_id:
            raise GatewayError("Zarinpal merchant_id missing from gateway config")
        self.merchant_id = merchant_id
        self.sandbox = flag_on(sandbox) if not isinstance(sandbox, bool) else sandbox
        self.currency = (currency or 'IRR').upper()
        self.proxy = proxy
        self.timeout = timeout
        self.urls = self._build_urls()

    def _build_urls(self):
        if self.sandbox:
            return dict(ZARINPAL_SANDBOX_URLS)
        urls = dict(ZARINPAL_URLS)
        if self.proxy:
            urls['request'] = f'{self.proxy.base}/pg/v4/payment/request.json'
            urls['verify'] = f'{self.proxy.base}/pg/v4/payment/verify.json'
        return urls

    def _post(self, url, payload):
        headers = {'accept': 'application/json'}
        verify = True
        # Decide from the URL actually being called, not from a flag: sandbox
        # mode keeps direct sandbox URLs even when a proxy is configured, and
        # the proxy secret must never be sent to Zarinpal's own hosts.
        via_proxy = bool(self.proxy) and url.startswith(self.proxy.base)
        if via_proxy:
            if self.proxy.secret:
                headers[self.proxy.secret_header] = self.proxy.secret
            verify = self.proxy.verify_tls
            if not verify:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        is_sandbox = 'sandbox.zarinpal.com' in url
        route = 'از طریق پروکسی ایران' if via_proxy else ('مستقیم به سندباکس' if is_sandbox else 'مستقیم')
        try:
            response = requests.post(url, json=payload, headers=headers,
                                     timeout=self.timeout, verify=verify)
        except requests.Timeout:
            raise GatewayError(f"زرین‌پال پاسخ نداد (Timeout، مسیر: {route}). اتصال شبکه/پروکسی را بررسی کنید.")
        except requests.RequestException as e:
            logger.error("Zarinpal connection error (%s): %s", route, e)
            raise GatewayError(f"ارتباط با زرین‌پال برقرار نشد (مسیر: {route}): {type(e).__name__}")
        try:
            body = response.json()
        except ValueError:
            snippet = (response.text or '')[:100].replace('\n', ' ').strip()
            logger.error("Zarinpal non-JSON (HTTP %s, %s): %s", response.status_code, route, snippet)
            # Non-JSON almost always means the request never reached zarinpal
            # (IP filtering, or the proxy returned an error page) — say so.
            if is_sandbox:
                raise GatewayError(
                    f"پاسخ نامعتبر (HTTP {response.status_code}) — حالت SANDBOX فعال است و "
                    f"sandbox.zarinpal.com از سرور خارج از ایران در دسترس نیست. برای درگاه واقعی، SANDBOX را خاموش کنید.")
            if not via_proxy:
                raise GatewayError(
                    f"پاسخ نامعتبر (HTTP {response.status_code}) — احتمالاً IP سرور توسط زرین‌پال فیلتر شده است. "
                    f"اگر سرور خارج از ایران است، اتصال از طریق پروکسی ایران را فعال کنید.")
            raise GatewayError(
                f"پاسخ نامعتبر (HTTP {response.status_code}) — درخواست به api.zarinpal.com نرسید "
                f"(خطای پروکسی؛ اتصال/رمز پروکسی را بررسی کنید).")
        return body.get('data') or {}, body.get('errors') or {}

    def request_payment(self, amount, callback_url, description=None,
                        mobile=None, order_id=None):
        """Create a payment; returns ``(authority, startpay_url)``.

        ``amount`` must match ``self.currency`` (IRR by default).
        Raises :class:`GatewayError` with a Persian message on rejection.
        """
        metadata = {}
        if mobile:
            metadata['mobile'] = mobile
        if order_id:
            metadata['order_id'] = str(order_id)
        data, errors = self._post(self.urls['request'], {
            'merchant_id': self.merchant_id,
            'amount': int(amount),
            'currency': self.currency,
            'callback_url': callback_url,
            'description': description or (f'پرداخت سفارش {order_id}' if order_id else 'پرداخت'),
            'metadata': metadata,
        })
        if data.get('code') == 100 and data.get('authority'):
            authority = data['authority']
            return authority, f"{self.urls['startpay']}{authority}"
        logger.error("Zarinpal rejected payment request: %s", errors or data)
        raise GatewayError(self._error_message(errors, 'زرین‌پال درخواست پرداخت را نپذیرفت'))

    def verify(self, amount, authority):
        """Verify a returned payment; returns the ``ref_id`` string.

        Code 100 = verified now, 101 = already verified — both mean the money
        moved, so verification is idempotent (e.g. the customer refreshes
        the verify page).
        """
        data, errors = self._post(self.urls['verify'], {
            'merchant_id': self.merchant_id,
            'amount': int(amount),
            'authority': authority,
        })
        if data.get('code') in (100, 101):
            ref_id = data.get('ref_id')
            logger.info("Zarinpal payment verified. Authority: %s, RefID: %s", authority, ref_id)
            return str(ref_id)
        logger.error("Zarinpal verification rejected: %s", errors or data)
        raise GatewayError(self._error_message(errors, 'تایید پرداخت توسط زرین‌پال انجام نشد'))

    def test_connection(self, callback_url='https://example.com/'):
        """Exercise the real gateway and report success or the exact problem.

        Sends a request.json probe (nothing is charged — an unused authority
        just expires). Returns ``{ok, stage, code?, message}`` where message
        is admin-presentable Persian.
        """
        try:
            data, errors = self._post(self.urls['request'], {
                'merchant_id': self.merchant_id, 'amount': 10000,
                'currency': self.currency, 'callback_url': callback_url,
                'description': 'تست اتصال درگاه',
            })
        except GatewayError as e:
            return {'ok': False, 'stage': 'connect', 'message': str(e)}
        if data.get('code') == 100 and data.get('authority'):
            return {'ok': True, 'stage': 'gateway',
                    'message': 'اتصال موفق بود؛ کد پذیرنده معتبر است و زرین‌پال درخواست آزمایشی را پذیرفت.'}
        code = str((errors or {}).get('code', ''))
        return {'ok': False, 'stage': 'gateway', 'code': code,
                'message': self._error_message(errors, 'زرین‌پال درخواست را نپذیرفت (پاسخ نامشخص).')}

    @staticmethod
    def _error_message(errors, fallback):
        code = str((errors or {}).get('code', ''))
        zp_msg = (errors or {}).get('message', '')
        friendly = ZARINPAL_ERROR_FA.get(code)
        if friendly:
            return friendly
        if zp_msg:
            return f'زرین‌پال خطا برگرداند: {zp_msg}' + (f' (کد {code})' if code else '')
        return fallback
