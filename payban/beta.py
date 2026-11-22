"""Bank Refah «بتا» (Beta — بستر تامین اعتبار) merchant client.

Talks to the Bank Refah open-banking BNPL API (``api.rb24.ir``): OAuth2
client-credentials token, customer credit inquiry, OTP request and OTP
consume, plus the order / installment services. Every bank response carries a
numeric ``status``; anything other than 200 raises :class:`BetaError` with a
Persian message from :data:`STATUS_MESSAGES`.

Units: **amounts are in Rials everywhere** (the bank's unit), exactly like
:class:`payban.RefahClient`. Convert at your edge if your shop prices in Toman.

Iran proxy: ``api.rb24.ir`` only answers whitelisted Iranian IPs. Pass a
:class:`payban.ProxyConfig` to route server-to-server calls through an Iranian
reverse proxy. Direct calls always verify TLS.

Mock mode (``mock_mode=True``) runs the whole state machine in-process with no
network access — handy for demos and tests while whitelisting is arranged. It
keeps OTP / sale state in ``cache`` (any object with ``get(key)``,
``set(key, value, ttl_seconds)`` and ``delete(key)``; a Django cache works). The
default is a per-client in-memory cache.

Secrets (client_secret / apikey) and OTP values are never logged.
"""

import logging
import time
import uuid

import requests

from .errors import GatewayError, flag_on
from .jalali import (
    add_months as _jalali_add_months,
    format_jalali,
    jalali_str_to_date as _jalali_to_date,
    today_jalali,
)
from .proxy import ProxyConfig

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = 'https://api.rb24.ir'

#: (connect, read) — a hung bank call must never hold a web worker.
TIMEOUT = (10, 30)

# Bank Refah hard limits (rev 3.1).
MAX_INSTALLMENTS = 36
MAX_PURCHASES_PER_MONTH = 8

#: status -> customer-facing Persian message. 200 is success (empty message).
STATUS_MESSAGES = {
    200: '',
    4403: 'کلید سرویس (API Key) نادرست است.',
    4404: 'دسترسی محدود است.',
    4406: 'این درخواست تکراری است.',
    4440: 'اطلاعات ورودی معتبر نیست.',
    4441: 'مبلغ یا تعداد اقساط نادرست است.',
    4442: 'عنوان فروش نادرست است.',
    4443: 'کد ملی وارد شده نادرست است.',
    4444: 'کد تایید پیش‌تر ارسال شده است؛ لطفاً کمی صبر کنید.',
    4446: 'کد تایید نادرست است.',
    4450: 'شما هنوز وکالت‌نامه استعلام و کسر اقساط را در سامانه بتا امضا نکرده‌اید.',
    4451: 'پروفایل شما در سامانه بتا کامل نیست.',
    4452: 'اعتبارسنجی شما انجام نشده یا نیاز به اعتبارسنجی مجدد دارد.',
    4453: 'در حال حاضر اعتباری برای شما ثبت نشده است.',
    4455: 'شما در طرح دیگری عضو هستید و تا پایان آن امکان استفاده از این طرح را ندارید.',
    4461: 'مبلغ درخواستی از اعتبار شما بیشتر است.',
    4462: 'تعداد اقساط نمی‌تواند بیشتر از ۳۶ ماه باشد.',
    4463: 'در هر ماه حداکثر ۸ خرید اقساطی از این فروشگاه امکان‌پذیر است.',
    4470: 'سرویس اقساطی برای این فروشگاه فعال نشده است.',
    4471: 'اطلاعات پذیرنده یافت نشد.',
    4473: 'پذیرنده غیرفعال است.',
    4474: 'پذیرنده مجاز به ثبت فروش نیست.',
    4475: 'پذیرنده تفاهم‌نامه فعالی ندارد.',
    4476: 'کارمزد پذیرنده به‌درستی تعریف نشده است.',
}

