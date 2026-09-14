import argparse
from pathlib import Path
from typing import Callable, Optional, Tuple

from src.services.detection_service import DetectionRequest, DetectionService


def _probe_frame_count(video_path: Path) -> Optional[int]:
    try:
        import cv2

        cap = cv2.VideoCapture(str(video_path))
        count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        cap.release()
        if count and count > 0:
            return int(count)
    except (ImportError, OSError):
        return None
    return None


def _make_progress_filter(
    total_frames: Optional[int],
    every_n_frames: int = 1000,
) -> Tuple[Callable[[str], None], Callable[[str], None]]:
    """Return an unfiltered emitter and a compact CLI progress callback."""

    def emit(message: str) -> None:
        print(message, flush=True)

    last_logged = {"frame": -10**18}

    def progress_cb(message: str) -> None:
        if not isinstance(message, str):
            emit(str(message))
            return
        prefix = "[progress] frame "
        if not message.startswith(prefix):
            emit(message)
            return
        try:
            frame_id = int(message[len(prefix) :].strip())
        except ValueError:
            emit(message)
            return
        if frame_id - last_logged["frame"] < every_n_frames:
            return
        last_logged["frame"] = frame_id
        if total_frames and total_frames > 0:
            percentage = min(100.0, (float(frame_id + 1) / float(total_frames)) * 100.0)
            emit(f"[progress] {frame_id + 1}/{total_frames} 프레임 처리중 ({percentage:.1f}%)")
        else:
            emit(f"[progress] {frame_id} 프레임 처리중")

    return emit, progress_cb


def main() -> None:
    parser = argparse.ArgumentParser(description="Run detection+tracking and save to SQLite.")
    parser.add_argument("--config", default="config/app_config.json", help="App config path")
    parser.add_argument("--video", required=True, help="Video file path")
    parser.add_argument("--line-settings", default=None, help="Optional line/ROI config path")
    parser.add_argument("--session-id", default=None, help="Optional session id")
    parser.add_argument(
        "--overwrite-session",
        action="store_true",
        help="Replace the session only after successful detection; preserve original data on failure or cancellation.",
    )
    parser.add_argument(
        "--flush-minutes",
        type=int,
        choices=[5, 15, 30, 60],
        help="DB flush interval in minutes (overrides config).",
    )
    args = parser.parse_args()

    video_path = Path(args.video)
    total_frames = _probe_frame_count(video_path) if video_path.exists() else None
    emit, progress_cb = _make_progress_filter(total_frames, every_n_frames=1000)
    overrides = {}
    if args.flush_minutes:
        overrides["flush_interval_minutes"] = args.flush_minutes

    result = DetectionService().run(
        DetectionRequest(
            cfg_path=Path(args.config),
            video_path=video_path,
            line_path=Path(args.line_settings) if args.line_settings else None,
            overrides=overrides,
            session_id=args.session_id,
            overwrite_session=bool(args.overwrite_session),
        ),
        progress_cb=progress_cb,
    )
    emit(f"[done] DB={result.db_path} session_id={result.session_id}")


if __name__ == "__main__":
    main()
