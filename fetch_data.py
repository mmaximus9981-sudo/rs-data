"""
fetch_data.py — 네트워크 담당. 여기만 인터넷을 탄다.

yfinance 는 상업용 재배포가 안 된다. 지금 단계(개인 확인용)에는 문제없지만
앱을 유료로 열 때는 이 파일만 EODHD/FMP 어댑터로 갈아끼우면 되도록,
바깥에 노출하는 함수 시그니처를 공급자와 무관하게 잡아 두었다.

    fetch_prices(tickers, years)   -> {ticker: OHLCV DataFrame}
    fetch_meta(tickers)            -> {ticker: {name, sector, sub_industry, mcap_B, ...}}
    fetch_fundamentals(tickers)    -> {ticker: [{date, eps, rev}, ...]}
    fetch_earnings_dates(tickers)  -> {ticker: [발표일, ...]}
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 1. 유니버스
# ---------------------------------------------------------------------------

def fetch_sp500() -> pd.DataFrame:
    """위키피디아 S&P500 구성표. ticker / name / sector / sub_industry."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    tbl = pd.read_html(url)[0]
    df = tbl.rename(columns={
        "Symbol": "ticker", "Security": "name",
        "GICS Sector": "sector", "GICS Sub-Industry": "sub_industry",
    })[["ticker", "name", "sector", "sub_industry"]]
    # BRK.B → BRK-B (야후 표기)
    df["ticker"] = df["ticker"].str.replace(".", "-", regex=False).str.strip()
    print(f"[유니버스] S&P500 {len(df)}종목")
    return df


# ---------------------------------------------------------------------------
# 2. 가격
# ---------------------------------------------------------------------------

def fetch_prices(tickers: Sequence[str], years: float = 3.0,
                 batch: int = 60, pause: float = 1.0) -> Dict[str, pd.DataFrame]:
    """일봉 OHLCV. 배치로 나눠 받는다 — 500개를 한 번에 던지면 자주 잘린다.

    auto_adjust=True: 액면분할·배당 조정. RS와 패턴 모두 조정가 기준이어야 한다.
    """
    import yfinance as yf

    period = f"{int(years * 365)}d"
    out: Dict[str, pd.DataFrame] = {}
    tickers = list(dict.fromkeys(tickers))

    for i in range(0, len(tickers), batch):
        chunk = tickers[i:i + batch]
        try:
            raw = yf.download(chunk, period=period, interval="1d",
                              auto_adjust=True, group_by="ticker",
                              threads=True, progress=False)
        except Exception as e:
            print(f"[가격] 배치 {i//batch+1} 실패: {e}")
            continue

        for tk in chunk:
            try:
                df = raw[tk] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.dropna(how="all")
                if len(df) < 260:
                    continue
                df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
                df.index = pd.to_datetime(df.index).tz_localize(None)
                out[tk] = df.sort_index()
            except Exception:
                continue
        print(f"[가격] {min(i+batch, len(tickers))}/{len(tickers)} · 누적 {len(out)}")
        time.sleep(pause)

    missing = set(tickers) - set(out)
    if missing:
        print(f"[가격] 누락 {len(missing)}종목: {sorted(list(missing))[:10]}...")
    return out


def fetch_benchmark(period_days: int = 1200) -> pd.Series:
    """RS선의 분모. S&P500 지수를 쓴다."""
    import yfinance as yf
    df = yf.download("^GSPC", period=f"{period_days}d", interval="1d",
                     auto_adjust=True, progress=False)
    s = df["Close"]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    s.index = pd.to_datetime(s.index).tz_localize(None)
    return s.sort_index()


# ---------------------------------------------------------------------------
# 3. 메타 · 펀더멘털
# ---------------------------------------------------------------------------

def fetch_meta(tickers: Sequence[str], base: Optional[pd.DataFrame] = None,
               pause: float = 0.12) -> Dict[str, dict]:
    """시총과 이름. 섹터/서브인더스트리는 위키 표(base)를 우선하고 결측만 야후로 메운다."""
    import yfinance as yf

    seed: Dict[str, dict] = {}
    if base is not None:
        for _, r in base.iterrows():
            seed[r["ticker"]] = {"name": r["name"], "sector": r["sector"],
                                 "sub_industry": r["sub_industry"]}

    out: Dict[str, dict] = {}
    for n, tk in enumerate(tickers, 1):
        m = dict(seed.get(tk, {}))
        try:
            info = yf.Ticker(tk).fast_info
            mc = getattr(info, "market_cap", None)
            if mc:
                m["mcap_B"] = round(float(mc) / 1e9, 1)
        except Exception:
            pass
        if not m.get("sub_industry"):
            try:
                i = yf.Ticker(tk).info
                m["sector"] = m.get("sector") or i.get("sector")
                m["sub_industry"] = i.get("industry")
                m["name"] = m.get("name") or i.get("shortName")
            except Exception:
                pass
        out[tk] = m
        if n % 50 == 0:
            print(f"[메타] {n}/{len(tickers)}")
        time.sleep(pause)
    return out


