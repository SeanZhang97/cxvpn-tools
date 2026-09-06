from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageChops


ASSET_SIZE = 512


def _apply_alpha(image: Image.Image, alpha: Image.Image) -> Image.Image:
    rgba = Image.new("RGBA", image.size, (0, 0, 0, 0))
    visible = alpha.point(lambda value: 255 if value else 0)
    rgba.paste(image.convert("RGB"), (0, 0), visible)
    rgba.putalpha(alpha)
    return rgba


def _selected_line_logo(source: Path, assets_dir: Path) -> tuple[Image.Image, Image.Image, Image.Image]:
    source_image = Image.open(source).convert("RGB")
    if source_image.size == (520, 520):
        crop = source_image
    else:
        crop = source_image.crop((110, 140, 630, 660))
        crop.save(assets_dir / "chaoxing-line-source.png", optimize=True)
    crop = crop.resize((ASSET_SIZE, ASSET_SIZE), Image.Resampling.LANCZOS)

    red, green, blue = crop.split()
    luminance = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    signal = luminance.point(lambda value: 0 if value < 42 else min(255, round((value - 42) * 3.2)))

    radial = Image.new("L", crop.size, 0)
    radial_pixels = radial.load()
    center_x, center_y = 248, 252
    for y in range(ASSET_SIZE):
        for x in range(ASSET_SIZE):
            distance = math.hypot(x - center_x, y - center_y)
            if distance <= 184:
                radial_pixels[x, y] = 255
            elif distance < 196:
                radial_pixels[x, y] = round(255 * (196 - distance) / 12)
    alpha = ImageChops.multiply(signal, radial)

    logo = _apply_alpha(crop, alpha)

    star_region = Image.new("L", crop.size, 0)
    star_region_pixels = star_region.load()
    for y in range(235):
        for x in range(230, ASSET_SIZE):
            star_region_pixels[x, y] = 255
    star_alpha = ImageChops.multiply(alpha, star_region)
    paths_alpha = ImageChops.subtract(alpha, star_alpha)

    paths = _apply_alpha(crop, paths_alpha)
    star = _apply_alpha(crop, star_alpha)
    return logo, paths, star


def _network_orbit(source: Path) -> Image.Image:
    image = Image.open(source).convert("RGB")
    red, green, blue = image.split()
    luminance = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    alpha = luminance.point(lambda value: 0 if value < 3 else min(255, round((value - 3) * 1.18)))
    orbit = image.convert("RGBA")
    orbit.putalpha(alpha)

    signal_box = luminance.point(lambda value: 255 if value >= 8 else 0).getbbox()
    if signal_box:
        center_x, center_y = image.width / 2, image.height / 2
        radius = max(
            center_x - signal_box[0],
            signal_box[2] - center_x,
            center_y - signal_box[1],
            signal_box[3] - center_y,
        )
        crop_box = (
            max(0, round(center_x - radius)),
            max(0, round(center_y - radius)),
            min(image.width, round(center_x + radius)),
            min(image.height, round(center_y + radius)),
        )
        orbit = orbit.crop(crop_box)

    fitted_size = ASSET_SIZE - 24
    orbit = orbit.resize((fitted_size, fitted_size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (ASSET_SIZE, ASSET_SIZE), (0, 0, 0, 0))
    canvas.alpha_composite(orbit, (12, 12))
    return canvas


def build_assets(assets_dir: Path, orbit_source: Path, logo_source: Path) -> None:
    assets_dir.mkdir(parents=True, exist_ok=True)

    logo, paths, star = _selected_line_logo(logo_source, assets_dir)
    logo.save(assets_dir / "chaoxing-line.png", optimize=True)
    paths.save(assets_dir / "chaoxing-line-paths.png", optimize=True)
    star.save(assets_dir / "chaoxing-line-star.png", optimize=True)

    _network_orbit(orbit_source).save(assets_dir / "chaoxing-network-orbit.png", optimize=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the Chaoxing line-art status icon assets.")
    parser.add_argument("--assets-dir", type=Path, default=Path("ui/assets"))
    parser.add_argument("--orbit-source", type=Path, required=True)
    parser.add_argument("--logo-source", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    build_assets(arguments.assets_dir, arguments.orbit_source, arguments.logo_source)
