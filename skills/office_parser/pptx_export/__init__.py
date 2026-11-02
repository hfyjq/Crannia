"""
PPTX export package — vendored from PPT Master (MIT License).
Copyright (c) 2025-2026 Hugo He. Full licence text:
`skills/office_parser/references/ppt-master/LICENSE`.

Provides SVG-to-DrawingML PPTX conversion with native PowerPoint shapes.
Hard dependency: python-pptx. Soft deps: resvg-py, Pillow (graceful fallbacks).

Public API:
    create_pptx_with_native_svg(svg_files, output_path, ...) -> bool
    convert_svg_to_slide_shapes(svg_path, ...) -> tuple
"""

from .svg_to_pptx import convert_svg_to_slide_shapes
from .svg_to_pptx.pptx_package.builder import create_pptx_with_native_svg

__all__ = [
    "convert_svg_to_slide_shapes",
    "create_pptx_with_native_svg",
]