#: The customer must finish something at the bank (وکالت‌نامه / پروفایل /
#: اعتبارسنجی).
ENROLMENT_STATUSES = (4450, 4451, 4452, 4455)

#: The MERCHANT must fix something. Never show these raw to a customer.
MERCHANT_STATUSES = (4470, 4471, 4473, 4474, 4475, 4476)

GENERIC_ERROR = 'خطا در سامانه بتا.'
MERCHANT_PUBLIC_MESSAGE = 'خرید اقساطی موقتاً در دسترس نیست.'
CONNECTION_ERROR = 'خطا در ارتباط با سامانه بتا. لطفاً بعداً تلاش کنید.'
INVALID_RESPONSE = 'پاسخ نامعتبر از سامانه بتا.'
CONFIG_ERROR = 'تنظیمات سرویس اقساطی کامل نیست.'

MOCK_OTP = '11111'
MOCK_OTP_TTL = 120  # seconds — mirrors the bank's 2-minute OTP lifetime.

#: Mock-only national-code suffix triggers so every error branch is reachable
#: without the bank: ...0 -> 4450, ...1 -> 4451, ...2 -> 4452, ...3 -> 4453.
MOCK_SUFFIX_STATUSES = {'0': 4450, '1': 4451, '2': 4452, '3': 4453}


def message_for(status, bank_message=''):
    """Persian text for a bank status. Unmapped statuses get a generic message
    with the bank's own wording appended when it sent any."""
    if status in STATUS_MESSAGES:
        return STATUS_MESSAGES[status]
    text = GENERIC_ERROR
    if bank_message:
        text = f'{text} ({bank_message})'
    return text


def kind_for(status):
    """'enrolment' (customer fixes it at the bank) / 'merchant' (store fixes it)
    / 'input' (everything else)."""
    if status in ENROLMENT_STATUSES:
        return 'enrolment'
    if status in MERCHANT_STATUSES:
        return 'merchant'
    return 'input'


class BetaError(GatewayError):
    """A Beta bank-level failure. ``message`` is always Persian and safe to show
    to a customer *unless* ``kind == 'merchant'`` (then show
    :data:`MERCHANT_PUBLIC_MESSAGE`)."""

    def __init__(self, message, status=None, raw=None):
        self.status = status
        self.message = message
        self.raw = raw or {}
        super().__init__(message)

    @property
    def kind(self):
        return kind_for(self.status)

    def __str__(self):
        return self.message


def _mask(value):
    return '***' if value else ''


def _digits(value):
    return ''.join(ch for ch in str(value or '') if ch.isdigit())


def year_month_of(start_date):
    """'YYYY/MM/DD' (or 'YYYYMMDD') -> 'YYYYMM'."""
    raw = _digits(start_date)
    if len(raw) >= 6:
        return raw[:6]
    y, m, _ = today_jalali()
    return f'{y:04d}{m:02d}'


def wire_start_date(start_date):
    """The bank's ``startDate`` is a datetime. Accepts the Jalali 'YYYY/MM/DD'
    this library uses internally (or an ISO datetime, passed through) and returns
    an ISO-8601 datetime at the end of that Gregorian day in Iran time — the
    shape the bank is known to accept — so the bank's date-only reading never
    slips a day."""
    text = str(start_date or '')
    if 'T' in text:
        return text
    day = _jalali_to_date(text)
    if day is None:
        return text
    return f'{day.isoformat()}T23:59:59+03:30'


def _rows(data):
    """List endpoints answer ``data = {totalRecords, data: [...]}`` (a bare list
    is accepted too)."""
    if isinstance(data, dict):
        data = data.get('data')
    return data if isinstance(data, list) else []


class MemoryCache:
    """Tiny in-process TTL cache; the default for tokens and mock state."""

    def __init__(self):
        self._data = {}

    def get(self, key):
        item = self._data.get(key)
        if not item:
            return None
        value, expires = item
        if expires is not None and expires <= time.time():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key, value, ttl=None):
        self._data[key] = (value, time.time() + ttl if ttl else None)

    def delete(self, key):
        self._data.pop(key, None)


