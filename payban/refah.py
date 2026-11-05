"""Bank Refah (بانک رفاه) payment-gateway client (Shaparak ``refipg`` API).

Flow (stateless, same shape as Zarinpal):
  purchase -> token ->
  redirect the customer to ``{refui}?token=...`` ->
  the bank returns the customer to your callback with ``Token``, ``RRN``, ``status`` ->
  verify(amount, token) -> the bank's RRN (reference number).

Amounts are in Rials everywhere in this module (the bank's unit).
"""

import logging

import requests

from .errors import GatewayError, REFAH_ERROR_FA, flag_on
from .proxy import ProxyConfig

logger = logging.getLogger(__name__)

REFAH_URLS = {
    'purchase': 'https://pna.shaparak.ir/refipg/api/purchase',
    'startpay': 'https://pna.shaparak.ir/refui',
    'confirm': 'https://pna.shaparak.ir/refipg/api/confirm-transaction',
}


class RefahClient:
    """Bank Refah client with optional Iran-proxy routing.

    ``proxy`` reroutes only the server-to-server calls (purchase/confirm); the
    redirect URL stays direct because the customer's browser opens it. The
    proxy must forward ``/refipg/`` to ``pna.shaparak.ir``.
    """

    def __init__(self, username, password, terminal_number, sandbox=False,
                 proxy: ProxyConfig = None, timeout=20):
        if not (username and password and terminal_number):
            raise GatewayError('اطلاعات ورود Bank Refah (نام کاربری، رمز عبور و شماره ترمینال) کامل نیست.')
        self.username = username
        self.password = password
        self.terminal_number = terminal_number
        # The bank publishes no sandbox; the flag is accepted for config
        # parity with the other clients and is otherwise ignored.
        self.sandbox = flag_on(sandbox) if not isinstance(sandbox, bool) else sandbox
        self.proxy = proxy
        self.timeout = timeout
        self.urls = self._build_urls()

    def _build_urls(self):
        urls = dict(REFAH_URLS)
        if self.proxy:
            urls['purchase'] = f'{self.proxy.base}/refipg/api/purchase'
            urls['confirm'] = f'{self.proxy.base}/refipg/api/confirm-transaction'
        return urls

    def _post(self, url, payload):
        headers = {'accept': 'application/json', 'content-type': 'application/json'}
        verify = True
        via_proxy = bool(self.proxy) and url.startswith(self.proxy.base)
        if via_proxy:
            if self.proxy.secret:
                headers[self.proxy.secret_header] = self.proxy.secret
            verify = self.proxy.verify_tls
            if not verify:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        route = 'از طریق پروکسی ایران' if via_proxy else 'مستقیم'
        try:
            response = requests.post(url, json=payload, headers=headers,
                                     timeout=self.timeout, verify=verify)
        except requests.Timeout:
            raise GatewayError(f'بانک رفاه پاسخ نداد (Timeout، مسیر: {route}). اتصال شبکه/پروکسی را بررسی کنید.')
        except requests.RequestException as e:
            logger.error('Refah connection error (%s): %s', route, e)
            raise GatewayError(f'ارتباط با بانک رفاه برقرار نشد (مسیر: {route}): {type(e).__name__}')
        try:
            body = response.json()
        except ValueError:
            logger.error('Refah non-JSON (HTTP %s, %s)', response.status_code, route)
            if not via_proxy:
                raise GatewayError(
                    f'پاسخ نامعتبر (HTTP {response.status_code}) — احتمالاً IP سرور توسط بانک رفاه فیلتر شده است. '
                    f'اگر سرور خارج از ایران است، اتصال از طریق پروکسی ایران را فعال کنید.')
            raise GatewayError(
                f'پاسخ نامعتبر (HTTP {response.status_code}) — درخواست به بانک رفاه نرسید '
                f'(خطای پروکسی؛ اتصال/رمز پروکسی را بررسی کنید).')
        if not isinstance(body, dict):
            raise GatewayError('پاسخ نامعتبر از بانک رفاه.')
        return body

    @staticmethod
    def _error_message(body, fallback):
        errors = body.get('errors') or []
        first = errors[0] if errors and isinstance(errors[0], dict) else {}
        bank_msg = first.get('message', '')
        code = str(first.get('code', '') or '')
        if code in REFAH_ERROR_FA:
            return REFAH_ERROR_FA[code]
        if bank_msg:
            return f'بانک رفاه خطا برگرداند: {bank_msg}' + (f' (کد {code})' if code else '')
        return fallback

    def purchase(self, amount, callback_url, order_id, mobile=None, description=None):
        """Create a payment; returns ``(token, startpay_url)``.

        ``order_id`` must be numeric (the bank rejects non-digit order ids).
        """
        body = self._post(self.urls['purchase'], {
            'userName': self.username,
            'password': self.password,
            'amount': int(amount),
            'callBack': callback_url,
            'celNo': mobile or None,
            'additionalData': description or f'پرداخت سفارش {order_id}',
            'terminalNumber': self.terminal_number,
            'orderId': str(order_id),
        })
        token = (body.get('data') or {}).get('token')
        if body.get('success') and isinstance(token, int) and token > 0:
            return str(token), f"{REFAH_URLS['startpay']}?token={token}"
        logger.error('Refah rejected purchase: %s', body.get('errors'))
        raise GatewayError(self._error_message(body, 'بانک رفاه درخواست پرداخت را نپذیرفت'))

    def verify(self, amount, token, rrn, status):
        """Confirm a returned payment; returns the RRN as a string.

        Call with the values the bank posted back to the callback. A callback
        without a positive RRN and ``status == 0`` is a failed or cancelled
        payment and is rejected before any confirm call is made.
        """
        if str(status) != '0' or not str(rrn).isdigit() or int(rrn) <= 0:
            raise GatewayError(f'پرداخت توسط بانک رفاه تأیید نشد (وضعیت: {status}).')
        body = self._post(self.urls['confirm'], {
            'userName': self.username,
            'password': self.password,
            'amount': int(amount),
            'token': str(token),
        })
        confirmed_rrn = (body.get('data') or {}).get('rrn')
        if body.get('success') and confirmed_rrn:
            logger.info('Refah payment confirmed. Token: %s, RRN: %s', token, confirmed_rrn)
            return str(confirmed_rrn)
        logger.error('Refah confirmation rejected: %s', body.get('errors'))
        raise GatewayError(self._error_message(body, 'تأیید پرداخت توسط بانک رفاه انجام نشد'))

    def test_connection(self, callback_url='https://example.com/'):
        """Probe the purchase endpoint. Nothing is charged: an unused token expires.

        Returns ``{ok, stage, code?, message}`` with admin-presentable Persian.
        """
        try:
            body = self._post(self.urls['purchase'], {
                'userName': self.username,
                'password': self.password,
                'amount': 10000,
                'callBack': callback_url,
                'celNo': None,
                'additionalData': 'تست اتصال درگاه',
                'terminalNumber': self.terminal_number,
                'orderId': '1',
            })
        except GatewayError as e:
            return {'ok': False, 'stage': 'connect', 'message': str(e)}
        token = (body.get('data') or {}).get('token')
        if body.get('success') and isinstance(token, int) and token > 0:
            return {'ok': True, 'stage': 'gateway',
                    'message': 'اتصال موفق بود؛ اطلاعات ورود و ترمینال معتبر است و بانک رفاه درخواست آزمایشی را پذیرفت.'}
        errors = body.get('errors') or []
        code = str(errors[0].get('code', '')) if errors and isinstance(errors[0], dict) else ''
        return {'ok': False, 'stage': 'gateway', 'code': code,
                'message': self._error_message(body, 'بانک رفاه درخواست را نپذیرفت (پاسخ نامشخص).')}
