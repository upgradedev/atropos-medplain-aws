"""Render the bounded editorial assets in CI, without application or model execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/assets/story"
COVER_SHA = "a7a923e56e8eb377a8e6f2afa21fdec925c2a8835a3aea94db452f19897009c5"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dimensions(path: Path) -> tuple[int, int]:
    raw = path.read_text(encoding="utf-8")
    if re.search(r"<!DOCTYPE|<!ENTITY|<script\b|https?://(?!www\.w3\.org/2000/svg)", raw, re.I):
        raise ValueError(f"SVG must be self-contained and non-executable: {path.name}")
    node = ET.fromstring(raw)
    for child in node.iter():
        if child.tag.rsplit("}", 1)[-1] in {"script", "foreignObject", "image", "use"}:
            raise ValueError(f"Unsupported SVG element in {path.name}")
        if any(key.lower().startswith("on") for key in child.attrib):
            raise ValueError(f"Event handler in {path.name}")
    size = tuple(int(node.attrib[key]) for key in ("width", "height"))
    if not all(320 <= value <= 2400 for value in size):
        raise ValueError(f"Unbounded SVG dimensions: {path.name}")
    return size


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("Rendering is CI-only; do not install or render on the local C: drive.")
    output = args.output.resolve()
    if not output.is_relative_to(ROOT):
        parser.error("Output must remain inside this checkout.")
    output.mkdir(parents=True, exist_ok=True)
    source_cover = SOURCE / "cover-source.png"
    if sha(source_cover) != COVER_SHA:
        raise ValueError("The reviewed original CGI cover has changed.")

    from PIL import Image, ImageOps
    from playwright.sync_api import sync_playwright

    with Image.open(source_cover) as original:
        cover = ImageOps.fit(original.convert("RGB"), (1200, 675), Image.Resampling.LANCZOS)
        cover.save(output / "cover.jpg", "JPEG", quality=92, optimize=True, progressive=True)
    sources = {"cover-source.png": COVER_SHA}
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            for name in ("workflow", "architecture"):
                source = SOURCE / f"{name}.svg"
                width, height = dimensions(source)
                sources[source.name] = sha(source)
                page = browser.new_page(viewport={"width": width, "height": height})
                try:
                    page.route("**/*", lambda route: route.abort())
                    markup = source.read_text(encoding="utf-8")
                    page.set_content(
                        '<!doctype html><html><head><meta charset="utf-8">'
                        '<style>html,body{margin:0;background:#0b131b}svg{display:block}</style>'
                        f"</head><body>{markup}</body></html>"
                    )
                    page.locator("svg").screenshot(path=str(output / f"{name}.png"))
                finally:
                    page.close()
        finally:
            browser.close()
    assets = {}
    for name in ("cover.jpg", "workflow.png", "architecture.png"):
        path = output / name
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            size = image.size
        if path.stat().st_size > 2_000_000:
            raise ValueError(f"Builder upload exceeds the bounded 2 MB allowance: {name}")
        assets[name] = {"sha256": sha(path), "bytes": path.stat().st_size, "size": size}
    manifest = {
        "schema": "medplain.editorial-assets-render.v1",
        "source_commit": os.environ["GITHUB_SHA"],
        "ci_run_id": os.environ["GITHUB_RUN_ID"],
        "sources": sources,
        "outputs": assets,
        "kind": "Original CGI cover and explanatory diagrams, not clinical or model evidence",
        "application_or_model_calls": 0,
    }
    (output / "render-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
