"""
Builds the app icons from one source image.

    python3 tools/make_icons.py [source]

source: assets/logo.png if it exists, otherwise assets/icon.svg.
Writes: assets/icon.png (256 px: window icon and Linux menu icon)
        assets/icon.ico (all sizes: Windows .exe and installer)
Needs:  pip install pillow cairosvg
"""

import io
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def load_source(path):
    if path.suffix.lower() == ".svg":
        import cairosvg
        png = cairosvg.svg2png(url=str(path), output_width=1024,
                               output_height=1024)
        return Image.open(io.BytesIO(png)).convert("RGBA")
    img = Image.open(path).convert("RGBA")
    # pad to a square canvas so the logo isn't stretched
    side = max(img.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    return canvas


def main():
    if len(sys.argv) > 1:
        source = Path(sys.argv[1])
    elif (ASSETS / "logo.png").exists():
        source = ASSETS / "logo.png"
    else:
        source = ASSETS / "icon.svg"
    img = load_source(source)

    icon256 = img.resize((256, 256), Image.LANCZOS)
    icon256.save(ASSETS / "icon.png")
    img.resize((1024, 1024), Image.LANCZOS).save(
        ASSETS / "icon.ico", sizes=[(s, s) for s in SIZES])
    print(f"icons built from {source.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
