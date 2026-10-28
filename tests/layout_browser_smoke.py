#!/usr/bin/env python3
"""Headless Chrome smoke: actual OntologyEX control-room, no external services.

Uses disposable local source fixtures. No live GitHub token or production data.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ontology-extraction/scripts/control_room.py"


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        raise RuntimeError("Chrome/Chromium is required; no silent browser-test skip")
    with tempfile.TemporaryDirectory(prefix="ontologyex-browser-fixture-") as tmp:
        root = Path(tmp)
        repo = root / "business"
        repo.mkdir()
        (repo / "compose.yml").write_text(
            "services:\n  web:\n    depends_on: [api]\n  api:\n    depends_on: [db]\n  db: {}\n",
            encoding="utf8",
        )
        (repo / "package.json").write_text('{"name":"smoke"}', encoding="utf8")
        port = available_port()
        cmd = [sys.executable, str(SCRIPT), "--repo", str(repo),
               "--port", str(port)]
        process = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True)
        driver = None
        try:
            # Wait only for a loopback server. Browser still checks all scripts/DOM.
            import urllib.request
            for _ in range(80):
                if process.poll() is not None:
                    raise RuntimeError("control-room server exited before browser test")
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/static/layout_engine.js",
                        timeout=1,
                    ) as resp:
                        if resp.status == 200:
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("local control-room server was not available")

            options = Options()
            options.binary_location = chrome
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-first-run")
            options.add_argument("--window-size=1440,900")
            driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(20)
            for width, height in [(320, 640), (390, 844), (768, 1024),
                                  (1024, 768), (1440, 900), (1920, 1080)]:
                driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                    "width": width, "height": height,
                    "deviceScaleFactor": 1, "mobile": False})
                driver.get(f"http://127.0.0.1:{port}/")
                driver.find_element("id", "map-business").click()
                WebDriverWait(driver, 10).until(lambda d: d.execute_script(
                    "return typeof lastLayout !== 'undefined' && !!lastLayout && "
                    "document.getElementById('graph-coverage').textContent.includes('Showing ')"))
                report = driver.execute_script("""
                    const plan=lastLayout;
                    const root=document.getElementById('workspace');
                    const result=OntologyLayout.verifyDOM(plan,root);
                    return {cols:plan.columns,valid:result.ok,violations:result.violations,
                      panelCount:Object.keys(plan.rects).length,
                      width:document.documentElement.scrollWidth,
                      viewport:document.documentElement.clientWidth,
                      incidentY:plan.rects.incidents.y,mapY:plan.rects.map.y,
                      controls:['map-business','graph-search','graph-focus','lens','incidents']
                         .every(id=>!!document.getElementById(id))};
                """)
                assert report["valid"], (width, height, report)
                assert report["controls"] and report["panelCount"] == 7
                assert report["width"] <= report["viewport"]+3, (width,report)
                if width <= 390:
                    assert report["cols"] == 1 and report["incidentY"] < report["mapY"]
                print("CHROME",width,height,"layout",report["cols"],"verified",flush=True)

            # Dense data: keep all 400 entities searchable without rendering a
            # 400-node SVG or suppressing the critical incident panel.
            driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                "width": 1440, "height": 900,
                "deviceScaleFactor": 1, "mobile": False})
            dense = driver.execute_script("""
                const s=JSON.parse(JSON.stringify(state));
                const base=s.nodes.find(n=>n.kind==='project')||s.nodes[0];
                for(let i=0;i<400;i++)s.nodes.push({...base,
                  id:'discovery:source_module:test/'+i,kind:'source_module',
                  label:'synthetic-module-'+i,status:i===399?'error':'unknown',
                  potentially_impacted_by:[],ci_run:null});
                s.coverage.node_status_counts.error=1;
                s.coverage.node_status_counts.unknown=s.nodes.length-1;
                draw(s);
                return {shown:document.querySelectorAll('#svg g[role="button"]').length,
                  label:document.getElementById('graph-coverage').textContent,
                  failures:document.getElementById('incident-list').textContent,
                  valid:OntologyLayout.verifyDOM(lastLayout,
                    document.getElementById('workspace')).ok};
            """)
            assert dense["shown"] <= 90 and dense["shown"] > 0, dense
            assert "405" in dense["label"] and "synthetic-module-399" in dense["failures"], dense
            WebDriverWait(driver, 4).until(lambda d: d.execute_script("""
                return OntologyLayout.verifyDOM(lastLayout,
                  document.getElementById('workspace')).ok
            """))
            found = driver.execute_script("""
                const search=document.getElementById('graph-search');
                search.value='synthetic-module-399';
                search.dispatchEvent(new Event('input',{bubbles:true}));
                return document.getElementById('graph-coverage').textContent;
            """)
            assert "Showing 1 of 1" in found, found
            print("CHROME dense graph 400 entities, prioritized incident and search verified",
                  flush=True)
            driver.find_element("id", "graph-focus").click()
            assert driver.find_element("id", "graph-focus").get_attribute("aria-pressed") == "true"
            driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {
                "features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})
            transition = driver.execute_script(
                "return getComputedStyle(document.getElementById('map')).transitionDuration")
            assert all(float(v.replace("s", "")) == 0 for v in transition.split(",")), transition
            driver.refresh()
            assert driver.find_element("id", "graph-focus").get_attribute("aria-pressed") == "true"
            print("CHROME graph preference persisted, reduced-motion disables transitions",
                  flush=True)
        finally:
            if driver is not None:
                driver.quit()
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


if __name__ == "__main__":
    main()
