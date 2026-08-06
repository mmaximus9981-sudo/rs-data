"""make_demo.py — 앱 확인용 데모 스냅샷 생성기.

실데이터가 붙기 전에 앱을 눈으로 확인하려면 '형태가 있는' 가격이 필요하다.
난수 워크만 쓰면 VCP도 파워플레이도 안 나와서 화면이 비어 보인다.
그래서 셋업별로 경로를 손으로 빚어 넣는다.
"""
from __future__ import annotations

import json
import numpy as np
import pandas as pd

import snapshot_export as se

RNG = np.random.default_rng(20260805)
N = 420
IDX = pd.bdate_range(end=pd.Timestamp("2026-08-04"), periods=N)


def _noise(n, s=0.008):
    return 1 + RNG.normal(0, s, n)


def fit(p):
    """경로 길이를 정확히 N으로 맞춘다 — 앞을 잘라내거나 앞에 평탄부를 덧댄다."""
    p = np.asarray(p, dtype=float)
    if len(p) >= N:
        return p[-N:]
    return np.r_[np.full(N - len(p), p[0]) * _noise(N - len(p), 0.006), p]


def path_uptrend_vcp(base=40.0):
    """완만한 상승 후 수축하는 베이스 — VCP."""
    p = np.r_[
        base * np.linspace(1.00, 1.15, 150),
        base * 1.15 * np.linspace(1.00, 1.75, 160),
    ]
    tail = []
    hi = p[-1]
    for depth, days in [(0.13, 26), (0.07, 18), (0.035, 12)]:
        leg = hi * (1 - depth * np.sin(np.linspace(0, np.pi, days)))
        tail.append(leg)
    p = np.r_[p, np.concatenate(tail)]
    return p[:N] if len(p) >= N else np.r_[np.full(N - len(p), p[0]), p]


def path_power_play(base=18.0):
    """8주 급등 후 5주 타이트 플래그. 플래그가 '지금'이어야 하므로 뒤에 아무것도 붙이지 않는다."""
    flat = np.full(250, base) * np.cumprod(_noise(250, 0.007))
    surge = flat[-1] * np.linspace(1.0, 2.20, 40)
    flag = surge[-1] * (1 - 0.075 * np.sin(np.linspace(0, np.pi * 0.85, 26)))
    return np.r_[flat, surge, flag]


def path_breakout_today(base=120.0):
    """저항선 아래에서 눌리다가 오늘 돌파."""
    p = np.r_[
        base * np.linspace(1.0, 1.35, 200),
        base * 1.35 * (1 - 0.16 * np.sin(np.linspace(0, np.pi, 90))),
        base * 1.30 * np.linspace(1.0, 1.05, 120),
    ]
    p = p[:N - 5]
    p = np.r_[p, p[-1] * np.linspace(1.0, 1.09, 5)]
    return p


def path_topping(base=210.0):
    """고점에서 힘이 빠지는 형태 — 약화 분면용."""
    p = np.r_[
        base * np.linspace(1.0, 1.9, 260),
        base * 1.9 * np.linspace(1.0, 1.02, 60),
        base * 1.94 * np.linspace(1.0, 0.86, 100),
    ]
    return p[:N]


def path_turning(base=55.0):
    """바닥에서 돌아서는 중 — 개선 분면용."""
    p = np.r_[
        base * np.linspace(1.0, 0.62, 240),
        base * 0.62 * np.linspace(1.0, 1.45, 180),
    ]
    return p[:N]


def path_lagging(base=32.0):
    p = np.full(N, base) * np.cumprod(_noise(N, 0.011))
    return p


def to_ohlcv(p):
    p = fit(p) * _noise(N, 0.004)
    vol = RNG.integers(8e5, 6e6, len(p)).astype(float)
    # 상승 구간 거래량을 키우고 베이스에서는 말린다 — U/D 볼륨이 의미를 갖게
    chg = np.r_[0, np.diff(p)]
    vol *= np.where(chg > 0, 1.35, 0.85)
    return pd.DataFrame({
        "Open": p * (1 + RNG.normal(0, 0.003, len(p))),
        "High": p * (1 + abs(RNG.normal(0.006, 0.004, len(p)))),
        "Low":  p * (1 - abs(RNG.normal(0.006, 0.004, len(p)))),
        "Close": p,
        "Volume": vol,
    }, index=IDX)


