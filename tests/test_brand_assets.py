from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "viewer" / "public"


def test_favicon_svg_is_self_contained_brand_artwork() -> None:
    svg_path = PUBLIC / "favicon.svg"
    root = ET.fromstring(svg_path.read_text(encoding="utf-8"))

    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.attrib["viewBox"] == "0 0 64 64"
    assert {node.tag.rsplit("}", 1)[-1] for node in root.iter()} <= {"svg", "rect", "path"}
    assert {node.attrib.get("fill") for node in root.iter() if node.attrib.get("fill")} == {
        "#292B2D",
        "#277451",
        "#A83A2A",
        "#F7F4EC",
    }
    assert "script" not in svg_path.read_text(encoding="utf-8").lower()
    assert "href=" not in svg_path.read_text(encoding="utf-8").lower()


def test_favicon_rasters_have_expected_dimensions() -> None:
    with (PUBLIC / "favicon.ico").open("rb") as handle:
        reserved, image_type, count = struct.unpack("<HHH", handle.read(6))
        entries = [struct.unpack("<BBBBHHII", handle.read(16)) for _ in range(count)]

    assert (reserved, image_type) == (0, 1)
    assert {(entry[0] or 256, entry[1] or 256) for entry in entries} == {
        (16, 16),
        (32, 32),
        (48, 48),
    }

    with Image.open(PUBLIC / "apple-touch-icon.png") as image:
        assert image.size == (180, 180)
        assert image.mode == "RGB"


def test_index_declares_all_brand_icons() -> None:
    index = (ROOT / "viewer" / "index.html").read_text(encoding="utf-8")

    assert '<link rel="icon" type="image/svg+xml" href="/favicon.svg?v=1" />' in index
    assert '<link rel="icon" href="/favicon.ico?v=1" sizes="any" />' in index
    assert '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png?v=1" />' in index
