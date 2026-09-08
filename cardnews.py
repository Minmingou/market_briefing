"""
analyze.py가 만든 분석 리포트(JSON)를 6장짜리 카드뉴스 이미지(PNG)로 만드는 스크립트.
네트워크 호출은 하지 않고, 이미 저장된 data/analysis/*.json 을 읽어서 그림만 그린다.

사용법:
  python cardnews.py 삼성전자          # data/analysis/ 에서 가장 최근 리포트를 찾아 사용
  python cardnews.py 005930
  python cardnews.py --file data/analysis/2026-09-04_삼성전자_005930.json

결과는 data/cardnews/{날짜}_{회사명}_{종목코드}/card_1.png ~ card_6.png 로 저장된다.
"""

import argparse
import glob
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from analyze import format_krw
import logo_client

DATA_DIR = Path(__file__).parent / "data" / "analysis"
OUT_DIR = Path(__file__).parent / "data" / "cardnews"

W, H = 1080, 1350
PAD = 72
CONTENT_W = W - 2 * PAD

# --- 색상 (dataviz 스킬 참고 팔레트, 라이트 모드) -----------------------------
C_SURFACE = (252, 252, 251)
C_INK = (11, 11, 11)
C_INK_2 = (82, 81, 78)
C_MUTED = (137, 135, 129)
C_GRID = (225, 224, 217)
C_BASELINE = (195, 194, 183)
C_HAIRLINE = (11, 11, 11, 26)

C_REVENUE = (42, 120, 214)     # slot1 blue - 매출액
C_OPINCOME = (235, 104, 52)    # slot2 orange - 영업이익
C_NETINCOME = (27, 175, 122)   # slot3 aqua - 당기순이익

C_UP = (214, 58, 58)      # 국내 증시 관례: 상승 = 빨강
C_DOWN = (43, 99, 214)    # 하락 = 파랑
C_FLAT = C_MUTED

C_GOOD = (12, 163, 12)
C_WARNING = (219, 155, 9)
C_CRITICAL = (208, 59, 59)

C_ROE = (74, 58, 167)     # slot7 violet - ROE
C_ROA = (0, 131, 0)       # slot6 green - ROA

