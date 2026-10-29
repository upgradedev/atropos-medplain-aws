"""Run the real anonymous journey with Chromium, including a 375px viewport.

Playwright is installed only by CI. The application runs with the real Strands
fixture model, not mocked HTTP responses. Pass --url to inspect a deployed build.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen


def wait_for_server(url: str) -> None:
    for _ in range(100):
        try:
            with urlopen(url + "/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("local application did not become ready")


def check(url: str, output: Path) -> dict:
    from playwright.sync_api import expect, sync_playwright

    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
        allowed_host = urlparse(url).hostname
        context.route("**/*", lambda route: route.continue_() if urlparse(route.request.url).hostname == allowed_host else route.abort())
        page = context.new_page()
        page.set_default_timeout(120_000)
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        response = page.goto(url, wait_until="networkidle")
        assert response and response.status == 200, "anonymous root must return HTTP 200"
        cards = page.locator("#packet-options .packet-card")
        expect(cards).to_have_count(3)
        expect(page.locator("#explain-button")).to_be_enabled()
        expect(page.locator("#download-receipt")).to_be_disabled()
        expect(page.locator("#download-reason")).to_be_visible()
        packets = cards.evaluate_all("nodes => nodes.map(node => ({ id: node.dataset.packetId, stress: node.dataset.adversarial === 'true' }))")
        for packet in packets:
            page.locator(f'.packet-card[data-packet-id="{packet["id"]}"]').click()
            expect(page.locator("#explain-button")).to_be_enabled()
            expect(page.locator("#source-text")).not_to_have_text("Loading the selected synthetic record...")
            if packet["stress"]:
                expect(page.locator("#stress-toggle")).to_be_checked()
            with page.expect_response(lambda res: res.url.endswith("/api/explain") and res.request.method == "POST") as completed:
                page.locator("#explain-button").click()
            result = completed.value.json()
            assert result.get("status") == "verified", f"{packet['id']}: backend refused final explanation"
            expect(page.locator("#result-badge")).to_have_text("SOURCE FIDELITY VERIFIED")
            expect(page.locator("#explanation")).to_be_visible()
            expect(page.locator("#verification-section")).to_be_visible()
            if packet["stress"]:
                expect(page.locator("#correction-callout")).to_be_visible()
                expect(page.locator('#trace-list li[data-status="refused"]')).not_to_have_count(0)
            with page.expect_download() as download_info:
                page.locator("#download-receipt").click()
            download_info.value.save_as(output / f'{packet["id"]}-receipt.json')
            page.locator("#verify-receipt").click()
            expect(page.locator("#chain-verification")).to_contain_text("Chain integrity verified")
        page.screenshot(path=str(output / "desktop.png"), full_page=True)
        page.set_viewport_size({"width": 375, "height": 812})
        page.reload(wait_until="networkidle")
        expect(page.locator("#explain-button")).to_be_enabled()
        page.locator('.packet-card[data-adversarial="true"]').click()
        expect(page.locator("#explain-button")).to_be_enabled()
        page.locator("#explain-button").click()
        expect(page.locator("#result-badge")).to_have_text("SOURCE FIDELITY VERIFIED")
        expect(page.locator("#correction-callout")).to_be_visible()
        overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        assert not overflow, "375px page has horizontal overflow"
        page.screenshot(path=str(output / "mobile-375.png"), full_page=True)
        assert not page_errors, f"browser errors: {page_errors}"
        result = {"url": url, "viewports": [1280, 375], "packets": packets, "page_errors": page_errors, "horizontal_overflow_375": overflow}
        (output / "results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        browser.close()
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--output", type=Path, default=Path("artifacts/browser"))
    args = parser.parse_args()
    process = None
    server_log = None
    try:
        url = args.url or "http://127.0.0.1:8765"
        if not args.url:
            args.output.mkdir(parents=True, exist_ok=True)
            server_log = (args.output / "server.log").open("w", encoding="utf-8")
            environment = dict(os.environ, MEDPLAIN_MODEL_MODE="fixture", AWS_EC2_METADATA_DISABLED="true", OTEL_SDK_DISABLED="true")
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "medplain.api:app", "--host", "127.0.0.1", "--port", "8765"],
                env=environment, stdout=server_log, stderr=subprocess.STDOUT,
            )
            wait_for_server(url)
        print(json.dumps(check(url, args.output), indent=2))
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        if server_log is not None:
            server_log.close()
