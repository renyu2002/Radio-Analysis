"""视频分析 · DeepSeek —— Chrome 扩展图标生成脚本。

用 Pillow 绘制「渐变底 + 播放三角 + 析字」品牌图标，生成 MV3 扩展所需的
16 / 32 / 48 / 128 四种尺寸 PNG（写入 extension/icons/）。

用法（项目根目录下）：
    .\\.venv\\Scripts\\python.exe extension\\make_icons.py
    .\\.venv\\Scripts\\python.exe extension\\make_icons.py --preview   # 额外生成 dev/icon_preview.png

无第三方资源、无网络请求；字体优先使用 Windows 自带的 Microsoft YaHei Bold。
"""

from __future__ import annotations

import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFont

EXT_DIR = os.path.dirname(os.path.abspath(__file__))
ICONS_DIR = os.path.join(EXT_DIR, "icons")
DEV_DIR = os.path.join(EXT_DIR, "dev")

# DeepSeek 蓝 → 靛紫 的对角渐变
GRADIENT_START = (108, 138, 255)
GRADIENT_MID = (77, 107, 254)
GRADIENT_END = (96, 58, 236)

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
)

SIZES = (16, 32, 48, 128)


def mix(color_a: tuple[int, int, int], color_b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """两色线性插值。"""
    t = max(0.0, min(1.0, t))
    return (
        round(color_a[0] + (color_b[0] - color_a[0]) * t),
        round(color_a[1] + (color_b[1] - color_a[1]) * t),
        round(color_a[2] + (color_b[2] - color_a[2]) * t),
    )


def gradient_image(size: int) -> Image.Image:
    """生成对角三段渐变图（左上亮蓝 → 中间 DeepSeek 蓝 → 右下靛紫）。"""
    pixels = []
    denom = max(1, 2 * (size - 1))
    for y in range(size):
        for x in range(size):
            t = (x + y) / denom
            if t <= 0.5:
                color = mix(GRADIENT_START, GRADIENT_MID, t / 0.5)
            else:
                color = mix(GRADIENT_MID, GRADIENT_END, (t - 0.5) / 0.5)
            pixels.append(color)
    image = Image.new("RGB", (size, size))
    image.putdata(pixels)
    return image


def load_font(pixel_size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """加载可绘制「析」的中文字体，失败时退回 Pillow 默认字体。"""
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, max(1, pixel_size))
            except OSError:
                continue
    return ImageFont.load_default()


def draw_play_triangle(draw: ImageDraw.ImageDraw, x_left: float, cy: float, width: float, height: float,
                       shadow_offset: float) -> None:
    """绘制播放三角（先画淡阴影，再叠加白色）。"""
    points = [
        (x_left, cy - height / 2),
        (x_left, cy + height / 2),
        (x_left + width, cy),
    ]
    shadow = [(x + shadow_offset, y + shadow_offset) for x, y in points]
    draw.polygon(shadow, fill=(24, 20, 72, 90))
    draw.polygon(points, fill=(255, 255, 255, 255))


def render_icon(size: int) -> Image.Image:
    """按目标尺寸渲染一个 RGBA 图标（内部超采样后缩小，保证抗锯齿）。"""
    scale = max(4, round(512 / size))
    canvas = size * scale

    art = round(canvas * 0.92)
    offset = (canvas - art) // 2
    radius = round(art * 0.23)

    # 圆角矩形渐变底
    icon = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    gradient = gradient_image(art).convert("RGBA")
    mask = Image.new("L", (art, art), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, art - 1, art - 1), radius=radius, fill=255)
    icon.paste(gradient, (offset, offset), mask)

    draw = ImageDraw.Draw(icon)
    cy = offset + art / 2

    # 内描边高光，增加体积感
    inset = max(1, round(art * 0.018))
    draw.rounded_rectangle(
        (offset + inset, offset + inset, offset + art - inset, offset + art - inset),
        radius=max(1, round(radius * 0.9)),
        outline=(255, 255, 255, 38),
        width=max(1, round(art * 0.011)),
    )

    show_char = size >= 48
    if show_char:
        # 左：播放三角；右：「析」字标
        draw_play_triangle(
            draw,
            x_left=offset + art * 0.15,
            cy=cy,
            width=art * 0.27,
            height=art * 0.40,
            shadow_offset=art * 0.012,
        )
        font = load_font(round(art * 0.40))
        draw.text(
            (offset + art - art * 0.095, cy + art * 0.015),
            "析",
            font=font,
            fill=(255, 255, 255, 255),
            anchor="rm",
        )
    else:
        # 小尺寸只保留播放三角，保证 16px 下可辨识
        draw_play_triangle(
            draw,
            x_left=offset + art * 0.355,
            cy=cy,
            width=art * 0.43,
            height=art * 0.54,
            shadow_offset=art * 0.014,
        )

    return icon.resize((size, size), Image.LANCZOS)


def build_preview(paths: dict[int, str], output_path: str) -> None:
    """把四种尺寸放大排列成一张预览图，便于肉眼检查。"""
    cell = 128
    gap = 14
    sheet = Image.new("RGBA", (len(SIZES) * cell + gap * (len(SIZES) + 1), cell + gap * 2), (11, 14, 20, 255))
    for index, size in enumerate(SIZES):
        icon = Image.open(paths[size]).convert("RGBA")
        enlarged = icon.resize((cell, cell), Image.NEAREST)
        x = gap + index * (cell + gap)
        sheet.alpha_composite(enlarged, (x, gap))
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    sheet.save(output_path)
    print(f"[preview] {output_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成视频分析 · DeepSeek Chrome 扩展图标")
    parser.add_argument("--preview", action="store_true", help="额外生成 dev/icon_preview.png 预览图")
    args = parser.parse_args(argv)

    os.makedirs(ICONS_DIR, exist_ok=True)
    paths: dict[int, str] = {}
    for size in SIZES:
        icon = render_icon(size)
        path = os.path.join(ICONS_DIR, f"icon{size}.png")
        icon.save(path, "PNG")
        paths[size] = path
        print(f"[icon] {path}  {icon.size[0]}x{icon.size[1]}  {os.path.getsize(path)} bytes")

    if args.preview:
        build_preview(paths, os.path.join(DEV_DIR, "icon_preview.png"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
