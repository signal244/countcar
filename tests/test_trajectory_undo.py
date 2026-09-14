"""궤적 뷰어(trajectory_viewer2) 되돌리기의 데이터 유실 회귀 테스트.

과거 되돌리기가 파이프라인 소유 테이블(track_merge_map)과 파이프라인이 기록한
가상 이벤트(track_virtual_events)까지 지워, 재분석 없이는 복구 불가능한
데이터 유실이 발생했다. 핵심 안전 속성 두 가지를 여기서 고정한다.

  1. _undo_merge 는 뷰어가 만든 track_merge_map_manual 만 지운다.
     파이프라인 소유 track_merge_map 은 절대 건드리지 않는다.
  2. _undo_extrap 은 method 로 범위를 좁혀 뷰어가 만든 virtual_event 만 지운다.
     파이프라인이 기록한 다른 method 의 이벤트는 남긴다.

위젯 전체를 띄우지 않고, 언바운드 메서드를 가짜 self 로 호출해 순수 DB
로직만 검증한다.
"""

import gc
import os
import sqlite3
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:  # PySide6/cv2 가 없으면 GUI 모듈을 건너뛴다.
    from src.ui.trajectory_viewer2 import TrajectoryViewer2Window
    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - 환경 의존
    TrajectoryViewer2Window = None
    _IMPORT_ERROR = exc

from src.db.schema import init_db


SESSION = "sess-1"


def _fake_self():
    """되돌리기 메서드가 참조하는 최소한의 위젯 상태만 갖는 대역."""
    return types.SimpleNamespace(
        _apply_merge_filter="something",
        _reload_all=lambda: None,
    )


@unittest.skipIf(
    TrajectoryViewer2Window is None,
    f"GUI 모듈 임포트 실패(환경 의존): {_IMPORT_ERROR}",
)
class UndoDataLossTests(unittest.TestCase):
    def setUp(self) -> None:
        # 되돌리기 메서드는 `with sqlite3.connect(...)` 를 쓰는데, 이 컨텍스트는
        # 트랜잭션만 관리하고 연결을 닫지 않는다. Windows 에선 연결이 GC 될
        # 때까지 파일을 붙잡아 임시 디렉터리 정리가 실패하므로, 정리 오류를
        # 무시하도록 둔다(tearDown 에서 gc 로 최대한 회수).
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.db"
        init_db(self.db_path)

    def tearDown(self) -> None:
        gc.collect()  # 되돌리기 메서드가 남긴 sqlite 연결을 회수해 파일 잠금 해제
        self._tmp.cleanup()

    def _count(self, table: str, where: str = "", params=()) -> int:
        sql = f"select count(*) from {table}"
        if where:
            sql += f" where {where}"
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(sql, params).fetchone()[0]

    def test_undo_merge_spares_pipeline_owned_map(self) -> None:
        """되돌리기는 수동 병합만 지우고 파이프라인 병합 맵은 남겨야 한다."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            # 파이프라인 소유: 절대 지워지면 안 됨
            conn.execute(
                "insert into track_merge_map"
                "(session_id, source_track_id, merged_track_id) values (?,?,?)",
                (SESSION, "10", "11"),
            )
            # 뷰어가 만든 수동 병합: 되돌리기 대상
            conn.execute(
                "insert into track_merge_map_manual"
                "(session_id, source_track_id, merged_track_id) values (?,?,?)",
                (SESSION, "10", "11"),
            )
            conn.commit()

        TrajectoryViewer2Window._undo_merge(
            _fake_self(),
            {
                "db_path": str(self.db_path),
                "session_id": SESSION,
                "pairs": [("10", "11")],
            },
        )

        self.assertEqual(
            self._count("track_merge_map_manual"), 0,
            "수동 병합 레코드는 되돌려져 삭제되어야 한다",
        )
        self.assertEqual(
            self._count("track_merge_map"), 1,
            "파이프라인 소유 병합 맵은 절대 삭제되면 안 된다",
        )

    def test_undo_extrap_spares_pipeline_virtual_events(self) -> None:
        """되돌리기는 뷰어가 만든 method 의 가상 이벤트만 지워야 한다."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            # 뷰어가 만든 외삽: 되돌리기 대상
            conn.execute(
                "insert into track_virtual_events"
                "(session_id, track_id, line_id, ts_ms, method) values (?,?,?,?,?)",
                (SESSION, "20", "L1", 100, "viewer2_manual"),
            )
            # 파이프라인이 기록한 이벤트: 같은 track_id 라도 남아야 함
            conn.execute(
                "insert into track_virtual_events"
                "(session_id, track_id, line_id, ts_ms, method) values (?,?,?,?,?)",
                (SESSION, "20", "L1", 200, "pipeline_horizon"),
            )
            conn.commit()

        TrajectoryViewer2Window._undo_extrap(
            _fake_self(),
            {
                "db_path": str(self.db_path),
                "session_id": SESSION,
                "keys": [("20", "viewer2_manual")],
            },
        )

        self.assertEqual(
            self._count("track_virtual_events", "method = 'viewer2_manual'"), 0,
            "뷰어가 만든 가상 이벤트는 되돌려져 삭제되어야 한다",
        )
        self.assertEqual(
            self._count("track_virtual_events", "method = 'pipeline_horizon'"), 1,
            "파이프라인이 기록한 가상 이벤트는 절대 삭제되면 안 된다",
        )

    def test_undo_extrap_legacy_stack_only_deletes_viewer_method(self) -> None:
        """method 정보가 없는 구버전 스택도 viewer2_ 접두 이벤트만 지워야 한다."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "insert into track_virtual_events"
                "(session_id, track_id, line_id, ts_ms, method) values (?,?,?,?,?)",
                (SESSION, "30", "L1", 100, "viewer2_extrap"),
            )
            conn.execute(
                "insert into track_virtual_events"
                "(session_id, track_id, line_id, ts_ms, method) values (?,?,?,?,?)",
                (SESSION, "30", "L1", 200, "pipeline_horizon"),
            )
            conn.commit()

        TrajectoryViewer2Window._undo_extrap(
            _fake_self(),
            {
                "db_path": str(self.db_path),
                "session_id": SESSION,
                "track_ids": ["30"],  # 구버전: method 없음
            },
        )

        self.assertEqual(
            self._count("track_virtual_events", "method = 'viewer2_extrap'"), 0,
            "구버전 스택도 뷰어가 만든 이벤트는 삭제해야 한다",
        )
        self.assertEqual(
            self._count("track_virtual_events", "method = 'pipeline_horizon'"), 1,
            "구버전 스택이라도 파이프라인 이벤트는 삭제되면 안 된다",
        )


if __name__ == "__main__":
    unittest.main()
