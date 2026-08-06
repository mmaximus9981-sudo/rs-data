# 데이터를 인터넷에 올리기 — 클릭 단위 안내

목표: `latest.json` 이 주소를 갖고, 앱이 그 주소에서 데이터를 받아오게 만든다.
30분이면 끝난다. 돈은 들지 않는다.

---

## 1단계 · 깃허브 계정 (5분)

[github.com](https://github.com) → **Sign up** → 이메일·비밀번호·아이디 입력 → 이메일 인증.

아이디는 주소에 그대로 들어간다. `haejun` 으로 만들면 데이터 주소가
`https://haejun.github.io/...` 가 된다. 나중에 못 바꾸니 한 번 생각하고 정할 것.

## 2단계 · 저장소 만들기 (2분)

오른쪽 위 **+** → **New repository**

| 항목 | 값 |
|---|---|
| Repository name | `rs-data` |
| Public / Private | **Public** ← 반드시 |
| Add a README file | 체크 |

**Create repository** 를 누른다.

> Private 으로 만들면 앱이 데이터를 못 읽는다. 올라가는 건 주가 스크리닝 결과뿐이니
> 공개해도 문제될 게 없다. 토큰이나 개인정보는 여기 올리지 않는다.

## 3단계 · Pages 켜기 (3분)

저장소 안에서 **Settings** → 왼쪽 메뉴 **Pages**

- Source: **Deploy from a branch**
- Branch: **main** / **/ (root)** → **Save**

1~2분 뒤 새로고침하면 위쪽에 주소가 뜬다. 이게 데이터 주소의 뿌리다.

```
https://내아이디.github.io/rs-data/
```

## 4단계 · 토큰 발급 (5분)

파이썬이 대신 파일을 올리려면 열쇠가 필요하다.

[github.com/settings/tokens](https://github.com/settings/tokens)
→ **Fine-grained tokens** 탭 → **Generate new token**

| 항목 | 값 |
|---|---|
| Token name | `rs-app` |
| Expiration | 1년 |
| Repository access | **Only select repositories** → `rs-data` |
| Permissions → Repository permissions → **Contents** | **Read and write** |

**Generate token** → `github_pat_...` 로 시작하는 문자열이 뜬다.

> **이 화면을 벗어나면 다시 볼 수 없다.** 메모장에 복사해 두고,
> 절대 저장소에 올리거나 남에게 보여주지 말 것. 유출되면 즉시 Revoke.

## 5단계 · 올리기 (2분)

`latest.json` 이 있는 폴더에서:

```
python publish_github.py --repo 내아이디/rs-data --token github_pat_여기에붙여넣기
```

성공하면 앱에 넣을 주소가 출력된다.

```
https://내아이디.github.io/rs-data/latest.json
```

브라우저 주소창에 붙여넣어 JSON 이 보이면 성공이다.
Pages 반영에 1~2분 걸리니 404 가 나오면 잠깐 기다렸다 다시 열어본다.

## 6단계 · 앱에 주소 넣기 (2분)

`app_template.html` 을 메모장으로 열고 위쪽의 이 줄을 찾는다.

```javascript
const SNAPSHOT_URL = "";
```

주소를 넣는다.

```javascript
const SNAPSHOT_URL = "https://내아이디.github.io/rs-data/latest.json";
```

저장하고 다시 빌드한다.

```
python run_sp500.py --skip-fetch
```

또는 `run_sp500.py` 를 돌릴 것도 없이 이 한 줄로도 된다.

```
python -c "t=open('app_template.html',encoding='utf-8').read();open('app.html','w',encoding='utf-8').write(t.replace('/*__DATA__*/',open('latest.json',encoding='utf-8').read()))"
```

## 7단계 · 확인

`app.html` 을 열고 오른쪽 위 기준일을 본다.

- **날짜만 보이면** 원격에서 받아온 것 — 성공
- **`· 내장` 이 붙어 있으면** 원격을 못 받아 파일에 박힌 데이터로 뜬 것

기준일을 탭하면 다시 받아온다.

---

## 이제부터의 일상

```
python run_sp500.py                                    # 데이터 갱신
python publish_github.py --repo 내아이디/rs-data --token ...   # 올리기
```

두 줄이면 폰에 있는 앱까지 최신이 된다. 앱을 다시 설치할 필요가 없다.
**이게 이번 작업의 핵심이다** — 데이터와 앱이 분리됐다.

토큰을 매번 치기 싫으면 환경변수로 둔다.

```
setx GH_TOKEN github_pat_...     (윈도우, 한 번만. 새 창부터 적용)
python publish_github.py --repo 내아이디/rs-data
```

## 막힐 때

**`토큰이 거부됐습니다`**
토큰을 잘못 복사했거나 만료됐다. 4단계를 다시.

**`권한이 없습니다`**
토큰의 Repository access 에 `rs-data` 가 빠졌거나 Contents 권한이 Read 만 있다.

**주소를 열면 404**
Pages 반영 전이거나 3단계를 안 했다. 1~2분 기다리거나
`https://raw.githubusercontent.com/내아이디/rs-data/main/latest.json` 을 대신 쓴다.

**앱에 `· 내장` 이 계속 붙는다**
주소 오타이거나 `app.html` 을 다시 빌드하지 않은 것이다.
브라우저 F12 → Console 에 붉은 글씨가 있으면 그게 단서다.