# --- 폰트 (macOS 기본 한글 폰트) ---------------------------------------------
FONT_PATH = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
FONT_INDEX = {"thin": 10, "light": 8, "regular": 0, "medium": 2, "semibold": 4, "bold": 6}
_font_cache = {}


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    key = (weight, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(FONT_PATH, size, index=FONT_INDEX[weight])
    return _font_cache[key]


# --- 공용 드로잉 헬퍼 ---------------------------------------------------------

def new_card() -> tuple:
    img = Image.new("RGB", (W, H), C_SURFACE)
    return img, ImageDraw.Draw(img)


def chrome(draw: ImageDraw.ImageDraw, company_name: str, page: int, total: int = 6):
    """모든 카드 공통 상단 라벨 + 하단 페이지 표시."""
    draw.text((PAD, 56), "투자 참고 카드뉴스", font=font("semibold", 26), fill=C_MUTED)
    draw.text((W - PAD, 56), f"{company_name}", font=font("semibold", 26), fill=C_MUTED, anchor="ra")
    draw.line([(PAD, 104), (W - PAD, 104)], fill=C_GRID, width=2)
    draw.text((W / 2, H - 50), f"{page} / {total}", font=font("medium", 22), fill=C_MUTED, anchor="mm")


_NO_BREAK_BEFORE = set(".,!?)]}·’”\"'")


def wrap_text(text: str, fnt: ImageFont.FreeTypeFont, max_width: int) -> list:
    """한글은 어절 단위보다 글자 단위로 감싸는 편이 넘침이 적다.
    다만 마침표·쉼표 같은 종결부호가 줄 끝에서 혼자 다음 줄로 넘어가면 마무리가
    지저분해 보이므로, 그런 부호는 살짝 넘치더라도 앞 글자에 붙여 마무리한다."""
    words = list(text)
    lines, current = [], ""
    for ch in words:
        trial = current + ch
        if font_width(trial, fnt) > max_width and current and ch not in _NO_BREAK_BEFORE:
            lines.append(current)
            current = ch
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def font_width(text: str, fnt: ImageFont.FreeTypeFont) -> float:
    return fnt.getlength(text)


def wrap_and_truncate(text: str, fnt: ImageFont.FreeTypeFont, max_width: int, max_lines: int) -> list:
    """max_lines를 넘으면 마지막 줄 끝에 말줄임표를 붙이고 자른다."""
    lines = wrap_text(text, fnt, max_width)
    if len(lines) <= max_lines:
        return lines
    lines = lines[:max_lines]
    last = lines[-1]
    while last and font_width(last + "…", fnt) > max_width:
        last = last[:-1]
    lines[-1] = last + "…"
    return lines


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def fit_sentences(text: str, fnt: ImageFont.FreeTypeFont, max_width: int, max_lines: int,
                   hard_max_lines: int = None) -> list:
    """문장 단위로 최대한 채우되, 어느 회사의 사업 개요를 넣어도 문장 중간에서
    말줄임표로 끊기지 않고 완결된 문장으로 시작과 끝이 보이도록 줄을 만든다.
    문장을 하나씩 줄여가며 max_lines 안에 들어가는 가장 긴 조합을 고른다.
    그래도 문장 하나조차 안 들어가면 (쉼표 없이 아주 긴 단문 등) hard_max_lines까지
    줄 수를 늘려서라도 문장을 통째로 보여주고, 그마저도 안 되면 말줄임표로 자른다."""
    hard_max_lines = max(hard_max_lines or max_lines, max_lines)
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if not sentences:
        sentences = [text]

    for budget in range(max_lines, hard_max_lines + 1):
        for i in range(len(sentences), 0, -1):
            candidate = " ".join(sentences[:i])
            lines = wrap_text(candidate, fnt, max_width)
            if len(lines) <= budget:
                return lines

    return wrap_and_truncate(sentences[0], fnt, max_width, max_lines)


def rounded_bar(draw, x0, x1, y_base, y_end, color, radius=10):
    """세로 막대. y_end가 y_base보다 위면 양수(위로 자람, 위쪽 라운드),
    아래면 음수(아래로 자람, 아래쪽 라운드)."""
    top, bottom = min(y_base, y_end), max(y_base, y_end)
    if bottom - top < 1:
        return
    up = y_end < y_base
    corners = (True, True, False, False) if up else (False, False, True, True)
    draw.rounded_rectangle([x0, top, x1, bottom], radius=radius, fill=color, corners=corners)


def stat_tile(draw, x, y, w, label, value, value_color=C_INK, value_size=40):
    draw.text((x, y), label, font=font("medium", 24), fill=C_MUTED)
    draw.text((x, y + 34), value, font=font("bold", value_size), fill=value_color)


def legend_row(draw, x, y, items):
    """items: [(color, label), ...] 가로 범례."""
    cx = x
    for color, label in items:
        r = 8
        draw.ellipse([cx, y - r, cx + 2 * r, y + r], fill=color)
        cx += 2 * r + 12
        draw.text((cx, y), label, font=font("medium", 24), fill=C_INK_2, anchor="lm")
        cx += font_width(label, font("medium", 24)) + 36


# --- 카드 1: 표지 -------------------------------------------------------------

def card_cover(report: dict) -> Image.Image:
    img, d = new_card()
    c = report["company"]
    q = report.get("quote")

    d.text((PAD, 56), "투자 참고 카드뉴스", font=font("semibold", 26), fill=C_MUTED)
    d.text((W - PAD, 56), report["generated_at"][:10], font=font("medium", 24), fill=C_MUTED, anchor="ra")
    d.line([(PAD, 104), (W - PAD, 104)], fill=C_GRID, width=2)

    market = c.get("market") or "비상장"
    d.text((PAD, 260), f"{market}  ·  {c['stock_code']}", font=font("semibold", 30), fill=C_INK_2)
    d.text((PAD, 305), c["corp_name"], font=font("bold", 86), fill=C_INK)

    y = 470
    if q:
        arrow = "▲" if q["direction"] == "up" else ("▼" if q["direction"] == "down" else "-")
        color = C_UP if q["direction"] == "up" else (C_DOWN if q["direction"] == "down" else C_FLAT)
        d.text((PAD, y), f"{q['price']:,.0f}원", font=font("bold", 64), fill=C_INK)
        change_txt = f"{arrow} {abs(q.get('change') or 0):,.0f}  ({q.get('change_rate', 0):+.2f}%)"
        d.text((PAD, y + 90), change_txt, font=font("bold", 34), fill=color)

        ty = y + 190
        d.line([(PAD, ty), (W - PAD, ty)], fill=C_GRID, width=2)
        ty += 40
        tile_w = CONTENT_W / 3
        stat_tile(d, PAD, ty, tile_w, "시가총액", format_krw(q.get("market_cap")))
        stat_tile(d, PAD + tile_w, ty, tile_w, "PER", f"{q['per']:.1f}" if q.get("per") is not None else "N/A")
        stat_tile(d, PAD + 2 * tile_w, ty, tile_w, "PBR", f"{q['pbr']:.2f}" if q.get("pbr") is not None else "N/A")
    else:
        d.text((PAD, y), "실시간 시세 조회 실패", font=font("bold", 40), fill=C_MUTED)

    yearly = report.get("yearly_financials") or []
    if yearly:
        first, last = yearly[0]["연도"], yearly[-1]["연도"]
        summary = f"최근 {first}~{last}년 사업보고서 기준 재무제표를 분석했습니다"
    else:
        summary = "DART 전자공시 재무제표를 기반으로 분석했습니다"
    d.text((PAD, H - 190), summary, font=font("medium", 26), fill=C_INK_2)
    d.text((PAD, H - 150), "DART(전자공시) · 한국투자증권(KIS) 공개 데이터 기반", font=font("regular", 22), fill=C_MUTED)

    d.text((W / 2, H - 50), "1 / 6", font=font("medium", 22), fill=C_MUTED, anchor="mm")
    return img


def fmt_date(date_str) -> str:
    """DART 스타일 YYYYMMDD 문자열을 YYYY.MM.DD로 바꾼다."""
    if not date_str or len(date_str) != 8:
        return "N/A"
    return f"{date_str[:4]}.{date_str[4:6]}.{date_str[6:8]}"


def fmt_shares(n) -> str:
    if n is None:
        return "N/A"
    if n >= 1_0000_0000:
        return f"{n / 1_0000_0000:,.1f}억주"
    if n >= 1_0000:
        return f"{n / 1_0000:,.0f}만주"
    return f"{n:,.0f}주"


# --- 카드 2: 기업 소개 (업종 · 기본 정보 · 배당) -------------------------------

def card_intro(report: dict) -> Image.Image:
    img, d = new_card()
    c = report["company"]
    chrome(d, c["corp_name"], 2)

    logo_path = logo_client.get_logo_path(c.get("stock_code"))
    if logo_path:
        try:
            logo_img = Image.open(logo_path).convert("RGBA")
            max_dim = 96
            ratio = min(max_dim / logo_img.width, max_dim / logo_img.height)
            size = (max(1, int(logo_img.width * ratio)), max(1, int(logo_img.height * ratio)))
            logo_img = logo_img.resize(size, Image.LANCZOS)
            img.paste(logo_img, (W - PAD - size[0], 130), logo_img)
        except Exception:
            pass  # 로고 파일이 손상됐거나 못 읽어도 나머지 카드는 정상 출력한다

    d.text((PAD, 140), "기업 소개", font=font("bold", 40), fill=C_INK)
    d.text((PAD, 194), "이 회사는 무엇을 하는 회사인가요?", font=font("regular", 24), fill=C_MUTED)

    y = 250
    overview = c.get("business_overview")
    if overview:
        for line in fit_sentences(overview, font("regular", 25), CONTENT_W, max_lines=3, hard_max_lines=5):
            d.text((PAD, y), line, font=font("regular", 25), fill=C_INK_2)
            y += 36
    else:
        d.text((PAD, y), "사업보고서에서 사업 개요를 확인하지 못했습니다.", font=font("regular", 25), fill=C_MUTED)
        y += 36

    y += 20
    d.text((PAD, y), "업종", font=font("medium", 24), fill=C_MUTED)
    d.text((PAD, y + 34), c.get("industry") or "업종 정보 없음", font=font("bold", 44), fill=C_INK)

    y += 130
    d.line([(PAD, y), (W - PAD, y)], fill=C_GRID, width=2)
    y += 46

    tile_w2 = CONTENT_W / 2
    q = report.get("quote") or {}
    ms = report.get("major_shareholder")
    stat_tile(d, PAD, y, tile_w2, "상장주식수", fmt_shares(q.get("listed_shares")))
    stat_tile(d, PAD + tile_w2, y, tile_w2, "최대주주 지분율",
              f"{ms['total_ratio']}%" if ms else "N/A")

    y += 84
    if ms:
        d.text((PAD + tile_w2, y), f"{ms['name']} 외 {ms['holder_count'] - 1}인 (특수관계인 포함)",
                font=font("regular", 20), fill=C_MUTED)

    y += 66
    d.line([(PAD, y), (W - PAD, y)], fill=C_GRID, width=2)
    y += 46

    d.text((PAD, y), "배당 · 자기주식", font=font("semibold", 30), fill=C_INK)
    y += 56

    tile_w4 = CONTENT_W / 4
    div = report.get("dividend") or {}
    ts = report.get("treasury_stock") or {}
    dps = f"{div['dividend_per_share']:,.0f}원" if div.get("dividend_per_share") is not None else "N/A"
    dy_txt = f"{div['dividend_yield']:.2f}%" if div.get("dividend_yield") is not None else "N/A"
    payout = f"{div['payout_ratio']:.1f}%" if div.get("payout_ratio") is not None else "N/A"
    treasury = f"{ts['treasury_ratio']}%" if ts.get("treasury_ratio") is not None else "N/A"
    stat_tile(d, PAD, y, tile_w4, "주당 배당금", dps)
    stat_tile(d, PAD + tile_w4, y, tile_w4, "배당수익률", dy_txt)
    stat_tile(d, PAD + 2 * tile_w4, y, tile_w4, "배당성향", payout)
    stat_tile(d, PAD + 3 * tile_w4, y, tile_w4, "자기주식 비율", treasury)

    y += 110
    caption = (
        "배당수익률은 현재 주가 대비 연간 배당금 비율, 배당성향은 순이익 중 배당으로 지급한 비율입니다. "
        "자기주식 비율은 회사가 스스로 사들여 보유 중인 자사 주식이 전체 발행주식에서 차지하는 비중으로, "
        "높을수록 주주환원 의지가 있다고 볼 수 있습니다."
    )
    for line in wrap_text(caption, font("regular", 22), CONTENT_W - 40):
        d.text((PAD, y), line, font=font("regular", 22), fill=C_MUTED)
        y += 32

    return img


# --- 미니 바 차트 (단일 지표, 연도별) -----------------------------------------

def mini_bar_chart(d, x, y, w, h, yearly, key, color, title, growth=None):
    d.text((x, y), title, font=font("semibold", 30), fill=C_INK)
    if growth is not None:
        gcolor = C_UP if growth >= 0 else C_DOWN
        gtxt = f"전년대비 {growth:+.1f}%"
        d.text((x + w, y + 4), gtxt, font=font("semibold", 24), fill=gcolor, anchor="ra")

    chart_top = y + 68
    chart_h = h - 68 - 44  # 아래쪽에 연도 라벨 44px 확보
    values = [row.get(key) for row in yearly if row.get(key) is not None]
    if not values:
        d.text((x, chart_top + chart_h / 2), "데이터 없음", font=font("regular", 24), fill=C_MUTED)
        return
    vmax = max(values + [0])
    vmin = min(values + [0])
    span_raw = (vmax - vmin) or 1
    # 막대 끝 라벨이 제목/범례와 겹치지 않도록 위아래에 여유 공간을 둔다.
    vmax_p = vmax + span_raw * 0.18 if vmax > 0 else vmax
    vmin_p = vmin - span_raw * 0.18 if vmin < 0 else vmin
    span = (vmax_p - vmin_p) or 1
    baseline_y = chart_top + chart_h * (vmax_p / span)

    d.line([(x, baseline_y), (x + w, baseline_y)], fill=C_BASELINE, width=2)

    n = len(yearly)
    slot_w = w / n
    bar_w = min(56, slot_w * 0.5)
    for i, row in enumerate(yearly):
        cx = x + slot_w * i + slot_w / 2
        val = row.get(key)
        label_y = chart_top + chart_h + 14
        d.text((cx, label_y), str(row["연도"]), font=font("medium", 22), fill=C_MUTED, anchor="ma")
        if val is None:
            continue
        bar_end_y = chart_top + chart_h * (1 - (val - vmin_p) / span)
        rounded_bar(d, cx - bar_w / 2, cx + bar_w / 2, baseline_y, bar_end_y, color, radius=8)
        is_last = i == n - 1
        label = format_krw(val)
        label_color = C_INK if is_last else C_INK_2
        lfont = font("bold" if is_last else "medium", 21)
        if val >= 0:
            d.text((cx, bar_end_y - 10), label, font=lfont, fill=label_color, anchor="mb")
        else:
            d.text((cx, bar_end_y + 10), label, font=lfont, fill=label_color, anchor="mt")


# --- 카드 2: 매출액 · 영업이익 · 당기순이익 추이 -------------------------------

def card_performance(report: dict) -> Image.Image:
    img, d = new_card()
    c = report["company"]
    chrome(d, c["corp_name"], 3)

    yearly = report.get("yearly_financials") or []
    growth = report.get("latest_growth_yoy") or {}
    n = len(yearly)
    d.text((PAD, 140), f"최근 {n}개년 실적 추이", font=font("bold", 40), fill=C_INK)
    d.text((PAD, 194), "매출액 · 영업이익 · 당기순이익 (단위 자동 환산)", font=font("regular", 24), fill=C_MUTED)

    panel_h, gap = 290, 36
    y0 = 270
    mini_bar_chart(d, PAD, y0, CONTENT_W, panel_h, yearly, "매출액", C_REVENUE,
                    "매출액", growth.get("revenue"))
    mini_bar_chart(d, PAD, y0 + (panel_h + gap), CONTENT_W, panel_h, yearly, "영업이익", C_OPINCOME,
                    "영업이익", growth.get("operating_income"))
    mini_bar_chart(d, PAD, y0 + 2 * (panel_h + gap), CONTENT_W, panel_h, yearly, "당기순이익", C_NETINCOME,
                    "당기순이익", growth.get("net_income"))
    return img


# --- 카드 3: 수익성 지표 -------------------------------------------------------

def line_chart(d, x, y, w, h, yearly, series, title):
    """series: [(key, color, label), ...] - 퍼센트 값(같은 단위)만 비교할 때 사용."""
    d.text((x, y), title, font=font("semibold", 30), fill=C_INK)
    legend_row(d, x, y + 44, [(color, label) for _, color, label in series])

    chart_top = y + 90
    chart_h = h - 90 - 44
    all_vals = [row.get(k) for k, _, _ in series for row in yearly if row.get(k) is not None]
    if not all_vals:
        d.text((x, chart_top + chart_h / 2), "데이터 없음", font=font("regular", 24), fill=C_MUTED)
        return
    vmax, vmin = max(all_vals + [0]), min(all_vals + [0])
    span_raw = (vmax - vmin) or 1
    vmax += span_raw * 0.1
    vmin -= span_raw * 0.1
    span = (vmax - vmin) or 1
    baseline_y = chart_top + chart_h * (vmax / span)
    d.line([(x, baseline_y), (x + w, baseline_y)], fill=C_GRID, width=2)

    n = len(yearly)
    slot_w = w / n
    for key, color, _ in series:
        points = []
        for i, row in enumerate(yearly):
            val = row.get(key)
            if val is None:
                points.append(None)
                continue
            cx = x + slot_w * i + slot_w / 2
            cy = chart_top + chart_h * (1 - (val - vmin) / span)
            points.append((cx, cy))
        segs = [p for p in points if p is not None]
        for a, b in zip(segs, segs[1:]):
            d.line([a, b], fill=color, width=4, joint="curve")
        for p in points:
            if p is None:
                continue
            r = 7
            d.ellipse([p[0] - r - 2, p[1] - r - 2, p[0] + r + 2, p[1] + r + 2], fill=C_SURFACE)
            d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=color)

    # 마지막 연도 값 라벨. 값이 비슷해서 겹칠 수 있으니 최소 간격을 두고 밀어낸다.
    last_i = n - 1
    end_labels = []
    for key, color, _ in series:
        val = yearly[last_i].get(key)
        if val is None:
            continue
        cy = chart_top + chart_h * (1 - (val - vmin) / span)
        end_labels.append([cy, color, val])
    end_labels.sort(key=lambda item: item[0])
    min_gap = 30
    for i in range(1, len(end_labels)):
        if end_labels[i][0] - end_labels[i - 1][0] < min_gap:
            end_labels[i][0] = end_labels[i - 1][0] + min_gap

    cx = x + slot_w * last_i + slot_w / 2
    for cy, color, val in end_labels:
        d.text((cx + 16, cy), f"{val:.1f}%", font=font("bold", 24), fill=color, anchor="lm")

    for i, row in enumerate(yearly):
        cx = x + slot_w * i + slot_w / 2
        d.text((cx, chart_top + chart_h + 14), str(row["연도"]), font=font("medium", 22), fill=C_MUTED, anchor="ma")


