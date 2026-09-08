"""
기업을 검색하면 DART 최근 N개년 재무제표 + KIS 실시간 시세를 모아
투자 참고용으로 한눈에 보기 쉬운 리포트를 만드는 스크립트.

사용법:
  python analyze.py 삼성전자
  python analyze.py 005930 --years 3

동작:
  1. DART 고유번호 목록에서 회사명/종목코드로 기업을 찾는다.
  2. DART 기업개황 + 최근 N개년 주요계정(매출액/영업이익/당기순이익/자산총계/부채총계/자본총계)을 가져온다.
  3. KIS로 현재가/시가총액/PER/PBR/EPS/BPS를 가져온다 (실패해도 나머지는 계속 진행).
  4. 터미널에 보기 좋은 리포트를 출력하고, data/analysis/ 에 JSON으로 저장한다.
     이 JSON이 다음 단계(카드뉴스 생성)의 입력이 될 예정이다.
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import dart_client
import kis_client
import naver_client

DATA_DIR = Path(__file__).parent / "data" / "analysis"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# DART 주요계정 API는 회사/업종에 따라 계정명이 조금씩 다르게 나온다
# (예: "당기순이익(손실)", 금융업은 "영업수익"). 별칭을 순서대로 시도한다.
ACCOUNT_ALIASES = {
    "매출액": ["매출액", "영업수익"],
    "영업이익": ["영업이익", "영업이익(손실)"],
    "당기순이익": ["당기순이익(손실)", "당기순이익"],
    "자산총계": ["자산총계"],
    "부채총계": ["부채총계"],
    "자본총계": ["자본총계"],
}
MARKET_NAMES = {"Y": "코스피", "K": "코스닥", "N": "코넥스", "E": "기타"}


def resolve_company(query: str) -> dict:
    result = dart_client.find_company(query)
    if isinstance(result, dict):
        return result
    if not result:
        print(f"'{query}'에 해당하는 상장사를 찾지 못했습니다.", file=sys.stderr)
        sys.exit(1)
    print(f"'{query}'에 해당하는 회사가 여러 개 있습니다. 아래 중 하나로 다시 검색해주세요:", file=sys.stderr)
    for c in result[:15]:
        print(f"  - {c['corp_name']} (종목코드 {c['stock_code']})", file=sys.stderr)
    sys.exit(1)


def _amount(row: dict):
    raw = (row.get("thstrm_amount") or "").replace(",", "").strip()
    if not raw or raw == "-":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def extract_key_accounts(rows: list) -> dict:
    result = {}
    for canonical, aliases in ACCOUNT_ALIASES.items():
        candidates = [r for r in rows if r.get("account_nm") in aliases]
        if not candidates:
            result[canonical] = None
            continue
        preferred = next((r for r in candidates if r.get("fs_div") == "CFS"), candidates[0])
        result[canonical] = _amount(preferred)
    return result


def collect_financials(corp_code: str, years: int) -> list:
    """최근 회계연도부터 거슬러 올라가며 사업보고서 주요계정을 모은다.
    아직 공시되지 않은 연도는 자연히 건너뛴다."""
    this_year = datetime.now().year
    yearly = []
    for year in range(this_year - 1, this_year - 1 - years - 2, -1):
        if len(yearly) >= years:
            break
        rows = dart_client.get_key_accounts(corp_code, str(year))
        if not rows:
            continue
        accounts = extract_key_accounts(rows)
        if accounts.get("매출액") is None and accounts.get("당기순이익") is None:
            continue
        accounts["연도"] = year
        yearly.append(accounts)
    yearly.sort(key=lambda x: x["연도"])
    return yearly


def collect_dividend(corp_code: str, years: int = 3):
    """최근 회계연도부터 거슬러 올라가며 배당 정보를 찾는다 (최초로 찾은 연도를 사용)."""
    this_year = datetime.now().year
    for year in range(this_year - 1, this_year - 1 - years, -1):
        info = dart_client.get_dividend_info(corp_code, str(year))
        if info:
            info["연도"] = year
            return info
    return None


_SELF_REF_RE = re.compile(r"^(당사|동사|회사)(는|가|은|이)")
_FOOTNOTE_RE = re.compile(r"^[☞※]|참고하시기 바랍니다")
# DART 원문은 "가. 회사의 개요"같은 소제목과 본문이 같은 <P> 안에서 띄어쓰기 없이
# 붙어버리는 경우가 있다 (예: "가. 회사 사업 개요당사는 ..."). 카드 첫머리가 지저분해지므로
# 본문이 시작되는 자기지칭 표현(당사는/회사는 등) 앞의 소제목 조각을 잘라낸다.
_HEADING_PREFIX_RE = re.compile(r"^[가-힣0-9]{1,4}[.)]\s*.{0,14}?개요\s*(?=(당사|동사|회사)(는|가|은|이))")


def _company_particle(name: str, topic: bool) -> str:
    """회사명 마지막 글자의 받침 유무로 은/는(topic) 또는 이/가(주어) 조사를 고른다."""
    if not name:
        return "는" if topic else "가"
    last = name[-1]
    has_batchim = "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 != 0
    if topic:
        return "은" if has_batchim else "는"
    return "이" if has_batchim else "가"


def _humanize_overview(text: str, corp_name: str, max_sentences: int = 2, max_chars: int = 170) -> str:
    """공시 특유의 '당사는/당사가' 문체를 SNS 카드용 문장으로 다듬는다.
    문장 시작을 회사명으로 바꾸고, 안내성 각주 문장은 빼고, 앞의 1~2문장만 남겨 요약한다."""
    if not text:
        return text

    text = _HEADING_PREFIX_RE.sub("", text, count=1)

    match = _SELF_REF_RE.match(text)
    if match:
        is_topic = match.group(2) in ("는", "은")
        text = _SELF_REF_RE.sub(corp_name + _company_particle(corp_name, is_topic), text, count=1)

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    sentences = [s for s in sentences if not _FOOTNOTE_RE.search(s)]
    if not sentences:
        return text.strip()

    picked = [sentences[0]]
    total = len(sentences[0])
    for s in sentences[1:]:
        if len(picked) >= max_sentences or total + len(s) > max_chars:
            break
        picked.append(s)
        total += len(s)

    return " ".join(picked)


def collect_business_overview(disclosures: list, corp_name: str):
    """공시 목록에서 사업보고서를 최우선으로 찾아 '사업의 개요' 텍스트를 가져온다.
    사업보고서가 없으면 반기/분기보고서라도 시도한다."""
    candidates = sorted(
        disclosures,
        key=lambda d: 0 if "사업보고서" in (d.get("report_nm") or "") else 1,
    )
    for d in candidates:
        rcept_no = d.get("rcept_no")
        if not rcept_no:
            continue
        overview = dart_client.get_business_overview(rcept_no)
        if overview:
            # 문장 수/글자수는 넉넉하게 남겨두고, 실제로 카드에 몇 줄이 들어갈지는
            # cardnews.py가 렌더링 시점에 픽셀 폭 기준으로 문장 단위로 잘라 결정한다.
            return _humanize_overview(overview, corp_name, max_sentences=4, max_chars=320)
    return None


def collect_major_shareholder(corp_code: str, years: int = 3):
    """최근 회계연도부터 거슬러 올라가며 최대주주 현황을 찾는다 (최초로 찾은 연도를 사용)."""
    this_year = datetime.now().year
    for year in range(this_year - 1, this_year - 1 - years, -1):
        info = dart_client.get_major_shareholder(corp_code, str(year))
        if info:
            info["연도"] = year
            return info
    return None


def collect_treasury_stock(corp_code: str, years: int = 3):
    """최근 회계연도부터 거슬러 올라가며 자기주식 보유 현황을 찾는다."""
    this_year = datetime.now().year
    for year in range(this_year - 1, this_year - 1 - years, -1):
        info = dart_client.get_treasury_stock(corp_code, str(year))
        if info:
            info["연도"] = year
            return info
    return None


def collect_audit_opinion(corp_code: str, years: int = 3):
    """최근 회계연도부터 거슬러 올라가며 감사의견을 찾는다."""
    this_year = datetime.now().year
    for year in range(this_year - 1, this_year - 1 - years, -1):
        info = dart_client.get_audit_opinion(corp_code, str(year))
        if info:
            info["연도"] = year
            return info
    return None


def format_krw(amount) -> str:
    if amount is None:
        return "N/A"
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    if amount >= 1_0000_0000_0000:  # 1조
        return f"{sign}{amount / 1_0000_0000_0000:,.1f}조원"
    if amount >= 1_0000_0000:  # 1억
        return f"{sign}{amount / 1_0000_0000:,.0f}억원"
    return f"{sign}{amount:,.0f}원"


def pct(value) -> str:
    return "N/A" if value is None else f"{value:+.1f}%"


def build_report(company: dict, info: dict, yearly: list, quote, disclosures: list,
                  dividend=None, major_shareholder=None, treasury_stock=None,
                  business_overview=None, audit_opinion=None, industry_comparison=None) -> dict:
    for row in yearly:
        rev, op, net = row.get("매출액"), row.get("영업이익"), row.get("당기순이익")
        assets, liab, equity = row.get("자산총계"), row.get("부채총계"), row.get("자본총계")
        row["영업이익률"] = round(op / rev * 100, 1) if rev and op is not None else None
        row["순이익률"] = round(net / rev * 100, 1) if rev and net is not None else None
        row["부채비율"] = round(liab / equity * 100, 1) if equity else None
        row["ROE"] = round(net / equity * 100, 1) if equity and net is not None else None
        row["ROA"] = round(net / assets * 100, 1) if assets and net is not None else None

    growth = {}
    if len(yearly) >= 2:
        prev, last = yearly[-2], yearly[-1]
        for key, label in [("매출액", "revenue"), ("영업이익", "operating_income"), ("당기순이익", "net_income")]:
            if prev.get(key) and last.get(key) is not None:
                growth[label] = round((last[key] - prev[key]) / abs(prev[key]) * 100, 1)
            else:
                growth[label] = None

    if treasury_stock and treasury_stock.get("treasury_shares") is not None:
        listed_shares = (quote or {}).get("listed_shares")
        if listed_shares:
            treasury_stock["treasury_ratio"] = round(treasury_stock["treasury_shares"] / listed_shares * 100, 1)
        else:
            treasury_stock["treasury_ratio"] = None

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "company": {
            "corp_name": company["corp_name"],
            "corp_code": company["corp_code"],
            "stock_code": company["stock_code"],
            "market": MARKET_NAMES.get(info.get("corp_cls"), info.get("corp_cls")),
            "ceo": info.get("ceo_nm"),
            "est_dt": info.get("est_dt"),
            "acc_mt": info.get("acc_mt"),
            "industry": (quote or {}).get("industry"),
            "website": (info.get("hm_url") or "").strip() or None,
            "ir_url": (info.get("ir_url") or "").strip() or None,
            "business_overview": business_overview,
        },
        "yearly_financials": yearly,
        "latest_growth_yoy": growth,
        "quote": quote,
        "dividend": dividend,
        "major_shareholder": major_shareholder,
        "treasury_stock": treasury_stock,
        "audit_opinion": audit_opinion,
        "industry_comparison": industry_comparison,
        "recent_disclosures": [
            {
                "title": d.get("report_nm"),
                "date": d.get("rcept_dt"),
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={d.get('rcept_no')}",
            }
            for d in disclosures
        ],
    }


def print_report(report: dict):
    c = report["company"]
    q = report["quote"]

    print("=" * 64)
    print(f" {c['corp_name']} ({c['stock_code']})  |  {c['market'] or '비상장'}  |  대표 {c['ceo'] or '-'}")
    print("=" * 64)

    print(f"\n[기업 개요] 업종 {c.get('industry') or 'N/A'}")
    if c.get("business_overview"):
        print(f"  {c['business_overview'][:200]}{'...' if len(c['business_overview']) > 200 else ''}")

    if q:
        arrow = {"up": "▲", "down": "▼", "flat": "-"}.get(q["direction"], "")
        change = q.get("change") or 0
        change_rate = q.get("change_rate") or 0
        print(f"\n[현재 시세] {q['price']:,.0f}원  {arrow}{abs(change):,.0f} ({change_rate:+.2f}%)")
        per = f"{q['per']:.1f}" if q.get("per") is not None else "N/A"
        pbr = f"{q['pbr']:.2f}" if q.get("pbr") is not None else "N/A"
        eps = f"{q['eps']:,.0f}원" if q.get("eps") is not None else "N/A"
        bps = f"{q['bps']:,.0f}원" if q.get("bps") is not None else "N/A"
        print(f"시가총액 {format_krw(q.get('market_cap'))}  |  PER {per}  |  PBR {pbr}  |  EPS {eps}  |  BPS {bps}")
        frgn = q.get("foreign_ownership_ratio")
        print(f"외국인 지분율 {f'{frgn:.2f}%' if frgn is not None else 'N/A'}")
    else:
        print("\n[현재 시세] 조회 실패 (KIS API 키/토큰을 확인하세요)")

    ind = report.get("industry_comparison")
    if ind and ind.get("industry_per") is not None:
        print(f"[업종 비교] {ind.get('industry_name') or '동일업종'} 평균 PER {ind['industry_per']:.2f}배")

    yearly = report["yearly_financials"]
    if yearly:
        print(f"\n[최근 {len(yearly)}개년 실적 추이 (연결 우선)]")
        header = f"{'연도':<6}{'매출액':>14}{'영업이익':>14}{'순이익':>14}{'영업이익률':>10}{'순이익률':>10}{'부채비율':>10}"
        print(header)
        for row in yearly:
            om = f"{row['영업이익률']}%" if row.get("영업이익률") is not None else "N/A"
            nm = f"{row['순이익률']}%" if row.get("순이익률") is not None else "N/A"
            dr = f"{row['부채비율']}%" if row.get("부채비율") is not None else "N/A"
            print(
                f"{row['연도']:<6}"
                f"{format_krw(row.get('매출액')):>14}"
                f"{format_krw(row.get('영업이익')):>14}"
                f"{format_krw(row.get('당기순이익')):>14}"
                f"{om:>10}{nm:>10}{dr:>10}"
            )
        g = report["latest_growth_yoy"]
        print(
            f"\n최근 전년대비 성장률: 매출 {pct(g.get('revenue'))}  "
            f"영업이익 {pct(g.get('operating_income'))}  순이익 {pct(g.get('net_income'))}"
        )
    else:
        print("\n[최근 실적 추이] 조회된 재무제표가 없습니다.")

    div = report.get("dividend")
    if div:
        dps = f"{div['dividend_per_share']:,.0f}원" if div.get("dividend_per_share") is not None else "N/A"
        dy = f"{div['dividend_yield']:.2f}%" if div.get("dividend_yield") is not None else "N/A"
        payout = f"{div['payout_ratio']:.1f}%" if div.get("payout_ratio") is not None else "N/A"
        print(f"\n[배당 정보 ({div['연도']}년 기준)] 주당배당금 {dps}  |  배당수익률 {dy}  |  배당성향 {payout}")
    else:
        print("\n[배당 정보] 최근 배당 내역이 없습니다.")

    ms = report.get("major_shareholder")
    if ms:
        print(f"\n[최대주주 현황 ({ms['연도']}년 기준)] {ms['name']} 외 {ms['holder_count'] - 1}인  |  합산 지분율 {ms['total_ratio']}%")
    else:
        print("\n[최대주주 현황] 조회된 정보가 없습니다.")

    ts = report.get("treasury_stock")
    if ts:
        ratio = f"{ts['treasury_ratio']}%" if ts.get("treasury_ratio") is not None else "N/A"
        print(f"[자기주식 보유 ({ts['연도']}년 기준)] {ts['treasury_shares']:,.0f}주  |  발행주식 대비 {ratio}")
    else:
        print("[자기주식 보유] 조회된 정보가 없습니다.")

    audit = report.get("audit_opinion")
    if audit:
        print(f"[감사의견 ({audit['연도']}년 기준)] {audit.get('opinion') or 'N/A'}  |  감사인 {audit.get('auditor') or 'N/A'}")
    else:
        print("[감사의견] 조회된 정보가 없습니다.")

    disclosures = report["recent_disclosures"]
    if disclosures:
        print("\n[최근 정기공시]")
        for d in disclosures:
            print(f"  - {d['date']} {d['title']}")
            print(f"    {d['url']}")
    print()


def main():
    parser = argparse.ArgumentParser(description="기업 검색 → DART/KIS 기반 투자 참고 리포트 생성")
    parser.add_argument("company", help="회사명 또는 6자리 종목코드 (예: 삼성전자, 005930)")
    parser.add_argument("--years", type=int, default=5, help="조회할 최근 연도 수 (기본 5)")
    args = parser.parse_args()

    try:
        company = resolve_company(args.company)
    except RuntimeError as e:
        print(f"[오류] {e}", file=sys.stderr)
        sys.exit(1)
    print(f"'{company['corp_name']}' 조회 중...", file=sys.stderr)

    info = {}
    try:
        info = dart_client.get_company_info(company["corp_code"])
    except Exception as e:
        print(f"[경고] 기업개황 조회 실패: {e}", file=sys.stderr)

    yearly = collect_financials(company["corp_code"], args.years)

    quote = None
    try:
        quote = kis_client.fetch_stock_quote(company["stock_code"])
    except Exception as e:
        print(f"[경고] KIS 시세 조회 실패: {e}", file=sys.stderr)

    disclosures = []
    try:
        disclosures = dart_client.get_recent_disclosures(company["corp_code"])
    except Exception as e:
        print(f"[경고] 공시 목록 조회 실패: {e}", file=sys.stderr)

    dividend = None
    try:
        dividend = collect_dividend(company["corp_code"])
    except Exception as e:
        print(f"[경고] 배당 정보 조회 실패: {e}", file=sys.stderr)

    business_overview = None
    try:
        business_overview = collect_business_overview(disclosures, company["corp_name"])
    except Exception as e:
        print(f"[경고] 사업 개요 조회 실패: {e}", file=sys.stderr)

    major_shareholder = None
    try:
        major_shareholder = collect_major_shareholder(company["corp_code"])
    except Exception as e:
        print(f"[경고] 최대주주 현황 조회 실패: {e}", file=sys.stderr)

    treasury_stock = None
    try:
        treasury_stock = collect_treasury_stock(company["corp_code"])
    except Exception as e:
        print(f"[경고] 자기주식 현황 조회 실패: {e}", file=sys.stderr)

    audit_opinion = None
    try:
        audit_opinion = collect_audit_opinion(company["corp_code"])
    except Exception as e:
        print(f"[경고] 감사의견 조회 실패: {e}", file=sys.stderr)

    industry_comparison = None
    try:
        industry_comparison = naver_client.get_industry_per(company["stock_code"])
    except Exception as e:
        print(f"[경고] 업종 평균 PER 조회 실패: {e}", file=sys.stderr)

    report = build_report(company, info, yearly, quote, disclosures, dividend,
                           major_shareholder, treasury_stock, business_overview,
                           audit_opinion, industry_comparison)
    print_report(report)

    out_path = DATA_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_{company['corp_name']}_{company['stock_code']}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장 완료: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
