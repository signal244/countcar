"""저장된 DB를 읽는 조회 헬퍼.

집계 단계에서는 어떤 모델로 만든 DB인지 알 수 없으므로,
실제로 저장된 차종 이름을 DB에서 직접 읽어 UI에 보여준다.
"""

from __future__ import annotations

import sqlite3
from collections import Counter

from src.db.trajectories import iter_trajectory_headers
from contextlib import closing
from pathlib import Path
from typing import Dict, List, Optional


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "select name from sqlite_master where type='table' and name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def distinct_vehicle_types(
    db_path: str | Path,
    session_id: Optional[str] = None,
    table_name: str = "track_trajs",
) -> Dict[str, int]:
    """DB에 실제로 저장된 차종 이름과 트랙 수를 돌려준다.

    vehicle_type(매핑된 이름)을 우선 쓰고 없으면 class_name(모델 원본 이름)을 쓴다.
    이는 count_tracks 가 집계할 때 쓰는 규칙과 동일하다.
    """
    path = Path(db_path)
    if not path.exists():
        return {}
    try:
        with closing(sqlite3.connect(str(path))) as conn:
            if table_name == "track_trajs":
                counts = Counter(name.strip() for _sess, _cam, _tid, name, _start
                                 in iter_trajectory_headers(conn, session_id) if name.strip())
                return dict(counts.most_common())
            if not _table_exists(conn, table_name):
                return {}
            sql = (
                f"select coalesce(nullif(trim(vehicle_type), ''), class_name) as cls_name, count(*) "
                f"from {table_name}"
            )
            params: List[object] = []
            if session_id:
                sql += " where session_id = ?"
                params.append(session_id)
            sql += " group by cls_name order by count(*) desc"
            rows = conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.Error:
        return {}

    result: Dict[str, int] = {}
    for name, count in rows:
        cleaned = str(name or "").strip()
        if cleaned:
            result[cleaned] = int(count or 0)
    return result