def card_profitability(report: dict) -> Image.Image:
    img, d = new_card()
    c = report["company"]
    chrome(d, c["corp_name"], 4)

    yearly = report.get("yearly_financials") or []
    n = len(yearly)
    d.text((PAD, 140), f"최근 {n}개년 수익성 지표", font=font("bold", 40), fill=C_INK)
    d.text((PAD, 194), "이익률과 자본 효율성(ROE·ROA)을 함께 비교", font=font("regular", 24), fill=C_MUTED)

    panel_h = 430
    line_chart(d, PAD, 280, CONTENT_W, panel_h, yearly,
               [("영업이익률", C_OPINCOME, "영업이익률"), ("순이익률", C_NETINCOME, "순이익률")],
               "이익률 (%)")
    line_chart(d, PAD, 280 + panel_h + 60, CONTENT_W, panel_h, yearly,
               [("ROE", C_ROE, "ROE (자기자본이익률)"), ("ROA", C_ROA, "ROA (총자산이익률)")],
               "자본 효율성 (%)")
    return img


# --- 카드 4: 밸류에이션 · 재무안정성 ------------------------------------------

def range_bar(d, x, y, w, low, high, current, label):
    d.text((x, y), label, font=font("medium", 24), fill=C_MUTED)
    track_y = y + 40
    d.rounded_rectangle([x, track_y, x + w, track_y + 14], radius=7, fill=C_GRID)
    if high > low and current is not None:
        ratio = max(0.0, min(1.0, (current - low) / (high - low)))
        px = x + w * ratio
        d.ellipse([px - 12, track_y - 5, px + 12, track_y + 19], fill=C_INK)
    d.text((x, track_y + 30), f"{low:,.0f}", font=font("regular", 20), fill=C_MUTED)
    d.text((x + w, track_y + 30), f"{high:,.0f}", font=font("regular", 20), fill=C_MUTED, anchor="ra")


