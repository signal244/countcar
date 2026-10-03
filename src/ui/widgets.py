"""CountCar v6.0 커스텀 위젯.

- WorkflowStepper: 워크플로우 진행 단계 표시기
- StatusBar: 하단 상태 표시줄 (모델/DB/GPU)
- section_divider: 구분선 + 라벨
- wrap_in_scroll: 내용이 창보다 클 때만 스크롤바를 보여주는 래퍼
- fit_to_screen: 초기 창 크기를 사용 가능한 화면 영역 안으로 제한
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


TOOLTIPS = {
    '찾기': '옆 입력란에 표시된 영상·DB·분석라인 파일을 선택하는 창을 엽니다.',
    'videoBrowseButton': '분석할 원본 영상을 선택합니다. 파일명에 교차로명과 세션명이 있으면 해당 입력란도 자동으로 채웁니다.',
    'detectDbBrowseButton': '탐지·추적 궤적을 기록할 SQLite DB 파일의 위치를 선택합니다. 교차로별 DB를 나누어 관리할 때 사용하세요.',
    'countDbBrowseButton': '카운팅할 궤적이 저장된 SQLite DB를 선택합니다. 감지 단계에서 만든 DB를 지정하세요.',
    'countLinesBrowseButton': '교차 여부와 방향 판정에 사용할 분석라인 JSON을 선택합니다. 분석 영상에 맞는 라인 파일을 사용하세요.',
    '탐지 차종 선택': '현재 모델의 클래스 중 새 감지 작업에서 사용할 차종을 고릅니다. 기존 DB 데이터에는 영향을 주지 않습니다.',
    '경로 설정': '탐지·추적 궤적을 기록할 SQLite DB 파일 경로를 지정합니다.',
    '폴더 선택': '집계 결과 Excel·CSV 파일을 저장할 폴더를 선택합니다.',
    '목록 갱신': '선택된 DB에서 session_id 목록을 다시 읽습니다. DB 경로를 바꾼 뒤 사용하세요.',
    '세션 삭제': '선택한 session_id의 DB 저장 데이터를 삭제합니다. 삭제 후 복구하기 어려우므로 세션명을 확인하세요.',
    '집계 차종 설정': 'DB 차종을 Excel 집계 열에 연결·제외하고 결과 열 순서를 정합니다. 원본 DB 차종명은 바뀌지 않습니다.',
    '📋 Colab 코드 생성': '현재 영상·모델·분석라인·DB 설정을 Colab용 실행 셀로 변환해 클립보드에 복사합니다. 로컬 경로 접근 안내도 확인하세요.',
    '💾 설정 저장': '현재 분석 설정과 경로를 사용자 상태 파일에 저장해 다음 실행 때 다시 불러옵니다.',
    '🔍 차종 미리보기': '영상을 선택하면 현재 모델로 프레임별 차종 탐지를 보여줍니다. 창에서 일시정지·재생할 수 있고 결과 영상 저장을 켜면 프로젝트 폴더의 output_result.mp4에 기록합니다.',
    '👁 궤적보기/수정': '선택한 DB 궤적을 영상·분석라인 위에서 검토하고 병합·외삽·수동 보정을 합니다. 후처리 결과는 해당 DB에 저장됩니다.',
    '📐 교차로 카운팅': 'DB 궤적과 분석라인을 기준으로 교차로 회전 방향별 교통량을 집계하고 Excel/CSV를 만듭니다.',
    '📐 접근로 카운팅': '분석라인 진입 방향별 교통량을 집계해 Excel/CSV 결과를 만듭니다.',
    '▶ 로컬 감지/궤적 저장(DB)': '선택한 영상·모델로 감지와 추적을 시작해 SQLite DB에 궤적을 저장합니다. 실행 전에 영상·모델·라인·세션·DB 경로를 확인하세요.',
    '🖼 Extract Images + Labels': '영상에서 프레임 이미지를 추출하고 모델 탐지 결과를 YOLO 라벨로 저장합니다. 학습 데이터 준비용이며 일반 DB 감지와는 별도입니다.',
    '종료': '프로그램을 종료하며 현재 설정을 저장하고 하위 창과 백그라운드 작업을 정리합니다.',
    '다시 복사': '표시된 Colab 실행 셀 전체를 클립보드에 다시 복사합니다.',
    '닫기': '현재 창을 닫습니다. 미리보기 창은 영상 파일도 함께 해제합니다.',
    '새로고침': '현재 DB와 화면 정보를 다시 읽습니다. 외부에서 데이터를 바꾼 뒤 사용하세요.',
    '창 종료': '궤적 검토 창을 닫습니다.',
    '세션 새로고침': 'DB에서 사용 가능한 session_id 목록을 다시 읽습니다.',
    '슬롯 새로고침': '선택된 세션의 시간 슬롯 목록을 다시 읽습니다.',
    '영상 선택': '궤적 배경으로 표시할 원본 영상을 선택합니다. 영상과 트랙 위치를 비교할 때 사용하세요.',
    'DB 선택': '궤적과 후처리 이벤트가 들어 있는 SQLite DB를 선택합니다.',
    'DB 보기': '현재 DB의 테이블과 행을 별도 창에서 확인합니다. 데이터는 수정하지 않습니다.',
    'Lines 선택': '화면에 표시하고 편집할 분석라인 JSON을 선택합니다.',
    '후처리 보기': '현재 DB 세션에 저장된 병합·가상 교차 등 후처리 결과를 별도 창으로 엽니다.',
    '화면 맞춤': '현재 궤적 전체가 보이도록 확대율과 화면 위치를 맞춥니다.',
    '100%': '확대율을 기본 100% 보기로 되돌립니다.',
    '배경 켜기': '선택 영상의 프레임 배경 표시를 켜거나 끕니다. 궤적만 확인하려면 배경을 숨기세요.',
    '병합 설정': '끊어진 트랙을 같은 차량으로 연결할 조건과 방향을 지정합니다. 미리보기로 후보를 먼저 확인하세요.',
    '외삽 설정': '트랙의 이동 방향을 연장해 가상 라인 교차 이벤트를 추정할 범위와 방법을 지정합니다.',
    '병합 미리보기': '현재 세션의 병합 후보를 화면에서 강조 표시합니다. DB에는 저장하지 않으므로 실행 전 검토용입니다.',
    '병합 실행': '설정한 조건으로 연결한 트랙 관계를 현재 세션의 DB에 저장합니다. 후보와 session_id를 확인한 뒤 실행하세요.',
    '병합 취소': '현재 세션에 저장된 병합 후처리 결과를 취소합니다. 대상 session_id를 확인하세요.',
    '병합 완화': '병합 후보 조건을 완화해 더 많은 후보를 찾습니다. 오병합 가능성도 커지므로 미리보기로 검토하세요.',
    '병합 디버그': '궤적을 선택해 병합 조건을 점검하는 디버그 표시 모드를 켜거나 끕니다.',
    '외삽 미리보기': '가상 교차로 이어질 외삽 후보를 화면에 표시합니다. DB에는 아직 저장하지 않습니다.',
    '외삽 실행': '현재 세션의 트랙을 연장해 가상 교차 이벤트를 계산하고 DB에 저장합니다. 미리보기와 session_id를 확인하세요.',
    '외삽 취소': '확인 후 현재 세션의 가상 외삽 이벤트를 모두 삭제합니다. 원본 트랙 궤적은 삭제하지 않습니다.',
    '수동 선택': '화면에서 궤적을 클릭해 병합·외삽할 대상을 고르는 모드를 켜거나 끕니다.',
    '선택2개 병합': '수동 선택한 궤적 두 개를 한 차량의 연결 관계로 현재 세션 DB에 저장합니다. 방향과 연결이 맞는지 확인하세요.',
    '선택1개 앞외삽': '선택한 궤적의 시작 쪽으로 연장해 가상 교차 이벤트를 저장합니다.',
    '선택1개 뒤외삽': '선택한 궤적의 끝 쪽으로 연장해 가상 교차 이벤트를 저장합니다.',
    '선택 초기화': '수동 작업에서 선택한 궤적만 해제합니다. DB 데이터는 변경하지 않습니다.',
    '라인 추가': '화면에서 지정한 점들로 새 분석라인을 추가합니다. JSON에 반영하려면 Lines 저장을 누르세요.',
    '라인 선택 삭제': '선택한 분석라인을 현재 편집 목록에서 삭제합니다. 저장 전까지 JSON 파일은 바뀌지 않습니다.',
    'in점지정': '선택 라인의 진행 방향을 지정합니다. 이어 화면에서 in 방향을 클릭하세요.',
    'in점초기화': '선택한 라인의 진행 방향 지정점만 초기화합니다.',
    '포인트초기화': '아직 라인으로 등록하지 않은 현재 점들을 지웁니다.',
    '라인 전체 초기화': '현재 편집 중인 분석라인을 모두 비웁니다. 저장하면 Lines JSON에도 반영되므로 주의하세요.',
    'Lines 저장': '현재 라인 목록과 in점 설정을 선택한 Lines JSON 파일에 저장합니다.',
    'Lines 불러오기': '입력한 Lines JSON을 다시 읽어 현재 편집 내용을 교체합니다. 저장하지 않은 변경은 사라질 수 있습니다.',
    '↩ 되돌리기': '가장 최근 병합·외삽 후처리 한 건을 되돌립니다. 원본 감지 궤적은 변경하지 않습니다.',
    '전체 선택': '모델에 표시된 모든 클래스를 새 감지 작업의 대상으로 선택합니다.',
    '전체 해제': '모든 클래스 선택을 해제합니다. 확인하려면 최소 한 개를 다시 선택해야 합니다.',
    '위로': '선택한 Excel 집계 열을 한 단계 앞 순서로 옮깁니다.',
    '아래로': '선택한 Excel 집계 열을 한 단계 뒤 순서로 옮깁니다.',
    '기본값으로 되돌리기': '현재 모델 클래스 이름을 바탕으로 권장 집계 열 매핑과 순서를 다시 만듭니다.',
    '라인 JSON 선택': '편집할 분석라인 JSON 파일을 엽니다. 불러오면 현재 화면의 라인 목록이 바뀝니다.',
    '저장 경로...': '편집한 분석라인을 저장할 JSON 파일 경로를 지정합니다.',
    '현재 점들로 라인 추가 (우클릭)': '영상에서 우클릭으로 찍은 두 점 이상을 새 분석라인으로 등록합니다. 라인 ID와 bound를 확인하세요.',
    'Undo': '아직 라인으로 등록하지 않은 가장 최근 점 하나를 되돌립니다.',
    '포인트 초기화': '현재 입력 중인 점만 지웁니다. 이미 등록된 라인은 유지됩니다.',
    '저장': '현재 편집한 분석라인을 지정한 JSON 파일에 기록합니다.',
    '불러오기': '입력한 JSON 파일을 다시 읽습니다. 저장하지 않은 편집 내용은 사라질 수 있습니다.',
    '삭제': '이 라인을 현재 편집 목록에서 삭제합니다. 변경을 보존하려면 삭제 후 JSON 파일을 저장하세요.',
    '일시정지': '현재 영상 프레임에서 탐지를 멈춥니다. 재생을 누르면 이어서 진행합니다.',
    '재생': '영상 재생과 모델의 프레임별 차종 탐지를 계속합니다.',
    '결과 영상 저장': '탐지 상자와 신뢰도를 그린 영상을 프로젝트 폴더의 output_result.mp4에 저장합니다. 기존 동명 파일은 덮어쓸 수 있습니다.',
}


def apply_button_tooltips(root: QWidget, overrides: dict[str, str] | None = None) -> None:
    """버튼의 objectName 또는 표시 문구에 맞는 도움말을 설정한다."""
    descriptions = dict(TOOLTIPS)
    if overrides:
        descriptions.update(overrides)
    for button in root.findChildren(QPushButton):
        tip = descriptions.get(button.objectName()) or descriptions.get(button.text().strip())
        if tip:
            button.setToolTip(tip)


def wrap_in_scroll(content: QWidget) -> QScrollArea:
    """``content`` 를 스크롤 영역으로 감싼다.

    ``setWidgetResizable(True)`` 이므로 공간이 충분하면 기존과 동일하게
    보이고, 창이 작아 내용이 잘릴 때만 스크롤바가 나타난다.
    """
    scroll = QScrollArea()
    scroll.setWidget(content)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    return scroll


def fit_to_screen(window: QWidget, width: int, height: int, margin: int = 80) -> None:
    """요청한 크기로 창을 열되, 사용 가능한 화면 영역을 넘지 않게 제한한다.

    작은 노트북 화면이나 높은 디스플레이 배율에서 창이 화면 밖으로
    나가는 것을 막는다.
    """
    screen = window.screen() or QGuiApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        width = min(int(width), max(480, available.width() - margin))
        height = min(int(height), max(360, available.height() - margin))
    window.resize(int(width), int(height))


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
