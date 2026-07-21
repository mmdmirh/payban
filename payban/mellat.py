"""Mellat (Behpardakht) Shaparak SOAP client.

Flow:
  pay_request -> redirect the customer to the returned StartPay URL ->
  the bank form-POSTs the result to YOUR BACKEND callback (a browser page
  cannot receive that POST) -> confirm(sale_order_id, sale_reference_id)
  there. Confirm runs bpVerifyRequest + bpSettleRequest; settling is
  mandatory — unsettled Mellat transactions auto-reverse in ~24h.
"""

import logging
import uuid
from time import gmtime, strftime

from .errors import GatewayError, MELLAT_RESCODE_FA
from .proxy import ProxyConfig

logger = logging.getLogger(__name__)

MELLAT_STARTPAY = 'https://bpm.shaparak.ir/pgwchannel/startpay.mellat'
MELLAT_WSDL = 'https://bpm.shaparak.ir/pgwchannel/services/pgw?wsdl'


class MellatClient:
    """Behpardakht client with optional Iran-proxy routing.

    Field naming maps to Behpardakht's terms:
    ``terminal_id`` -> terminalId, ``username`` -> userName,
    ``password`` -> userPassword.

    When ``proxy`` is set, the WSDL is fetched from ``{proxy}/?wsdl`` — the
    proxy must serve Shaparak's WSDL with the SOAP endpoint rewritten to
    itself — and every SOAP call carries the proxy's secret header.
    ``bpm.shaparak.ir`` refuses non-Iranian source IPs, so direct calls
    require the server's outbound IP to be whitelisted by the bank.
    """

    #: Mellat's proxy convention predates the shared default; keep its header.
    PROXY_SECRET_HEADER = 'X-Proxy-Auth'

    def __init__(self, terminal_id, username, password,
                 proxy: ProxyConfig = None, timeout=30):
        if not (terminal_id and username and password):
            raise GatewayError("Mellat terminal_id/username/password missing from gateway config")
        self.terminal_id = int(terminal_id)
        self.username = username
        self.password = password
        self.proxy = proxy
        self.timeout = timeout
        self._client = None

    def _soap_client(self):
        if self._client is not None:
            return self._client
        from zeep import Client, Transport
        if self.proxy:
            import requests
            session = requests.Session()
            session.verify = self.proxy.verify_tls
            if not self.proxy.verify_tls:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            if self.proxy.secret:
                # Authenticates us to the proxy (it rejects requests without
                # it), sent on both the WSDL fetch and every SOAP call.
                header = self.proxy.secret_header or self.PROXY_SECRET_HEADER
                session.headers[header] = self.proxy.secret
            transport = Transport(timeout=self.timeout, operation_timeout=self.timeout,
                                  session=session)
            wsdl = f'{self.proxy.base}/?wsdl'
            logger.info("Mellat client initialized via SOAP proxy WSDL: %s", wsdl)
            self._client = Client(wsdl, transport=transport)
        else:
            transport = Transport(timeout=self.timeout, operation_timeout=self.timeout)
            self._client = Client(MELLAT_WSDL, transport=transport)
        return self._client

    def _credentials(self):
        return {
            'terminalId': self.terminal_id,
            'userName': self.username,
            'userPassword': self.password,
        }

    @staticmethod
    def _fresh_order_id():
        # Unique numeric per attempt — duplicates are rejected (code 41).
        return int(str(uuid.uuid4().int)[-12:])

    def pay_request(self, amount, callback_url, description=None, order_id=None,
                    payer_id=0):
        """bpPayRequest; returns ``(ref_id, startpay_url)``.

        ``amount`` is in Rials. ``callback_url`` is where the bank form-POSTs
        the result (must be a backend endpoint). ``order_id`` defaults to a
        fresh unique number; recover your own order at callback time via the
        stored RefId, not this number.
        """
        client = self._soap_client()
        response = str(client.service.bpPayRequest(
            **self._credentials(),
            orderId=order_id or self._fresh_order_id(),
            amount=int(amount),
            localDate=strftime('%Y%m%d', gmtime()),
            localTime=strftime('%H%M%S', gmtime()),
            additionalData=description or 'پرداخت',
            callBackUrl=callback_url,
            payerId=payer_id,
        ))
        res_code, _, ref_id = response.partition(',')
        if res_code == '0' and ref_id:
            return ref_id, f"{MELLAT_STARTPAY}?RefId={ref_id}"
        logger.error("Mellat rejected payment request (code %s)", res_code)
        raise GatewayError(MELLAT_RESCODE_FA.get(
            res_code, f"بانک ملت درخواست پرداخت را نپذیرفت (کد {res_code})"))

    def confirm(self, sale_order_id, sale_reference_id):
        """Server-side confirmation after a ResCode=0 callback POST.

        Verify, then settle. Returns ``True`` when the money is confirmed
        yours. Idempotent across callback retries: verify code 43 (already
        verified) falls back to bpInquiryRequest, settle code 45 (already
        settled) counts as success.
        """
        client = self._soap_client()
        data = {
            **self._credentials(),
            'orderId': int(sale_order_id),
            'saleOrderId': int(sale_order_id),
            'saleReferenceId': int(sale_reference_id),
        }
        verify_result = str(client.service.bpVerifyRequest(**data))
        if verify_result != '0':
            inquiry = str(client.service.bpInquiryRequest(**data))
            if inquiry != '0':
                logger.error("Mellat verify failed (verify=%s, inquiry=%s) for saleOrderId=%s",
                             verify_result, inquiry, sale_order_id)
                return False
        settle_result = str(client.service.bpSettleRequest(**data))
        if settle_result not in ('0', '45'):
            logger.error("Mellat settle failed (%s) for saleOrderId=%s",
                         settle_result, sale_order_id)
            return False
        return True

    def test_connection(self, callback_url='https://example.com/'):
        """Exercise the real gateway and report success or the exact problem.

        Does a bpPayRequest (no charge — the returned RefId just expires
        unused), which validates credentials, IP whitelist AND connectivity
        in one call. Returns ``{ok, stage, code?, message}``.
        """
        try:
            client = self._soap_client()
        except Exception as e:
            return {'ok': False, 'stage': 'connect',
                    'message': f'اتصال به درگاه/پروکسی برقرار نشد: {e}'}
        try:
            resp = str(client.service.bpPayRequest(
                **self._credentials(),
                orderId=self._fresh_order_id(), amount=10000,
                localDate=strftime('%Y%m%d', gmtime()),
                localTime=strftime('%H%M%S', gmtime()),
                additionalData='تست اتصال درگاه',
                callBackUrl=callback_url, payerId=0,
            ))
        except Exception as e:
            return {'ok': False, 'stage': 'soap',
                    'message': f'خطا در فراخوانی سرویس بانک: {e}'}
        code = resp.partition(',')[0].strip()
        return {
            'ok': code == '0', 'stage': 'gateway', 'code': code,
            'message': MELLAT_RESCODE_FA.get(code, f'کد پاسخ بانک: {code}'),
        }