def debt_meter(d, x, y, w, ratio, year):
    d.text((x, y), f"부채비율 ({year})", font=font("medium", 24), fill=C_MUTED)
    if ratio is None:
        d.text((x, y + 40), "데이터 없음", font=font("regular", 24), fill=C_MUTED)
        return
    if ratio < 100:
        color, status = C_GOOD, "안정적"
    elif ratio < 200:
        color, status = C_WARNING, "보통"
    else:
        color, status = C_CRITICAL, "주의"
    track_w = w
    track_y = y + 40
    d.rounded_rectangle([x, track_y, x + track_w, track_y + 22], radius=11, fill=C_GRID)
    fill_ratio = min(ratio / 300, 1.0)  # 300% 이상은 꽉 찬 것으로 표시
    d.rounded_rectangle([x, track_y, x + track_w * fill_ratio, track_y + 22], radius=11, fill=color)
    d.text((x, track_y + 36), f"{ratio:.1f}%  ·  {status}", font=font("bold", 26), fill=color)


def card_valuation(report: dict) -> Image.Image:
    img, d = new_card()
    c = report["company"]
    chrome(d, c["corp_name"], 5)

    q = report.get("quote") or {}
    d.text((PAD, 140), "밸류에이션 · 재무안정성", font=font("bold", 40), fill=C_INK)
    d.text((PAD, 194), "현재 시세 기준 투자지표", font=font("regular", 24), fill=C_MUTED)

    gy = 280
    tile_w = CONTENT_W / 2
    per = f"{q['per']:.1f}배" if q.get("per") is not None else "N/A"
    pbr = f"{q['pbr']:.2f}배" if q.get("pbr") is not None else "N/A"
    eps = f"{q['eps']:,.0f}원" if q.get("eps") is not None else "N/A"
    bps = f"{q['bps']:,.0f}원" if q.get("bps") is not None else "N/A"
    stat_tile(d, PAD, gy, tile_w, "PER (주가수익비율)", per)
    stat_tile(d, PAD + tile_w, gy, tile_w, "PBR (주가순자산비율)", pbr)
    stat_tile(d, PAD, gy + 130, tile_w, "EPS (주당순이익)", eps)
    stat_tile(d, PAD + tile_w, gy + 130, tile_w, "BPS (주당순자산)", bps)

    y = gy + 300
    d.line([(PAD, y), (W - PAD, y)], fill=C_GRID, width=2)
    y += 60

    if q.get("w52_low") is not None and q.get("w52_high") is not None:
        range_bar(d, PAD, y, CONTENT_W, q["w52_low"], q["w52_high"], q.get("price"), "52주 최고 · 최저 대비 현재가")
    y += 130

    yearly = report.get("yearly_financials") or []
    if yearly:
        latest = yearly[-1]
        debt_meter(d, PAD, y, CONTENT_W, latest.get("부채비율"), latest["연도"])
    y += 110

    d.line([(PAD, y), (W - PAD, y)], fill=C_GRID, width=2)
    y += 46

    d.text((PAD, y), "외국인 보유 · 업종 비교", font=font("semibold", 30), fill=C_INK)
    y += 56

    tile_w2 = CONTENT_W / 2
    frgn = q.get("foreign_ownership_ratio")
    frgn_txt = f"{frgn:.2f}%" if frgn is not None else "N/A"
    ind = report.get("industry_comparison") or {}
    ind_per_txt = f"{ind['industry_per']:.2f}배" if ind.get("industry_per") is not None else "N/A"
    stat_tile(d, PAD, y, tile_w2, "외국인 지분율", frgn_txt)
    stat_tile(d, PAD + tile_w2, y, tile_w2, "업종 평균 PER", ind_per_txt)
    y += 90

    if q.get("per") is not None and ind.get("industry_per"):
        diff = q["per"] / ind["industry_per"]
        note = (
            f"{ind.get('industry_name') or '동일업종'} 평균 PER 대비 {diff:.1f}배 "
            f"{'높은' if diff >= 1 else '낮은'} 수준입니다."
        )
        d.text((PAD, y), note, font=font("regular", 22), fill=C_MUTED)
        y += 32

    d.text((PAD, H - 100),
           "PER·PBR은 특정 종목의 저평가/고평가를 단정하지 않습니다. 업종 평균과 함께 참고하세요.",
           font=font("regular", 20), fill=C_MUTED)
    return img