def fetch_fundamentals(tickers: Sequence[str], pause: float = 0.15
                       ) -> Dict[str, List[dict]]:
    """분기 EPS(희석, GAAP)와 분기 매출.

    TTM 이 아니라 분기값 그대로 쓴다 — 가속·둔화가 TTM 에서는 뭉개지기 때문.
    """
    import yfinance as yf

    out: Dict[str, List[dict]] = {}
    for n, tk in enumerate(tickers, 1):
        try:
            fin = yf.Ticker(tk).quarterly_income_stmt
            if fin is None or fin.empty:
                continue
            rows = []
            for col in sorted(fin.columns):
                def pick(*keys):
                    for k in keys:
                        if k in fin.index:
                            v = fin.loc[k, col]
                            if pd.notna(v):
                                return float(v)
                    return None
                eps = pick("Diluted EPS", "Basic EPS")
                rev = pick("Total Revenue", "Operating Revenue")
                if eps is None and rev is None:
                    continue
                rows.append({"date": pd.Timestamp(col),
                             "eps": eps,
                             "rev": None if rev is None else rev / 1e6})
            if rows:
                out[tk] = rows[-8:]
        except Exception:
            pass
        if n % 50 == 0:
            print(f"[펀더] {n}/{len(tickers)} · 확보 {len(out)}")
        time.sleep(pause)
    return out


def fetch_earnings_dates(tickers: Sequence[str], pause: float = 0.15
                         ) -> Dict[str, List[pd.Timestamp]]:
    """과거 발표일(익일 반응 계산용) + 다음 발표일(D-day 계산용)."""
    import yfinance as yf

    out: Dict[str, List[pd.Timestamp]] = {}
    for n, tk in enumerate(tickers, 1):
        try:
            ed = yf.Ticker(tk).get_earnings_dates(limit=16)
            if ed is None or ed.empty:
                continue
            idx = pd.to_datetime(ed.index)
            try:
                idx = idx.tz_localize(None)
            except (TypeError, AttributeError):
                idx = idx.tz_convert(None) if getattr(idx, "tz", None) else idx
            out[tk] = sorted(pd.Timestamp(d).normalize() for d in idx)
        except Exception:
            pass
        if n % 50 == 0:
            print(f"[실적일] {n}/{len(tickers)} · 확보 {len(out)}")
        time.sleep(pause)
    return out


# ---------------------------------------------------------------------------
# 4. 파생 지표
# ---------------------------------------------------------------------------

def derive_growth(quarters: Sequence[dict]) -> dict:
    """분기 EPS/매출 → YoY 성장률과 가속 판정.

    가속 = 직전 분기 YoY 보다 이번 분기 YoY 가 더 높다. 미너비니가 보는 건 수준이 아니라 방향이다.
    """
    if not quarters or len(quarters) < 5:
        return {}
    q = sorted(quarters, key=lambda r: r["date"])

    def yoy(i, key):
        cur, prev = q[i].get(key), q[i - 4].get(key)
        if cur is None or prev is None or prev == 0:
            return None
        return (cur - prev) / abs(prev) * 100

    e_now, e_prev = yoy(-1, "eps"), (yoy(-2, "eps") if len(q) >= 6 else None)
    r_now = yoy(-1, "rev")

    accel = None
    if e_now is not None and e_prev is not None:
        accel = "가속" if e_now > e_prev + 2 else ("둔화" if e_now < e_prev - 2 else "확대")

    return {"eps_yoy": None if e_now is None else round(e_now, 1),
            "rev_yoy": None if r_now is None else round(r_now, 1),
            "eps_accel": accel}


def next_earnings_gap(dates: Sequence[pd.Timestamp], as_of: pd.Timestamp) -> Optional[int]:
    """다음 발표까지 남은 일수."""
    fut = [d for d in dates if d > as_of]
    return int((min(fut) - as_of).days) if fut else None


def past_earnings(dates: Sequence[pd.Timestamp], as_of: pd.Timestamp,
                  n: int = 6) -> List[pd.Timestamp]:
    return [d for d in dates if d <= as_of][-n:]
