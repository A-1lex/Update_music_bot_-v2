from __future__ import annotations

from pathlib import Path
from typing import Iterable


def is_path_inside(path: Path, roots: Iterable[Path]) -> bool:
    candidate = path.resolve()
    for root in roots:
        root_resolved = root.resolve()
        try:
            candidate.relative_to(root_resolved)
            return True
        except ValueError:
            continue
    return False


def list_media_files(music_dir: Path, video_dir: Path) -> list[Path]:
    """Щоразу перечитує локальні каталоги та повертає актуальні MP3/MP4.

    Пошук рекурсивний: файли в підпапках music/ і video/ також
    відображаються в адміністративній панелі /delete.
    """
    audio = sorted(
        (
            path.resolve()
            for path in music_dir.rglob("*")
            if path.is_file() and path.suffix.lower() == ".mp3"
        ),
        key=lambda path: (path.name.casefold(), str(path).casefold()),
    )
    video = sorted(
        (
            path.resolve()
            for path in video_dir.rglob("*")
            if path.is_file() and path.suffix.lower() == ".mp4"
        ),
        key=lambda path: (path.name.casefold(), str(path).casefold()),
    )
    return audio + video
