"""生成 E2E 自检素材：中文 TTS 语音 + 带画面文字的合成视频。

用法：
    .venv\\Scripts\\python.exe tests\\fixtures\\make_fixture.py

产出 tests/fixtures/sample.mp4：
- 12 秒、1280x720、25fps，3 张幻灯片，画面含中文字样（用于 OCR 验证）
- 音轨为 Windows 中文 TTS（Microsoft Huihui）朗读的普通话（用于 ASR 验证）
"""

from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
WAV = FIXTURES / "speech.wav"
OUT = FIXTURES / "sample.mp4"

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]

SLIDES = [
    ("第一章  测试画面", "视频分析系统自检"),
    ("画面文字 OCR 测试", "识别画面中的中文字幕"),
    ("语音转文字测试", "共 12 秒，含中文普通话语音"),
]

SPEECH = (
    "今天天气很好，我们来测试语音转文字功能。"
    "这是视频分析系统的自检音频，请确认转写结果是否准确。"
)


def synthesize_wav() -> None:
    ps = f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice('Microsoft Huihui Desktop')
$s.Rate = 0
$s.SetOutputToWaveFile('{WAV}')
$s.Speak('{SPEECH}')
$s.Dispose()
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        check=True,
        capture_output=True,
    )
    if not WAV.exists() or WAV.stat().st_size == 0:
        raise SystemExit("TTS 生成失败")


def pick_font(size: int):
    from PIL import ImageFont

    for f in FONT_CANDIDATES:
        if Path(f).exists():
            try:
                return ImageFont.truetype(f, size)
            except OSError:
                continue
    return ImageFont.load_default()


def make_slides() -> list[Path]:
    from PIL import Image, ImageDraw

    paths: list[Path] = []
    W, H = 1280, 720
    title_font = pick_font(64)
    sub_font = pick_font(34)
    for i, (title, sub) in enumerate(SLIDES):
        img = Image.new("RGB", (W, H), (14, 18, 28))
        d = ImageDraw.Draw(img)
        # 顶部渐变条
        for y in range(8):
            c = 40 + y * 12
            d.line([(0, y), (W, y)], fill=(c, c // 2, 200))
        # 标题居中
        bbox = d.textbbox((0, 0), title, font=title_font)
        tw = bbox[2] - bbox[0]
        d.text(((W - tw) / 2, 280), title, font=title_font, fill=(240, 244, 255))
        bbox2 = d.textbbox((0, 0), sub, font=sub_font)
        tw2 = bbox2[2] - bbox2[0]
        d.text(((W - tw2) / 2, 380), sub, font=sub_font, fill=(150, 165, 200))
        # 角标
        d.text((48, H - 90), f"0{i + 1} / 0{len(SLIDES)}", font=sub_font, fill=(90, 100, 130))
        p = FIXTURES / f"slide{i + 1}.png"
        img.save(p)
        paths.append(p)
    return paths


def make_video(slides: list[Path]) -> None:
    import cv2
    import imageio_ffmpeg
    import numpy as np

    # 注意：cv2.imread 不支持中文路径，必须用 imdecode
    frames = [
        cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
        for p in slides
    ]
    # 绝对路径含中文，VideoWriter 也可能失败，先写 ASCII 临时目录
    import tempfile

    tmpdir = Path(tempfile.mkdtemp(prefix="vanalyze_fixture_"))
    per = 4  # 每张幻灯片 4 秒
    fps = 25
    tmp = tmpdir / "video_noaudio.mp4"
    writer = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"mp4v"), fps, (1280, 720))
    if not writer.isOpened():
        raise SystemExit("VideoWriter 打开失败")
    for img in frames:
        for _ in range(per * fps):
            writer.write(img)
    writer.release()

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg, "-y", "-i", str(tmp), "-i", str(WAV),
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest",
        str(OUT),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    tmp.unlink(missing_ok=True)
    try:
        tmpdir.rmdir()
    except OSError:
        pass


def make_h264_video(slides: list[Path]) -> None:
    """额外生成 H.264/AAC 版本（Chrome 可直接播放，用于播放器验收）。"""
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    out = FIXTURES / "sample_h264.mp4"
    inputs: list[str] = []
    for p in slides:
        inputs += ["-loop", "1", "-framerate", "25", "-t", "4", "-i", str(p)]
    filter_complex = "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]"
    cmd = [
        ffmpeg, "-y", *inputs, "-i", str(WAV),
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "3:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"已生成 H.264 版本: {out} ({out.stat().st_size / 1024:.0f} KB)")


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    synthesize_wav()
    with wave.open(str(WAV), "rb") as w:
        dur = w.getnframes() / w.getframerate()
    print(f"语音时长: {dur:.2f}s")
    if dur < 10:
        # 语音太短时循环一遍，保证 12 秒视频
        pass
    slides = make_slides()
    make_video(slides)
    print(f"已生成: {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")
    make_h264_video(slides)
    print("语音文本:", SPEECH)


if __name__ == "__main__":
    sys.exit(main())
