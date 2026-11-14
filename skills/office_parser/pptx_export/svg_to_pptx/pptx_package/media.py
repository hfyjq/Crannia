"""SVG to PNG conversion for Office compatibility mode."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

# SVG to PNG library detection
# CairoSVG / svglib 都是 LGPL，改用 `resvg_py`
#   （wrapper MIT · resvg 0.48+ = Apache-2.0 OR MIT）。这同时避开一个真问题：
#   runtime 里没有 `libcairo-2.dll`，CairoSVG 在随包环境里从来就没加载成功过。
PNG_RENDERER: str | None = None

try:
    import resvg_py
    PNG_RENDERER = 'resvg'
except (ImportError, OSError):
    pass


def get_png_renderer_info() -> tuple[str | None, str, str | None]:
    """Get PNG renderer status information.

    Returns:
        (renderer_name, status_text, install_hint) tuple.
    """
    if PNG_RENDERER == 'resvg':
        return ('resvg', '(full gradient/filter support)', None)
    else:
        return (None, '(not installed)',
                'Install via: pip install resvg-py')


def convert_svg_to_png(
    svg_path: Path,
    png_path: Path,
    width: int | None = None,
    height: int | None = None,
) -> bool:
    """Convert SVG to PNG using the available renderer.

    Args:
        svg_path: SVG file path.
        png_path: Output PNG file path.
        width: Output width in pixels.
        height: Output height in pixels.

    Returns:
        Whether the conversion was successful.
    """
    if PNG_RENDERER is None:
        return False

    try:
        if PNG_RENDERER == 'resvg':
            png = resvg_py.svg_to_bytes(
                svg_path=str(svg_path),
                width=width,
                height=height,
            )
            Path(png_path).write_bytes(png)
            return True

    except Exception as e:
        print(f"  Warning: SVG to PNG conversion failed ({svg_path.name}): {e}")
        return False

    return False


def _cache_key(svg_path: Path, width: int | None, height: int | None) -> str:
    h = hashlib.sha256()
    with open(svg_path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return f"{h.hexdigest()}_{width or 0}x{height or 0}_{PNG_RENDERER or 'none'}"


def convert_svg_to_png_cached(
    svg_path: Path,
    png_path: Path,
    width: int | None = None,
    height: int | None = None,
    cache_dir: Path | None = None,
) -> bool:
    """Cache-aware SVG→PNG conversion.

    Returns True on success (cache hit or fresh render). Cache key bakes in
    SVG content hash + size + renderer name; switching renderers invalidates
    naturally. Failures are never cached.
    """
    if cache_dir is None:
        return convert_svg_to_png(svg_path, png_path, width, height)

    if PNG_RENDERER is None:
        return False

    try:
        key = _cache_key(svg_path, width, height)
    except OSError as e:
        print(f"  Warning: Failed to hash SVG ({svg_path.name}): {e}")
        return convert_svg_to_png(svg_path, png_path, width, height)

    cached = cache_dir / f"{key}.png"
    if cached.is_file():
        try:
            shutil.copy(cached, png_path)
            return True
        except OSError as e:
            print(f"  Warning: Cache copy failed, re-rendering ({svg_path.name}): {e}")

    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(suffix='.png', dir=str(cache_dir))
    tmp_path = Path(tmp_name)
    import os
    os.close(tmp_fd)

    ok = convert_svg_to_png(svg_path, tmp_path, width, height)
    if not ok:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return False

    try:
        os.replace(tmp_path, cached)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    try:
        shutil.copy(cached, png_path)
        return True
    except OSError as e:
        print(f"  Warning: Cache copy failed ({svg_path.name}): {e}")
        return False
