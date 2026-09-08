"""
네이버 금융 종목 페이지에서 "동일업종 PER"(업종 평균 PER)을 가져온다.

DART/KIS 어디에도 업종 평균 밸류에이션을 제공하는 공식 API가 없어서,
개별 종목 페이지에 노출되는 이 값을 대신 사용한다.
공식 문서가 있는 API가 아니라 페이지 구조가 바뀌면 깨질 수 있는데,
그런 경우 조용히 None을 반환하고 나머지 카드는 정상적으로 만들어지게 한다.
"""

import re

import requests

ITEM_PAGE_URL = "https://finance.naver.com/item/main.naver?code={code}"
HEADERS = {"User-Agent": "Mozilla/5.0"}

_INDUSTRY_NAME_RE = re.compile(r"업종명\s*:\s*<a[^>]*>([^<]+)</a>")
_INDUSTRY_PER_RE = re.compile(r"동일업종\s*PER\s*정보\">.*?<em>([\d,.]+)</em>\s*배", re.S)


def get_industry_per(stock_code: str):
    """{'industry_name': str, 'industry_per': float} 또는 실패 시 None."""
    if not stock_code:
        return None
    try:
        res = requests.get(ITEM_PAGE_URL.format(code=stock_code), headers=HEADERS, timeout=5)
        if res.status_code != 200:
            return None
        html = res.text
    except requests.RequestException:
        return None

    per_match = _INDUSTRY_PER_RE.search(html)
    if not per_match:
        return None
    try:
        industry_per = float(per_match.group(1).replace(",", ""))
    except ValueError:
        return None

    name_match = _INDUSTRY_NAME_RE.search(html)
    industry_name = name_match.group(1).strip() if name_match else None

    return {"industry_name": industry_name, "industry_per": industry_per}
