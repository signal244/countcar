---
name: git-sync
description: 이 프로젝트의 GitHub 동기화를 안전하게 처리한다. "커밋해줘",
  "푸시해줘", "동기화해줘", "pull 해줘", "GitHub에 올려줘", 충돌 처리,
  "변경사항이 사라진 것 같다" 같은 요청에 사용한다. 구글드라이브 위의
  레포라서 유령 수정 파일과 stale lock 이 자주 생기며, 그 판별과 처리
  절차가 여기 담겨 있다.
---

# Git 동기화 (count_car_ver6.0)

브랜치: `claude/unknown-session-l2oio4`
리모트: `origin` = https://github.com/signal244/countcar.git

작업 환경이 두 곳이다. 이 폴더(구글드라이브, 커밋 작성자 `signal244`)와
다른 환경의 Claude(커밋 작성자 `Claude <noreply@anthropic.com>`)가 같은
브랜치를 공유한다. VS Code 안의 Claude Code 는 이 폴더에서 직접 작업하므로
자기 변경을 받으려고 pull 할 필요가 없다.

## 절대 규칙

- **`git stash` 금지.** pull 전에 변경이 남아 있으면 stash 가 아니라 먼저 커밋한다.
- **push 는 사용자 확인을 받고 실행한다.** 확인 없이 푸시하지 않는다.
- **`.git` 내부 파일 삭제는 사용자 확인을 받는다.** (lock 처리 포함)
- 커밋 메시지는 한국어로, 무엇을 왜 바꿨는지 쓴다.

---

## 1. 상태 점검 (항상 여기서 시작)

```bash
git status --short
git log --oneline -5
git status -sb | head -2        # 원격과 몇 커밋 차이인지
```

`M` 으로 뜬 파일이 있으면 **바로 커밋하지 말고 2번으로 간다.**

## 2. 유령 수정 파일 걸러내기

`core.autocrlf=true` + 구글드라이브 동기화 때문에 **내용 변경이 0인데도**
`M` 으로 뜨는 파일이 흔하다. 줄바꿈(CRLF/LF) 차이일 뿐이다.

```bash
git diff --stat                 # 여기 안 나오는 M 파일 = 유령
git diff --numstat <파일>        # 출력이 비면 실제 변경 없음
```

- `git add -A` 하면 유령 파일은 스테이징에서 자동으로 빠진다. 정상이다.
- **사용자에게 "변경이 유실된 것 같다"고 보고하기 전에 반드시 이것부터 확인한다.**
  실제로 한 번 오진한 적이 있다.

## 3. 커밋

```bash
git add -A
git status --short              # 무엇이 올라가는지 확인 후 진행
git commit -F - <<'EOF'
제목: 무엇을 바꿨는지 한 줄

- 세부 변경 1
- 세부 변경 2

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

내 작업과 무관한 변경이 섞여 있으면 사용자에게 알리고, 필요하면 나눠서 커밋한다.

## 4. 푸시 (사용자 확인 후)

```bash
git push origin claude/unknown-session-l2oio4
git status -sb | head -2        # 원격과 일치하는지 확인
```

## 5. Pull — 다른 환경이 푸시했을 때만

```bash
git status --short              # 먼저 로컬 변경 확인
```

변경이 남아 있으면 **커밋부터** 한 뒤(3번) pull 한다.

```bash
git pull origin claude/unknown-session-l2oio4
```

충돌이 나면 `<<<<<<<` / `=======` / `>>>>>>>` 를 정리하고 `git add -A && git commit`.

---

## 문제 해결

### `cannot lock ref 'HEAD': ... HEAD.lock: File exists`

구글드라이브가 `.git` 파일을 붙잡고 있어서 생기는 stale lock 이다. 자주 난다.

```bash
ls -la .git/*.lock              # 생성 시각과 크기 확인 (보통 0 bytes)
tasklist | grep -i "^git"       # 실제 git 프로세스 확인
```

몇 분 이상 0바이트로 남아 있으면 stale 이다. **사용자 확인을 받고** 지운다:

```powershell
Remove-Item "G:\내 드라이브\vm\count_car_ver6.0\.git\HEAD.lock"
```

거부당하면(파일 사용 중) VS Code 를 완전히 종료하고 드라이브 동기화가
끝난 뒤 별도 PowerShell 에서 재시도하도록 안내한다.

### 커밋을 되돌리고 싶을 때

```bash
git reset HEAD~1                # 마지막 커밋 취소, 파일 내용은 유지
git checkout -- <파일>           # 특정 파일의 수정만 버림
git reflog                      # 잃어버린 커밋 찾기 (대부분 복구 가능)
```

### 커밋이 어느 환경 것인지

```bash
git log --pretty='%h %an %s' -8
```

`signal244` = 이 폴더, `Claude` = 다른 환경.

---

## 커밋하면 안 되는 것

`models/` 의 모델 파일, 결과 DB, Excel 산출물은 용량이 크거나 현장
데이터일 수 있다. 스테이징에 들어오면 사용자에게 확인한다.
