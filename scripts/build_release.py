"""다른 PC 에 설치할 배포판 zip 을 만든다.

    python scripts/build_release.py
    python scripts/build_release.py --models models/best.pt models/yolo26n_v1.pt

- 프로그램 파일: git 이 관리하는 파일(커밋 전 변경·새 파일 포함, .gitignore 제외)만 담는다.
  개발용 파일(tests, .claude, 개발 문서)은 뺀다.
- release/ 의 설치·실행 배치파일과 설치_및_사용법.md 를 최상위에, release/sample 을 sample/ 에 둔다.
- --models 로 고른 .pt 와, 옆에 있는 <이름>_int8_openvino_model 폴더를 함께 넣는다.
- 샘플 영상, tools/uv.exe(설치 도구)도 넣는다.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_PREFIXES = ("tests/", ".github/", ".claude/", "release/")
EXCLUDE_FILES = {
    "AGENTS.md", "CLAUDE.md", "README.md", "Git_명령어_가이드.md", "countcar.bat",
    ".gitignore", ".gitattributes", ".editorconfig",
}
SAMPLE_VIDEO = Path(r"G:\내 드라이브\video\AB테스트\경원교사거리_오후첨두_10-35분.mp4")
DEFAULT_OUT = Path(r"G:\내 드라이브\vm\count_car_release")
STORED = {".mp4", ".pt", ".onnx", ".bin", ".exe", ".zip"}


def program_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout.decode("utf-8")
    files = []
    for rel in filter(None, out.split("\0")):
        if rel.startswith(EXCLUDE_PREFIXES) or rel in EXCLUDE_FILES or rel.startswith("REVIEW_"):
            continue
        if (ROOT / rel).is_file():
            files.append(rel)
    return files


def version_text() -> str:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout.strip()

    # status 는 줄바꿈만 다른(구글드라이브 동기화) 파일도 수정으로 보여 numstat 으로 실제 차이만 본다.
    dirty = "있음(커밋 전 변경 포함)" if git("diff", "HEAD", "--numstat") else "없음"
    return (
        f"Count Car 배포판\n만든 날짜: {datetime.now():%Y-%m-%d %H:%M}\n"
        f"기준 커밋: {git('log', '-1', '--format=%h %s')}\n커밋 이후 변경: {dirty}\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="*", default=["models/best.pt"], help="넣을 .pt 모델")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--no-sample-video", action="store_true")
    args = ap.parse_args()

    entries: list[tuple[Path, str]] = [(ROOT / rel, rel) for rel in program_files()]

    release = ROOT / "release"
    for item in release.iterdir():
        if item.is_file():
            entries.append((item, item.name))
    for item in (release / "sample").iterdir():
        entries.append((item, f"sample/{item.name}"))
    if not args.no_sample_video:
        if not SAMPLE_VIDEO.is_file():
            raise SystemExit(f"샘플 영상이 없습니다: {SAMPLE_VIDEO}")
        entries.append((SAMPLE_VIDEO, f"sample/{SAMPLE_VIDEO.name}"))

    for model in [*args.models, "models/yolo26n-reid.onnx"]:
        pt = (ROOT / model).resolve()
        if not pt.is_file():
            raise SystemExit(f"모델이 없습니다: {pt}")
        entries.append((pt, f"models/{pt.name}"))
        for suffix in ("_int8_openvino_model", "_openvino_model"):
            ov = pt.with_name(pt.stem + suffix)
            if ov.is_dir():
                entries += [(f, f"models/{ov.name}/{f.relative_to(ov).as_posix()}") for f in ov.rglob("*") if f.is_file()]

    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv.exe 를 찾지 못했습니다. 이 PC 에 uv 를 설치하세요.")
    entries.append((Path(uv), "tools/uv.exe"))

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"CountCar_{datetime.now():%Y%m%d_%H%M}.zip"
    tmp = out.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w") as zf:
        zf.writestr("CountCar/VERSION.txt", version_text())
        for src, rel in entries:
            arc = f"CountCar/{rel}"
            if src.suffix.lower() == ".bat":
                # 배치파일은 CRLF 여야 goto/라벨이 안전하다.
                text = src.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                zf.writestr(arc, text, compress_type=zipfile.ZIP_DEFLATED)
            else:
                method = zipfile.ZIP_STORED if src.suffix.lower() in STORED else zipfile.ZIP_DEFLATED
                zf.write(src, arc, compress_type=method)
    tmp.replace(out)
    size = out.stat().st_size / 1e6
    print(f"배포판: {out}  ({len(entries)}개 파일, {size:.0f}MB)")
    print("모델:", ", ".join(sorted({rel for _s, rel in entries if rel.startswith('models/') and rel.count('/') == 1})))


if __name__ == "__main__":
    main()
