from unittest import mock

import pytest

from payban import MellatClient, ProxyConfig, GatewayError


def _client_with_service(service):
    m = MellatClient(123, 'u', 'p')
    soap = mock.Mock()
    soap.service = service
    m._client = soap
    return m


class TestConfig:
    def test_missing_credentials_raise(self):
        with pytest.raises(GatewayError):
            MellatClient(None, 'u', 'p')
        with pytest.raises(GatewayError):
            MellatClient(123, '', 'p')

    def test_terminal_id_coerced_to_int(self):
        assert MellatClient('123', 'u', 'p').terminal_id == 123


class TestPayRequest:
    def test_success_returns_refid_and_startpay(self):
        service = mock.Mock()
        service.bpPayRequest.return_value = '0,REF123'
        ref, url = _client_with_service(service).pay_request(10000, 'https://cb/')
        assert ref == 'REF123'
        assert url == 'https://bpm.shaparak.ir/pgwchannel/startpay.mellat?RefId=REF123'

    def test_rescode_maps_to_persian(self):
        service = mock.Mock()
        service.bpPayRequest.return_value = '21'
        with pytest.raises(GatewayError, match='کد ترمینال'):
            _client_with_service(service).pay_request(10000, 'https://cb/')


class TestConfirm:
    def test_verify_then_settle(self):
        service = mock.Mock()
        service.bpVerifyRequest.return_value = '0'
        service.bpSettleRequest.return_value = '0'
        assert _client_with_service(service).confirm(1, 2) is True

    def test_callback_retry_uses_inquiry_and_45_settle(self):
        # 43 = verify already requested; 45 = already settled — both are
        # what a refreshed/retried callback produces and must count as paid.
        service = mock.Mock()
        service.bpVerifyRequest.return_value = '43'
        service.bpInquiryRequest.return_value = '0'
        service.bpSettleRequest.return_value = '45'
        assert _client_with_service(service).confirm(1, 2) is True

    def test_failed_verify_and_inquiry_is_unpaid(self):
        service = mock.Mock()
        service.bpVerifyRequest.return_value = '17'
        service.bpInquiryRequest.return_value = '17'
        assert _client_with_service(service).confirm(1, 2) is False


class TestConnectionTest:
    def test_whitelist_code_message(self):
        service = mock.Mock()
        service.bpPayRequest.return_value = '421'
        r = _client_with_service(service).test_connection()
        assert not r['ok'] and r['code'] == '421'
        assert 'whitelist' in r['message']

    def test_success(self):
        service = mock.Mock()
        service.bpPayRequest.return_value = '0,REF'
        r = _client_with_service(service).test_connection()
        assert r['ok'] and r['code'] == '0'


class TestProxyWiring:
    def test_proxy_session_carries_secret_header(self):
        proxy = ProxyConfig(base_url='https://1.2.3.4', secret='s',
                            secret_header='X-Proxy-Auth')
        m = MellatClient(1, 'u', 'p', proxy=proxy)
        with mock.patch('zeep.Client') as ZClient, mock.patch('zeep.Transport') as ZTransport:
            m._soap_client()
            session = ZTransport.call_args.kwargs['session']
            assert session.headers['X-Proxy-Auth'] == 's'
            assert session.verify is False
            assert ZClient.call_args.args[0] == 'https://1.2.3.4/?wsdl'
