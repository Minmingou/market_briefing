"""
DART(전자공시시스템) Open API 클라이언트.

수집 항목:
  1. 고유번호(corp_code) 검색 - 회사명/종목코드로 DART 내부 회사 코드를 찾는다.
     전체 목록(zip)을 받아서 data/corp_code_cache.json 에 캐싱하고 7일간 재사용한다.
  2. 기업개황(company.json) - 대표자, 상장시장, 결산월 등 기본 정보
  3. 단일회사 주요계정(fnlttSinglAcnt.json) - 연도별 매출액/영업이익/당기순이익/
     자산총계/부채총계/자본총계 (DART가 표준화해서 제공하는 12개 핵심 계정)
  4. 공시검색(list.json) - 최근 정기공시(사업/반기/분기보고서) 원문 링크

DART_API_KEY는 .env 파일에 넣어서 사용한다 (opendart.fss.or.kr 에서 무료 발급).
"""

import io
import json
import os
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv 없으면 환경변수를 시스템에서 직접 읽는다

DART_API_KEY = os.environ.get("DART_API_KEY")
DART_BASE_URL = "https://opendart.fss.or.kr/api"

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CORP_CODE_CACHE_PATH = DATA_DIR / "corp_code_cache.json"
CORP_CODE_CACHE_MAX_AGE = 7 * 24 * 3600  # 7일 - DART 고유번호 목록은 자주 안 바뀐다


def _require_api_key():
    if not DART_API_KEY:
        raise RuntimeError(
            "DART_API_KEY 환경변수가 없습니다. .env 파일을 확인하세요."
        )


def _download_corp_codes() -> list:
    """DART 전체 고유번호 목록(zip)을 내려받아 파싱한다."""
    _require_api_key()
    res = requests.get(
        f"{DART_BASE_URL}/corpCode.xml",
        params={"crtfc_key": DART_API_KEY},
        timeout=30,
    )
    res.raise_for_status()

    try:
        with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
            xml_bytes = zf.read("CORPCODE.xml")
    except zipfile.BadZipFile:
        # 키가 잘못됐거나 오류가 나면 zip 대신 XML 오류 메시지가 온다.
        root = ET.fromstring(res.content)
        status = root.findtext("status")
        message = root.findtext("message")
        raise RuntimeError(f"DART 고유번호 다운로드 실패 (status={status}): {message}")

    root = ET.fromstring(xml_bytes)
    corps = []
    for node in root.findall("list"):
        corps.append({
            "corp_code": (node.findtext("corp_code") or "").strip(),
            "corp_name": (node.findtext("corp_name") or "").strip(),
            "stock_code": (node.findtext("stock_code") or "").strip(),
            "modify_date": (node.findtext("modify_date") or "").strip(),
        })
    return corps


def _load_corp_codes() -> list:
    """캐시가 있고 최신이면 재사용하고, 없거나 오래됐으면 새로 받는다."""
    if CORP_CODE_CACHE_PATH.exists():
        age = time.time() - CORP_CODE_CACHE_PATH.stat().st_mtime
        if age < CORP_CODE_CACHE_MAX_AGE:
            return json.loads(CORP_CODE_CACHE_PATH.read_text(encoding="utf-8"))

    corps = _download_corp_codes()
    CORP_CODE_CACHE_PATH.write_text(
        json.dumps(corps, ensure_ascii=False), encoding="utf-8"
    )
    return corps


def find_company(query: str):
    """
    회사명 또는 종목코드(6자리)로 상장사를 찾는다.
    - 정확히 하나로 좁혀지면 dict 하나를 반환
    - 여러 후보가 있으면 list[dict]를 반환 (호출부에서 사용자가 다시 고르게 함)
    - 못 찾으면 빈 list를 반환
    """
    corps = _load_corp_codes()
    query = query.strip()

    if query.isdigit() and len(query) == 6:
        matches = [c for c in corps if c["stock_code"] == query]
        return matches[0] if len(matches) == 1 else matches

    listed = [c for c in corps if c["stock_code"]]  # 비상장사는 검색 대상에서 제외

    exact = [c for c in listed if c["corp_name"] == query]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return exact

    return [c for c in listed if query in c["corp_name"]]


def _api_get(endpoint: str, params: dict) -> dict:
    _require_api_key()
    res = requests.get(
        f"{DART_BASE_URL}/{endpoint}",
        params={**params, "crtfc_key": DART_API_KEY},
        timeout=10,
    )
    res.raise_for_status()
    return res.json()


