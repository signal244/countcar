"""소스 인코딩 회귀 테스트 - 한글 깨짐(mojibake) 방지.

이 프로젝트 소스는 UTF-8 이다. 한국 Windows 의 기본 코드페이지는 CP949 라서,
에디터가 UTF-8 한글을 CP949 로 잘못 열어 저장하면 한글이 깨진다. 최초
커밋(6ef9d26)부터 문자열 여러 곳이 이 문제로 깨져 있었고 나중에 손으로
복원해야 했다(8540f62). .editorconfig 로 예방하되, 재발을 자동으로 잡기 위해
아래 세 가지를 검사한다.

  1. 모든 소스가 strict UTF-8 로 디코딩된다 (바이트 손상 없음).
  2. U+FFFD(대체 문자)가 없다 (디코딩 중 유실된 바이트 흔적).
  3. CJK 한자(U+4E00-U+9FFF)가 없다 - 이 프로젝트엔 정당한 한자가 없고,
     UTF-8 -> CP949 오해독 mojibake 는 U+FFFD 없이 한자 영역으로 자주 새기
     때문에, 유효 유니코드로 깨진 경우까지 잡는다.

주의: 이 파일 자신이 검사 대상이다. 경계 문자(한자/U+FFFD)를 리터럴로 쓰면
스스로를 오탐하므로, 아래 상수는 chr() 로만 정의한다.
"""

import unittest
from pathlib import Path

# 검사 경계 문자 - 리터럴을 피해 코드포인트로 정의(이 파일 자기 오탐 방지).
_REPLACEMENT_CHAR = chr(0xFFFD)   # U+FFFD 대체 문자
_CJK_START = chr(0x4E00)          # CJK 통합 한자 시작
_CJK_END = chr(0x9FFF)            # CJK 통합 한자 끝

# tests/ 의 부모가 프로젝트 루트.
ROOT = Path(__file__).resolve().parent.parent

# 텍스트로 검사할 확장자.
TEXT_SUFFIXES = {".py", ".md", ".json", ".txt", ".cfg", ".ini", ".toml"}

# 검사에서 제외할 디렉터리.
EXCLUDE_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules"}

# 검사 대상 소스 디렉터리. 프로젝트 전체 rglob 은 구글드라이브 위라 느리고
# 모델/DB 등 무관한 트리까지 훑으므로, 우리가 작성하는 소스만 스캔한다.
SCAN_DIRS = ["src", "tests", "config", "colab"]


def _iter_text_files():
    roots = [ROOT / d for d in SCAN_DIRS if (ROOT / d).is_dir()]
    # 루트 바로 아래의 텍스트 파일(CLAUDE.md, README 등)도 포함한다.
    top_level = [p for p in ROOT.iterdir() if p.is_file()]
    for base in roots:
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if any(part in EXCLUDE_DIRS for part in path.relative_to(ROOT).parts):
                continue
            yield path
    for path in top_level:
        if path.suffix.lower() in TEXT_SUFFIXES:
            yield path


class SourceEncodingTests(unittest.TestCase):
    def test_all_text_files_are_valid_utf8(self) -> None:
        bad = []
        for path in _iter_text_files():
            try:
                path.read_bytes().decode("utf-8")
            except UnicodeDecodeError as exc:
                bad.append(f"{path.relative_to(ROOT)}: {exc}")
        self.assertFalse(
            bad,
            "UTF-8 로 디코딩되지 않는 파일이 있습니다(인코딩 깨짐):\n"
            + "\n".join(bad),
        )

    def test_no_replacement_character(self) -> None:
        bad = []
        for path in _iter_text_files():
            text = path.read_bytes().decode("utf-8", errors="replace")
            if _REPLACEMENT_CHAR in text:
                bad.append(str(path.relative_to(ROOT)))
        self.assertFalse(
            bad,
            "U+FFFD(대체 문자)가 포함된 파일 - 바이트가 유실된 깨짐입니다:\n"
            + "\n".join(bad),
        )

    def test_no_cjk_ideographs(self) -> None:
        """한자 출현은 이 프로젝트에선 mojibake 신호다."""
        bad = []
        for path in _iter_text_files():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # 위 테스트가 별도로 잡는다.
            for lineno, line in enumerate(text.splitlines(), 1):
                cjk = [ch for ch in line if _CJK_START <= ch <= _CJK_END]
                if cjk:
                    bad.append(
                        f"{path.relative_to(ROOT)}:{lineno} "
                        f"한자 {cjk[:5]} (mojibake 의심): {line.strip()[:60]}"
                    )
        self.assertFalse(
            bad,
            "한자가 발견되었습니다 - UTF-8 한글이 CP949 로 깨졌을 가능성이 큽니다:\n"
            + "\n".join(bad),
        )


if __name__ == "__main__":
    unittest.main()
