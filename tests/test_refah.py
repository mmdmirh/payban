import json
from unittest import mock

import pytest
import requests

from payban import RefahClient, ProxyConfig, GatewayError


PROXY = ProxyConfig(base_url='https://1.2.3.4', secret='s3cret')


def _client(**kw):
    return RefahClient('user', 'pass', 'term-1', **kw)


def _resp(status=200, body=None, text=''):
    r = mock.Mock(spec=requests.Response)
    r.status_code = status
    r.text = text if body is None else json.dumps(body)
    if body is None:
        r.json.side_effect = ValueError('no json')
    else:
        r.json.return_value = body
    return r


class TestConfig:
    def test_missing_credentials_raise(self):
        with pytest.raises(GatewayError):
            RefahClient('', 'pass', 'term')
        with pytest.raises(GatewayError):
            RefahClient('user', 'pass', '')

    def test_proxy_reroutes_api_calls_only(self):
        c = _client(proxy=PROXY)
        assert c.urls['purchase'] == 'https://1.2.3.4/refipg/api/purchase'
        assert c.urls['confirm'] == 'https://1.2.3.4/refipg/api/confirm-transaction'
        # The customer's browser opens the redirect page — never proxied.
        assert c.urls['startpay'] == 'https://pna.shaparak.ir/refui'

    def test_no_proxy_stays_direct(self):
        assert _client().urls['purchase'].startswith('https://pna.shaparak.ir/')


class TestPurchase:
    @mock.patch('payban.refah.requests.post')
    def test_returns_token_and_startpay_url(self, post):
        post.return_value = _resp(body={'success': True, 'data': {'token': 12345}, 'errors': []})
        token, url = _client().purchase(1_000_000, 'https://cb/', order_id='1234', mobile='0912')
        assert token == '12345'
        assert url == 'https://pna.shaparak.ir/refui?token=12345'
        sent = post.call_args.kwargs['json']
        assert sent['amount'] == 1_000_000
        assert sent['orderId'] == '1234'
        assert sent['celNo'] == '0912'
        assert sent['terminalNumber'] == 'term-1'
        assert sent['callBack'] == 'https://cb/'

    @mock.patch('payban.refah.requests.post')
    def test_bank_rejection_surfaces_its_message(self, post):
        post.return_value = _resp(body={'success': False,
                                        'errors': [{'code': 7, 'message': 'terminal is not valid'}]})
        with pytest.raises(GatewayError, match='terminal is not valid'):
            _client().purchase(1000, 'https://cb/', order_id='1')

    @mock.patch('payban.refah.requests.post')
    def test_zero_token_is_rejected(self, post):
        post.return_value = _resp(body={'success': True, 'data': {'token': 0}, 'errors': []})
        with pytest.raises(GatewayError):
            _client().purchase(1000, 'https://cb/', order_id='1')

    @mock.patch('payban.refah.requests.post')
    def test_timeout_is_a_persian_gateway_error(self, post):
        post.side_effect = requests.Timeout()
        with pytest.raises(GatewayError, match='پاسخ نداد'):
            _client().purchase(1000, 'https://cb/', order_id='1')

    @mock.patch('payban.refah.requests.post')
    def test_non_json_direct_says_ip_may_be_filtered(self, post):
        post.return_value = _resp(status=403, text='<html>forbidden</html>')
        with pytest.raises(GatewayError, match='IP'):
            _client().purchase(1000, 'https://cb/', order_id='1')


class TestVerify:
    @mock.patch('payban.refah.requests.post')
    def test_confirms_and_returns_rrn(self, post):
        post.return_value = _resp(body={'success': True, 'data': {'rrn': '987654'}, 'errors': []})
        rrn = _client().verify(1000, token='12345', rrn='987654', status='0')
        assert rrn == '987654'
        sent = post.call_args.kwargs['json']
        assert sent == {'userName': 'user', 'password': 'pass', 'amount': 1000, 'token': '12345'}

    @mock.patch('payban.refah.requests.post')
    def test_failed_callback_status_never_calls_the_bank(self, post):
        with pytest.raises(GatewayError):
            _client().verify(1000, token='1', rrn='987654', status='1')
        post.assert_not_called()

    @mock.patch('payban.refah.requests.post')
    def test_zero_or_missing_rrn_never_calls_the_bank(self, post):
        for rrn in ('0', '', None, 'abc'):
            with pytest.raises(GatewayError):
                _client().verify(1000, token='1', rrn=rrn, status='0')
        post.assert_not_called()

    @mock.patch('payban.refah.requests.post')
    def test_bank_refusing_confirmation_raises(self, post):
        post.return_value = _resp(body={'success': False, 'errors': [{'message': 'duplicate'}]})
        with pytest.raises(GatewayError, match='duplicate'):
            _client().verify(1000, token='1', rrn='987654', status='0')


class TestSecretHygiene:
    @mock.patch('payban.refah.requests.post')
    def test_secret_sent_only_to_proxy(self, post):
        post.return_value = _resp(body={'success': True, 'data': {'token': 1}, 'errors': []})
        _client(proxy=PROXY).purchase(1000, 'https://cb/', order_id='1')
        assert post.call_args.kwargs['headers']['X-Proxy-Secret'] == 's3cret'
        assert post.call_args.args[0].startswith('https://1.2.3.4/')

    @mock.patch('payban.refah.requests.post')
    def test_no_proxy_header_on_direct_calls(self, post):
        post.return_value = _resp(body={'success': True, 'data': {'token': 1}, 'errors': []})
        _client(proxy=PROXY).purchase(1000, 'https://cb/', order_id='1')
        _client().purchase(1000, 'https://cb/', order_id='1')
        direct_headers = post.call_args_list[1].kwargs['headers']
        assert 'X-Proxy-Secret' not in direct_headers


class TestConnectionProbe:
    @mock.patch('payban.refah.requests.post')
    def test_ok_when_bank_issues_token(self, post):
        post.return_value = _resp(body={'success': True, 'data': {'token': 9}, 'errors': []})
        result = _client().test_connection()
        assert result['ok'] is True and result['stage'] == 'gateway'

    @mock.patch('payban.refah.requests.post')
    def test_connect_failure_reports_stage(self, post):
        post.side_effect = requests.ConnectionError()
        result = _client().test_connection()
        assert result['ok'] is False and result['stage'] == 'connect'