def get_company_info(corp_code: str) -> dict:
    """기업개황 조회 (company.json)."""
    return _api_get("company.json", {"corp_code": corp_code})


def get_key_accounts(corp_code: str, bsns_year: str, reprt_code: str = "11011") -> list:
    """
    단일회사 주요계정 조회 (fnlttSinglAcnt.json).
    reprt_code: 11011=사업보고서(연간, 기본값), 11012=반기, 11013=1분기, 11014=3분기
    아직 공시되지 않은 연도 등 데이터가 없으면 빈 리스트를 반환한다.
    """
    payload = _api_get("fnlttSinglAcnt.json", {
        "corp_code": corp_code,
        "bsns_year": bsns_year,
        "reprt_code": reprt_code,
    })
    status = payload.get("status")
    if status == "013":  # 조회된 데이터가 없습니다 - 정상적인 "해당 연도 미공시" 케이스
        return []
    if status != "000":
        print(f"[경고] DART 주요계정 조회 오류 ({bsns_year}, status={status}): {payload.get('message')}")
        return []
    return payload.get("list", [])


def _dart_amount(raw):
    if not raw:
        return None
    raw = raw.replace(",", "").strip()
    if not raw or raw == "-":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def get_dividend_info(corp_code: str, bsns_year: str, reprt_code: str = "11011"):
    """배당에 관한 사항 조회 (alotMatter.json).
    사업보고서에 배당 관련 공시가 없으면(013) None을 반환한다."""
    payload = _api_get("alotMatter.json", {
        "corp_code": corp_code,
        "bsns_year": bsns_year,
        "reprt_code": reprt_code,
    })
    status = payload.get("status")
    if status == "013":
        return None
    if status != "000":
        print(f"[경고] DART 배당 정보 조회 오류 ({bsns_year}, status={status}): {payload.get('message')}")
        return None

    rows = payload.get("list", [])
    # 보통주/우선주가 나뉘어 있으면 보통주 기준으로만 본다 (없으면 전체에서 찾는다).
    common_rows = [r for r in rows if "보통주" in (r.get("stock_knd") or "")]
    search_rows = common_rows or rows

    def find_amount(*keywords):
        for row in search_rows:
            se = row.get("se", "")
            if all(k in se for k in keywords):
                amount = _dart_amount(row.get("thstrm"))
                if amount is not None:
                    return amount
        return None

    dps = find_amount("주당", "현금배당금")
    dividend_yield = find_amount("현금배당수익률")
    payout_ratio = find_amount("현금배당성향")

    if dps is None and dividend_yield is None and payout_ratio is None:
        return None

    return {
        "dividend_per_share": dps,
        "dividend_yield": dividend_yield,
        "payout_ratio": payout_ratio,
    }


def get_major_shareholder(corp_code: str, bsns_year: str, reprt_code: str = "11011"):
    """최대주주 현황 조회 (hyslrSttus.json).
    최대주주 본인 + 특수관계인 전원의 지분율을 합산해서 반환한다."""
    payload = _api_get("hyslrSttus.json", {
        "corp_code": corp_code,
        "bsns_year": bsns_year,
        "reprt_code": reprt_code,
    })
    status = payload.get("status")
    if status == "013":
        return None
    if status != "000":
        print(f"[경고] DART 최대주주 현황 조회 오류 ({bsns_year}, status={status}): {payload.get('message')}")
        return None

    rows = payload.get("list", [])
    if not rows:
        return None
    common_rows = [r for r in rows if "보통주" in (r.get("stock_knd") or "")] or rows
    main_row = next((r for r in common_rows if "본인" in (r.get("relate") or "")), common_rows[0])
    total_ratio = sum(_dart_amount(r.get("trmend_posesn_stock_qota_rt")) or 0 for r in common_rows)

    return {
        "name": main_row.get("nm"),
        "total_ratio": round(total_ratio, 1),
        "holder_count": len(common_rows),
    }


