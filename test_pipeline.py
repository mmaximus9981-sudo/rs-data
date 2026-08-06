"""test_pipeline.py — 네트워크 없이 파이프라인 전체를 돌려보는 통합 테스트.

실데이터를 붙이기 전에 로직이 깨지지 않는지 확인하는 용도.
    python test_pipeline.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import screener_live as sl
import snapshot_export as se

RNG = np.random.default_rng(11)
N = 700
IDX = pd.bdate_range(end=pd.Timestamp("2026-08-04"), periods=N)


def noise(n, s=0.009):
    return 1 + RNG.normal(0, s, n)


def fit(p):
    p = np.asarray(p, float)
    return p[-N:] if len(p) >= N else np.r_[np.full(N - len(p), p[0]) * noise(N - len(p)), p]


def ohlcv(p):
    p = fit(p) * noise(N, 0.004)
    chg = np.r_[0, np.diff(p)]
    vol = RNG.integers(6e5, 5e6, N).astype(float) * np.where(chg > 0, 1.3, 0.85)
    return pd.DataFrame({
        "Open": p * (1 + RNG.normal(0, .003, N)),
        "High": p * (1 + abs(RNG.normal(.006, .004, N))),
        "Low":  p * (1 - abs(RNG.normal(.006, .004, N))),
        "Close": p, "Volume": vol}, index=IDX)


def vcp_path(b=40.):
    p = np.r_[b * np.linspace(1, 1.2, 300), b * 1.2 * np.linspace(1, 1.8, 220)]
    hi = p[-1]
    for d, days in [(.14, 28), (.08, 20), (.035, 14)]:
        p = np.r_[p, hi * (1 - d * np.sin(np.linspace(0, np.pi, days)))]
    return p


def w_path(b=60.):
    p = np.r_[b * np.linspace(1, 1.7, 320)]
    hi = p[-1]
    p = np.r_[p, hi * (1 - .17 * np.sin(np.linspace(0, np.pi, 40)))]
    p = np.r_[p, hi * .93 * np.linspace(1, 1.05, 15)]
    p = np.r_[p, hi * .975 * (1 - .15 * np.sin(np.linspace(0, np.pi, 38)))]
    p = np.r_[p, hi * .97 * np.linspace(1, 1.03, 12)]
    return p


def ihs_path(b=25.):
    """왼어깨 -18% · 머리 -34% · 오른어깨 -19% · 넥라인 재접근."""
    seg = lambda a, z, n: np.linspace(a, z, n)
    p = np.r_[seg(b, b * 1.02, 260),
              seg(b * 1.02, b * .82, 22), seg(b * .82, b * 1.00, 20),   # 왼어깨
              seg(b * 1.00, b * .66, 30), seg(b * .66, b * .99, 28),    # 머리
              seg(b * .99, b * .81, 22), seg(b * .81, b * .99, 22)]     # 오른어깨
    return p


def downtrend_bo(b=90.):
    p = np.r_[b * np.linspace(1, 1.4, 200)]
    seg = b * 1.4
    for k in range(5):
        p = np.r_[p, seg * (1 - .10 * np.sin(np.linspace(0, np.pi, 24)))]
        seg *= .955
    p = np.r_[p, p[-1] * np.linspace(1, 1.10, 3)]   # 갓 돌파
    return p


def power_path(b=15.):
    flat = np.full(300, b) * np.cumprod(noise(300, .007))
    surge = flat[-1] * np.linspace(1, 2.3, 40)
    return np.r_[flat, surge[-1] * (1 - .07 * np.sin(np.linspace(0, np.pi * .8, 24)))]


def drift(b=50., mu=.0002):
    return b * np.cumprod(1 + RNG.normal(mu, .013, N))


SHAPES = [("LEAD1", vcp_path), ("LEAD2", w_path), ("LEAD3", ihs_path),
          ("LEAD4", downtrend_bo), ("LEAD5", power_path), ("LEAD6", vcp_path)]


def main():
    prices, meta, fund, earn = {}, {}, {}, {}
    for tk, fn in SHAPES:
        prices[tk] = ohlcv(fn())
    for i in range(34):                       # 나머지는 평범한 표류 종목
        prices[f"BASE{i:02d}"] = ohlcv(drift(40 + i, RNG.normal(0, .0004)))

    subs = ["Semiconductors", "Software", "Oil & Gas E&P", "Ground Transport", "Biotechnology"]
    edates = [IDX[-i] for i in (280, 218, 155, 92, 30)]
    for n, tk in enumerate(prices):
        meta[tk] = {"name": f"{tk} Corp", "sector": "—",
                    "sub_industry": subs[n % len(subs)], "mcap_B": 20.0 + n,
                    "d_to_earn": (n * 7) % 60 + 1, "eps_yoy": 30 - n,
                    "eps_accel": "가속" if n % 3 == 0 else "둔화", "rev_yoy": 15 - n / 2}
        earn[tk] = edates
        fund[tk] = [{"date": d, "eps": round(0.8 * (1.09 ** i), 2),
                     "rev": round(700 * (1.06 ** i))} for i, d in enumerate(edates)]

    bench = pd.Series(100 * np.cumprod(1 + RNG.normal(.0003, .008, N)), index=IDX)
    themes = {"AI 반도체": ["LEAD1", "LEAD5"], "oil and gas": ["LEAD3", "BASE01"],
              "육상 화물": ["LEAD2", "LEAD4"]}

    pp = lambda df: se.detect_power_play(df, se.PowerPlayParams())
    leaders, trail, y_split = sl.screen(
        prices=prices, bench_close=bench, meta=meta, themes=themes,
        power_play_fn=pp, config=sl.ScreenConfig(rs_min=60))

    print("\n리더 표", leaders.shape)
    cols = ["ticker", "rs", "rs_s_chg", "rs_traj", "setup", "tt_pass",
            "grp_rs", "stop1_pct", "stop2_pct"]
    print(leaders[cols].head(15).to_string(index=False))

    hits = leaders[leaders.setup.notna()]
    print("\n패턴 적중:", dict(hits.setup.value_counts()))
    print("궤적 보유:", len(trail), "· y_split:", round(y_split, 1))

    snap = se.build_snapshot(
        as_of=IDX[-1], leaders=leaders.drop(columns=["_hits"]), prices=prices,
        market={"m_gauge": "확장", "distribution_days": 3, "pct_above_200ma": 58.0},
        themes=themes, sp500=set(prices), earnings=earn, trail=trail,
        fundamentals=fund, config=se.SnapshotConfig(universe="S&P 500"))
    snap["axes"]["y_split"] = round(y_split, 1)

    t = snap["tickers"][0]
    print("\n스냅샷 필드 점검:", {k: (type(v).__name__ if not isinstance(v, (int, float, str, type(None))) else v)
                            for k, v in t.items() if k in
                            ("ticker", "rs", "rs_quad", "setup", "base_count", "ud_volume",
                             "series", "quarters", "earn_reactions", "trail", "power_play")})
    info = se.write_snapshot(snap, ".")
    print("스냅샷:", int(info["bytes"]) // 1024, "KB ·", len(snap["tickers"]), "종목")
    assert all(x["series"] and x["series"].get("ma200") for x in snap["tickers"]), "MA200 누락"
    print("\nOK")


if __name__ == "__main__":
    main()
