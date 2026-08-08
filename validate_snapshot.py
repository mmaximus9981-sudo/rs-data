"""
validate_snapshot.py — 스냅샷이 쓸 만한지 확인한다. 자동 실행의 안전핀.

사람이 안 보는 자동 파이프라인에서 제일 위험한 건 '실패'가 아니라
'조용히 망가진 결과가 배포되는 것'이다. 야후가 절반만 응답해도
run_sp500.py 는 성공으로 끝나고, 종목 3개짜리 스냅샷이 폰까지 간다.

그래서 배포 직전에 여기서 막는다. 통과 못 하면 종료 코드 1 을 내고,
워크플로는 커밋하지 않는다. 폰에는 어제 데이터가 그대로 남는다.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

MIN_TICKERS = 20          # RS 상위 종목이 이보다 적으면 가격 행렬이 깨진 것
MAX_AGE_DAYS = 6          # 연휴를 감안한 상한
REQUIRED = ("ticker", "rs", "rs_quad", "rs_x", "rs_y", "price")


def fail(msg: str) -> None:
    print(f"검증 실패: {msg}")
    sys.exit(1)


def main(path: str = "latest.json") -> None:
    try:
        snap = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        fail(f"{path} 을 읽지 못했습니다 — {e}")

    meta, tickers = snap.get("meta", {}), snap.get("tickers", [])

    if len(tickers) < MIN_TICKERS:
        fail(f"종목이 {len(tickers)}개뿐입니다 (최소 {MIN_TICKERS})")

    try:
        as_of = datetime.strptime(meta["as_of"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        fail(f"기준일이 이상합니다 — {meta.get('as_of')!r}")

    age = (datetime.now(timezone.utc) - as_of).days
    if age > MAX_AGE_DAYS:
        fail(f"기준일이 {age}일 전입니다 — 가격을 못 받은 듯합니다")
    if age < 0:
        fail(f"기준일이 미래입니다 — {meta['as_of']}")

    missing = [k for k in REQUIRED if k not in tickers[0]]
    if missing:
        fail(f"필드 누락 — {missing}")

    with_series = sum(1 for t in tickers if t.get("series"))
    if with_series < len(tickers) * 0.8:
        fail(f"차트 시리즈가 {with_series}/{len(tickers)} 종목에만 있습니다")

    # 봉 수가 줄면 차트의 기간 조절이 조용히 1년짜리로 쪼그라든다. 눈에 띄지 않는
    # 고장이라 여기서 막는다. 상장한 지 얼마 안 된 종목이 섞이므로 중앙값으로 본다.
    lens = sorted(len((t.get("series") or {}).get("c") or [])
                  for t in tickers if t.get("series"))
    if lens and lens[len(lens) // 2] < 900:
        fail(f"일봉이 중앙값 {lens[len(lens)//2]}봉뿐입니다 "
             f"(5년이면 약 1260봉) — 수집 기간을 확인하세요")

    quads = {t["rs_quad"] for t in tickers if t.get("rs_quad")}
    if len(quads) < 2:
        fail(f"4분면이 한쪽으로 쏠렸습니다 — {quads}")

    setups = sum(1 for t in tickers if t.get("setup"))
    size_mb = len(json.dumps(snap).encode()) / 1e6

    print(f"검증 통과 · {meta['as_of']} · {len(tickers)}종목 · "
          f"패턴 {setups} · 분면 {len(quads)}종 · {size_mb:.1f} MB")
    if size_mb > 20:
        print(f"경고: 스냅샷이 {size_mb:.0f} MB 입니다. 앱 첫 로딩이 느려집니다.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "latest.json")