class BetaClient:
    """Beta client for one merchant.

    :param client_id / client_secret: OAuth2 client-credentials pair.
    :param apikey: the ``apikey`` header issued by the Beta panel.
    :param terminal_name: the ``TerminalName`` header (equals ``client_id``
        unless the bank says otherwise).
    :param sale_title: default sale title sent to the bank.
    :param base_url: bank origin (override for a staging host).
    :param proxy: optional :class:`ProxyConfig` for Iran-proxy routing.
    :param cache: ``get/set/delete`` cache for the OAuth token and mock state.
    :param scope: opaque string isolating this merchant's cache keys.
    """

    def __init__(self, client_id='', client_secret='', apikey='', terminal_name='',
                 sale_title='خرید اقساطی', base_url='', proxy: ProxyConfig = None,
                 mock_mode=False, cache=None, scope='default', timeout=TIMEOUT):
        self.client_id = str(client_id or '').strip()
        self.client_secret = str(client_secret or '').strip()
        self.apikey = str(apikey or '').strip()
        self.terminal_name = str(terminal_name or '').strip()
        self.sale_title = str(sale_title or '').strip() or 'خرید اقساطی'
        self.base_url = (str(base_url or '').strip() or DEFAULT_BASE_URL).rstrip('/')
        self.proxy = proxy if proxy else None
        self.mock_mode = flag_on(mock_mode) if not isinstance(mock_mode, bool) else mock_mode
        self.cache = cache if cache is not None else MemoryCache()
        self.scope = str(scope)
        self.timeout = timeout

    # --- plumbing ----------------------------------------------------------

    @property
    def proxied(self):
        """True when calls leave through the Iran proxy."""
        return bool(self.proxy)

    def _origin(self):
        return self.proxy.base if self.proxied else self.base_url

    def _extra_headers(self):
        if self.proxied and self.proxy.secret:
            return {self.proxy.secret_header: self.proxy.secret}
        return {}

    def _verify(self):
        # A self-signed proxy is an endpoint you operate; direct calls verify.
        if not self.proxied:
            return True
        if not self.proxy.verify_tls:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return self.proxy.verify_tls

    def _require_config(self, *keys):
        missing = [k for k in keys if not getattr(self, k, '')]
        if missing:
            logger.error('Beta config incomplete (scope %s): missing %s', self.scope, ','.join(missing))
            raise BetaError(CONFIG_ERROR, status=None, raw={'missing': missing})

    def _post(self, path, *, json_body=None, method='POST'):
        url = f'{self._origin()}{path}'
        headers = dict(self._extra_headers())
        headers.update({
            'Authorization': f'bearer {self.token()}',
            'apikey': self.apikey,
            'TerminalName': self.terminal_name,
        })
        try:
            if method == 'GET':
                response = requests.get(url, headers=headers, timeout=self.timeout,
                                        verify=self._verify())
            else:
                response = requests.post(url, json=json_body or {}, headers=headers,
                                         timeout=self.timeout, verify=self._verify())
        except requests.RequestException as exc:
            # Endpoint and exception type only — never the payload.
            logger.error('Beta %s %s connection error (scope %s): %s',
                         method, path, self.scope, type(exc).__name__)
            raise BetaError(CONNECTION_ERROR, status=None, raw={})

        logger.info('Beta %s %s -> HTTP %s (scope %s, proxy=%s)',
                    method, path, response.status_code, self.scope, self.proxied)

        try:
            body = response.json()
        except ValueError:
            logger.error('Beta %s %s returned non-JSON (HTTP %s)', method, path, response.status_code)
            raise BetaError(INVALID_RESPONSE, status=None, raw={'http_status': response.status_code})

        if not isinstance(body, dict):
            body = {'data': body}
        return self._check(body, path, response.status_code)

    @staticmethod
    def _check(body, path, http_status):
        """Raise on any non-200 bank status; return the body otherwise."""
        raw_status = body.get('status', body.get('Status'))
        status = None
        if raw_status is not None:
            try:
                status = int(raw_status)
            except (TypeError, ValueError):
                status = None

        if status is not None and status != 200:
            bank_message = str(body.get('message') or body.get('Message') or '')
            message = message_for(status, bank_message)
            logger.warning('Beta %s rejected with status %s', path, status)
            raise BetaError(message, status=status, raw=body)

        if status is None and http_status >= 400:
            logger.error('Beta %s failed with HTTP %s and no bank status', path, http_status)
            raise BetaError(GENERIC_ERROR, status=None, raw={'http_status': http_status})

        return body

    # --- token -------------------------------------------------------------

    def _token_cache_key(self):
        return f'beta_token:{self.scope}:{self.client_id}'

    def token(self):
        """OAuth2 client-credentials token, cached until 60s before it expires."""
        if self.mock_mode:
            return 'mock-token'

        self._require_config('client_id', 'client_secret')
        key = self._token_cache_key()
        cached = self.cache.get(key)
        if cached:
            return cached

        url = f'{self._origin()}/connect/token'
        headers = dict(self._extra_headers())
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
        try:
            response = requests.post(
                url,
                data={
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
                    'grant_type': 'client_credentials',
                },
                headers=headers, timeout=self.timeout, verify=self._verify(),
            )
        except requests.RequestException as exc:
            logger.error('Beta token connection error (scope %s): %s', self.scope, type(exc).__name__)
            raise BetaError(CONNECTION_ERROR, status=None, raw={})

        logger.info('Beta POST /connect/token -> HTTP %s (scope %s, client_id %s, proxy=%s)',
                    response.status_code, self.scope, self.client_id, self.proxied)

        try:
            body = response.json()
        except ValueError:
            logger.error('Beta token returned non-JSON (HTTP %s)', response.status_code)
            raise BetaError(INVALID_RESPONSE, status=None, raw={'http_status': response.status_code})

        access_token = (body or {}).get('access_token')
        if not access_token:
            logger.error('Beta token request rejected (HTTP %s, client_secret=%s)',
                         response.status_code, _mask(self.client_secret))
            raise BetaError('احراز هویت با سامانه بتا انجام نشد.', status=None,
                            raw={'http_status': response.status_code})

        try:
            expires_in = int(body.get('expires_in') or 0)
        except (TypeError, ValueError):
            expires_in = 0
        self.cache.set(key, access_token, max(expires_in - 60, 30))
        return access_token

    # --- bank services (amounts in Rials) -----------------------------------

    def inquiry(self, national_code):
        """استعلام توان بازپرداخت ماهانه. Returns
        ``{'national_code', 'credit', 'cellphone'}`` — ``credit`` is the
        customer's predicted MONTHLY repayment capacity in Rials, not a
        purchase ceiling."""
        code = _digits(national_code)
        if self.mock_mode:
            return self._mock_inquiry(code)

        self._require_config('apikey', 'terminal_name')
        body = self._post(f'/beta/1.0/credit/{code}/inquiry')
        data = body.get('data') or {}
        try:
            credit = int(data.get('credit') or 0)
        except (TypeError, ValueError):
            credit = 0
        return {
            'national_code': str(data.get('nationalCode') or code),
            'credit': credit,
            'cellphone': str(data.get('cellphone') or ''),
        }

    def request_otp(self, national_code, amount, months, title=None, start_date=None):
        """Ask the bank to SMS the customer an OTP (valid 2 minutes). A new OTP
        cannot be issued while a valid one exists (status 4444)."""
        code = _digits(national_code)
        amount = int(amount or 0)
        months = int(months or 0)
        start = start_date or format_jalali(*today_jalali())

        if self.mock_mode:
            return self._mock_request_otp(code, amount, months, start)

        self._require_config('apikey', 'terminal_name')
        self._post(f'/beta/1.0/credit/{code}/request', json_body={
            'title': title or self.sale_title,
            'startDate': wire_start_date(start),
            'amount': amount,
            'numberOfInstallments': months,
            'startDateYearMonth': year_month_of(start),
        })
        return {'ok': True}

    def consume(self, national_code, amount, months, otp, request_id=None,
                title=None, start_date=None):
        """Spend the customer's credit. The OTP is bound to (amount, months);
        change either and the OTP is void. Returns ``{'sale_id': str}``."""
        code = _digits(national_code)
        amount = int(amount or 0)
        months = int(months or 0)
        start = start_date or format_jalali(*today_jalali())

        if self.mock_mode:
            return self._mock_consume(code, amount, months, otp, request_id, start)

        self._require_config('apikey', 'terminal_name')
        body = self._post(f'/beta/1.0/credit/{code}/consume', json_body={
            'title': title or self.sale_title,
            'startDate': wire_start_date(start),
            'amount': amount,
            'numberOfInstallments': months,
            'otp': str(otp or ''),
            'requestId': str(request_id or ''),
            'startDateYearMonth': year_month_of(start),
        })
        return {'sale_id': str(body.get('data') or '')}

    # --- order services ----------------------------------------------------

    def list_orders(self, year_month):
        """فهرست فروش‌های یک ماه. ``year_month`` is Jalali 'YYYYMM'."""
        ym = _digits(year_month)[:6]
        if self.mock_mode:
            return self._mock_list_orders(ym)
        self._require_config('apikey', 'terminal_name')
        body = self._post(f'/beta/1.0/order/{ym}/all', method='GET')
        return _rows(body.get('data'))

    def get_installments(self, order_id):
        """اقساط یک فروش, normalised to
        ``[{'row_number', 'due_date', 'due_date_g', 'amount', 'status', 'raw'}]``."""
        if self.mock_mode:
            return self._mock_installments(str(order_id))
        self._require_config('apikey', 'terminal_name')
        body = self._post(f'/beta/1.0/order/{order_id}/installments', method='GET')
        rows = _rows(body.get('data'))
        return [self._normalise_installment(row, index) for index, row in enumerate(rows, start=1)]

    @staticmethod
    def _normalise_installment(row, index):
        """Bank row (``Id, Amount, OverdueDate, SettelDate, IsSetteled, OrderId,
        Status, StatusTitle, IsDeleted``; key case varies) -> a stable shape."""
        if not isinstance(row, dict):
            return {'row_number': index, 'id': None, 'due_date': '', 'due_date_g': None,
                    'amount': 0, 'status': '', 'settled': False, 'deleted': False,
                    'raw': row}
        v = {str(k).lower(): val for k, val in row.items()}
        due = v.get('overduedate') or v.get('duedate') or v.get('date') or v.get('paydate') or ''
        try:
            amount = int(v.get('amount') or 0)
        except (TypeError, ValueError):
            amount = 0
        try:
            row_number = int(v.get('rownumber') or v.get('row') or index)
        except (TypeError, ValueError):
            row_number = index
        due_text = str(due)
        due_date = None
        if due_text:
            head = due_text.split('T')[0].replace('-', '/')
            # Gregorian ISO ('2026-12-21T...') or Jalali ('1405/09/30').
            digits = ''.join(ch for ch in head if ch.isdigit())
            if len(digits) == 8 and int(digits[:4]) > 1700:
                from datetime import date as _date
                try:
                    due_date = _date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
                except ValueError:
                    due_date = None
            else:
                due_date = _jalali_to_date(head)
        return {
            'row_number': row_number,
            'id': v.get('id'),
            'due_date': due_text,
            'due_date_g': due_date,
            'amount': amount,
            'status': str(v.get('statustitle') or v.get('status') or v.get('state') or ''),
            'settled': bool(v.get('issetteled') or v.get('issettled')),
            'deleted': bool(v.get('isdeleted')),
            'raw': row,
        }

    def delete_order(self, order_id):
        if self.mock_mode:
            self.cache.delete(self._mock_sale_key(str(order_id)))
            return {'ok': True}
        self._require_config('apikey', 'terminal_name')
        self._post(f'/beta/1.0/order/{order_id}/delete', json_body={})
        return {'ok': True}

    def delete_installment(self, order_id, installment_id):
        if self.mock_mode:
            return {'ok': True}
        self._require_config('apikey', 'terminal_name')
        self._post(f'/beta/1.0/order/{order_id}/installments/{installment_id}/delete', json_body={})
        return {'ok': True}

    def validate_date(self):
        """آیا امروز تاریخ مجاز ثبت فروش است."""
        if self.mock_mode:
            return {'ok': True, 'data': True}
        self._require_config('apikey', 'terminal_name')
        body = self._post('/beta/1.0/date/validate', method='GET')
        return {'ok': True, 'data': body.get('data')}

    # --- admin ------------------------------------------------------------

    def test_connection(self):
        """``{'ok', 'stage', 'message'}`` — same shape as the other payban
        clients' ``test_connection``. ``stage`` is 'error' (config), 'token' or
        'inquiry'."""
        if self.mock_mode:
            return {'ok': True, 'stage': 'inquiry',
                    'message': 'حالت آزمایشی فعال است؛ اتصال به بانک لازم نیست.'}

        try:
            self._require_config('client_id', 'client_secret', 'apikey', 'terminal_name')
        except BetaError as exc:
            return {'ok': False, 'stage': 'error', 'message': exc.message}

        try:
            self.token()
        except BetaError as exc:
            return {'ok': False, 'stage': 'token', 'message': exc.message}

        try:
            # date/validate is the cheapest authenticated call: it proves the
            # apikey/TerminalName pair is accepted without touching customer data.
            self.validate_date()
        except BetaError as exc:
            return {'ok': False, 'stage': 'inquiry', 'message': exc.message}

        return {'ok': True, 'stage': 'inquiry', 'message': 'اتصال به سامانه بتا برقرار است.'}

    # --- mock mode ---------------------------------------------------------
    # A complete in-process state machine: no sockets are opened at all.

    def _mock_otp_key(self, national_code):
        return f'beta_mock_otp:{self.scope}:{national_code}'

    def _mock_sale_key(self, sale_id):
        return f'beta_mock_sale:{self.scope}:{sale_id}'

    def _mock_guard(self, national_code):
        """کد ملی must be 10 digits; the last digit picks a scripted failure so
        every error branch is reachable without the bank."""
        if len(national_code) != 10:
            raise BetaError(STATUS_MESSAGES[4443], status=4443, raw={'mock': True})
        status = MOCK_SUFFIX_STATUSES.get(national_code[-1])
        if status:
            raise BetaError(STATUS_MESSAGES[status], status=status, raw={'mock': True})

    @staticmethod
    def _mock_credit(national_code):
        """Deterministic monthly repayment capacity (Rials) derived from the
        national code so a customer always sees the same number."""
        seed = int(national_code[-6:] or 0)
        return 10_000_000 + (seed % 90) * 1_000_000

    @staticmethod
    def _mock_cellphone(national_code):
        return f'0912{national_code[-7:]}'

    def _mock_validate_amount(self, amount, months):
        if amount <= 0 or months <= 0:
            raise BetaError(STATUS_MESSAGES[4441], status=4441, raw={'mock': True})
        if months > MAX_INSTALLMENTS:
            raise BetaError(STATUS_MESSAGES[4462], status=4462, raw={'mock': True})

    def _mock_inquiry(self, national_code):
        self._mock_guard(national_code)
        return {
            'national_code': national_code,
            'credit': self._mock_credit(national_code),
            'cellphone': self._mock_cellphone(national_code),
        }

    def _mock_request_otp(self, national_code, amount, months, start_date):
        self._mock_guard(national_code)
        self._mock_validate_amount(amount, months)

        key = self._mock_otp_key(national_code)
        if self.cache.get(key):
            # A live OTP already exists — the bank refuses to send another.
            raise BetaError(STATUS_MESSAGES[4444], status=4444, raw={'mock': True})

        if amount // months > self._mock_credit(national_code):
            raise BetaError(STATUS_MESSAGES[4461], status=4461, raw={'mock': True})

        self.cache.set(key, {
            'otp': MOCK_OTP,
            'amount': amount,
            'months': months,
            'start_date': start_date,
        }, MOCK_OTP_TTL)
        logger.info('Beta mock OTP issued (scope %s, national code ***%s)',
                    self.scope, national_code[-2:])
        # The OTP is exposed only in mock mode so tests and demos can continue.
        return {'ok': True, 'mock_otp': MOCK_OTP}

    def _mock_consume(self, national_code, amount, months, otp, request_id, start_date):
        self._mock_guard(national_code)
        self._mock_validate_amount(amount, months)

        key = self._mock_otp_key(national_code)
        pending = self.cache.get(key)
        if not pending:
            # Expired or never requested — indistinguishable from a wrong code.
            raise BetaError(STATUS_MESSAGES[4446], status=4446, raw={'mock': True})

        # The OTP is bound to the input data: changing the amount or the
        # number of installments voids it.
        if pending.get('amount') != amount or pending.get('months') != months:
            self.cache.delete(key)
            raise BetaError(STATUS_MESSAGES[4446], status=4446, raw={'mock': True})

        if str(otp or '').strip() != pending.get('otp'):
            raise BetaError(STATUS_MESSAGES[4446], status=4446, raw={'mock': True})

        self.cache.delete(key)
        sale_id = f'MOCK{uuid.uuid4().hex[:12].upper()}'
        self.cache.set(self._mock_sale_key(sale_id), {
            'national_code': national_code,
            'amount': amount,
            'months': months,
            'start_date': pending.get('start_date') or start_date,
            'request_id': str(request_id or ''),
        }, 60 * 60 * 24 * 30)
        logger.info('Beta mock sale %s created (scope %s)', sale_id, self.scope)
        return {'sale_id': sale_id}

    def _mock_installments(self, sale_id):
        sale = self.cache.get(self._mock_sale_key(sale_id))
        if not sale:
            return []
        return build_installment_schedule(sale['amount'], sale['months'], sale.get('start_date'))

    def _mock_list_orders(self, year_month):
        # The mock keeps sales in the cache only, so there is no month index to scan.
        return []


def build_installment_schedule(amount, months, start_date=None):
    """Even monthly schedule for callers that only have the sale total and do
    not want a second bank round-trip. Remainder lands on the first
    installment; the first due date is one Jalali month after ``start_date``.
    Same row shape as :meth:`BetaClient.get_installments` (unit-neutral)."""
    amount = int(amount or 0)
    months = max(int(months or 0), 1)
    raw = _digits(start_date)
    if len(raw) == 8:
        year, month, day = int(raw[:4]), int(raw[4:6]), int(raw[6:8])
    else:
        year, month, day = today_jalali()
    base = amount // months
    remainder = amount - base * months
    rows = []
    for index in range(1, months + 1):
        y, m, d = _jalali_add_months(year, month, day, index)
        due = format_jalali(y, m, d)
        rows.append({
            'row_number': index,
            'id': None,
            'due_date': due,
            'due_date_g': _jalali_to_date(due),
            'amount': base + (remainder if index == 1 else 0),
            'status': 'pending',
            'settled': False,
            'deleted': False,
            'raw': {'mock': True},
        })
    return rows