# --- 카드 5: 요약 · 공시 · 디스클레이머 --------------------------------------

def build_insights(report: dict) -> list:
    insights = []
    yearly = report.get("yearly_financials") or []
    growth = report.get("latest_growth_yoy") or {}

    rev_g = growth.get("revenue")
    if rev_g is not None:
        direction = "증가" if rev_g >= 0 else "감소"
        insights.append(f"최근 매출액이 전년대비 {abs(rev_g):.1f}% {direction}했습니다.")

    if len(yearly) >= 2:
        prev_margin = yearly[-2].get("영업이익률")
        last_margin = yearly[-1].get("영업이익률")
        if prev_margin is not None and last_margin is not None:
            diff = last_margin - prev_margin
            trend = "개선" if diff >= 0 else "악화"
            insights.append(f"영업이익률은 {prev_margin:.1f}%에서 {last_margin:.1f}%로 {trend}되었습니다.")

    if yearly:
        ratio = yearly[-1].get("부채비율")
        if ratio is not None:
            if ratio < 100:
                insights.append(f"부채비율 {ratio:.1f}%로 재무구조가 안정적인 편입니다.")
            elif ratio < 200:
                insights.append(f"부채비율 {ratio:.1f}%로 보통 수준의 재무구조입니다.")
            else:
                insights.append(f"부채비율 {ratio:.1f}%로 다소 높은 편이라 참고가 필요합니다.")

    frgn = (report.get("quote") or {}).get("foreign_ownership_ratio")
    if frgn is not None:
        if frgn >= 30:
            level = "높은"
        elif frgn >= 10:
            level = "보통"
        else:
            level = "낮은"
        insights.append(f"외국인 지분율은 {frgn:.1f}%로, 외국인 투자자 비중이 {level} 편입니다.")

    dy = (report.get("dividend") or {}).get("dividend_yield")
    if dy is not None:
        insights.append(f"배당수익률은 {dy:.2f}%로, 배당을 통한 수익도 기대할 수 있습니다.")

    return insights[:5]


