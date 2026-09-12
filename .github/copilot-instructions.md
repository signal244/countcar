# Copilot instructions for Count Car Ver5.5

## Architecture

- All detection entry points must use `src/services/detection_service.py:DetectionService`.
- `src/app.py` owns PySide6 UI only; `src/pipeline/detect_to_db.py` is the headless CLI adapter.
- Detection/tracking is implemented in `src/pipeline/detect_track.py`.
- Counting, merge, and virtual-event logic lives in `src/pipeline/count_tracks.py`, `track_merge.py`, and `virtual_events.py`.
- Shared geometry belongs in `src/pipeline/geometry.py`.
- SQLite schema and compressed trajectory writes live in `src/db/schema.py` and `src/db/writer.py`.

## Project conventions

- Keep `config/app_config.json` portable and repository-relative.
- Store machine-specific GUI paths and window state only in ignored `config/user_state.json`.
- Preserve legacy `tracks` reads while `track_trajs` compatibility is required.
- Use `TrackTrajDBWriter` for new detector output and close SQLite connections explicitly.
- Increment `DB_USER_VERSION` and record migrations when the schema changes.
- Keep line JSON compatible with 2-point lines and multi-point polylines.
- Validate `image_width`, `image_height`, ROI, `bound`, and coordinate scaling together.
- Do not commit model weights, generated databases, spreadsheets, or local user state.

## Verification

```powershell
python -m unittest discover -s tests -v
python -m src.pipeline.detect_to_db --help
python -m src.pipeline.count_tracks --help
```

User-facing setup, GUI, CLI, Colab, and line-editing instructions are consolidated in `사용설명서.md`.
