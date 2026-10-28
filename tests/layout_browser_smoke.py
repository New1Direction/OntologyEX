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
from datetime import datetime, timezone

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
            "services:\n  web:\n    depends_on: [api]\n  api:\n    depends_on: [db]\n  db: {}\n" +
            "".join(f"  test_service_{i:03d}: {{}}\n" for i in range(400)),
            encoding="utf8",
        )
        (repo / "package.json").write_text('{"name":"smoke"}', encoding="utf8")
        observations = root / "health.jsonl"
        observations.write_text(json.dumps({
            "schema_version":1,
            "component_id":"discovery:service:compose.yml:test_service_399",
            "observed_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
            "status":"error", "source":"synthetic",
            "expires_after_seconds":86400,"metrics":{}
        })+"\n",encoding="utf-8")
        port = available_port()
        cmd = [sys.executable, str(SCRIPT), "--repo", str(repo),
               "--observations",str(observations),"--port", str(port)]
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
                safe_before = driver.execute_script("""
                    const rs=[...document.querySelectorAll('#workspace>section')]
                      .map(el=>el.getBoundingClientRect());
                    return rs.every((a,i)=>rs.every((b,j)=>i===j||
                      !(a.left < b.right-1 && a.right > b.left+1 &&
                        a.top < b.bottom-1 && a.bottom > b.top+1)));
                """)
                assert safe_before, ("pre-discovery controls must not overlap",width,height)
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

            # Real >400-node repository fixture: no whole graph is sent by API.
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state") as response:
                state_summary=json.load(response)
            assert state_summary["kind"]=="LOCAL_CONTROL_ROOM_SUMMARY"
            assert state_summary["total_nodes"]>=400
            assert all(k not in state_summary for k in ("nodes","edges","source_evidence"))
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/view?lens=all&q=&offset=0") as response:
                first=json.load(response)
            assert first["kind"]=="BOUNDED_GRAPH_PAGE" and len(first["nodes"])==90
            assert first["total_matches"]>=400 and first["next_offset"]==90
            assert all(e["source"] in {n["id"] for n in first["nodes"]} and
                       e["target"] in {n["id"] for n in first["nodes"]}
                       for e in first["edges"])
            driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                "width":1440,"height":900,"deviceScaleFactor":1,"mobile":False})
            driver.get(f"http://127.0.0.1:{port}/")
            driver.find_element("id","map-business").click()
            WebDriverWait(driver,10).until(lambda d:d.execute_script(
                "return !!lastPage && lastPage.visible_count===90 && !!lastLayout"))
            assert driver.find_element("id","graph-next").is_enabled()
            driver.find_element("id","graph-next").click()
            WebDriverWait(driver,10).until(lambda d:d.execute_script(
                "return !!lastPage && lastPage.offset===90"))
            driver.find_element("id","graph-prev").click()
            WebDriverWait(driver,10).until(lambda d:d.execute_script(
                "return !!lastPage && lastPage.offset===0"))
            search=driver.find_element("id","graph-search")
            search.send_keys("test_service_399")
            WebDriverWait(driver,10).until(lambda d:d.execute_script(
                "return !!lastPage && lastPage.query==='test_service_399' && lastPage.total_matches===1"))
            assert "Showing 1 of 1" in driver.find_element("id","graph-coverage").text
            assert "ERROR" in driver.find_element("id","incident-list").text
            driver.find_element("css selector",'#svg g[role="button"]').click()
            WebDriverWait(driver,10).until(lambda d:
                "TEST_SERVICE_399" in
                d.find_element("id","details").text)
            assert "SOURCE FILE: compose.yml" in driver.find_element("id","details").text
            print("CHROME 405-node repo; bounded paging, search, on-demand evidence verified",flush=True)
            # Navigate across graph pages using the server-side edge set.
            driver.find_element("id","graph-back").click()
            WebDriverWait(driver,10).until(lambda d:d.execute_script(
                "return state?.kind==='BOUNDED_GRAPH_PAGE'"))
            driver.execute_script("""
                const q=document.getElementById("graph-search");
                q.value="web";q.dispatchEvent(new Event("input",{bubbles:true}));
            """)
            WebDriverWait(driver,10).until(lambda d:d.execute_script(
                "return state?.kind==='BOUNDED_GRAPH_PAGE' && state?.query==='web' && state.nodes.some(n=>n.label==='web')"))
            driver.find_element("css selector",'#svg g[role="button"]').click()
            WebDriverWait(driver,10).until(lambda d:d.execute_script(
                "return state?.kind==='BOUNDED_GRAPH_NEIGHBORHOOD' && state?.requested_depth===1 && state.nodes.some(n=>n.label==='api')"))
            one=driver.execute_script("""
                return {center:state.center_id,kind:state.kind,
                  edges:state.edges.map(e=>e.relation),
                  nodeLabels:state.nodes.map(n=>n.label),
                  backEnabled:!document.getElementById("graph-back").disabled};
            """)
            assert one["center"].endswith(":web") and one["backEnabled"]
            assert "api" in one["nodeLabels"] and "db" not in one["nodeLabels"]
            assert "DEPENDS_ON" in one["edges"]
            driver.execute_script("""
                const depth=document.getElementById("graph-depth");
                depth.value="2";depth.dispatchEvent(new Event("change",{bubbles:true}));
            """)
            WebDriverWait(driver,10).until(lambda d:d.execute_script(
                "return state?.kind==='BOUNDED_GRAPH_NEIGHBORHOOD' && state?.requested_depth===2 && state.nodes.some(n=>n.label==='db')"))
            two=driver.execute_script("""
                return {nodes:state.nodes.length,edges:state.edges.length,
                  completeness:state.is_complete_within_depth,
                  scope:state.scope,
                  webToApi:state.edges.some(e=>e.relation==="DEPENDS_ON"&&
                    e.source.endsWith(":web")&&e.target.endsWith(":api")),
                  apiToDb:state.edges.some(e=>e.relation==="DEPENDS_ON"&&
                    e.source.endsWith(":api")&&e.target.endsWith(":db"))};
            """)
            assert two["webToApi"] and two["apiToDb"],two
            assert two["nodes"]<=90 and two["edges"]<=300
            assert two["scope"]=="BIDIRECTIONAL_GRAPH_NAVIGATION_NOT_CAUSAL_IMPACT"
            driver.find_element("id","graph-back").click()
            WebDriverWait(driver,10).until(lambda d:d.execute_script(
                "return state?.kind==='BOUNDED_GRAPH_PAGE' && !document.getElementById('graph-back').disabled===false"))
            print("CHROME 1/2-hop cross-page dependency navigation, direction, safety and back verified",flush=True)
            driver.find_element("id", "graph-focus").click()
            assert driver.find_element("id", "graph-focus").get_attribute("aria-pressed") == "true"
            driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {
                "features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})
            transition = driver.execute_script(
                "return getComputedStyle(document.getElementById('map')).transitionDuration")
            assert all(float(v.replace("s", "")) == 0 for v in transition.split(",")), transition
            driver.refresh()
            assert driver.find_element("id", "graph-focus").get_attribute("aria-pressed") == "true"
            # A refresh is deliberately not an automatic source discovery.
            driver.find_element("id", "map-business").click()
            WebDriverWait(driver, 10).until(lambda d: d.execute_script(
                "return typeof lastLayout !== 'undefined' && !!lastLayout"))
            print("CHROME graph preference persisted, reduced-motion disables transitions",
                  flush=True)
            # Explicit noncritical collapse must keep its toggle accessible.
            driver.find_element("id", "toggle-versions").click()
            assert driver.find_element("id", "toggle-versions").get_attribute("aria-expanded") == "false"
            assert driver.execute_script(
                "return getComputedStyle(document.getElementById('versions')).display") == "none"
            # User-controlled width range overrides temporary graph focus.
            resized = driver.execute_script("""
                const slider=document.getElementById("graph-share");
                slider.value="76";
                slider.dispatchEvent(new Event("input",{bubbles:true}));
                return {share:slider.value,
                   focus:document.getElementById("graph-focus").getAttribute("aria-pressed"),
                   valid:OntologyLayout.verifyDOM(lastLayout,document.getElementById("workspace")).ok};
            """)
            assert resized["share"] == "76" and resized["focus"] == "false", resized
            driver.refresh()
            assert driver.find_element("id", "graph-share").get_attribute("value") == "76"
            assert driver.find_element("id", "toggle-versions").get_attribute("aria-expanded") == "false"
            print("CHROME explicit revision collapse and graph-width preference persist",flush=True)
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
