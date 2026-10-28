"""
Builds the app icons from the project logo.

    python3 tools/make_icons.py [source]

source: assets/logo.png by default (transparent PNG).
Writes: assets/icon.png  (256 px: window icon and Linux menu icon)
        assets/icon.ico  (16–256 px: Windows .exe and installer)
        assets/icon-small.png (logo without the falling petal, 256 px)

Small sizes (≤ 48 px) use the main shape only — the separate falling petal
would make the rose too tiny to recognize.

Needs:  pip install pillow numpy scipy
"""

import sys
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SMALL = [16, 20, 24, 32, 40, 48]
LARGE = [64, 128, 256]


def square(img, margin=0.04):
    """Crop to the visible part and center it on a square transparent canvas."""
    img = img.crop(img.getbbox())
    side = int(max(img.size) * (1 + margin))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
    return canvas


def main_shape(img):
    """Keeps only the largest connected shape (drops the falling petal)."""
    alpha = np.asarray(img.getchannel("A")) > 16
    labels, count = ndimage.label(alpha)
    if count <= 1:
        return img
    sizes = ndimage.sum(alpha, labels, range(1, count + 1))
    keep = labels == (int(np.argmax(sizes)) + 1)
    keep = ndimage.binary_dilation(keep, iterations=3)   # keep soft edges
    out = np.asarray(img).copy()
    out[..., 3] = np.where(keep, out[..., 3], 0)
    return Image.fromarray(out)


def main():
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else ASSETS / "logo.png"
    full = square(Image.open(source).convert("RGBA"))
    small = square(main_shape(full), margin=0.02)

    full.resize((256, 256), Image.LANCZOS).save(ASSETS / "icon.png")
    small.resize((256, 256), Image.LANCZOS).save(ASSETS / "icon-small.png")

    frames = [small.resize((s, s), Image.LANCZOS) for s in SMALL]
    frames += [full.resize((s, s), Image.LANCZOS) for s in LARGE]
    frames[-1].save(ASSETS / "icon.ico", format="ICO",
                    sizes=[f.size for f in frames], append_images=frames[:-1])
    print(f"icons built from {source.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
