"""
run_sp500.py — 실데이터 파이프라인. 이거 하나 돌리면 latest.json 과 app.html 이 나온다.

    python run_sp500.py                 # 전체
    python run_sp500.py --limit 60      # 빠른 확인용 (앞 60종목만)
    python run_sp500.py --no-fundamentals   # 실적 수집 생략 (가장 느린 구간)

Colab 이면:
    !pip -q install yfinance
    !python run_sp500.py

캐시를 쓴다. 가격/펀더멘털은 cache/ 에 parquet·pickle 로 남고, 같은 날 다시 돌리면
네트워크를 타지 않는다. 강제로 새로 받으려면 --refresh.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
from datetime import date
from typing import Dict

import pandas as pd

import fetch_data as fd
import screener_live as sl
import snapshot_export as se

CACHE = "cache"
OUT_DIR = "."


# ---------------------------------------------------------------------------
# 테마 — GICS 서브인더스트리보다 잘게. 'AI 인프라'처럼 내러티브 단위로 묶는다.
# 여기가 이 앱의 편집 지점이다. 시장이 바뀌면 이 딕셔너리를 손보면 된다.
# ---------------------------------------------------------------------------
THEMES: Dict[str, list] = {
    "AI 반도체": ["NVDA", "AMD", "AVGO", "MRVL", "MU", "TXN", "ADI", "NXPI", "MCHP", "ON"],
    "AI 반도체 장비": ["AMAT", "LRCX", "KLAC", "TER", "ENPH"],
    "AI 인프라·전력": ["VST", "CEG", "NRG", "ETR", "PWR", "ETN", "PH", "EMR", "HUBB", "GEV"],
    "AI 소프트웨어": ["MSFT", "GOOGL", "META", "PLTR", "NOW", "SNPS", "CDNS", "ANSS"],
    "데이터센터·네트워크": ["ANET", "CSCO", "EQIX", "DLR", "SMCI", "JNPR", "KEYS"],
    "클라우드": ["AMZN", "ORCL", "CRM", "WDAY", "DDOG", "SNOW", "MDB"],
    "사이버보안": ["PANW", "CRWD", "FTNT", "GEN", "ZS", "OKTA"],
    "비만·대사질환": ["LLY", "NVO", "AMGN", "VKTX", "PFE"],
    "비만 외 바이오": ["VRTX", "REGN", "GILD", "BIIB", "MRNA", "INCY"],
    "방산": ["LMT", "RTX", "NOC", "GD", "LHX", "HII", "TDG", "HWM", "TXT", "AXON"],
    "육상 화물": ["UNP", "CSX", "NSC", "ODFL", "JBHT", "CHRW", "XPO", "SAIA"],
    "항공": ["DAL", "UAL", "LUV", "AAL", "ALK"],
    "oil and gas": ["XOM", "CVX", "COP", "EOG", "PXD", "DVN", "FANG", "OXY", "HES", "MRO",
                    "APA", "SLB", "HAL", "BKR", "OKE", "WMB", "KMI"],
    "원자력·우라늄": ["CEG", "SO", "DUK", "PEG"],
    "귀금속·광물": ["NEM", "FCX", "MOS", "CF", "ALB"],
    "건설·주택": ["DHI", "LEN", "PHM", "NVR", "MAS", "BLD", "MLM", "VMC"],
    "결제·핀테크": ["V", "MA", "PYPL", "FIS", "FISV", "GPN", "AXP", "COF"],
    "소비 리오프닝": ["MAR", "HLT", "RCL", "CCL", "NCLH", "BKNG", "EXPE", "ABNB", "LVS", "WYNN"],
    "비만 수혜 소비": ["COST", "WMT", "TGT", "DG", "DLTR"],
}


def load_or(path: str, fn, refresh: bool = False):
    """당일 캐시가 있으면 재사용. 없으면 fn() 을 돌리고 저장."""
    os.makedirs(CACHE, exist_ok=True)
    stamped = os.path.join(CACHE, f"{date.today():%Y%m%d}_{path}")
    if not refresh and os.path.exists(stamped):
        with open(stamped, "rb") as fh:
            print(f"[캐시] {path}")
            return pickle.load(fh)
    obj = fn()
    with open(stamped, "wb") as fh:
        pickle.dump(obj, fh)
    return obj


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="앞 N종목만 (빠른 확인)")
    ap.add_argument("--rs-min", type=int, default=80)
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--no-fundamentals", action="store_true")
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()

    # 1. 유니버스 -------------------------------------------------------------
    uni = load_or("sp500.pkl", fd.fetch_sp500, args.refresh)
    if args.limit:
        uni = uni.head(args.limit)
    tickers = uni["ticker"].tolist()

    # 2. 가격 -----------------------------------------------------------------
    prices = load_or("prices.pkl",
                     lambda: fd.fetch_prices(tickers, years=args.years), args.refresh)
    bench = load_or("bench.pkl", fd.fetch_benchmark, args.refresh)
    if not prices:
        raise SystemExit("가격을 하나도 받지 못했습니다. 네트워크를 확인하세요.")

    as_of = max(df.index[-1] for df in prices.values())
    print(f"[기준일] {as_of:%Y-%m-%d} · 종목 {len(prices)}")

    # 3. 메타 -----------------------------------------------------------------
    meta = load_or("meta.pkl",
                   lambda: fd.fetch_meta(list(prices), base=uni), args.refresh)

    # 4. 실적 -----------------------------------------------------------------
    fundamentals: Dict[str, list] = {}
    earn_dates: Dict[str, list] = {}
    if not args.no_fundamentals:
        earn_dates = load_or("earndates.pkl",
                             lambda: fd.fetch_earnings_dates(list(prices)), args.refresh)
        fundamentals = load_or("fundamentals.pkl",
                               lambda: fd.fetch_fundamentals(list(prices)), args.refresh)

        for tk in prices:
            g = fd.derive_growth(fundamentals.get(tk, []))
            meta.setdefault(tk, {}).update(g)
            if tk in earn_dates:
                meta[tk]["d_to_earn"] = fd.next_earnings_gap(earn_dates[tk], as_of)

    # 5. 스크리닝 -------------------------------------------------------------
    cfg = sl.ScreenConfig(rs_min=args.rs_min)
    pp = lambda df: se.detect_power_play(df, se.PowerPlayParams())
    leaders, trail, y_split = sl.screen(
        prices=prices, bench_close=bench, as_of=as_of, meta=meta,
        themes=THEMES, power_play_fn=pp, config=cfg)

    if leaders.empty:
        raise SystemExit("RS 기준을 넘는 종목이 없습니다. --rs-min 을 낮춰 확인해보세요.")

    # 6. 시장 국면 ------------------------------------------------------------
    market = market_state(prices, bench, as_of)

    # 7. 스냅샷 ---------------------------------------------------------------
    snap = se.build_snapshot(
        as_of=as_of,
        leaders=leaders.drop(columns=["_hits"], errors="ignore"),
        prices=prices,
        market=market,
        themes={k: [t for t in v if t in set(leaders["ticker"])] for k, v in THEMES.items()},
        sp500=set(tickers),
        earnings={tk: fd.past_earnings(d, as_of) for tk, d in earn_dates.items()},
        trail=trail,
        fundamentals=fundamentals,
        config=se.SnapshotConfig(universe="S&P 500", engine_version="live-1",
                                 rs_min=args.rs_min),
    )
    snap["axes"]["y_split"] = round(y_split, 1)

    info = se.write_snapshot(snap, out_dir=args.out)
    print(f"[스냅샷] {info['latest']} · {int(info['bytes'])/1024:.0f} KB · "
          f"{len(snap['tickers'])}종목")

    rebuild_app(args.out)
    summary(snap)


def market_state(prices: Dict[str, pd.DataFrame], bench: pd.Series,
                 as_of: pd.Timestamp) -> dict:
    """M 게이지 — 시장이 추세추종을 허용하는 구간인가.

    분산일: 최근 25거래일 중 지수가 -0.2% 이상 하락하면서 거래량이 전일보다 늘어난 날.
    """
    above = []
    for df in prices.values():
        d = df.loc[:as_of]
        if len(d) < 200:
            continue
        ma200 = d["Close"].rolling(200).mean().iloc[-1]
        above.append(float(d["Close"].iloc[-1]) > float(ma200))
    pct_above = round(100 * sum(above) / max(1, len(above)), 1)

    b = bench.loc[:as_of].tail(26)
    ret = b.pct_change().dropna()
    dist = int((ret <= -0.002).sum())

    healthy = pct_above >= 50 and dist <= 5
    return {
        "m_gauge": "확장" if healthy else "방어",
        "distribution_days": dist,
        "pct_above_200ma": pct_above,
        "note": "추세추종 가능 구간" if healthy else "신규 진입 축소 구간",
    }


def rebuild_app(out_dir: str) -> None:
    """app_template.html + latest.json → app.html (내장 데이터 갱신)."""
    tpl_path = os.path.join(out_dir, "app_template.html")
    if not os.path.exists(tpl_path):
        return
    tpl = open(tpl_path, encoding="utf-8").read()
    data = open(os.path.join(out_dir, "latest.json"), encoding="utf-8").read()
    app = os.path.join(out_dir, "app.html")
    with open(app, "w", encoding="utf-8") as fh:
        fh.write(tpl.replace("/*__DATA__*/", data))
    print(f"[앱] {app} 갱신 — 더블클릭하면 바로 열립니다")

    asset = os.path.join(out_dir, "android/app/src/main/assets/app.html")
    if os.path.isdir(os.path.dirname(asset)):
        with open(asset, "w", encoding="utf-8") as fh:
            fh.write(open(app, encoding="utf-8").read())
        print(f"[앱] 안드로이드 assets 도 갱신")


def summary(snap: dict) -> None:
    ts = snap["tickers"]
    print("\n── 패턴 적중 ──")
    for name in sl.PRIORITY:
        hit = [t for t in ts if t["setup"] == name]
        if hit:
            print(f"  {name:6} {len(hit):2}종목  " +
                  " ".join(t["ticker"] for t in hit[:12]))
    print("\n── 분면 ──")
    for q in ("주도", "개선", "약화", "부진"):
        print(f"  {q} {sum(1 for t in ts if t['rs_quad']==q)}")
    print("\n── 테마 상위 ──")
    for th in snap["themes"][:6]:
        print(f"  {th['name']:16} RS85 {th['rs85_count']}/{th['count']}")


if __name__ == "__main__":
    main()