def get_treasury_stock(corp_code: str, bsns_year: str, reprt_code: str = "11011"):
    """자기주식 취득 및 처분 현황 조회 (tesstkAcqsDspsSttus.json).
    보통주 기말 보유 수량(총계 행)을 반환한다."""
    payload = _api_get("tesstkAcqsDspsSttus.json", {
        "corp_code": corp_code,
        "bsns_year": bsns_year,
        "reprt_code": reprt_code,
    })
    status = payload.get("status")
    if status == "013":
        return None
    if status != "000":
        print(f"[경고] DART 자기주식 현황 조회 오류 ({bsns_year}, status={status}): {payload.get('message')}")
        return None

    rows = payload.get("list", [])
    total_row = next(
        (r for r in rows if r.get("stock_knd") == "보통주"
         and r.get("acqs_mth1") == "총계" and r.get("acqs_mth2") == "총계" and r.get("acqs_mth3") == "총계"),
        None,
    )
    if not total_row:
        return None
    qty = _dart_amount(total_row.get("trmend_qy"))
    if qty is None:
        return None
    return {"treasury_shares": qty}


def get_audit_opinion(corp_code: str, bsns_year: str, reprt_code: str = "11011"):
    """회계감사인의 명칭 및 감사의견 조회 (accnutAdtorNmNdAdtOpinion.json).
    같은 연도라도 감사인 변경 등으로 행이 중복될 수 있어, '당기' 표시가 붙은 첫 행만 사용한다."""
    payload = _api_get("accnutAdtorNmNdAdtOpinion.json", {
        "corp_code": corp_code,
        "bsns_year": bsns_year,
        "reprt_code": reprt_code,
    })
    status = payload.get("status")
    if status == "013":
        return None
    if status != "000":
        print(f"[경고] DART 감사의견 조회 오류 ({bsns_year}, status={status}): {payload.get('message')}")
        return None

    rows = payload.get("list", [])
    current = next((r for r in rows if "당기" in (r.get("bsns_year") or "")), None)
    if not current:
        return None

    return {
        "opinion": current.get("adt_opinion"),
        "auditor": current.get("adtor"),
    }


def get_business_overview(rcept_no: str):
    """공시 원문(document.xml)에서 'II. 사업의 내용 > 1. 사업의 개요' 섹션 텍스트를 추출한다.
    DART 원문은 회사/보고서마다 서식이 조금씩 달라 파싱이 실패할 수 있는데,
    그런 경우 조용히 None을 반환하고 나머지 카드는 정상적으로 만들어지게 한다."""
    _require_api_key()
    try:
        res = requests.get(
            f"{DART_BASE_URL}/document.xml",
            params={"crtfc_key": DART_API_KEY, "rcept_no": rcept_no},
            timeout=30,
        )
        res.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
            main_name = f"{rcept_no}.xml"
            name = main_name if main_name in zf.namelist() else zf.namelist()[0]
            xml_text = zf.read(name).decode("utf-8", errors="ignore")
    except Exception:
        return None

    # 제목 표기가 "1. 사업의 개요", "1. (제조서비스업)사업의 개요"처럼 회사마다 달라서
    # "사업의 개요"라는 문구가 들어간 첫 TITLE 태그를 느슨하게 찾는다.
    match = re.search(r"<TITLE[^>]*>[^<]*사업의\s*개요[^<]*</TITLE>(.*?)(?=<TITLE)", xml_text, re.S)
    if not match:
        return None

    paragraphs = re.findall(r"<P[^>]*>(.*?)</P>", match.group(1), re.S)
    texts = []
    total_len = 0
    for raw in paragraphs:
        clean = re.sub(r"<[^>]+>", "", raw).replace("&nbsp;", " ")
        clean = re.sub(r"\s+", " ", clean).strip()
        if not clean:
            continue
        texts.append(clean)
        total_len += len(clean)
        if total_len > 400:  # 카드 한 장에 쓸 만큼 모이면 그만 모은다
            break

    return " ".join(texts) if texts else None


def get_recent_disclosures(corp_code: str, count: int = 5) -> list:
    """최근 정기공시(사업/반기/분기보고서) 목록 조회 (list.json).
    bgn_de/end_de를 안 주면 DART가 데이터 없음(013)을 반환하므로 최근 2년으로 명시한다."""
    from datetime import datetime, timedelta
    end_de = datetime.now().strftime("%Y%m%d")
    bgn_de = (datetime.now() - timedelta(days=730)).strftime("%Y%m%d")
    payload = _api_get("list.json", {
        "corp_code": corp_code,
        "pblntf_ty": "A",  # A=정기공시
        "bgn_de": bgn_de,
        "end_de": end_de,
        "page_count": count,
        "sort": "date",
        "sort_mth": "desc",
    })
    if payload.get("status") != "000":
        return []
    return payload.get("list", [])
