from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from src.enrichment.consultation_schedule import midday_procedure

URL = "https://okawa-ganka.jp/"

print("REAL_MIDDAY_TEST")
print("url =", URL)
print("DBへの保存・変更は行いません。")

try:
    req = Request(
        URL,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/152 Safari/537.36"
        },
    )
    with urlopen(req, timeout=30) as res:
        html = res.read().decode("utf-8", errors="replace")
        print("http_status =", getattr(res, "status", 200))
        print("bytes =", len(html))

    result = midday_procedure(html)
    print("midday_result =", result)

    if result:
        print("TEST_RESULT = PASS")
        print("procedure_slot =", result.get("procedure_slot"))
        print("evidence =", result.get("evidence"))
    else:
        print("TEST_RESULT = NOT_DETECTED")

except (HTTPError, URLError, TimeoutError) as e:
    print("TEST_RESULT = FETCH_ERROR")
    print("error =", repr(e))
except Exception as e:
    print("TEST_RESULT = EXCEPTION")
    print("error_type =", type(e).__name__)
    print("error =", repr(e))