SPEC = [
    # ticker, name, path, theme, sub_industry, rs, rs_s_chg, setup, eps0, growth
    ("NVDX", "노바텍스 반도체", path_uptrend_vcp,    "AI 인프라",   "Semiconductors",  98, 4.8, "VCP",       0.62, 0.34),
    ("ARCL", "아크라이트 전력",  path_power_play,     "AI 인프라",   "Electrical Equip", 96, 6.1, None,        0.21, 0.51),
    ("VERT", "버텍스 클라우드",  path_breakout_today, "AI 인프라",   "Software",        93, 3.2, "추세선돌파", 1.10, 0.22),
    ("HALO", "할로 네트웍스",    path_uptrend_vcp,    "AI 인프라",   "Semiconductors",  91, 1.4, "W",         0.44, 0.19),
    ("KRDG", "카리지 물류",      path_uptrend_vcp,    "육상 화물",   "Ground Transport", 89, 2.6, "VCP",       1.85, 0.16),
    ("FRTL", "프레이트라인",     path_turning,        "육상 화물",   "Ground Transport", 84, 5.2, None,        0.95, 0.11),
    ("PTRA", "페트라 에너지",    path_topping,        "oil and gas", "Oil & Gas E&P",    94, -6.4, None,       2.40, -0.08),
    ("BSNL", "베이슨 리소스",    path_lagging,        "oil and gas", "Oil & Gas E&P",    81, -2.1, None,       1.30, 0.02),
    ("MEDA", "메디아 바이오",    path_turning,        "비만 치료",   "Biotechnology",    87, 7.3, "역헤숄",     -0.30, 0.40),
    ("CLNS", "클렌시아 제약",    path_uptrend_vcp,    "비만 치료",   "Pharmaceuticals",  92, 2.0, "VCP",        1.55, 0.24),
    ("ORBT", "오르빗 방산",      path_breakout_today, "방산",        "Aerospace & Def",  90, 3.9, "추세선돌파",  1.02, 0.18),
    ("STGR", "스타가드 시스템",  path_topping,        "방산",        "Aerospace & Def",  86, -4.7, None,        0.88, 0.05),
]


def build():
    prices, leaders, themes, earn, fund, trail = {}, [], {}, {}, {}, {}
    earn_dates = [IDX[-i] for i in (300, 240, 180, 120, 62, 8)]

    for tk, name, fn, theme, sub, rs, schg, setup, eps0, g in SPEC:
        df = to_ohlcv(fn())
        prices[tk] = df
        themes.setdefault(theme, []).append(tk)
        earn[tk] = earn_dates
        fund[tk] = [
            {"date": d, "eps": round(eps0 * (1 + g) ** i, 2),
             "rev": round(eps0 * 900 * (1 + g * 0.7) ** i, 0)}
            for i, d in enumerate(earn_dates)
        ]
        last = float(df["Close"].iloc[-1])
        hi52 = float(df["High"].tail(252).max())
        leaders.append({
            "ticker": tk, "name": name, "rs": rs, "rs_s_chg": schg,
            "rs_traj": f"{max(1, rs - int(schg * 2))}→{rs}",
            "setup": setup, "tt_pass": rs >= 85 and schg > -5,
            "rs_line_hi": rs >= 92 and schg > 0,
            "sector": "—", "sub_industry": sub,
            "grp_rs": {"AI 인프라": 95, "육상 화물": 86, "oil and gas": 62,
                       "비만 치료": 88, "방산": 83}[theme],
            "price": round(last, 2),
            "pct_52w_hi": round((last / hi52 - 1) * 100, 1),
            "d_to_earn": int(abs(hash(tk)) % 45) + 1,
            "eps_yoy%": round(g * 100, 1),
            "eps_accel": "가속" if g > 0.2 else ("확대" if g > 0.08 else "둔화"),
            "rev_yoy%": round(g * 70, 1),
            "stop1_pct": -round(3.5 + (abs(hash(tk)) % 30) / 10, 1),
            "stop2_pct": -round(7.0 + (abs(hash(tk)) % 50) / 10, 1),
            "mcap_B": round(8 + (abs(hash(tk)) % 400), 1),
            "dollar_vol_M": round(45 + (abs(hash(tk)) % 900), 1),
        })
        # 4주 궤적 — 현재 위치로 수렴하게
        trail[tk] = [[rs - schg * k * 0.9, 50 + schg * (4 - k) * 3] for k in (4, 3, 2, 1)]

    snap = se.build_snapshot(
        as_of=IDX[-1],
        leaders=pd.DataFrame(leaders),
        prices=prices,
        market={"m_gauge": "확장", "distribution_days": 2, "pct_above_200ma": 61.4,
                "note": "추세추종 가능 구간"},
        themes=themes,
        sp500={"NVDX", "VERT", "PTRA", "CLNS", "ORBT", "KRDG"},
        earnings=earn,
        trail=trail,
        fundamentals=fund,
    )
    return snap


if __name__ == "__main__":
    snap = build()
    blob = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
    with open("latest.json", "w", encoding="utf-8") as fh:
        fh.write(blob)
    print(f"{len(snap['tickers'])} tickers, {len(blob)/1024:.0f} KB")
    for t in snap["tickers"]:
        print(f"  {t['ticker']:5} {t['rs_quad']:3} rs={t['rs']:>3} "
              f"setup={str(t['setup']):8} pp={'Y' if t['power_play'] else '-'} "
              f"ud={t['ud_volume']} base={t['base_count']}")