def card_summary(report: dict) -> Image.Image:
    img, d = new_card()
    c = report["company"]
    chrome(d, c["corp_name"], 6)

    d.text((PAD, 140), "핵심 요약", font=font("bold", 40), fill=C_INK)

    y = 220
    for line in build_insights(report):
        r = 5
        d.ellipse([PAD, y + 14, PAD + 2 * r, y + 14 + 2 * r], fill=C_INK)
        for j, wrapped in enumerate(wrap_text(line, font("medium", 28), CONTENT_W - 40)):
            d.text((PAD + 34, y + j * 40), wrapped, font=font("medium", 28), fill=C_INK_2)
        y += 40 * max(1, len(wrap_text(line, font("medium", 28), CONTENT_W - 40))) + 26

    y += 20
    d.line([(PAD, y), (W - PAD, y)], fill=C_GRID, width=2)
    y += 36
    d.text((PAD, y), "최근 정기공시", font=font("semibold", 30), fill=C_INK)
    y += 50
    for item in (report.get("recent_disclosures") or [])[:5]:
        d.text((PAD, y), fmt_date(item.get("date", "")), font=font("medium", 22), fill=C_MUTED)
        d.text((PAD + 130, y), item.get("title", ""), font=font("medium", 24), fill=C_INK_2)
        y += 38

    audit = report.get("audit_opinion")
    if audit and audit.get("opinion"):
        y += 30
        d.line([(PAD, y), (W - PAD, y)], fill=C_GRID, width=2)
        y += 40
        d.text((PAD, y), "회계감사의견", font=font("semibold", 26), fill=C_INK)
        opinion = audit["opinion"]
        color = C_GOOD if "적정" in opinion else C_WARNING
        d.text((PAD + 200, y - 2), opinion, font=font("bold", 28), fill=color)
        if audit.get("auditor"):
            d.text((PAD, y + 36), f"감사인: {audit['auditor']}", font=font("regular", 20), fill=C_MUTED)
        y += 66

    y += 30
    box_h = 190
    d.rounded_rectangle([PAD, y, W - PAD, y + box_h], radius=16, fill=(240, 239, 236))
    disclaimer = (
        "본 카드뉴스는 DART(전자공시) · 한국투자증권 공개 데이터를 기반으로 자동 생성된 "
        "투자 참고 자료이며, 특정 종목의 매수 · 매도를 권유하지 않습니다. "
        "투자 판단과 그 결과에 대한 책임은 투자자 본인에게 있습니다."
    )
    ty = y + 26
    for line in wrap_text(disclaimer, font("regular", 22), CONTENT_W - 48):
        d.text((PAD + 24, ty), line, font=font("regular", 22), fill=C_INK_2)
        ty += 32

    return img


