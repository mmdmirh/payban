from unittest import mock

import pytest
import requests

from payban import BetaClient, BetaError, GatewayError, ProxyConfig
from payban.beta import (
    CONNECTION_ERROR, MERCHANT_PUBLIC_MESSAGE, MOCK_OTP, STATUS_MESSAGES,
    build_installment_schedule, kind_for, year_month_of,
)
from payban.jalali import (
    add_months, gregorian_to_jalali, jalali_str_to_date, jalali_to_gregorian,
)

NC = '0012345678'
CREDS = dict(client_id='app', client_secret='sec', apikey='key', terminal_name='app')


def resp(body, status=200):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = body
    return r


def test_error_is_gateway_error_and_value_error():
    err = BetaError('x', status=4450)
    assert isinstance(err, GatewayError) and isinstance(err, ValueError)
    assert err.kind == 'enrolment' and str(err) == 'x'


def test_kinds():
    assert kind_for(4473) == 'merchant'
    assert kind_for(4461) == 'input'
    assert kind_for(None) == 'input'


# --- jalali -------------------------------------------------------------------

def test_jalali_roundtrip_known_dates():
    assert gregorian_to_jalali(2024, 3, 20) == (1403, 1, 1)
    assert jalali_to_gregorian(1403, 1, 1) == (2024, 3, 20)
    assert jalali_str_to_date('1403/01/01').isoformat() == '2024-03-20'
    assert jalali_str_to_date('garbage') is None


def test_add_months_clamps_to_month_length():
    assert add_months(1403, 6, 31, 1) == (1403, 7, 30)
    assert add_months(1403, 11, 30, 1) == (1403, 12, 29)
    assert add_months(1403, 12, 5, 1) == (1404, 1, 5)


def test_year_month_of():
    assert year_month_of('1404/05/12') == '140405'


# --- mock mode ----------------------------------------------------------------

def mock_client(**kw):
    return BetaClient(mock_mode=True, **kw)


def test_mock_full_flow():
    c = mock_client()
    credit = c.inquiry(NC)['credit']
    assert credit > 0
    assert c.request_otp(NC, 6_000_000, 6)['mock_otp'] == MOCK_OTP
    sale = c.consume(NC, 6_000_000, 6, MOCK_OTP)
    rows = c.get_installments(sale['sale_id'])
    assert len(rows) == 6 and sum(r['amount'] for r in rows) == 6_000_000


def test_mock_otp_bound_to_amount_and_single_use():
    c = mock_client()
    c.request_otp(NC, 6_000_000, 6)
    with pytest.raises(BetaError) as e:
        c.consume(NC, 7_000_000, 6, MOCK_OTP)
    assert e.value.status == 4446
    with pytest.raises(BetaError):          # the OTP was voided
        c.consume(NC, 6_000_000, 6, MOCK_OTP)


def test_mock_second_otp_blocked_and_over_credit():
    c = mock_client()
    c.request_otp(NC, 6_000_000, 6)
    with pytest.raises(BetaError) as e:
        c.request_otp(NC, 6_000_000, 6)
    assert e.value.status == 4444
    with pytest.raises(BetaError) as e:
        mock_client().request_otp(NC, 10 ** 12, 1)
    assert e.value.status == 4461


@pytest.mark.parametrize('suffix,status', [('0', 4450), ('1', 4451), ('2', 4452), ('3', 4453)])
def test_mock_scripted_failures(suffix, status):
    with pytest.raises(BetaError) as e:
        mock_client().inquiry('001234567' + suffix)
    assert e.value.status == status


def test_mock_scope_isolates_state():
    shared = BetaClient(mock_mode=True).cache
    a = BetaClient(mock_mode=True, cache=shared, scope='a')
    b = BetaClient(mock_mode=True, cache=shared, scope='b')
    a.request_otp(NC, 6_000_000, 6)
    b.request_otp(NC, 6_000_000, 6)   # would be 4444 if state were shared


def test_schedule_remainder_on_first_row():
    rows = build_installment_schedule(100, 3, '1404/01/10')
    assert [r['amount'] for r in rows] == [34, 33, 33]
    assert rows[0]['due_date'] == '1404/02/10'


# --- real HTTP path (requests mocked) -----------------------------------------

