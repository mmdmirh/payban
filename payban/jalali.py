"""Minimal Jalali (Persian) calendar helpers — no third-party dependency.

Bank Refah's Beta (بتا) API speaks Jalali dates, so the client needs
Gregorian <-> Jalali conversion, month arithmetic and nothing else.
"""

from datetime import date

_G_DAYS_BEFORE_MONTH = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)


def gregorian_to_jalali(gy, gm, gd):
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100
            + (gy2 + 399) // 400 + gd + _G_DAYS_BEFORE_MONTH[gm - 1])
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def jalali_to_gregorian(jy, jm, jd):
    jy += 1595
    days = -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33) + 3) // 4 + jd
    days += (jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    leap = (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0
    month_lengths = (0, 31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    gm = 0
    while gm < 13 and gd > month_lengths[gm]:
        gd -= month_lengths[gm]
        gm += 1
    return gy, gm, gd


def today_jalali():
    t = date.today()
    return gregorian_to_jalali(t.year, t.month, t.day)


def format_jalali(y, m, d):
    return f'{y:04d}/{m:02d}/{d:02d}'


def month_length(year, month):
    """Days in a Jalali month. Esfand is clamped to 29 — always safe for a due date."""
    if month <= 6:
        return 31
    if month <= 11:
        return 30
    return 29


def add_months(year, month, day, count):
    total = year * 12 + (month - 1) + count
    y, m = divmod(total, 12)
    m += 1
    return y, m, min(day, month_length(y, m))


def jalali_str_to_date(value):
    """'1404/05/12' (or '14040512') -> datetime.date, or None when unparseable."""
    raw = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(raw) != 8:
        return None
    try:
        return date(*jalali_to_gregorian(int(raw[:4]), int(raw[4:6]), int(raw[6:8])))
    except Exception:
        return None
