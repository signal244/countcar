"""Colab 에서 Drive 를 안전하게 다루는 도우미 (notebook_snippet.py 와 같은 방식).

- Drive 위 SQLite 는 느리고 disk I/O error 가 날 수 있어 /content 로 복사해 쓰고 backup API 로 되돌려 저장한다.
- 같은 교차로 DB 를 두 런타임이 동시에 쓰면 마지막 저장이 이겨 누락이 생겨 Drive 에 잠금 폴더를 만든다.
google.colab 에 의존하지 않아 로컬에서도 테스트할 수 있다(재마운트는 함수로 받는다).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional


def backup_sqlite(src: Path, dst: Path, retries: int = 5, sleep_sec: float = 1.0) -> bool:
    """SQLite backup API 로 일관된 스냅샷을 dst 에 저장한다(임시 파일에 쓴 뒤 교체)."""
    if not Path(src).exists():
        return False
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    for i in range(max(1, retries)):
        try:
            if tmp.exists():
                tmp.unlink()
            # sqlite3 의 with 는 커밋만 하고 닫지 않는다. 닫지 않으면 Windows 에서 replace 가 실패하고
            # 리눅스에서도 연결이 쌓인다.
            with closing(sqlite3.connect(str(src))) as s, closing(sqlite3.connect(str(tmp))) as d:
                s.backup(d)
            tmp.replace(dst)
            return True
        except Exception as exc:  # noqa: BLE001 — Drive 일시 오류는 재시도
            if i == retries - 1:
                print(f"[drive][warn] DB 저장 실패: {exc}", flush=True)
                return False
            time.sleep(sleep_sec)
    return False


def copy_file(src: Path, dst: Path, retries: int = 3, remount: Optional[Callable[[], None]] = None) -> None:
    """Drive -> 로컬 복사. 마운트가 끊기면 remount 후 재시도, sendfile 실패 시 스트리밍 복사."""
    last: Optional[Exception] = None
    for i in range(max(1, retries)):
        try:
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            if Path(dst).exists():
                Path(dst).unlink()
            try:
                shutil.copy2(src, dst)
            except OSError:
                with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst, length=16 * 1024 * 1024)
            return
        except OSError as exc:
            last = exc
            print(f"[drive][warn] 복사 실패 ({i + 1}/{retries}): {exc}", flush=True)
            if remount is not None:
                remount()
            time.sleep(2)
    raise RuntimeError(f"복사 실패: {src} -> {dst}: {last}")


def _touch(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(datetime.now()), encoding="utf-8")
    except OSError:
        pass


def acquire_lock(lock_dir: Path, timeout_sec: float = 6 * 3600, stale_sec: float = 3600,
                 poll_sec: float = 5.0) -> None:
    """mkdir 로 원자적으로 잠금을 잡는다. heartbeat 가 stale_sec 넘게 없으면 죽은 잠금으로 보고 지운다."""
    lock_dir = Path(lock_dir)
    hb, owner = lock_dir / "heartbeat.txt", lock_dir / "owner.txt"
    start = time.time()
    while True:
        try:
            lock_dir.parent.mkdir(parents=True, exist_ok=True)
            lock_dir.mkdir()
            owner.write_text(f"pid={os.getpid()} time={datetime.now()}\n", encoding="utf-8")
            _touch(hb)
            print(f"[lock] 획득: {lock_dir.name}", flush=True)
            return
        except FileExistsError:
            try:
                mtime = hb.stat().st_mtime if hb.exists() else lock_dir.stat().st_mtime
                if time.time() - mtime > stale_sec:
                    print(f"[lock][warn] 오래된 잠금 해제: {lock_dir.name}", flush=True)
                    release_lock(lock_dir)
                    continue
            except OSError:
                pass
            waited = time.time() - start
            if waited > timeout_sec:
                raise TimeoutError(f"잠금 대기 시간 초과: {lock_dir}")
            print(f"[lock] 다른 런타임이 같은 DB 를 쓰는 중... 대기 {int(waited)}초", flush=True)
            time.sleep(poll_sec)


def heartbeat(lock_dir: Path) -> None:
    _touch(Path(lock_dir) / "heartbeat.txt")


def release_lock(lock_dir: Path) -> None:
    lock_dir = Path(lock_dir)
    for name in ("heartbeat.txt", "owner.txt"):
        try:
            (lock_dir / name).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        lock_dir.rmdir()
    except OSError:
        pass
