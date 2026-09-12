from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QFileDialog, QWidget


def select_video(parent: Optional[QWidget] = None) -> Optional[Path]:
    file_path, _ = QFileDialog.getOpenFileName(
        parent,
        "Select Video",
        "",
        "Video Files (*.mp4 *.avi *.mov *.mkv);;All Files (*)",
    )
    if not file_path:
        return None
    return Path(file_path)
