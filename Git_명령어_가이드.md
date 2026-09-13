# Git 동기화 명령어 가이드

## 브랜치명
```
claude/unknown-session-l2oio4
```

---

## 클로드 변경사항 받기 (클로드 → 로컬)

클로드가 작업 완료했다고 하면 VS Code 터미널에서:
```powershell
git pull origin claude/unknown-session-l2oio4
```

충돌 나면:
```powershell
git stash
git pull origin claude/unknown-session-l2oio4
git stash pop
```

---

## 내 변경사항 보내기 (로컬 → 클로드)

로컬에서 수정한 걸 클로드에게 보낼 때:
```powershell
git add -A
git commit -m "수정 내용 설명"
git push origin claude/unknown-session-l2oio4
```

푸시 후 클로드에게 **"pull 해줘"** 라고 말하면 됨.

---

## 현재 상태 확인
```powershell
git status
git log --oneline -5
```
