"""
한국투자증권(KIS) Open API - 국내주식 현재가/시가총액/PER/PBR 조회.

collect.py 와 동일한 앱키/앱시크릿, 동일한 token_cache.json 을 공유해서
접근토큰을 불필요하게 재발급받지 않는다 (KIS는 토큰 발급 빈도에 제한이 있다).
여기서는 "시세 조회"만 하며 주문/매매 관련 기능은 전혀 사용하지 않는다.
"""

import json
import os
import time
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# 실전투자 서버. 모의투자 계좌라면 openapivts.koreainvestment.com:29443 로 바꿔야 한다.
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"

KIS_APP_KEY = os.environ.get("KIS_APP_KEY")
KIS_APP_SECRET = os.environ.get("KIS_APP_SECRET")
TOKEN_CACHE_PATH = Path(__file__).parent / "token_cache.json"


def get_kis_token() -> str:
    """접근토큰을 발급받거나, 캐시된 유효한 토큰을 재사용한다."""
    if TOKEN_CACHE_PATH.exists():
        cached = json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
        if cached.get("expire_at", 0) > time.time() + 600:
            return cached["access_token"]

    if not KIS_APP_KEY or not KIS_APP_SECRET:
        raise RuntimeError(
            "KIS_APP_KEY / KIS_APP_SECRET 환경변수가 없습니다. .env 파일을 확인하세요."
        )

    res = requests.post(
        f"{KIS_BASE_URL}/oauth2/tokenP",
        json={
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
        },
        timeout=5,
    )
    res.raise_for_status()
    payload = res.json()

    token = payload["access_token"]
    expires_in = payload.get("expires_in", 86400)
    TOKEN_CACHE_PATH.write_text(
        json.dumps({"access_token": token, "expire_at": time.time() + expires_in}),
        encoding="utf-8",
    )
    return token


def _to_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def fetch_stock_quote(stock_code: str) -> dict:
    """국내주식 현재가 시세 조회 (현재가/등락률/PER/PBR/EPS/BPS/시가총액)."""
    token = get_kis_token()
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST01010100",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": stock_code,
    }
    res = requests.get(
        f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
        headers=headers,
        params=params,
        timeout=5,
    )
    res.raise_for_status()
    payload = res.json()
    if payload.get("rt_cd") != "0":
        raise RuntimeError(f"KIS 시세 조회 실패: {payload.get('msg1')}")
    output = payload["output"]

    sign_map = {"1": "up", "2": "up", "3": "flat", "4": "down", "5": "down"}
    price = _to_float(output.get("stck_prpr"))
    listed_shares = _to_float(output.get("lstn_stcn"))

    return {
        "price": price,
        "change": _to_float(output.get("prdy_vrss")),
        "change_rate": _to_float(output.get("prdy_ctrt")),
        "direction": sign_map.get(output.get("prdy_vrss_sign"), "flat"),
        "market_cap": price * listed_shares if price and listed_shares else None,
        "listed_shares": listed_shares,
        "per": _to_float(output.get("per")),
        "pbr": _to_float(output.get("pbr")),
        "eps": _to_float(output.get("eps")),
        "bps": _to_float(output.get("bps")),
        "w52_high": _to_float(output.get("w52_hgpr")),
        "w52_low": _to_float(output.get("w52_lwpr")),
        "industry": (output.get("bstp_kor_isnm") or "").strip() or None,
        "foreign_ownership_ratio": _to_float(output.get("hts_frgn_ehrt")),
    }
