# payban

کلاینت پایتون برای درگاه‌های پرداخت ایرانی — با پیام‌های خطای فارسیِ قابل‌فهم و پشتیبانی از پروکسی داخل ایران برای سرورهای خارج از کشور.

Python clients for Iranian payment gateways with **actionable Persian error messages** and built-in **Iran-proxy routing** for servers hosted abroad.

| Gateway | Protocol | Support |
|---|---|---|
| Zarinpal (v4) | REST | request / verify / connection test |
| Mellat — Behpardakht (Shaparak) | SOAP | pay request / confirm (verify+settle) / connection test |

## Why

- Both PSPs **refuse non-Iranian source IPs**. If your server runs on foreign hosting (Azure, AWS, Hetzner…), API calls must ride a reverse proxy inside Iran. `payban` makes that a one-line `ProxyConfig` — customer-facing redirect URLs always stay direct.
- Raw gateway failures are useless to a store admin. `payban` maps every failure to a Persian message that says **what to fix**: a wrong merchant code reads differently from "your IP is filtered", which reads differently from "the proxy is down".
- `test_connection()` exercises the real gateway without charging anything and reports `{ok, stage, code, message}` — perfect behind an admin "تست اتصال" button.

## Install

```bash
pip install "payban[mellat] @ git+https://github.com/mmdmirh/payban@v0.1.0"
# Zarinpal-only (no SOAP/zeep dependency):
pip install "payban @ git+https://github.com/mmdmirh/payban@v0.1.0"
```

## Zarinpal

```python
from payban import ZarinpalClient, ProxyConfig, GatewayError

zp = ZarinpalClient(
    merchant_id="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    currency="IRR",                      # amounts below are in Rials
    proxy=ProxyConfig(                   # omit if your server is inside Iran
        base_url="https://YOUR-IRAN-PROXY",
        secret="shared-secret",          # sent as X-Proxy-Secret
    ),
)

authority, startpay_url = zp.request_payment(
    amount=1_000_000, callback_url="https://shop.example/payment/verify",
    mobile="0912xxxxxxx", order_id="1234",
)
# redirect the customer to startpay_url ...
# ... on callback (?Authority=..&Status=OK):
ref_id = zp.verify(amount=1_000_000, authority=authority)   # idempotent (100/101)
```

## Mellat (Behpardakht)

```python
from payban import MellatClient, ProxyConfig

mellat = MellatClient(
    terminal_id=1234567, username="user", password="pass",
    proxy=ProxyConfig(base_url="https://YOUR-IRAN-PROXY",
                      secret="shared-secret",
                      secret_header="X-Proxy-Auth"),
)

ref_id, startpay_url = mellat.pay_request(
    amount=1_000_000,                            # Rials
    callback_url="https://api.shop.example/payment/mellat/callback/",
    description="پرداخت سفارش ۱۲۳۴",
)
# redirect the customer to startpay_url; the bank form-POSTs the result to
# your backend callback, where you MUST confirm (unsettled transactions
# auto-reverse in ~24h):
ok = mellat.confirm(sale_order_id, sale_reference_id)
```

The Mellat proxy must serve Shaparak's WSDL at `{base_url}/?wsdl` with the SOAP endpoint rewritten to itself, and forward `/pgwchannel/` — an nginx `sub_filter` does it:

```nginx
location = / {
    rewrite ^ /pgwchannel/services/pgw break;
    proxy_pass https://bpm.shaparak.ir;
    proxy_set_header Host bpm.shaparak.ir;
    proxy_ssl_name bpm.shaparak.ir;
    proxy_ssl_server_name on;
    proxy_set_header Accept-Encoding "";
    sub_filter_once off;
    sub_filter_types application/xml text/xml application/wsdl+xml application/soap+xml;
    sub_filter "https://bpm.shaparak.ir" "https://YOUR-IRAN-PROXY";
}
location /pgwchannel/ { proxy_pass https://bpm.shaparak.ir; proxy_set_header Host bpm.shaparak.ir; proxy_ssl_name bpm.shaparak.ir; proxy_ssl_server_name on; }
location /pg/       { proxy_pass https://api.zarinpal.com; proxy_set_header Host api.zarinpal.com; proxy_ssl_name api.zarinpal.com; proxy_ssl_server_name on; }
```

## Connection test

```python
result = zp.test_connection()      # or mellat.test_connection()
# {'ok': False, 'stage': 'gateway', 'code': '-10',
#  'message': 'کد پذیرنده (Merchant ID) معتبر نیست.'}
```

Mellat's `421` means the bank hasn't whitelisted your (proxy's) IP yet; Zarinpal's `-10`/`-11` mean the merchant code is wrong or inactive.

## Error handling

Every failure raises `payban.GatewayError` with a Persian, admin-presentable message. `GatewayError` subclasses `ValueError`, so existing `except ValueError` code keeps working.

The raw code→message maps are exported as `MELLAT_RESCODE_FA` and `ZARINPAL_ERROR_FA`.

## License

MIT
