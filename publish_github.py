"""
publish_github.py — latest.json 을 깃허브에 올린다. 앱은 그 주소에서 받아간다.

깃(git) 명령을 쓰지 않는다. 깃허브 API 로 파일 하나만 덮어쓰는 방식이라
설치할 것도, 배울 것도 없다. 표준 라이브러리만 쓴다.

준비물 두 가지
  1. 공개 저장소 (예: 내계정/rs-data)
  2. 개인 액세스 토큰 — github.com/settings/tokens 에서 발급
     "Fine-grained token" → 해당 저장소만 선택 → Contents: Read and write

쓰는 법
    python publish_github.py --repo 내계정/rs-data --token ghp_xxxx

토큰을 매번 치기 싫으면 환경변수로 둔다
    set GH_TOKEN=ghp_xxxx        (윈도우)
    python publish_github.py --repo 내계정/rs-data
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"


def _req(url: str, token: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "rs-dynamics-publisher",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return e.code, {"message": raw.decode(errors="replace")[:300]}


def publish(repo: str, token: str, local: str = "latest.json",
            remote: str = "latest.json", branch: str = "main") -> str:
    if not os.path.exists(local):
        sys.exit(f"'{local}' 이 없습니다. run_sp500.py 를 먼저 돌리세요.")

    blob = open(local, "rb").read()
    url = f"{API}/repos/{repo}/contents/{remote}"

    # 이미 있는 파일이면 sha 를 넘겨야 덮어쓸 수 있다
    status, cur = _req(f"{url}?ref={branch}", token)
    sha = cur.get("sha") if status == 200 else None
    if status == 404 and "Not Found" in str(cur.get("message", "")):
        sha = None
    elif status == 401:
        sys.exit("토큰이 거부됐습니다. 새로 발급하고 Contents 권한을 확인하세요.")
    elif status == 403:
        sys.exit("권한이 없습니다. 토큰의 저장소 접근 범위를 확인하세요.")

    payload = {
        "message": f"snapshot {json.loads(blob)['meta']['as_of']}",
        "content": base64.b64encode(blob).decode(),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    status, res = _req(url, token, "PUT", payload)
    if status not in (200, 201):
        sys.exit(f"업로드 실패 ({status}): {res.get('message')}")

    owner, name = repo.split("/", 1)
    pages = f"https://{owner}.github.io/{name}/{remote}"
    raw = f"https://raw.githubusercontent.com/{repo}/{branch}/{remote}"
    size_kb = len(blob) / 1024

    print(f"업로드 완료 · {size_kb:.0f} KB")
    print(f"\n앱에 넣을 주소 (Pages 켠 경우):\n  {pages}")
    print(f"\nPages 를 아직 안 켰다면 이 주소도 됩니다:\n  {raw}")
    print("\napp_template.html 의 SNAPSHOT_URL 에 붙여넣고 다시 빌드하세요.")
    return pages


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="계정/저장소 (예: haejun/rs-data)")
    ap.add_argument("--token", default=os.environ.get("GH_TOKEN", ""))
    ap.add_argument("--file", default="latest.json")
    ap.add_argument("--branch", default="main")
    a = ap.parse_args()

    if not a.token:
        sys.exit("토큰이 없습니다. --token 으로 주거나 GH_TOKEN 환경변수를 설정하세요.")
    publish(a.repo, a.token, local=a.file, branch=a.branch)


if __name__ == "__main__":
    main()