def test_token_is_cached_and_sent_form_encoded():
    c = BetaClient(**CREDS)
    with mock.patch('payban.beta.requests.post') as post:
        post.return_value = resp({'access_token': 'T', 'expires_in': 3600})
        assert c.token() == 'T'
        assert c.token() == 'T'
    assert post.call_count == 1
    kwargs = post.call_args.kwargs
    assert kwargs['data']['grant_type'] == 'client_credentials'
    assert kwargs['verify'] is True


def test_inquiry_sends_headers_and_returns_rials():
    c = BetaClient(**CREDS)
    c.cache.set(c._token_cache_key(), 'T', 100)
    with mock.patch('payban.beta.requests.post') as post:
        post.return_value = resp({'status': 200, 'data': {'nationalCode': NC, 'credit': 5000}})
        out = c.inquiry(NC)
    assert out['credit'] == 5000
    headers = post.call_args.kwargs['headers']
    assert headers['Authorization'] == 'bearer T'
    assert headers['apikey'] == 'key' and headers['TerminalName'] == 'app'
    assert post.call_args.args[0] == f'https://api.rb24.ir/beta/1.0/credit/{NC}/inquiry'


def test_consume_posts_rial_amount():
    c = BetaClient(**CREDS)
    c.cache.set(c._token_cache_key(), 'T', 100)
    with mock.patch('payban.beta.requests.post') as post:
        post.return_value = resp({'status': 200, 'data': 777})
        out = c.consume(NC, 6000, 3, '1', request_id='r', start_date='1404/01/10')
    assert out == {'sale_id': '777'}
    body = post.call_args.kwargs['json']
    assert body['amount'] == 6000 and body['startDateYearMonth'] == '140401'


def test_non_200_status_raises_with_persian_message():
    c = BetaClient(**CREDS)
    c.cache.set(c._token_cache_key(), 'T', 100)
    with mock.patch('payban.beta.requests.post', return_value=resp({'status': 4461, 'message': 'm'})):
        with pytest.raises(BetaError) as e:
            c.inquiry(NC)
    assert e.value.status == 4461 and e.value.message == STATUS_MESSAGES[4461]


def test_connection_error_is_wrapped():
    c = BetaClient(**CREDS)
    c.cache.set(c._token_cache_key(), 'T', 100)
    with mock.patch('payban.beta.requests.post', side_effect=requests.ConnectionError('boom')):
        with pytest.raises(BetaError) as e:
            c.inquiry(NC)
    assert e.value.message == CONNECTION_ERROR


def test_missing_config_raises_before_network():
    with mock.patch('payban.beta.requests.post') as post:
        with pytest.raises(BetaError):
            BetaClient(client_id='a', client_secret='b').inquiry(NC)
    post.assert_not_called()


def test_proxy_routing_uses_secret_header_and_skips_tls_verify():
    proxy = ProxyConfig(base_url='https://1.2.3.4', secret='s3', secret_header='X-Proxy-Auth')
    c = BetaClient(proxy=proxy, **CREDS)
    c.cache.set(c._token_cache_key(), 'T', 100)
    with mock.patch('payban.beta.requests.post') as post:
        post.return_value = resp({'status': 200, 'data': {'credit': 1}})
        c.inquiry(NC)
    assert post.call_args.args[0].startswith('https://1.2.3.4/beta/1.0/')
    assert post.call_args.kwargs['headers']['X-Proxy-Auth'] == 's3'
    assert post.call_args.kwargs['verify'] is False


def test_get_installments_normalises_rows():
    c = BetaClient(**CREDS)
    c.cache.set(c._token_cache_key(), 'T', 100)
    rows = [{'dueDate': '1404/02/10', 'amount': 500, 'status': 'Success'}]
    with mock.patch('payban.beta.requests.get', return_value=resp({'status': 200, 'data': rows})):
        out = c.get_installments(9)
    assert out[0]['amount'] == 500 and out[0]['due_date_g'] is not None


def test_test_connection_stages():
    assert BetaClient(client_id='a').test_connection()['stage'] == 'error'
    c = BetaClient(**CREDS)
    with mock.patch('payban.beta.requests.post', return_value=resp({})):
        assert c.test_connection()['stage'] == 'token'
    c.cache.set(c._token_cache_key(), 'T', 100)
    with mock.patch('payban.beta.requests.get', return_value=resp({'status': 4403})):
        r = c.test_connection()
    assert r['ok'] is False and r['stage'] == 'inquiry'
    with mock.patch('payban.beta.requests.get', return_value=resp({'status': 200, 'data': True})):
        assert c.test_connection()['ok'] is True
