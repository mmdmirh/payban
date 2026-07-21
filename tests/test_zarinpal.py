import json
from unittest import mock

import pytest
import requests

from payban import ZarinpalClient, ProxyConfig, GatewayError
from payban.errors import flag_on


PROXY = ProxyConfig(base_url='https://1.2.3.4', secret='s3cret')


def _resp(status=200, body=None, text=''):
    r = mock.Mock(spec=requests.Response)
    r.status_code = status
    r.text = text if body is None else json.dumps(body)
    if body is None:
        r.json.side_effect = ValueError('no json')
    else:
        r.json.return_value = body
    return r


class TestUrlRouting:
    def test_proxy_reroutes_api_calls_only(self):
        zp = ZarinpalClient('m', proxy=PROXY)
        assert zp.urls['request'] == 'https://1.2.3.4/pg/v4/payment/request.json'
        assert zp.urls['verify'] == 'https://1.2.3.4/pg/v4/payment/verify.json'
        # The customer's browser opens StartPay — never proxied.
        assert zp.urls['startpay'].startswith('https://www.zarinpal.com')

    def test_no_proxy_stays_direct(self):
        zp = ZarinpalClient('m')
        assert zp.urls['request'].startswith('https://api.zarinpal.com')

    def test_sandbox_ignores_proxy(self):
        zp = ZarinpalClient('m', sandbox=True, proxy=PROXY)
        assert 'sandbox.zarinpal.com' in zp.urls['request']

    def test_sandbox_string_flags(self):
        # Web forms send strings; "0"/"false" must mean OFF.
        for off in ('0', 'false', '', None):
            assert not ZarinpalClient('m', sandbox=off).sandbox
        for on in ('1', 'true', True):
            assert ZarinpalClient('m', sandbox=on).sandbox

    def test_missing_merchant_raises(self):
        with pytest.raises(GatewayError):
            ZarinpalClient('')


class TestSecretHygiene:
    @mock.patch('payban.zarinpal.requests.post')
    def test_secret_sent_to_proxy(self, post):
        post.return_value = _resp(body={'data': {'code': 100, 'authority': 'A'}, 'errors': {}})
        zp = ZarinpalClient('m', proxy=PROXY)
        zp.request_payment(1000, 'https://cb/')
        headers = post.call_args.kwargs['headers']
        assert headers['X-Proxy-Secret'] == 's3cret'
        assert post.call_args.kwargs['verify'] is False

    @mock.patch('payban.zarinpal.requests.post')
    def test_secret_never_sent_to_zarinpal_hosts(self, post):
        post.return_value = _resp(body={'data': {'code': 100, 'authority': 'A'}, 'errors': {}})
        # Sandbox keeps direct URLs even with a proxy configured.
        zp = ZarinpalClient('m', sandbox=True, proxy=PROXY)
        zp.request_payment(1000, 'https://cb/')
        headers = post.call_args.kwargs['headers']
        assert 'X-Proxy-Secret' not in headers
        assert post.call_args.kwargs['verify'] is True


class TestErrors:
    @mock.patch('payban.zarinpal.requests.post')
    def test_known_code_maps_to_persian(self, post):
        post.return_value = _resp(body={'data': {}, 'errors': {'code': -10, 'message': 'x'}})
        zp = ZarinpalClient('m')
        r = zp.test_connection()
        assert not r['ok'] and r['code'] == '-10'
        assert 'کد پذیرنده' in r['message']

    @mock.patch('payban.zarinpal.requests.post')
    def test_non_json_direct_blames_ip_filtering(self, post):
        post.return_value = _resp(status=403, body=None, text='<html>blocked</html>')
        zp = ZarinpalClient('m')
        with pytest.raises(GatewayError, match='فیلتر'):
            zp.request_payment(1000, 'https://cb/')

    @mock.patch('payban.zarinpal.requests.post')
    def test_non_json_via_proxy_blames_proxy(self, post):
        post.return_value = _resp(status=504, body=None, text='<html>504</html>')
        zp = ZarinpalClient('m', proxy=PROXY)
        with pytest.raises(GatewayError, match='پروکسی'):
            zp.request_payment(1000, 'https://cb/')

    @mock.patch('payban.zarinpal.requests.post')
    def test_non_json_sandbox_blames_sandbox(self, post):
        post.return_value = _resp(status=504, body=None, text='<html>504</html>')
        zp = ZarinpalClient('m', sandbox=True, proxy=PROXY)
        with pytest.raises(GatewayError, match='SANDBOX'):
            zp.request_payment(1000, 'https://cb/')

    @mock.patch('payban.zarinpal.requests.post')
    def test_timeout_message_names_route(self, post):
        post.side_effect = requests.Timeout()
        with pytest.raises(GatewayError, match='پروکسی ایران'):
            ZarinpalClient('m', proxy=PROXY).request_payment(1000, 'https://cb/')

    def test_gateway_error_is_value_error(self):
        # Migrating codebases keep their existing `except ValueError`.
        assert issubclass(GatewayError, ValueError)


class TestVerify:
    @mock.patch('payban.zarinpal.requests.post')
    def test_verify_idempotent_on_101(self, post):
        post.return_value = _resp(body={'data': {'code': 101, 'ref_id': 42}, 'errors': {}})
        assert ZarinpalClient('m').verify(1000, 'A') == '42'


def test_flag_on_parsing():
    assert flag_on('1') and flag_on('true') and flag_on('YES') and flag_on(True)
    assert not flag_on('0') and not flag_on('false') and not flag_on('') \
        and not flag_on(None) and not flag_on('******')