def build_cards(report: dict) -> list:
    return [
        card_cover(report),
        card_intro(report),
        card_performance(report),
        card_profitability(report),
        card_valuation(report),
        card_summary(report),
    ]


# --- 입력 리포트 찾기 / 저장 ---------------------------------------------------

def find_latest_report(query: str) -> Path:
    query = query.strip()
    candidates = sorted(glob.glob(str(DATA_DIR / f"*{query}*.json")))
    if not candidates:
        print(f"'{query}'에 해당하는 분석 리포트를 data/analysis/ 에서 찾지 못했습니다.", file=sys.stderr)
        print("먼저 python analyze.py 로 리포트를 생성해주세요.", file=sys.stderr)
        sys.exit(1)
    return Path(candidates[-1])  # 파일명이 날짜로 시작하므로 정렬 시 최신이 마지막


def main():
    parser = argparse.ArgumentParser(description="분석 리포트 JSON → 카드뉴스 이미지 생성")
    parser.add_argument("company", nargs="?", help="회사명 또는 종목코드 (data/analysis/ 에서 검색)")
    parser.add_argument("--file", help="분석 리포트 JSON 경로를 직접 지정")
    args = parser.parse_args()

    if args.file:
        report_path = Path(args.file)
    elif args.company:
        report_path = find_latest_report(args.company)
    else:
        parser.error("회사명/종목코드 또는 --file 중 하나는 필요합니다.")
        return

    import json
    report = json.loads(report_path.read_text(encoding="utf-8"))
    c = report["company"]

    out_dir = OUT_DIR / f"{report['generated_at'][:10]}_{c['corp_name']}_{c['stock_code']}"
    out_dir.mkdir(parents=True, exist_ok=True)

    for i, img in enumerate(build_cards(report), start=1):
        path = out_dir / f"card_{i}.png"
        img.save(path)
        print(f"저장 완료: {path}")


if __name__ == "__main__":
    main()
