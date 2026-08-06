"""
screener_live.py — S&P500 실데이터용 스크리닝 엔진 (네트워크 없음, 순수 계산).

기존 `us_screener_dashboard_sp500` 계열의 사양을 그대로 옮겼다.
* RS 레이팅: IBD 가중 성과(63/126/189/252) → 유니버스 백분위
* 트렌드 템플릿: 미너비니 8조건
* 패턴 5종: VCP · W · 역헤숄 · 하향추세선 돌파 · 파워 플레이
* 중복 적중 시 우선순위: VCP → W → 역헤숄 → 추세선돌파 → 파워플레이
* 네 패턴은 서로 독립 — 한 패턴의 계산이 다른 패턴에 영향을 주지 않는다
  (지그재그 감도도 패턴별로 분리)
* RS 다이나믹스: 장기 RS 백분위 × 단기 RS(RS선 21일 변화율) 백분위

데이터 위생이 이 파일의 절반이다. 과거에 결과가 1종목으로 붕괴한 원인이
패턴 로직이 아니라 합집합 인덱스의 스트레이 행이었기 때문에,
build_close_matrix() 에서 중복 날짜 병합·ffill·커버리지 컷을 먼저 건다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# 0. 데이터 위생
# ---------------------------------------------------------------------------

def build_close_matrix(prices: Dict[str, pd.DataFrame],
                       min_coverage: float = 0.90,
                       ffill_limit: int = 5) -> pd.DataFrame:
    """티커별 OHLCV → 종가 행렬. RS 계산의 유일한 입력이다.

    합집합 인덱스를 그냥 쓰면 거래소 휴장일이 어긋난 티커 하나 때문에
    '스트레이 행'이 생기고, 그 행이 RS 표본 시점에 걸리면 표본이 몇 종목으로 붕괴한다.
    중복 날짜 병합 → 제한적 ffill → 커버리지 미달 행 제거 순으로 막는다.
    """
    series = {}
    for tk, df in prices.items():
        if df is None or df.empty or "Close" not in df:
            continue
        s = df["Close"]
        s = s[~s.index.duplicated(keep="last")]
        series[tk] = s

    if not series:
        raise ValueError("가격 데이터가 비었습니다")

    mat = pd.DataFrame(series).sort_index()
    mat = mat.ffill(limit=ffill_limit)
    coverage = mat.notna().mean(axis=1)
    kept = mat.loc[coverage >= min_coverage]

    dropped = len(mat) - len(kept)
    if dropped:
        print(f"[위생] 커버리지 {min_coverage:.0%} 미만 {dropped}행 제거 "
              f"({len(mat)} → {len(kept)})")
    if len(kept) < 260:
        print(f"[경고] 거래일이 {len(kept)}일뿐입니다. RS 계산이 부정확할 수 있습니다.")
    return kept


def rs_sanity_check(rs: pd.Series, universe_size: int) -> None:
    """RS 표본이 무너졌는지 조기에 잡는다. 조용히 1종목이 되는 것이 가장 위험하다."""
    n = int(rs.notna().sum())
    if n < universe_size * 0.5:
        print(f"[경고] RS 산출 종목 {n} / 유니버스 {universe_size} — 표본 붕괴 의심. "
              f"가격 행렬의 결측을 먼저 확인하세요.")


# ---------------------------------------------------------------------------
# 1. RS 레이팅과 RS 다이나믹스
# ---------------------------------------------------------------------------

_RS_WEIGHTS = ((63, 0.40), (126, 0.20), (189, 0.20), (252, 0.20))


def rs_raw(closes: pd.DataFrame, as_of: Optional[pd.Timestamp] = None) -> pd.Series:
    """IBD식 가중 성과. 백분위로 바꾸기 전의 원점수."""
    m = closes.loc[:as_of] if as_of is not None else closes
    if len(m) < 253:
        raise ValueError(f"RS 계산에 253거래일이 필요합니다 (현재 {len(m)})")
    last = m.iloc[-1]
    score = pd.Series(0.0, index=m.columns)
    for lag, w in _RS_WEIGHTS:
        past = m.iloc[-(lag + 1)]
        score += w * (last / past)
    score[last.isna()] = np.nan
    return score


def rs_rating(closes: pd.DataFrame, as_of: Optional[pd.Timestamp] = None) -> pd.Series:
    """1~99 백분위."""
    raw = rs_raw(closes, as_of)
    return (raw.rank(pct=True) * 98 + 1).round(0)


def rs_line(closes: pd.DataFrame, bench: pd.Series) -> pd.DataFrame:
    """RS선 = 종목 / 벤치마크. 신고가 판정과 단기 RS의 재료."""
    b = bench.reindex(closes.index).ffill()
    return closes.div(b, axis=0)


def short_rs(rsl: pd.DataFrame, span: int = 21,
             as_of: Optional[pd.Timestamp] = None) -> pd.Series:
    """단기 RS = RS선의 21일 변화율(%).

    미너비니 원전에 단기 RS 지표는 없다. 원전의 'RS선이 주가보다 먼저 돈다'는
    관찰을 수치화한 것이고, 4분면의 y축은 이 값의 백분위다.
    """
    m = rsl.loc[:as_of] if as_of is not None else rsl
    if len(m) < span + 1:
        return pd.Series(np.nan, index=m.columns)
    return (m.iloc[-1] / m.iloc[-(span + 1)] - 1.0) * 100


def rs_line_new_high(rsl: pd.DataFrame, lookback: int = 252,
                     as_of: Optional[pd.Timestamp] = None) -> pd.Series:
    m = rsl.loc[:as_of] if as_of is not None else rsl
    tail = m.tail(lookback)
    return tail.iloc[-1] >= tail.max() * 0.999


def quadrant(rs: pd.Series, s_rs: pd.Series,
             pool: Sequence[str], x_split: float = 90.0
             ) -> Tuple[pd.DataFrame, float]:
    """표시 대상(pool) 안에서 단기 RS 백분위를 다시 계산하고 중앙값으로 상하를 나눈다.

    전체 유니버스 백분위를 그대로 쓰면 네 분면의 넓이가 어긋난다.
    장기 RS 80~99 구간은 90에서 반반으로 갈리지만, 같은 종목들의 단기 RS는
    유니버스 기준 90에서 반반이 되지 않기 때문.
    """
    pool = [t for t in pool if t in rs.index]
    df = pd.DataFrame({"rs": rs.reindex(pool), "s": s_rs.reindex(pool)})
    df["rs_y"] = df["s"].rank(pct=True) * 100
    df["rs_x"] = df["rs"]
    y_split = float(df["rs_y"].median()) if len(df) else 50.0

    def lab(r):
        hx, hy = r["rs_x"] >= x_split, r["rs_y"] >= y_split
        return "주도" if (hx and hy) else "약화" if hx else "개선" if hy else "부진"

    df["rs_quad"] = df.apply(lab, axis=1) if len(df) else []
    return df, y_split


# ---------------------------------------------------------------------------
# 2. 트렌드 템플릿
# ---------------------------------------------------------------------------

def trend_template(df: pd.DataFrame, rs: float,
                   rs_min: int = 70) -> Tuple[bool, Dict[str, bool]]:
    """미너비니 8조건. 통과 여부와 조건별 상세를 함께 돌려준다."""
    if len(df) < 252:
        return False, {}
    c = df["Close"]
    ma50, ma150, ma200 = (c.rolling(p).mean() for p in (50, 150, 200))
    last = float(c.iloc[-1])
    lo52, hi52 = float(c.tail(252).min()), float(c.tail(252).max())

    ck = {
        "종가>150·200일선": last > ma150.iloc[-1] and last > ma200.iloc[-1],
        "150>200": ma150.iloc[-1] > ma200.iloc[-1],
        "200일선 상승": ma200.iloc[-1] > ma200.iloc[-22],
        "50>150·200": ma50.iloc[-1] > ma150.iloc[-1] and ma50.iloc[-1] > ma200.iloc[-1],
        "종가>50일선": last > ma50.iloc[-1],
        "저점대비 +30%": last >= lo52 * 1.30,
        "고점대비 -25% 이내": last >= hi52 * 0.75,
        f"RS≥{rs_min}": (rs or 0) >= rs_min,
    }
    return all(ck.values()), ck


# ---------------------------------------------------------------------------
# 3. 지그재그 (패턴 공통 재료 — 감도는 패턴별로 따로 준다)
# ---------------------------------------------------------------------------

def zigzag(df: pd.DataFrame, pct: float = 0.05) -> List[Tuple[int, float, int]]:
    """가격 되돌림 pct 이상일 때만 전환점으로 인정. (index, price, +1 고점/-1 저점)

    고점 후보와 저점 후보를 따로 들고 다닌다. 하나의 극값만 추적하면
    방향이 정해지기 전(dir_=0)에 고점 갱신과 저점 갱신이 서로를 덮어써서
    전환점이 하나도 안 잡힌다.
    """
    hi, lo = df["High"].to_numpy(float), df["Low"].to_numpy(float)
    n = len(hi)
    if n < 10:
        return []

    piv: List[Tuple[int, float, int]] = []
    dir_ = 0
    hi_i, hi_p = 0, hi[0]
    lo_i, lo_p = 0, lo[0]

    for i in range(1, n):
        if dir_ >= 0 and hi[i] >= hi_p:
            hi_i, hi_p = i, hi[i]
        if dir_ <= 0 and lo[i] <= lo_p:
            lo_i, lo_p = i, lo[i]

        if dir_ != -1 and lo[i] < hi_p * (1 - pct):
            piv.append((hi_i, hi_p, +1))
            dir_ = -1
            lo_i, lo_p = i, lo[i]
        elif dir_ != 1 and hi[i] > lo_p * (1 + pct):
            piv.append((lo_i, lo_p, -1))
            dir_ = +1
            hi_i, hi_p = i, hi[i]

    return piv


def volume_dryup(df: pd.DataFrame, base_days: int) -> Optional[float]:
    """베이스 안에서 거래량이 말라가는지. 후반부 평균 / 전반부 평균.

    돌파일 거래량 급증이 아니라 '돌파 직전' 을 찾는 게 목적이므로 이쪽을 본다.
    1보다 확실히 작으면 드라이업.
    """
    if base_days < 20 or len(df) < base_days:
        return None
    v = df["Volume"].tail(base_days).to_numpy(float)
    h = base_days // 2
    a, b = np.nanmean(v[:h]), np.nanmean(v[h:])
    return None if a <= 0 else round(float(b / a), 2)


# ---------------------------------------------------------------------------
# 4. 패턴 엔진
# ---------------------------------------------------------------------------

@dataclass
class PatternParams:
    # 공통
    anchor_skip: int = 5          # 베이스 앵커에서 마지막 N봉 제외 (갓 돌파한 봉이 앵커를 먹는 것 방지)
    max_base_days: int = 170
    pivot_within: float = 0.25    # 피벗이 전고점 대비 이 안쪽
    accept_after_bo: int = 3      # 돌파 직후 N봉까지는 수용
    # VCP
    vcp_zigzag: float = 0.05
    vcp_min_contractions: int = 2
    vcp_max_first_depth: float = 0.60
    vcp_max_last_depth: float = 0.15
    # W
    w_zigzag: float = 0.06
    # 역헤숄
    ihs_zigzag: float = 0.05
    ihs_shoulder_tol: float = 0.20
    # 하향추세선 돌파
    ihs_window: int = 200
    bo_lookback: int = 140
    bo_min_highs: int = 3
    bo_zigzag: float = 0.04


def _base_anchor(df: pd.DataFrame, p: PatternParams,
                 touch_tol: float = 0.02) -> Tuple[int, float]:
    """베이스의 시작 위치와 가격.

    창 안의 argmax 를 그냥 쓰면 안 된다. 베이스가 고점 근처로 몇 번 되돌아오는 형태에서는
    '가장 마지막에 고점을 찍은 봉'이 앵커가 되어 베이스가 몇 봉으로 쪼그라든다.
    (v19 감사에서 FTRE 가 안 잡히던 원인이 이것이었다.)

    그래서 창 최고가에 처음 닿은 봉을 앵커로 삼는다. 그래야 베이스가 통째로 잡힌다.
    마지막 anchor_skip 봉은 후보에서 빼서, 갓 돌파한 봉이 앵커를 먹는 것도 막는다.
    """
    win = df.tail(p.max_base_days)
    hh = win["High"].to_numpy(float)
    usable = hh[:-p.anchor_skip] if len(hh) > p.anchor_skip else hh
    if not len(usable):
        return 0, float("nan")
    mx = float(np.nanmax(usable))
    near = np.where(usable >= mx * (1 - touch_tol))[0]
    i = int(near[0]) if len(near) else int(np.argmax(usable))
    return len(df) - len(win) + i, mx


def detect_vcp(df: pd.DataFrame, p: PatternParams) -> Optional[dict]:
    """수축이 점점 얕아지는 베이스. 마지막 수축이 얕고 피벗 근처여야 한다."""
    a_i, a_px = _base_anchor(df, p)
    if not np.isfinite(a_px):
        return None
    base = df.iloc[a_i:]
    if len(base) < 25:
        return None

    piv = zigzag(base, p.vcp_zigzag)
    lows = [(i, px) for i, px, d in piv if d == -1]
    if len(lows) < p.vcp_min_contractions:
        return None

    contractions, running_high = [], a_px
    for i, lo in lows:
        seg_high = float(base["High"].iloc[:i + 1].max())
        running_high = max(running_high, seg_high)
        contractions.append(round((1 - lo / running_high) * 100, 1))
    if contractions[0] / 100 > p.vcp_max_first_depth:
        return None
    # 단조감소를 강제하지 않는다 — 원전도 완벽한 계단만 인정하지 않는다.
    if contractions[-1] > contractions[0]:
        return None

    last = float(base["Close"].iloc[-1])
    pivot = float(base["High"].max())
    if last < pivot * (1 - p.pivot_within):
        return None

    bars_since_bo = _bars_since_breakout(base, pivot)
    if bars_since_bo is not None and bars_since_bo > p.accept_after_bo:
        return None

    return {
        "pattern": "VCP",
        "contractions": contractions,
        "last_contr": contractions[-1],
        "base_days": len(base),
        "pivot": round(pivot, 2),
        "dist_to_pivot": round((pivot / last - 1) * 100, 2),
        "dryup": volume_dryup(df, len(base)),
        "bars_since_bo": bars_since_bo,
    }


def detect_w(df: pd.DataFrame, p: PatternParams) -> Optional[dict]:
    """두 저점이 비슷하고 가운데 봉우리가 있는 W. 두 번째 저점이 더 얕으면 더 좋다."""
    a_i, a_px = _base_anchor(df, p)
    base = df.iloc[a_i:]
    if len(base) < 30:
        return None
    piv = zigzag(base, p.w_zigzag)
    if len(piv) < 3:
        return None

    lows = [(i, px) for i, px, d in piv if d == -1]
    highs = [(i, px) for i, px, d in piv if d == +1]
    if len(lows) < 2 or not highs:
        return None

    # 마지막 두 저점을 그냥 쓰면 안 된다. 베이스 중간의 작은 흔들림이 저점으로 잡히면
    # 진짜 W의 왼발을 놓친다. 최근 저점들 중에서 짝을 찾아 가장 잘 맞는 조합을 고른다.
    best = None
    cands = lows[-4:]
    for a in range(len(cands) - 1):
        for b in range(a + 1, len(cands)):
            (i1, l1), (i2, l2) = cands[a], cands[b]
            mid = [px for i, px in highs if i1 < i < i2]
            if not mid:
                continue
            mh = max(mid)
            ratio = l2 / l1
            if not (0.85 <= ratio <= 1.10):
                continue
            if mh <= max(l1, l2) * 1.05:       # 가운데 봉우리가 있어야 W다
                continue
            if i2 - i1 < 12:                   # 두 발 사이가 너무 붙어 있으면 흔들림이다
                continue
            score = abs(1 - ratio)             # 두 발이 나란할수록 좋다
            if best is None or score < best[0]:
                best = (score, i1, l1, i2, l2, mh)
    if best is None:
        return None
    _, i1, l1, i2, l2, mh = best

    last = float(base["Close"].iloc[-1])
    pivot = float(base["High"].max())
    if max(l1, l2) > pivot * 0.92:             # 베이스 깊이가 8% 미만이면 베이스가 아니다
        return None
    if last < pivot * (1 - p.pivot_within):
        return None
    bars = _bars_since_breakout(base, pivot)
    if bars is not None and bars > p.accept_after_bo:
        return None

    return {"pattern": "W", "base_days": len(base), "pivot": round(pivot, 2),
            "dist_to_pivot": round((pivot / last - 1) * 100, 2),
            "low_ratio": round(l2 / l1, 3), "dryup": volume_dryup(df, len(base)),
            "bars_since_bo": bars}


def detect_ihs(df: pd.DataFrame, p: PatternParams) -> Optional[dict]:
    """역헤드앤숄더. 넥라인 돌파 직후(당일~3봉)는 자체 판정한다 — BO 엔진에 넘기지 않는다."""
    # 역헤숄은 앵커(전고점)에 매달 이유가 없다 — 머리·어깨가 고점 아래에서 만들어지므로
    # 고정 창으로 본다. 패턴 간 독립성을 지키는 조건이기도 하다.
    base = df.tail(p.ihs_window)
    if len(base) < 40:
        return None
    piv = zigzag(base, p.ihs_zigzag)
    lows = [(i, px) for i, px, d in piv if d == -1]
    highs = [(i, px) for i, px, d in piv if d == +1]
    if len(lows) < 3 or len(highs) < 2:
        return None

    # 마지막 세 저점으로 고정하지 않고, 최근 저점들 중 (왼어깨·머리·오른어깨) 조합을 찾는다.
    best = None
    cands = lows[-5:]
    for a in range(len(cands) - 2):
        for b in range(a + 1, len(cands) - 1):
            for c in range(b + 1, len(cands)):
                (li, lp), (hi_, hp), (ri, rp) = cands[a], cands[b], cands[c]
                if not (hp < lp and hp < rp):
                    continue                          # 가운데가 머리
                gap = abs(rp - lp) / max(lp, rp)
                if gap > p.ihs_shoulder_tol:
                    continue                          # 어깨 높이가 비슷해야
                if best is None or gap < best[0]:
                    best = (gap, li, lp, hi_, hp, ri, rp)
    if best is None:
        return None
    _, li, lp, hi_, hp, ri, rp = best

    neck = [px for i, px in highs if li < i < ri]
    if len(neck) < 2:
        return None
    neckline = float(np.mean(neck))                    # 외삽하지 않는다 — 평평한 넥라인으로 본다
    last = float(base["Close"].iloc[-1])
    if last < neckline * (1 - p.pivot_within):
        return None
    bars = _bars_since_breakout(base, neckline)
    if bars is not None and bars > p.accept_after_bo:
        return None

    return {"pattern": "역헤숄", "base_days": len(base), "pivot": round(neckline, 2),
            "dist_to_pivot": round((neckline / last - 1) * 100, 2),
            "head_depth": round((1 - hp / neckline) * 100, 1),
            "dryup": volume_dryup(df, len(base)), "bars_since_bo": bars}


def detect_breakout(df: pd.DataFrame, p: PatternParams) -> Optional[dict]:
    """우하향 '추세선' 상향 돌파. 수평 저항이 아니다.

    고점 피벗들에 회귀선을 얹어 기울기가 음수인지 확인하고, 종가가 그 선을 넘었는지 본다.
    """
    win = df.tail(p.bo_lookback)
    if len(win) < 40:
        return None
    piv = zigzag(win, p.bo_zigzag)
    n = len(win)
    # 돌파 이후의 고점이 추세선 적합에 끼면 기울기가 양수로 뒤집힌다. 최근 구간은 뺀다.
    cut = n - (p.accept_after_bo + 2)
    highs = [(i, px) for i, px, d in piv if d == +1 and i <= cut]
    if len(highs) < p.bo_min_highs:
        return None

    xs = np.array([i for i, _ in highs], float)
    ys = np.array([px for _, px in highs], float)
    slope, intercept = np.polyfit(xs, ys, 1)
    if slope >= 0:
        return None

    line_now = slope * (n - 1) + intercept
    line_prev = slope * (n - 2) + intercept
    c_now = float(win["Close"].iloc[-1])
    c_prev = float(win["Close"].iloc[-2])

    # 오늘 막 넘었거나, 넘은 지 accept_after_bo 봉 이내
    crossed_today = c_prev <= line_prev and c_now > line_now
    bars = None
    if not crossed_today:
        closes = win["Close"].to_numpy(float)
        line = slope * np.arange(n) + intercept
        above = closes > line
        if not above[-1]:
            return None
        k = 0
        while k < len(above) and above[-1 - k]:
            k += 1
        if k > p.accept_after_bo + 1:
            return None
        bars = k - 1

    return {"pattern": "추세선돌파", "slope_pct_per_day": round(slope / c_now * 100, 3),
            "pivot": round(float(line_now), 2), "dist_to_pivot": 0.0,
            "highs_used": len(highs), "bars_since_bo": 0 if crossed_today else bars,
            "dryup": volume_dryup(df, min(len(df), p.bo_lookback))}


def _bars_since_breakout(base: pd.DataFrame, level: float) -> Optional[int]:
    """종가가 level 위로 올라선 지 몇 봉 됐는지. 아직 아래면 None."""
    c = base["Close"].to_numpy(float)
    if c[-1] <= level:
        return None
    k = 0
    while k < len(c) and c[-1 - k] > level:
        k += 1
    return k - 1


PRIORITY = ["VCP", "W", "역헤숄", "추세선돌파", "파워플레이"]


def detect_all(df: pd.DataFrame, p: PatternParams,
               power_play_fn=None) -> Tuple[Optional[str], Dict[str, dict]]:
    """패턴 5종을 각각 독립적으로 돌리고, 우선순위 하나만 대표로 뽑는다."""
    hits: Dict[str, dict] = {}
    for fn in (detect_vcp, detect_w, detect_ihs, detect_breakout):
        try:
            r = fn(df, p)
        except Exception:
            r = None
        if r:
            hits[r["pattern"]] = r
    if power_play_fn is not None:
        try:
            pp = power_play_fn(df)
        except Exception:
            pp = None
        if pp:
            hits["파워플레이"] = pp
    for name in PRIORITY:
        if name in hits:
            return name, hits
    return None, hits


# ---------------------------------------------------------------------------
# 5. 지지선 / 손절
# ---------------------------------------------------------------------------

def support_levels(df: pd.DataFrame, lookback: int = 60,
                   zz: float = 0.04) -> Tuple[Optional[float], Optional[float]]:
    """현재가 아래의 1·2차 지지. 지그재그 저점 → 없으면 이동평균으로 대체.

    반환은 현재가 대비 %(음수).
    """
    win = df.tail(lookback)
    last = float(win["Close"].iloc[-1])
    piv = zigzag(win, zz)
    lows = sorted({round(px, 4) for _, px, d in piv if d == -1 and px < last}, reverse=True)

    if len(lows) < 2:
        for p in (21, 50):
            if len(df) >= p:
                ma = float(df["Close"].rolling(p).mean().iloc[-1])
                if ma < last:
                    lows.append(ma)
        lows = sorted(set(lows), reverse=True)

    # -25% 보다 먼 지지는 손절 자리로 쓸모가 없다. 그런 건 없는 것으로 친다.
    pcts = [round((lv / last - 1) * 100, 1) for lv in lows]
    pcts = [v for v in pcts if v > -25.0]
    s1 = pcts[0] if len(pcts) >= 1 else None
    s2 = pcts[1] if len(pcts) >= 2 else None
    return s1, s2


# ---------------------------------------------------------------------------
# 6. 리더 표 조립
# ---------------------------------------------------------------------------

@dataclass
class ScreenConfig:
    rs_min: int = 80
    tt_rs_min: int = 70
    trail_weeks: int = 4
    params: PatternParams = field(default_factory=PatternParams)


def screen(prices: Dict[str, pd.DataFrame],
           bench_close: pd.Series,
           as_of: Optional[pd.Timestamp] = None,
           meta: Optional[Dict[str, dict]] = None,
           themes: Optional[Dict[str, Sequence[str]]] = None,
           power_play_fn=None,
           config: Optional[ScreenConfig] = None
           ) -> Tuple[pd.DataFrame, Dict[str, List[List[float]]], float]:
    """전체 유니버스 → (리더 DataFrame, 4분면 궤적, y_split).

    반환 DataFrame 의 컬럼은 snapshot_export.build_snapshot 이 그대로 읽는다.
    """
    cfg = config or ScreenConfig()
    meta = meta or {}
    themes = themes or {}

    closes = build_close_matrix(prices)
    if as_of is None:
        as_of = closes.index[-1]
    closes = closes.loc[:as_of]

    rs = rs_rating(closes)
    rs_sanity_check(rs, len(prices))
    rsl = rs_line(closes, bench_close)
    s_rs = short_rs(rsl)
    rs_hi = rs_line_new_high(rsl)

    pool = [t for t in rs.index if pd.notna(rs[t]) and rs[t] >= cfg.rs_min]
    qdf, y_split = quadrant(rs, s_rs, pool)

    # 4분면 궤적 — 1~4주 전 시점의 좌표를 같은 방식으로 다시 구한다
    trail: Dict[str, List[List[float]]] = {t: [] for t in pool}
    for w in range(cfg.trail_weeks, 0, -1):
        cut = len(closes) - 5 * w
        if cut < 260:
            continue
        past = closes.iloc[:cut]
        try:
            p_rs = rs_rating(past)
            p_s = short_rs(rs_line(past, bench_close))
        except ValueError:
            continue
        p_q, _ = quadrant(p_rs, p_s, pool)
        for t in pool:
            if t in p_q.index and pd.notna(p_q.loc[t, "rs_y"]):
                trail[t].append([float(p_q.loc[t, "rs_x"]), float(p_q.loc[t, "rs_y"])])

    # 그룹(세부업종) RS = 소속 종목 RS 중앙값
    sub_of = {t: (meta.get(t, {}) or {}).get("sub_industry") for t in rs.index}
    grp: Dict[str, float] = {}
    for sub in {v for v in sub_of.values() if v}:
        members = [t for t, s in sub_of.items() if s == sub and pd.notna(rs.get(t))]
        if members:
            grp[sub] = float(np.median([rs[t] for t in members]))

    theme_of = {t: name for name, tks in themes.items() for t in tks}

    rows = []
    for tk in pool:
        df = prices.get(tk)
        if df is None or df.empty:
            continue
        df = df.loc[:as_of]
        if len(df) < 252:
            continue

        setup, hits = detect_all(df, cfg.params, power_play_fn)
        tt_pass, _ = trend_template(df, float(rs[tk]), cfg.tt_rs_min)
        s1, s2 = support_levels(df)
        last = float(df["Close"].iloc[-1])
        hi52 = float(df["High"].tail(252).max())
        m = meta.get(tk, {}) or {}
        past_rs = trail[tk][0][0] if trail.get(tk) else None

        rows.append({
            "ticker": tk,
            "name": m.get("name") or tk,
            "rs": int(rs[tk]),
            "rs_s_chg": round(float(s_rs.get(tk, np.nan)), 2) if pd.notna(s_rs.get(tk, np.nan)) else 0.0,
            "rs_traj": f"{int(past_rs)}→{int(rs[tk])}" if past_rs else None,
            "rs_line_hi": bool(rs_hi.get(tk, False)),
            "setup": setup,
            "tt_pass": bool(tt_pass),
            "sector": m.get("sector"),
            "sub_industry": m.get("sub_industry"),
            "grp_rs": round(grp.get(sub_of.get(tk), np.nan), 0) if sub_of.get(tk) in grp else None,
            "price": round(last, 2),
            "pct_52w_hi": round((last / hi52 - 1) * 100, 1) if hi52 else None,
            "mcap_B": m.get("mcap_B"),
            "dollar_vol_M": round(float(df["Close"].tail(50).mul(df["Volume"].tail(50)).mean()) / 1e6, 1),
            "d_to_earn": m.get("d_to_earn"),
            "eps_yoy%": m.get("eps_yoy"),
            "eps_accel": m.get("eps_accel"),
            "rev_yoy%": m.get("rev_yoy"),
            "stop1_pct": s1,
            "stop2_pct": s2,
            "theme": theme_of.get(tk),
            "_hits": hits,
        })

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["setup", "rs"], ascending=[True, False], na_position="last")
    print(f"[스크린] 유니버스 {len(prices)} · RS≥{cfg.rs_min} {len(pool)} · "
          f"패턴 적중 {int(out['setup'].notna().sum()) if not out.empty else 0}")
    return out, {k: v for k, v in trail.items() if v}, y_split
