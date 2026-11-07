from __future__ import annotations

from pathlib import Path

from app.media import MediaFile
from app.mp4info import VideoInfo, read_video_info

FIX = Path(__file__).parent / "fixtures"


def test_vertical_mp4_size_and_duration():
    # первая дорожка с размерами — видео; аудиодорожка (0×0) пропускается
    assert read_video_info(FIX / "vertical.mp4") == VideoInfo(72, 128, 3)


def test_rotated_mov_swaps_dimensions():
    info = read_video_info(FIX / "rotated.mov")
    assert (info.width, info.height) == (72, 128)
    assert info.duration == 2


def test_not_a_video(tmp_path):
    bad = tmp_path / "x.mp4"
    bad.write_bytes(b"\x00\x00\x00\x18ftypmp42 garbage")
    assert read_video_info(bad) is None
    assert read_video_info(tmp_path / "missing.mp4") is None


def test_video_kwargs():
    f = MediaFile("v.mp4", Path("v.mp4"), "p/v.mp4", 1, "video", info=VideoInfo(1080, 1920, 25))
    assert f.video_kwargs() == {"width": 1080, "height": 1920, "duration": 25}
    assert MediaFile("a.jpg", Path("a.jpg"), "p/a.jpg", 1, "photo").video_kwargs() == {}
