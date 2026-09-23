from datetime import date, datetime
from zoneinfo import ZoneInfo
import re
import unicodedata


def today_japan():
    return datetime.now(ZoneInfo("Asia/Tokyo")).date()


def parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        return None
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"^(令|平|昭|大)(?=[元\d])", lambda m: {"令": "令和", "平": "平成", "昭": "昭和", "大": "大正"}[m[1]], text)
    eras = {"令和": 2018, "平成": 1988, "昭和": 1925, "大正": 1911,
            "R": 2018, "H": 1988, "S": 1925, "T": 1911}
    match = re.fullmatch(r"(令和|平成|昭和|大正|R|H|S|T)(元|\d{1,2})[年./-](\d{1,2})[月./-](\d{1,2})日?", text, re.I)
    try:
        if match:
            era, year, month, day = match.groups()
            return date(eras[era.upper()] + (1 if year == "元" else int(year)), int(month), int(day))
        match = re.fullmatch(r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?(?:T.*|00:00:00)?", text)
        if match:
            return date(*map(int, match.groups()))
    except ValueError:
        return None
    return None


def parse_year(value):
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if re.fullmatch(r"\d{4}(?:\.0)?年?", text):
        return int(float(text.rstrip("年")))
    parsed = parse_date(value)
    if parsed:
        return parsed.year
    match = re.fullmatch(r"(令和|平成|昭和|大正|R|H|S|T)\s*(元|\d{1,2})年?", text, re.I)
    if match:
        bases = {"令和": 2018, "平成": 1988, "昭和": 1925, "大正": 1911,
                 "R": 2018, "H": 1988, "S": 1925, "T": 1911}
        return bases[match[1].upper()] + (1 if match[2] == "元" else int(match[2]))
    return None


def within_years(value, as_of=None, years=10):
    as_of = as_of or today_japan()
    parsed = parse_date(value)
    if parsed is None or parsed > as_of:
        return None
    try:
        anniversary = parsed.replace(year=parsed.year + years)
    except ValueError:
        anniversary = date(parsed.year + years, 2, 28)
    return as_of <= anniversary
