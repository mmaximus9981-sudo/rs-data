"""
snapshot_export.py — 안드로이드 앱용 스냅샷 생성 모듈 (Phase 1)

설계 원칙
---------
* screener_engine.py 를 수정하지 않는다. 이 모듈은 엔진 '바깥'에서 붙는 부가 계산 +
  직렬화 레이어다. 엔진의 결과 DataFrame 과 가격 캐시(dict[str, DataFrame])만 받는다.
* 앱은 계산하지 않는다. 4분면 좌표, 손절 %, 베이스 카운트까지 전부 여기서 확정한다.
* 출력 계약(JSON 스키마)이 엔진과 앱의 유일한 접점이다. 스키마를 바꾸면 앱도 바뀐다.

사용 (Colab 셀)
--------------
    import snapshot_export as se

    snap = se.build_snapshot(
        as_of      = pd.Timestamp("2026-08-05"),
        leaders    = leaders_df,            # 엔진 산출 RS 리더 표
        prices     = price_cache,           # {ticker: OHLCV DataFrame}
        market     = {"m_gauge": "확장", "distribution_days": 2, "pct_above_200ma": 61.4},
        themes     = THEMES,                # {테마명: [티커, ...]}
        sp500      = sp500_set,             # S&P500 편입 티커 집합
        earnings   = earn_dates,            # {ticker: [발표일, ...]}
        chart_dir  = "/content/drive/MyDrive/app_snapshot/charts",
    )
    se.write_snapshot(snap, out_dir="/content/drive/MyDrive/app_snapshot")

가격 DataFrame 규약
------------------
DatetimeIndex(오름차순) + 컬럼 Open/High/Low/Close/Volume (대문자).
컬럼명이 다르면 normalize_ohlcv() 로 한 번 감싸서 넣을 것.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

SCHEMA_VERSION = "1.0"

# ---------------------------------------------------------------------------
# 0. 유틸
# ---------------------------------------------------------------------------

_OHLCV_ALIASES = {
    "open": "Open", "high": "High", "low": "Low",
    "close": "Close", "adj close": "Close", "adj_close": "Close",
    "volume": "Volume", "vol": "Volume",
}


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """컬럼명을 Open/High/Low/Close/Volume 으로 통일하고 인덱스를 정렬한다."""
    out = df.copy()
    rename = {}
    for c in out.columns:
        key = str(c).strip().lower()
        if key in _OHLCV_ALIASES:
            rename[c] = _OHLCV_ALIASES[key]
    out = out.rename(columns=rename)
    missing = {"Open", "High", "Low", "Close", "Volume"} - set(out.columns)
    if missing:
        raise ValueError(f"OHLCV 컬럼 누락: {sorted(missing)}")
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index)
    return out.sort_index()


def _f(x: Any, nd: int = 2) -> Optional[float]:
    """JSON 안전한 float. NaN/inf 는 None 으로."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, nd)


