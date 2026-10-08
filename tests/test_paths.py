from pathlib import Path

from nyavview.utils.paths import ensure_directory
from nyavview.utils.paths import unique_path
from nyavview.utils import safe_filename


def test_safe_filename_handles_bad_characters() -> None:
    assert safe_filename('a<b>c"d/e\\f|g*h?i:j') == "a_b_c_d_e_f_g_h_i_j"


def test_unique_path_does_not_overwrite(tmp_path: Path) -> None:
    first = tmp_path / "video.mp4"
    first.touch()
    assert unique_path(first).name == "video-1.mp4"


def test_ensure_directory_creates_folder(tmp_path: Path) -> None:
    target = ensure_directory(tmp_path / "out" / "mp4")
    assert target.exists()
    assert target.is_dir()
