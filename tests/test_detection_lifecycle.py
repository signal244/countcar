import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from src.db.writer import TrackTrajDBWriter, decode_traj
from src.pipeline.detect_track import DetectionTracker


class Tensor:
    def __init__(self, value):
        self.value = np.array(value)
    def cpu(self):
        return self
    def numpy(self):
        return self.value


def hit():
    return SimpleNamespace(boxes=SimpleNamespace(
        id=Tensor([1]), cls=Tensor([1]), conf=Tensor([0.9]),
        xyxy=Tensor([[0, 0, 2, 2]])))


def tracker_for(stream):
    tracker = DetectionTracker.__new__(DetectionTracker)
    tracker.__dict__.update(
        model=SimpleNamespace(names={1: "car"}, track=Mock(return_value=stream)),
        camera_id="cam", roi=[], line_set_id=None, confidence=.25, device="cpu",
        tracker_config=None, allowed_classes=None, yolo_imgsz=640, yolo_rect=True,
        target_fps=1, max_idle_frames=2, flush_interval_ms=60000,
        class_mapping={}, apply_class_mapping=False,
    )
    tracker._probe_fps = Mock(return_value=1)
    return tracker


class DetectionLifecycleTests(unittest.TestCase):
    def test_empty_frames_finalize_before_eof_and_still_flush(self):
        for empty in (SimpleNamespace(boxes=None), SimpleNamespace(boxes=SimpleNamespace(id=None))):
            with self.subTest(empty=empty):
                writer = Mock()
                def stream():
                    yield hit()
                    yield empty
                    yield empty
                    # The track must already be saved, before the generator ends.
                    self.assertEqual(writer.add_track.call_count, 1)
                    yield from [empty] * 63
                tracker_for(stream()).run(Path("mock.mp4"), writer)
                self.assertEqual(writer.add_track.call_count, 1)
                self.assertEqual(writer.flush.call_count, 1)

    def test_active_track_checkpoint_survives_empty_frame_at_boundary(self):
        writer = Mock()
        tracker = tracker_for(iter([hit()] + [SimpleNamespace(boxes=None)] * 61))
        tracker.max_idle_frames = 100
        tracker.run(Path("mock.mp4"), writer)
        self.assertEqual(writer.checkpoint_track.call_count, 1)

    def test_decoder_exception_persists_active_trajectory_and_releases_capture(self):
        def stream():
            yield hit()
            raise RuntimeError("decoder failed")
        tracker = tracker_for(stream())
        cap = Mock()
        tracker.model.predictor = SimpleNamespace(dataset=SimpleNamespace(cap=cap))
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tracks.sqlite"
            with self.assertRaisesRegex(RuntimeError, "decoder failed"):
                with TrackTrajDBWriter(db) as writer:
                    tracker.run(Path("mock.mp4"), writer, session_id="s")
            with closing(sqlite3.connect(db)) as conn:
                rows = conn.execute("select traj from track_trajs").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(decode_traj(rows[0][0])), 1)
        cap.release.assert_called_once()

    def test_cancellation_closes_stream_and_saves_partial_track(self):
        closed = []
        def stream():
            try:
                yield hit()
                yield hit()
            finally:
                closed.append(True)
        writer = Mock()
        tracker_for(stream()).run(Path("mock.mp4"), writer,
                                  should_stop_cb=Mock(side_effect=[False, True]))
        self.assertEqual(writer.add_track.call_count, 1)
        self.assertEqual(closed, [True])

    def test_cleanup_failure_does_not_mask_inference_error(self):
        def stream():
            yield hit()
            raise RuntimeError("original decoder failure")
        writer = Mock()
        writer.add_track.side_effect = OSError("disk full")
        with self.assertLogs("src.pipeline.detect_track", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "original decoder failure"):
                tracker_for(stream()).run(Path("mock.mp4"), writer)

    def test_cleanup_failure_on_normal_eof_is_reported(self):
        writer = Mock()
        writer.add_track.side_effect = OSError("disk full")
        with self.assertLogs("src.pipeline.detect_track", level="ERROR"):
            with self.assertRaisesRegex(OSError, "disk full"):
                tracker_for(iter([hit()])).run(Path("mock.mp4"), writer)

    def test_writer_closes_connection_even_if_flush_fails(self):
        writer = TrackTrajDBWriter.__new__(TrackTrajDBWriter)
        writer.conn = Mock()
        writer.flush = Mock(side_effect=OSError("disk full"))
        with self.assertLogs("src.db.writer", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "original"):
                with writer:
                    raise RuntimeError("original")
        writer.conn.close.assert_called_once()

    def test_no_decodable_frames_is_an_error_not_successful_overwrite(self):
        with self.assertRaisesRegex(RuntimeError, "No video frames"):
            tracker_for(iter([])).run(Path("mock.mp4"), Mock())

    def test_video_with_no_vehicles_is_a_valid_empty_result(self):
        writer = Mock()
        tracker_for(iter([SimpleNamespace(boxes=None)])).run(Path("mock.mp4"), writer)
        writer.add_track.assert_not_called()

    def test_failed_final_write_keeps_checkpoint_until_successful_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tracks.sqlite"
            with TrackTrajDBWriter(db) as writer:
                record = {"session_id": "s", "camera_id": "cam", "track_id": "1"}
                writer.checkpoint_track(record, [[0, 0, 1, 1]])
                writer.conn.execute("create trigger reject_final before insert on track_trajs when NEW.extra is null begin select raise(ABORT, 'disk error'); end")
                writer.conn.commit()
                writer.add_track(record, [[0, 0, 1, 1], [1, 1000, 2, 2]])
                with self.assertRaises(sqlite3.IntegrityError):
                    writer.flush()
                with closing(sqlite3.connect(db)) as conn:
                    self.assertEqual(len(decode_traj(conn.execute("select traj from track_trajs").fetchone()[0])), 1)
                writer.conn.execute("drop trigger reject_final")
                writer.conn.commit()
            with closing(sqlite3.connect(db)) as conn:
                rows = conn.execute("select traj from track_trajs").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(len(decode_traj(rows[0][0])), 2)
