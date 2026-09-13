"""CountCar v6.0 커스텀 위젯.

- WorkflowStepper: 워크플로우 진행 단계 표시기
- StatusBar: 하단 상태 표시줄 (모델/DB/GPU)
- section_divider: 구분선 + 라벨
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class WorkflowStepper(QWidget):
    """워크플로우 단계 표시기: ① 설정 → ② Colab → ③ 궤적 DB → …

    각 스텝은 done / active / pending 세 가지 상태를 가지며,
    QSS dynamic property ``stepState`` 로 스타일이 바뀐다.
    """

    def __init__(self, steps: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("stepper")
        layout = QHBoxLayout()
        layout.setContentsMargins(12, 8, 12, 8)
        self._circles: list[QLabel] = []
        self._labels: list[QLabel] = []
        self._lines: list[QFrame] = []

        for i, text in enumerate(steps):
            circle = QLabel(str(i + 1))
            circle.setFixedSize(24, 24)
            circle.setAlignment(Qt.AlignmentFlag.AlignCenter)
            circle.setObjectName("stepCircle")
            self._circles.append(circle)
            layout.addWidget(circle)

            lbl = QLabel(text)
            lbl.setObjectName("stepLabel")
            self._labels.append(lbl)
            layout.addWidget(lbl)

            if i < len(steps) - 1:
                line = QFrame()
                line.setFixedHeight(2)
                line.setFrameShape(QFrame.Shape.NoFrame)
                line.setSizePolicy(
                    QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed,
                )
                line.setObjectName("stepLine")
                self._lines.append(line)
                layout.addWidget(line)

        self.setLayout(layout)
        self.set_step(0)

    # -- public API --

    def set_step(self, index: int) -> None:
        """0-based 활성 단계를 설정한다. 이전 단계는 done 으로 표시."""
        for i, (circle, label) in enumerate(zip(self._circles, self._labels)):
            if i < index:
                state = "done"
                circle.setText("✓")
            elif i == index:
                state = "active"
                circle.setText(str(i + 1))
            else:
                state = "pending"
                circle.setText(str(i + 1))
            for w in (circle, label):
                w.setProperty("stepState", state)
                w.style().unpolish(w)
                w.style().polish(w)

        for i, line in enumerate(self._lines):
            state = "done" if i < index else "pending"
            line.setProperty("stepState", state)
            line.style().unpolish(line)
            line.style().polish(line)


class StatusBar(QWidget):
    """하단 상태 표시줄.

    ``add_item("model", "모델: best_v6.pt", "ok")`` 처럼 항목을 추가하고
    ``update_item("model", label="모델: yolo11.pt")`` 로 갱신한다.

    dotState: "ok" (green) / "warn" (orange) / "off" (gray)
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("statusBar")
        self._layout = QHBoxLayout()
        self._layout.setContentsMargins(14, 6, 14, 6)
        self._layout.setSpacing(16)
        self._items: dict[str, tuple[QLabel, QLabel]] = {}
        self.setLayout(self._layout)

    def add_item(self, key: str, label: str, state: str = "off") -> None:
        dot = QLabel("●")
        dot.setObjectName("statusDot")
        dot.setProperty("dotState", state)
        text = QLabel(label)
        text.setObjectName("statusText")
        pair = QHBoxLayout()
        pair.setSpacing(5)
        pair.addWidget(dot)
        pair.addWidget(text)
        self._layout.addLayout(pair)
        self._items[key] = (dot, text)

    def update_item(
        self, key: str, label: str | None = None, state: str | None = None,
    ) -> None:
        if key not in self._items:
            return
        dot, text = self._items[key]
        if label is not None:
            text.setText(label)
        if state is not None:
            dot.setProperty("dotState", state)
            dot.style().unpolish(dot)
            dot.style().polish(dot)


def section_divider(text: str) -> QWidget:
    """중앙 텍스트 + 양쪽 수평선 구분자를 만든다."""
    widget = QWidget()
    layout = QHBoxLayout()
    layout.setContentsMargins(0, 4, 0, 4)
    line_l = QFrame()
    line_l.setFrameShape(QFrame.Shape.HLine)
    line_l.setObjectName("dividerLine")
    lbl = QLabel(text)
    lbl.setObjectName("dividerText")
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    line_r = QFrame()
    line_r.setFrameShape(QFrame.Shape.HLine)
    line_r.setObjectName("dividerLine")
    layout.addWidget(line_l, 1)
    layout.addWidget(lbl)
    layout.addWidget(line_r, 1)
    widget.setLayout(layout)
    return widget
