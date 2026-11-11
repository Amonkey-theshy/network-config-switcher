"""Render the geometric SVG design with Pillow; no network or image API needed."""
from pathlib import Path

from PIL import Image, ImageDraw


def render(size):
    scale = size / 512
    image = Image.new("RGBA", (size, size))
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            fraction = min(1, max(0, ((x / scale - 24) + (y / scale - 24)) / 928))
            pixels[x, y] = tuple(round(a + (b - a) * fraction) for a, b in zip((37, 62, 102), (17, 28, 50))) + (255,)
    mask = Image.new("L", (size, size))
    ImageDraw.Draw(mask).rounded_rectangle(tuple(round(v * scale) for v in (24, 24, 488, 488)), radius=round(112 * scale), fill=255)
    image.putalpha(mask)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(tuple(round(v * scale) for v in (26, 26, 486, 486)), radius=round(110 * scale), outline="#395278", width=max(1, round(3 * scale)))

    def line(points, color, width):
        coords = [(round(x * scale), round(y * scale)) for x, y in points]
        stroke = round(width * scale)
        draw.line(coords, fill=color, width=stroke, joint="curve")
        radius = stroke / 2
        for x, y in coords:
            draw.ellipse((round(x - radius), round(y - radius), round(x + radius), round(y + radius)), fill=color)

    for y, node, points, color in (
        (196, 144, [(314, 148), (366, 196), (314, 244)], "#f2f7ff"),
        (316, 368, [(198, 268), (146, 316), (198, 364)], "#52d8ed"),
    ):
        line([(144 if y == 196 else 368, y), (350 if y == 196 else 162, y)], color, 32)
        line(points, color, 32)
        box = tuple(round(v * scale) for v in (node - 37, y - 37, node + 37, y + 37))
        draw.ellipse(box, fill=color)
        box = tuple(round(v * scale) for v in (node - 21, y - 21, node + 21, y + 21))
        draw.ellipse(box, fill="#111c32")
    return image


if __name__ == "__main__":
    directory = Path(__file__).resolve().parent
    master = render(2048)
    master.resize((512, 512), Image.Resampling.LANCZOS).save(directory / "app-icon.png")
    master.resize((256, 256), Image.Resampling.LANCZOS).save(
        directory / "app-icon.ico", sizes=[(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)]
    )
    sheet = Image.new("RGB", (640, 280), "#f3f5f9")
    sheet.paste(master.resize((240, 240), Image.Resampling.LANCZOS), (20, 20), master.resize((240, 240), Image.Resampling.LANCZOS))
    draw = ImageDraw.Draw(sheet)
    for x, size in ((300, 128), (460, 64), (550, 32), (602, 16)):
        icon = master.resize((size, size), Image.Resampling.LANCZOS)
        sheet.paste(icon, (x, 110 - size // 2), icon)
        draw.text((x, 200), f"{size}px", fill="#455673")
    sheet.save(directory / "icon-preview.png")