def _slice_to(df: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """as_of 이후 데이터를 잘라낸다. 백필 시 미래 참조를 막는 안전장치."""
    return df.loc[:as_of]


# ---------------------------------------------------------------------------
# 1. 파워 플레이 (하이 타이트 플래그)
# ---------------------------------------------------------------------------

@dataclass
class PowerPlayParams:
    """미너비니 원전 수치는 '8주 이내 100%, 이후 3~6주 25% 이내'.
    스크리닝 대상이 대형주(Russell 1000)라 원전대로 두면 거의 안 걸린다.
    surge_min 을 0.60~0.80 으로 낮춰 쓰는 것을 권장하고, 원전값은 strict=True 로 남겨둔다."""
    surge_window: int = 40          # 급등 구간 최대 길이 (8주)
    surge_min: float = 0.60         # 급등 최소폭
    flag_min: int = 15              # 조정 구간 최소 (3주)
    flag_max: int = 30              # 조정 구간 최대 (6주)
    flag_max_depth: float = 0.25    # 조정 최대 깊이
    near_high: float = 0.10         # 현재가가 플래그 고점 대비 이 안쪽이어야 함
    strict: bool = False

    def resolved(self) -> "PowerPlayParams":
        if not self.strict:
            return self
        return PowerPlayParams(surge_window=40, surge_min=1.00, flag_min=15,
                               flag_max=30, flag_max_depth=0.20,
                               near_high=0.08, strict=True)


def detect_power_play(df: pd.DataFrame,
                      params: PowerPlayParams | None = None) -> Optional[dict]:
    """하이 타이트 플래그 판정.

    급등 구간(저점→고점)과 그 뒤의 횡보 구간을 분리해서 본다.
    VCP 처럼 수축이 단조감소할 필요는 없다 — 파워 플레이의 핵심은 '얕고 타이트하게 오래 버티는 것'.

    반환: 미검출 시 None, 검출 시 {surge_pct, flag_days, flag_depth, pivot, ...}
    """
    p = (params or PowerPlayParams()).resolved()
    need = p.surge_window + p.flag_max + 5
    if len(df) < need:
        return None

    close = df["Close"].to_numpy(dtype=float)
    high = df["High"].to_numpy(dtype=float)
    low = df["Low"].to_numpy(dtype=float)
    n = len(close)

    best = None
    # 플래그 길이를 넓은 쪽부터 훑는다 (오래 버틴 플래그가 더 좋은 셋업)
    for flag_len in range(p.flag_max, p.flag_min - 1, -1):
        f0 = n - flag_len              # 플래그 시작 인덱스
        s0 = max(0, f0 - p.surge_window)

        surge_low = float(np.min(low[s0:f0]))
        surge_high = float(np.max(high[s0:f0]))
        if surge_low <= 0:
            continue
        surge_pct = surge_high / surge_low - 1.0
        if surge_pct < p.surge_min:
            continue

        # 급등이 구간 끝에서 마무리됐는지 (저점이 고점보다 앞서야 함)
        i_low = int(np.argmin(low[s0:f0]))
        i_high = int(np.argmax(high[s0:f0]))
        if i_low >= i_high:
            continue

        flag_high = float(np.max(high[f0:]))
        flag_low = float(np.min(low[f0:]))
        if flag_high <= 0:
            continue
        depth = 1.0 - flag_low / flag_high
        if depth > p.flag_max_depth:
            continue

        last = float(close[-1])
        if last < flag_high * (1.0 - p.near_high):
            continue

        cand = {
            "surge_pct": _f(surge_pct * 100, 1),
            "surge_days": int(f0 - s0 - i_low),
            "flag_days": int(flag_len),
            "flag_depth": _f(depth * 100, 1),
            "pivot": _f(flag_high, 2),
            "dist_to_pivot": _f((flag_high / last - 1.0) * 100, 2),
            "broke_out": bool(last > flag_high),
        }
        # 같은 조건이면 더 얕고 더 오래 버틴 쪽을 채택
        if best is None or (cand["flag_depth"] or 99) < (best["flag_depth"] or 99):
            best = cand
    return best


# ---------------------------------------------------------------------------
# 2. U/D 볼륨 비율
# ---------------------------------------------------------------------------

def up_down_volume_ratio(df: pd.DataFrame, window: int = 50) -> Optional[float]:
    """상승일 거래량 합 / 하락일 거래량 합.

    기관 수급의 무료 프록시. 1.2 이상이면 축적, 0.8 이하면 분산으로 읽는다.
    보합일(변화 0)은 양쪽 어디에도 넣지 않는다.
    """
    if len(df) < window + 1:
        return None
    tail = df.tail(window + 1)
    chg = tail["Close"].diff().to_numpy(dtype=float)[1:]
    vol = tail["Volume"].to_numpy(dtype=float)[1:]
    up = float(np.nansum(vol[chg > 0]))
    dn = float(np.nansum(vol[chg < 0]))
    if dn <= 0:
        return None
    return _f(up / dn, 2)


# ---------------------------------------------------------------------------
# 3. 실적 발표 익일 반응 이력
# ---------------------------------------------------------------------------

def earnings_reactions(df: pd.DataFrame,
                       earn_dates: Sequence[Any],
                       as_of: pd.Timestamp,
                       n: int = 4) -> List[dict]:
    """최근 n개 분기의 발표 '익일' 반응.

    gap  = 익일 시가 / 발표일 종가 - 1   (시장이 즉각 매긴 값)
    day  = 익일 종가 / 발표일 종가 - 1   (하루를 다 소화한 뒤의 값)

    gap 은 양수인데 day 가 음수면 '갭업 후 소화 실패' — 미너비니가 싫어하는 반응이다.
    """
    if df.empty or not len(earn_dates):
        return []
    px = _slice_to(df, as_of)
    if px.empty:
        return []
    idx = px.index

    out: List[dict] = []
    for d in sorted(pd.to_datetime(list(earn_dates))):
        if d > as_of:
            continue
        pos = idx.searchsorted(d, side="left")
        # 발표일이 거래일이 아니면 직전 거래일을 기준봉으로
        if pos >= len(idx):
            continue
        if pos > 0 and idx[pos] > d:
            pos -= 1
        if pos + 1 >= len(idx):
            continue
        base_close = float(px["Close"].iloc[pos])
        nxt = px.iloc[pos + 1]
        if base_close <= 0:
            continue
        out.append({
            "date": idx[pos].strftime("%Y-%m-%d"),
            "gap": _f((float(nxt["Open"]) / base_close - 1) * 100, 1),
            "day": _f((float(nxt["Close"]) / base_close - 1) * 100, 1),
        })
    return out[-n:]


# ---------------------------------------------------------------------------
# 4. 베이스 카운트
# ---------------------------------------------------------------------------

def base_count(df: pd.DataFrame, as_of: pd.Timestamp,
               min_base_days: int = 25,
               breakout_buffer: float = 0.02) -> Optional[int]:
    """스테이지 2 진입 이후 몇 번째 베이스인가.

    스테이지 2 시작 = 200일선이 상승 전환하고 종가가 그 위에 자리잡은 시점.
    이후 '직전 고점을 넘어서는 돌파'가 나올 때마다 베이스를 하나씩 센다.

    1~2차 베이스는 안전하고 3차부터 실패율이 오른다 — 같은 VCP라도 이 숫자로 등급이 갈린다.
    """
    px = _slice_to(df, as_of)
    if len(px) < 250:
        return None
    close = px["Close"]
    ma200 = close.rolling(200).mean()
    slope = ma200.diff(20)

    above = (close > ma200) & (slope > 0)
    if not bool(above.iloc[-1]):
        return None  # 스테이지 2가 아니면 베이스 카운트가 의미 없다

    # 연속으로 참인 마지막 구간의 시작점 = 현재 스테이지 2의 출발
    arr = above.to_numpy()
    start = len(arr) - 1
    while start > 0 and arr[start - 1]:
        start -= 1

    seg = px.iloc[start:]
    if len(seg) < min_base_days:
        return 1

    count = 0
    running_high = float(seg["High"].iloc[0])
    days_since = 0
    for h, c in zip(seg["High"].to_numpy(dtype=float),
                    seg["Close"].to_numpy(dtype=float)):
        days_since += 1
        if c > running_high * (1 + breakout_buffer) and days_since >= min_base_days:
            count += 1
            days_since = 0
        running_high = max(running_high, h)
    return max(1, count)


# ---------------------------------------------------------------------------
# 5. 4분면 좌표
# ---------------------------------------------------------------------------

def quadrant_coords(leaders: pd.DataFrame,
                    long_col: str = "rs",
                    short_col: str = "rs_s_chg") -> pd.DataFrame:
    """장기 RS × 단기 RS 백분위 좌표와 4분면 라벨을 계산한다.

    축 분할 기준:
      x = 장기 RS 백분위, 90 고정 (IBD 관례)
      y = 단기 RS 백분위 — '표시 대상 종목들 안에서' 다시 계산한 뒤 중앙값으로 분할

    y 를 전체 유니버스 기준으로 두면 네 분면 넓이가 어긋난다(이전에 지적한 문제).
    표시 대상만으로 재계산해 중앙값을 쓰면 위아래가 정확히 반으로 갈린다.
    """
    df = leaders.copy()
    if long_col not in df.columns:
        raise ValueError(f"'{long_col}' 컬럼이 없습니다")
    if short_col not in df.columns:
        df[short_col] = 0.0

    df["rs_x"] = pd.to_numeric(df[long_col], errors="coerce")
    s = pd.to_numeric(df[short_col], errors="coerce")
    df["rs_y"] = s.rank(pct=True) * 100.0

    x_split = 90.0
    y_split = float(df["rs_y"].median())

    def label(r) -> str:
        hi_x = r["rs_x"] >= x_split
        hi_y = r["rs_y"] >= y_split
        if hi_x and hi_y:
            return "주도"
        if hi_x and not hi_y:
            return "약화"
        if not hi_x and hi_y:
            return "개선"
        return "부진"

    df["rs_quad"] = df.apply(label, axis=1)
    df.attrs["x_split"] = x_split
    df.attrs["y_split"] = y_split
    return df


# ---------------------------------------------------------------------------
# 5b. 차트 시리즈 (앱이 직접 그리도록 압축해서 싣는다)
# ---------------------------------------------------------------------------

def _pack_bars(px: pd.DataFrame, mas: Dict[int, pd.Series]) -> dict:
    """OHLCV + 이동평균을 앱이 그릴 최소 형태로. 소수점은 2자리에서 자른다 —
    픽셀로 그릴 때 그 이하는 어차피 보이지 않는다."""
    r = lambda s: [None if pd.isna(v) else round(float(v), 2) for v in s]
    return {
        "t": [d.strftime("%y%m%d") for d in px.index],
        "o": r(px["Open"]), "h": r(px["High"]),
        "l": r(px["Low"]),  "c": r(px["Close"]),
        "v": [0 if pd.isna(v) else int(v) for v in px["Volume"]],
        "ma50": r(mas[50]), "ma150": r(mas[150]), "ma200": r(mas[200]),
    }


def compact_series(df: pd.DataFrame, as_of: pd.Timestamp, bars: int = 1260) -> dict:
    """차트용 시리즈. 일봉 5년(약 1260 거래일)을 싣는다.

    PNG를 미리 굽는 대신 시리즈를 싣는 이유: 확대·스크롤이 되고, 굽는 단계가 사라진다.

    주봉은 따로 만들어 보내지 않는다. 일봉 5년이 실려 있으면 앱에서 주 단위로
    묶어낼 수 있고, 그러면 두 차트가 같은 원본을 보게 되어 어긋날 일이 없다.
    같은 구간을 두 벌 싣는 것보다 종목당 15KB 가량 가볍기도 하다.

    이동평균의 예열분은 화면에 내보내지 않는다. 200일선이 5년 구간의 첫날부터
    그려지려면 그 앞의 200 거래일이 계산에만 쓰이고 잘려 나가야 한다 —
    그래서 run_sp500 이 6년을 받아 온다. 받은 만큼이 5년에 못 미치면
    앞쪽 이동평균이 비고, 앱은 그 구간을 끊어서 그린다.
    """
    px = _slice_to(df, as_of)
    if px.empty:
        return {}
    # 이동평균은 전체 이력에서 계산한 뒤 자른다. 1260봉만 잘라서 계산하면 200일선의
    # 앞 200봉이 빈다.
    mas = {p: px["Close"].rolling(p).mean() for p in (50, 150, 200)}
    return _pack_bars(px.tail(bars), {p: s.tail(bars) for p, s in mas.items()})


def eps_steps(quarters: Sequence[dict], as_of: pd.Timestamp) -> List[dict]:
    """분기 EPS/매출을 계단으로 그릴 수 있는 형태로.

    quarters 원소: {"date": 발표일, "eps": GAAP 분기 EPS, "rev": 분기 매출(백만)}
    TTM이 아니라 분기값을 그대로 쓴다 — 가속·둔화가 TTM에서는 뭉개지기 때문.
    """
    out = []
    for q in sorted(quarters, key=lambda d: pd.Timestamp(d["date"])):
        d = pd.Timestamp(q["date"])
        if d > as_of:
            continue
        out.append({"date": d.strftime("%y%m%d"),
                    "eps": _f(q.get("eps"), 2), "rev": _f(q.get("rev"), 0)})
    return out


# ---------------------------------------------------------------------------
# 6. 스냅샷 조립
# ---------------------------------------------------------------------------

@dataclass
class SnapshotConfig:
    universe: str = "Russell 1000"
    engine_version: str = "v29"
    rs_min: int = 80
    account_risk_pct: float = 1.0     # 사이징 계산의 기본값 (앱에서 조정 가능)
    pp_params: PowerPlayParams = field(default_factory=PowerPlayParams)


def build_snapshot(as_of: pd.Timestamp,
                   leaders: pd.DataFrame,
                   prices: Dict[str, pd.DataFrame],
                   market: dict,
                   themes: Dict[str, Iterable[str]] | None = None,
                   sp500: Iterable[str] | None = None,
                   earnings: Dict[str, Sequence[Any]] | None = None,
                   trail: Dict[str, Sequence[Sequence[float]]] | None = None,
                   fundamentals: Dict[str, Sequence[dict]] | None = None,
                   include_series: bool = True,
                   chart_dir: str | None = None,
                   config: SnapshotConfig | None = None) -> dict:
    """엔진 결과 + 가격 캐시 → 앱이 그대로 그리는 JSON dict.

    trail: {ticker: [[x, y], ...]} — 4·3·2·1주 전 4분면 좌표. 앱의 궤적 꼬리에 쓰인다.
           DB의 과거 스냅샷에서 뽑아 넣으면 되고, 없으면 꼬리 없이 점만 찍힌다.
    """
    cfg = config or SnapshotConfig()
    as_of = pd.Timestamp(as_of)
    sp500 = set(sp500 or [])
    trail = trail or {}
    fundamentals = fundamentals or {}
    themes = {k: list(v) for k, v in (themes or {}).items()}
    earnings = earnings or {}

    theme_of: Dict[str, str] = {}
    for name, tks in themes.items():
        for t in tks:
            theme_of.setdefault(t, name)

    df = quadrant_coords(leaders)
    x_split = df.attrs.get("x_split", 90.0)
    y_split = df.attrs.get("y_split", 50.0)

    tickers: List[dict] = []
    for _, row in df.iterrows():
        tk = str(row.get("ticker") or row.get("Ticker") or "").upper()
        if not tk:
            continue

        px = prices.get(tk)
        pp = ud = bc = None
        series: dict = {}
        reactions: List[dict] = []
        if px is not None and not px.empty:
            px = _slice_to(normalize_ohlcv(px), as_of)
            if len(px) > 60:
                pp = detect_power_play(px, cfg.pp_params)
                ud = up_down_volume_ratio(px)
                bc = base_count(px, as_of)
                reactions = earnings_reactions(px, earnings.get(tk, []), as_of)
                if include_series:
                    series = compact_series(px, as_of)

        setup = row.get("setup")
        setup = None if (setup is None or (isinstance(setup, float) and math.isnan(setup))) else str(setup)
        if pp is not None and not setup:
            setup = "파워플레이"

        rec = {
            "ticker": tk,
            "name": _s(row.get("name")) or tk,
            "price": _f(row.get("price")),
            "rs": _i(row.get("rs")),
            "rs_x": _f(row.get("rs_x"), 1),
            "rs_y": _f(row.get("rs_y"), 1),
            "rs_quad": _s(row.get("rs_quad")),
            "rs_traj": _s(row.get("rs_traj")),
            "trail": [[_f(a, 1), _f(b, 1)] for a, b in trail.get(tk, [])] or None,
            "rs_line_hi": bool(row.get("rs_line_hi", False)),
            "setup": setup,
            "power_play": pp,
            "tt_pass": bool(row.get("tt_pass", False)),
            "base_count": bc,
            "sector": _s(row.get("sector")),
            "sub_industry": _s(row.get("sub_industry")),
            "theme": theme_of.get(tk),
            "grp_rs": _i(row.get("grp_rs")),
            "in_sp500": tk in sp500,
            "d_to_earn": _i(row.get("d_to_earn")),
            "eps_yoy": _f(row.get("eps_yoy%") if "eps_yoy%" in df.columns else row.get("eps_yoy"), 1),
            "eps_accel": _s(row.get("eps_accel")),
            "rev_yoy": _f(row.get("rev_yoy%") if "rev_yoy%" in df.columns else row.get("rev_yoy"), 1),
            "ud_volume": ud,
            "earn_reactions": reactions,
            "series": series or None,
            "quarters": eps_steps(fundamentals.get(tk, []), as_of) or None,
            "stop1_pct": _f(row.get("stop1_pct"), 1),
            "stop2_pct": _f(row.get("stop2_pct"), 1),
            "mcap_b": _f(row.get("mcap_B"), 1),
            "dollar_vol_m": _f(row.get("dollar_vol_M"), 1),
            "chart": f"charts/{tk}.png" if chart_dir else None,
        }
        tickers.append(rec)

    theme_rows = []
    for name, tks in themes.items():
        members = [t for t in tickers if t["theme"] == name]
        strong = [t for t in members if (t["rs"] or 0) >= 85]
        if not members:
            continue
        theme_rows.append({
            "name": name,
            "count": len(members),
            "rs85_count": len(strong),
            "tickers": [t["ticker"] for t in sorted(members, key=lambda r: -(r["rs"] or 0))],
        })
    theme_rows.sort(key=lambda r: (-r["rs85_count"], -r["count"]))

    return {
        "meta": {
            "schema": SCHEMA_VERSION,
            "as_of": as_of.strftime("%Y-%m-%d"),
            "universe": cfg.universe,
            "engine_version": cfg.engine_version,
            "rs_min": cfg.rs_min,
            "account_risk_pct": cfg.account_risk_pct,
        },
        "market": {
            "m_gauge": market.get("m_gauge"),
            "distribution_days": market.get("distribution_days"),
            "pct_above_200ma": _f(market.get("pct_above_200ma"), 1),
            "note": market.get("note"),
        },
        "axes": {"x_split": _f(x_split, 1), "y_split": _f(y_split, 1)},
        "themes": theme_rows,
        "tickers": tickers,
    }


def _s(x: Any) -> Optional[str]:
    if x is None:
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    s = str(x).strip()
    return s or None


def _i(x: Any) -> Optional[int]:
    v = _f(x)
    return None if v is None else int(v)


# ---------------------------------------------------------------------------
# 7. 기록
# ---------------------------------------------------------------------------

def write_snapshot(snap: dict, out_dir: str) -> Dict[str, str]:
    """스냅샷을 날짜본과 latest.json 두 벌로 떨어뜨린다.

    앱은 latest.json 만 본다. 날짜본은 백필·회귀 확인용으로 남긴다.
    """
    os.makedirs(out_dir, exist_ok=True)
    stamp = snap["meta"]["as_of"].replace("-", "")
    dated = os.path.join(out_dir, f"snapshot_{stamp}.json")
    latest = os.path.join(out_dir, "latest.json")
    blob = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
    for path in (dated, latest):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(blob)
    return {"dated": dated, "latest": latest, "bytes": str(len(blob.encode()))}
