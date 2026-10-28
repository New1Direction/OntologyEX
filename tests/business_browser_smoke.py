#!/usr/bin/env python3
"""Headless Chrome smoke for the business operating picture. Synthetic demo data only."""
from __future__ import annotations

import importlib.util
import json
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "ontologyex_operating_picture_smoke", ROOT / "examples/operating-picture/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def main():
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        raise RuntimeError("Chrome/Chromium is required; no silent browser-test skip")
    with tempfile.TemporaryDirectory(prefix="ontologyex-business-smoke-") as tmp:
        built = demo.build(Path(tmp) / "demo")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([*built["command"], "--port", str(port)], cwd=ROOT,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        driver = None
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("control room exited: " + process.stderr.read())
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/business", timeout=1):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("control room did not start")
            options = Options()
            options.binary_location = chrome
            for arg in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                        "--disable-gpu", "--no-first-run", "--window-size=1440,900"):
                options.add_argument(arg)
            options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
            driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(20)
            for width, height in ((390, 844), (1024, 768), (1440, 900)):
                driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                    "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
                driver.get(f"http://127.0.0.1:{port}/business")
                WebDriverWait(driver, 10).until(lambda d: d.execute_script(
                    "return document.querySelectorAll('#graph g.node').length===9"))
                layout = driver.execute_script("""
                    const doc=document.documentElement;
                    return {overflow: doc.scrollWidth - doc.clientWidth,
                            kpis: document.querySelectorAll('#kpis .kpi').length,
                            first: document.querySelector('#attention .attn').textContent,
                            empty: getComputedStyle(document.getElementById('empty')).display};
                """)
                assert layout["overflow"] <= 1, (width, layout)
                assert layout["kpis"] == 6 and layout["empty"] == "none", layout
                assert "IssueRefund" in layout["first"] and "AT RISK" in layout["first"], layout
                print(f"CHROME business view {width}x{height}: 9 nodes, no page overflow", flush=True)
            driver.find_element("css selector", "#attention .attn").click()
            WebDriverWait(driver, 5).until(lambda d: "IssueRefund" in d.find_element("id", "inspector").text)
            inspector = driver.find_element("id", "inspector").text
            assert "AT RISK" in inspector and "payments reports error" in inspector, inspector
            assert "G3" in inspector and "remaining-balance" in inspector, inspector
            dimmed = driver.execute_script("return document.querySelectorAll('#graph g.node.dim').length")
            assert dimmed >= 1, dimmed
            search = driver.find_element("id", "search")
            search.send_keys("ledger")
            WebDriverWait(driver, 5).until(lambda d: d.execute_script(
                "return [...document.querySelectorAll('#list-systems .row')].map(r=>r.textContent).join('|')").startswith("ledger"))
            assert "payments" not in driver.execute_script(
                "return [...document.querySelectorAll('#list-systems .row')].map(r=>r.textContent).join('|')")
            print("CHROME attention click, inspector grades, search filter", flush=True)
            errors = [entry for entry in driver.get_log("browser") if entry["level"] == "SEVERE"]
            assert not errors, json.dumps(errors)
            print("CHROME no console errors", flush=True)

            # One-tap linking: start unlinked, confirm a suggestion in the page.
            unlinked = demo.build(Path(tmp) / "unlinked", unlinked=True)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port2 = sock.getsockname()[1]
            second = subprocess.Popen([*unlinked["command"], "--port", str(port2), "--allow-link-decisions"],
                                      cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port2}/api/business", timeout=1):
                            break
                    except OSError:
                        time.sleep(0.1)
                driver.get(f"http://127.0.0.1:{port2}/business")
                WebDriverWait(driver, 10).until(lambda d: d.execute_script(
                    "return document.querySelectorAll('#suggestions .sug').length===2"))
                assert "UNLINKED" in driver.find_element("id", "attention").text
                driver.find_element("id", "who").send_keys("smoke-test")
                driver.execute_script("""
                    const card=[...document.querySelectorAll('#suggestions .sug')]
                      .find(c=>c.textContent.includes('payments.apply_refund'));
                    card.querySelector('.btn.yes').click();""")
                WebDriverWait(driver, 10).until(lambda d: "IssueRefund" in d.find_element(
                    "css selector", "#attention .attn").text and "AT RISK" in d.find_element(
                    "css selector", "#attention .attn").text)
                links = json.loads(Path(unlinked["links"]).read_text())["links"]
                assert [(l["binding"], l["declared_by"]) for l in links] == [
                    ("payments.apply_refund", "smoke-test")], links
                print("CHROME one-tap link confirmation updates the links file and health", flush=True)
            finally:
                second.terminate()
                second.wait(timeout=3)
        finally:
            if driver is not None:
                driver.quit()
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
