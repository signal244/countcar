# Git 동기화 명령어 가이드

## 이 프로젝트의 구조

작업 환경이 **두 곳**이고, GitHub 레포가 둘 사이의 중계소 역할을 한다.

```
   [A] 로컬 PC (VS Code)              [B] 다른 환경의 Claude
   G:\내 드라이브\vm\count_car_ver6.0        GitHub 레포로 직접 작업
   커밋 작성자: signal244                커밋 작성자: Claude
              \                          /
               \                        /
                >  GitHub: signal244/countcar  <
                   브랜치: claude/unknown-session-l2oio4
```

**중요**: VS Code 안의 Claude Code는 [A]와 같은 폴더에서 작업한다.
즉 VS Code의 Claude가 파일을 고치면 **그 즉시 내 디스크에 반영**된다.
그 변경을 받으려고 `pull` 할 필요가 없다. (예전 가이드의 오해)

`pull` 이 필요한 건 오직 **[B]가 GitHub에 푸시했을 때** 뿐이다.

---

## 브랜치명

```
claude/unknown-session-l2oio4
```

---

## 1. 내 작업 올리기 (A → GitHub)

VS Code에서 작업이 끝났으면:

```powershell
git add -A
git status              # 무엇이 올라가는지 반드시 눈으로 확인
git commit -m "수정 내용 설명"
git push origin claude/unknown-session-l2oio4
```

---

## 2. [B]가 한 작업 받기 (GitHub → A)

```powershell
git status              # 먼저 내 작업이 남아있는지 확인
git pull origin claude/unknown-session-l2oio4
```

### `git status` 에 수정된 파일이 있다면

**`git stash` 를 쓰지 말 것.** 작업을 잃기 쉽다.
대신 **먼저 커밋하고 pull** 한다:

```powershell
git add -A
git commit -m "작업 중간 저장"
git pull origin claude/unknown-session-l2oio4
```

같은 파일의 같은 줄을 양쪽에서 고쳤을 때만 충돌이 난다.
충돌 나면 파일 안의 `<<<<<<<` / `=======` / `>>>>>>>` 부분을 정리하고:

```powershell
git add -A
git commit
```

---

## 3. 현재 상태 확인

```powershell
git status                                  # 작업 중인 변경
git log --oneline -5                        # 최근 커밋
git log --pretty='%h %an %s' -5             # 누가 만든 커밋인지 (signal244 / Claude)
git diff --stat                             # 변경 분량 요약
```

---

## 알아둘 것

### "수정됨"으로 보이는데 실제 변경이 없는 파일

`core.autocrlf=true` + 구글드라이브 동기화 때문에 줄바꿈(CRLF/LF)만
달라져서 `git status` 에 `M` 으로 뜨는 경우가 있다. 확인 방법:

```powershell
git diff --numstat 파일경로     # 아무것도 안 나오면 실제 변경 없음
```

`git add -A` 하면 이런 파일은 알아서 스테이징에서 빠진다. 무시해도 된다.

### .git 폴더가 구글드라이브 안에 있다

동기화 중에 git 명령을 실행하면 드물게 레포가 깨질 수 있다.
큰 작업 전에는 드라이브 동기화가 끝난 상태인지 확인하는 게 안전하다.

### 실수했을 때

```powershell
git reset HEAD~1            # 마지막 커밋만 취소 (파일 내용은 그대로 유지)
git checkout -- 파일경로     # 특정 파일의 수정을 버리고 마지막 커밋 상태로
git reflog                  # 잃어버린 커밋 찾기 (거의 다 복구 가능)
```
