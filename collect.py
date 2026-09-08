"""
매일 장 마감 후 실행하는 시황 데이터 수집 스크립트.

수집 항목:
  1. 코스피 / 코스닥 (한국투자증권 Open API, 실전투자 계좌 기준)
  2. 다우 / 나스닥 / S&P500 (yfinance)
  3. 원/달러 환율 (Frankfurter API - 무료 공식 환율 API, 인증 불필요)

결과는 data/YYYY-MM-DD.json 으로 저장한다.
이 JSON을 다음 단계(generate.py)에서 Claude API에 넘겨 시황 요약 텍스트를 만든다.

한투 API 관련 주의사항:
  - 앱키/앱시크릿은 코드에 직접 쓰지 말고 .env 파일에 넣어서 사용한다 (아래 .env.example 참고).
  - 접근토큰은 발급 유효기간이 있고(약 24시간), 너무 자주 재발급하면 제한에 걸릴 수 있으므로
    token_cache.json 에 캐싱해서 재사용한다.
  - 여기서는 "시세 조회"만 하며 주문/매매 관련 기능은 전혀 사용하지 않는다.
  - 환율은 한투 API에 신뢰할 만한 전용 조회 tr_id를 문서에서 확실히 확인하지 못해
    Frankfurter(무료 공식 환율 API)로 대체했다. 추후 한투 포털에서 정확한 환율 조회
    엔드포인트를 확인하면 fetch_exchange_rate() 만 교체하면 된다.
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv 없으면 환경변수를 시스템에서 직접 읽는다

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
TOKEN_CACHE_PATH = Path(__file__).parent / "token_cache.json"

# 실전투자 서버. 모의투자 계좌라면 아래 URL을
# https://openapivts.koreainvestment.com:29443 로 바꿔야 한다.
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"

KIS_APP_KEY = os.environ.get("KIS_APP_KEY")
KIS_APP_SECRET = os.environ.get("KIS_APP_SECRET")


def get_kis_token() -> str:
    """한투 접근토큰을 발급받거나, 캐시된 유효한 토큰을 재사용한다."""
    if TOKEN_CACHE_PATH.exists():
        cached = json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
        # 만료 10분 전까지는 캐시를 그대로 쓴다.
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
    expires_in = payload.get("expires_in", 86400)  # 초 단위, 보통 24시간
    TOKEN_CACHE_PATH.write_text(
        json.dumps({"access_token": token, "expire_at": time.time() + expires_in}),
        encoding="utf-8",
    )
    return token


def fetch_kis_index(index_code: str, index_name: str) -> dict:
    """
    한투 API로 국내 업종(지수) 현재가를 조회한다.
    index_code: 코스피 '0001', 코스닥 '1001'
    """
    token = get_kis_token()
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": "FHPUP02100000",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "U",
        "FID_INPUT_ISCD": index_code,
    }
    res = requests.get(
        f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-index-price",
        headers=headers,
        params=params,
        timeout=5,
    )
    res.raise_for_status()
    output = res.json()["output"]

    sign_map = {"1": "up", "2": "up", "3": "flat", "4": "down", "5": "down"}
    return {
        "name": index_name,
        "close": float(output["bstp_nmix_prpr"]),
        "change": float(output["bstp_nmix_prdy_vrss"]),
        "change_rate": float(output["bstp_nmix_prdy_ctrt"]),
        "direction": sign_map.get(output.get("prdy_vrss_sign"), "flat"),
        "advancing_stocks": int(output.get("ascn_issu_cnt", 0)),
        "declining_stocks": int(output.get("down_issu_cnt", 0)),
    }


def fetch_exchange_rate() -> dict:
    """원/달러 환율을 Frankfurter(무료 공식 환율 API)로 가져온다."""
    res = requests.get(
        "https://api.frankfurter.app/latest",
        params={"from": "USD", "to": "KRW"},
        timeout=5,
    )
    res.raise_for_status()
    payload = res.json()
    rate = payload["rates"]["KRW"]
    return {
        "name": "USD/KRW",
        "close": round(rate, 2),
        "date": payload.get("date"),
        # Frankfurter는 전일대비 변화율을 안 주므로 change/change_rate는 비워둔다.
        # 필요하면 어제 날짜로 한 번 더 호출해서 직접 계산할 수 있다.
        "change": None,
        "change_rate": None,
    }


def fetch_us_market() -> dict:
    """다우/나스닥/S&P500 종가를 yfinance로 가져온다."""
    import yfinance as yf  # 지연 임포트: 설치 안 됐을 때 나머지 기능은 살아있게

    tickers = {
        "dow": "^DJI",
        "nasdaq": "^IXIC",
        "sp500": "^GSPC",
    }
    result = {}
    for key, symbol in tickers.items():
        hist = yf.Ticker(symbol).history(period="2d")
        if len(hist) < 2:
            result[key] = None
            continue
        prev_close = hist["Close"].iloc[-2]
        last_close = hist["Close"].iloc[-1]
        change = last_close - prev_close
        change_rate = (change / prev_close) * 100
        result[key] = {
            "close": round(float(last_close), 2),
            "change": round(float(change), 2),
            "change_rate": round(float(change_rate), 2),
        }
    return result


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    output = {"date": today}

    # 1. 국내 지수 (한국투자증권 Open API)
    try:
        output["kospi"] = fetch_kis_index("0001", "코스피")
        output["kosdaq"] = fetch_kis_index("1001", "코스닥")
    except Exception as e:
        print(f"[경고] 국내 지수 수집 실패: {e}", file=sys.stderr)
        output["kospi"] = None
        output["kosdaq"] = None

    # 2. 환율
    try:
        output["exchange_rate"] = fetch_exchange_rate()
    except Exception as e:
        print(f"[경고] 환율 수집 실패: {e}", file=sys.stderr)
        output["exchange_rate"] = None

    # 3. 미국 증시
    try:
        output["us_market"] = fetch_us_market()
    except Exception as e:
        print(f"[경고] 미국 증시 수집 실패: {e}", file=sys.stderr)
        output["us_market"] = None

    # 저장
    out_path = DATA_DIR / f"{today}.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장 완료: {out_path}")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
