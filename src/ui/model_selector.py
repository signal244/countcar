"""models 폴더를 자동으로 읽어 보여주는 모델 선택 콤보박스.

목록을 펼칠 때마다 폴더를 다시 훑기 때문에, 새 모델 파일을 넣으면
프로그램을 다시 켜지 않아도 바로 나타난다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QComboBox, QFileDialog, QWidget

from src.config.model_profiles import list_model_files

BROWSE_SENTINEL = "__browse__"
BROWSE_LABEL = "직접 선택…"


class ModelComboBox(QComboBox):
    """models 폴더의 모델 + '직접 선택…' 항목을 제공한다."""

    def __init__(self, models_dir: str | Path = "models", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.models_dir = Path(models_dir)
        self._external_path: str = ""
        self.refresh()

    # ------------------------------------------------------------------ 목록

    def showPopup(self) -> None:  # noqa: N802 (Qt 명명 규칙)
        """펼칠 때마다 폴더를 다시 스캔한다."""
        current = self.current_path()
        self.refresh()
        if current:
            self.set_path(current)
        super().showPopup()

    def refresh(self) -> None:
        """모델 목록을 다시 만든다. 선택값은 호출한 쪽에서 복원한다."""
        was_blocked = self.blockSignals(True)
        try:
            self.clear()
            for model_file in list_model_files(self.models_dir):
                self.addItem(model_file.name, str(model_file))
            if self._external_path:
                self.addItem(f"{Path(self._external_path).name}  (외부)", self._external_path)
            if self.count():
                self.insertSeparator(self.count())
            self.addItem(BROWSE_LABEL, BROWSE_SENTINEL)
        finally:
            self.blockSignals(was_blocked)

    # ------------------------------------------------------------------ 선택값

    def current_path(self) -> str:
        """현재 선택된 모델의 경로. '직접 선택…'이면 빈 문자열."""
        data = self.currentData()
        if not data or data == BROWSE_SENTINEL:
            return ""
        return str(data)

    def set_path(self, model_path: str | Path) -> None:
        """경로를 선택 상태로 만든다. 목록에 없으면 '외부' 항목으로 추가한다."""
        raw = str(model_path or "").strip()
        if not raw:
            return
        index = self.findData(raw)
        if index < 0:
            # 같은 파일을 다른 표기로 가리키는 경우도 맞춰 본다.
            resolved = str(Path(raw).resolve()) if Path(raw).exists() else raw
            for i in range(self.count()):
                data = self.itemData(i)
                if not data or data == BROWSE_SENTINEL:
                    continue
                try:
                    if str(Path(str(data)).resolve()) == resolved:
                        index = i
                        break
                except OSError:
                    continue
        if index < 0:
            self._external_path = raw
            self.refresh()
            index = self.findData(raw)
        if index >= 0:
            self.setCurrentIndex(index)

    def is_browse_selected(self) -> bool:
        return self.currentData() == BROWSE_SENTINEL

    # ------------------------------------------------------------------ 파일 선택

    def prompt_for_model(self, parent: Optional[QWidget] = None) -> str:
        """파일 대화상자로 models 폴더 밖의 모델을 고른다."""
        start_dir = self.models_dir if self.models_dir.is_dir() else Path.cwd()
        path, _ = QFileDialog.getOpenFileName(
            parent or self,
            "YOLO 모델 선택",
            str(start_dir.resolve()),
            "Model Files (*.pt *.onnx *.engine);;All Files (*)",
        )
        if path:
            self.set_path(path)
        return path
