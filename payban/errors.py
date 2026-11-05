"""Persian error mapping for gateway responses.

The point of this module is that a store admin reading the message can tell
*what to fix* — a wrong merchant code reads differently from "your server's
IP is filtered" which reads differently from "the proxy is down".
"""


class GatewayError(ValueError):
    """Raised for any gateway-level failure.

    Subclasses ``ValueError`` so codebases that already catch ``ValueError``
    around payment calls keep working when they migrate to this library.
    The message is end-user (store admin) presentable Persian.
    """


# Behpardakht bpPayRequest result codes -> Persian.
# The credential fields map to Behpardakht's terms:
#   terminal_id -> terminalId, username -> userName, password -> userPassword.
MELLAT_RESCODE_FA = {
    '0':   'اتصال موفق بود؛ درگاه پاسخ داد و درخواست پرداخت آزمایشی را پذیرفت.',
    '21':  'پذیرنده نامعتبر است — «کد ترمینال» را بررسی کنید.',
    '24':  'اطلاعات کاربری نامعتبر است — «نام کاربری» و «رمز عبور» ترمینال را بررسی کنید.',
    '25':  'مبلغ نامعتبر است.',
    '34':  'خطای سیستمی در سمت بانک.',
    '41':  'شماره درخواست تکراری است؛ دوباره تلاش کنید.',
    '421': 'IP نامعتبر است — IP سرور باید توسط بانک تأیید (whitelist) شود.',
}

# Zarinpal v4 error codes -> Persian.
ZARINPAL_ERROR_FA = {
    '-9':  'خطای اعتبارسنجی: «کد پذیرنده» یا مبلغ نامعتبر است (کد پذیرنده باید ۳۶ کاراکتر باشد).',
    '-10': 'کد پذیرنده (Merchant ID) معتبر نیست.',
    '-11': 'کد پذیرنده یافت نشد یا درگاه فعال نیست.',
    '-12': 'تلاش بیش از حد؛ چند دقیقه بعد دوباره امتحان کنید.',
    '-15': 'درگاه پذیرنده معلق شده است؛ با پشتیبانی زرین‌پال تماس بگیرید.',
    '-16': 'سطح تأیید حساب پذیرنده کافی نیست.',
    '-30': 'پذیرنده اجازهٔ این تراکنش را ندارد.',
    '-31': 'حساب بانکی تسویه پذیرنده تأیید نشده است.',
    '-33': 'مبلغ تراکنش نامعتبر است.',
    '-34': 'مبلغ از سقف مجاز بیشتر است.',
    '-40': 'دسترسی به متد مورد نظر مجاز نیست.',
    '-50': 'مبلغ پرداخت‌شده با مبلغ ارسالی یکسان نیست.',
    '-51': 'پرداخت ناموفق بوده است.',
    '-54': 'کد رهگیری (Authority) نامعتبر است.',
}


# Bank Refah error codes -> Persian. Intentionally empty for now: the public
# documentation does not list the bank's codes, so unmapped errors fall back
# to the bank's own message. Add entries here once verified against a real
# terminal's responses.
REFAH_ERROR_FA = {}

def flag_on(value):
    """Parse a config flag that may arrive as a string from a web form.

    ``"0"`` / ``"false"`` / ``""`` / ``None`` mean OFF — a bare truthiness
    check would read ``"0"`` (or a masked ``"******"``) as ON.
    """
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')
