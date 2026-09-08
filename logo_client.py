"""
네이버 금융이 제공하는 종목 로고 이미지를 내려받아 캐싱한다.

URL 패턴 (m.stock.naver.com API 응답의 itemLogoPngUrl 필드에서 확인):
  https://ssl.pstatic.net/imgstock/fn/real/logo/png/stock/Stock{종목코드}.png

공식 문서가 있는 API는 아니라서, 실패(404 등)는 조용히 넘어가고
카드에는 로고 없이 텍스트만 표시되도록 한다.
"""

from pathlib import Path

import requests

LOGO_URL_TEMPLATE = "https://ssl.pstatic.net/imgstock/fn/real/logo/png/stock/Stock{code}.png"

LOGO_DIR = Path(__file__).parent / "data" / "logos"
LOGO_DIR.mkdir(parents=True, exist_ok=True)


def get_logo_path(stock_code: str):
    """종목코드의 로고 PNG를 캐시에서 찾거나 새로 받는다.
    구할 수 없으면 None을 반환한다 (실패도 캐싱해서 매번 재시도하지 않는다)."""
    if not stock_code:
        return None

    cache_path = LOGO_DIR / f"{stock_code}.png"
    if cache_path.exists():
        return cache_path

    miss_marker = LOGO_DIR / f"{stock_code}.miss"
    if miss_marker.exists():
        return None

    try:
        res = requests.get(LOGO_URL_TEMPLATE.format(code=stock_code), timeout=5)
        if res.status_code != 200 or not res.content:
            miss_marker.touch()
            return None
        cache_path.write_bytes(res.content)
        return cache_path
    except requests.RequestException:
        miss_marker.touch()
        return None
