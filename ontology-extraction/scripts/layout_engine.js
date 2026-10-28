/* OntologyEX deterministic control-room layout v1.
 * Pure browser/Node geometry model. Does not claim CSS or text proof.
 * No network, external dependencies, secrets, or framework coupling.
 */
(function (root, make) {
  "use strict";
  const API = make();
  if (typeof module !== "undefined" && module.exports) module.exports = API;
  if (root) root.OntologyLayout = API;
})(typeof globalThis === "object" ? globalThis : null, function () {
  "use strict";
  const MAX_PANELS = 24;
  const MARGIN = 12, GAP = 12, MIN_MAIN = 240;
  const finite = n => typeof n === "number" && Number.isFinite(n);
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
  const sane = (n, fallback, lo, hi) => finite(n) ? clamp(n, lo, hi) : fallback;
  const px = n => Math.round(n * 2) / 2;

  function validateInput(input) {
    if (!input || !finite(input.width) || !finite(input.height) ||
        input.width < 280 || input.width > 16000 ||
        input.height < 240 || input.height > 16000) throw Error("invalid viewport bounds");
    if (!Array.isArray(input.panels) || input.panels.length < 2 ||
        input.panels.length > MAX_PANELS) throw Error("invalid panel budget");
    const ids = new Set();
    for (const p of input.panels) {
      if (!p || typeof p.id !== "string" || !/^[a-z][a-z0-9_-]{0,40}$/.test(p.id) ||
          ids.has(p.id)) throw Error("duplicate/invalid panel id");
      ids.add(p.id);
      if (!finite(p.minWidth) || !finite(p.minHeight) ||
          p.minWidth < 160 || p.minWidth > 1600 ||
          p.minHeight < 60 || p.minHeight > 1600) throw Error("invalid panel constraints");
    }
    if (!ids.has("map")) throw Error("map panel is required");
  }

  function importance(p) {
    return sane(p.priority, 10, 0, 100) + (p.critical ? 100 : 0) +
      (p.pinned ? 45 : 0);
  }

  function targetHeight(p, width, height, density) {
    const min = p.minHeight;
    if (p.collapsed && !p.critical && p.id !== "map") return px(min);
    if (p.id === "map") {
      return px(Math.max(min, clamp(height * 0.72 + Math.log2(1 + density) * 18, 320, 840)));
    }
    // Content count and urgency influence preferred sizes, but controls never disappear.
    const content = sane(p.items, 0, 0, 10000);
    const scale = p.critical ? 20 : 11;
    const pref = sane(p.preferredHeight, min + 65, min, 1400);
    const growth = Math.min(180, Math.sqrt(content) * scale);
    const spaceFactor = width < 320 ? 0.95 : 1;
    return px(Math.max(min, pref * spaceFactor + growth));
  }

  function orderPanels(panels) {
    return panels.filter(p => p.id !== "map").slice()
      .sort((a, b) => importance(b) - importance(a) || a.id.localeCompare(b.id));
  }

  function buildCandidate(input, columns, prefs) {
    const W = input.width, H = input.height;
    const available = W - 2 * MARGIN;
    const map = input.panels.find(p => p.id === "map");
    const others = orderPanels(input.panels);
    const density = sane(input.density, 0, 0, 100000);
    const rects = {};
    let mapW, sideW, thirdW;
    if (columns === 1) {
      mapW = available;
    } else if (columns === 2) {
      const minimumRight = Math.max(240, ...others.map(p => p.minWidth));
      mapW = px(clamp((available - GAP) * (prefs.focusGraph ? 0.72 : prefs.mapShare),
                      map.minWidth, available - GAP - minimumRight));
      sideW = px(available - GAP - mapW);
      if (sideW < minimumRight || mapW < map.minWidth) return null;
    } else {
      const sideMinimum = Math.max(230, ...others.map(p => p.minWidth));
      mapW = px(clamp((available - 2 * GAP) * clamp(
                        prefs.focusGraph ? 0.55 : prefs.mapShare, 0.40, 0.56),
                      map.minWidth, available - 2 * GAP - sideMinimum * 2));
      sideW = px((available - 2 * GAP - mapW) / 2);
      thirdW = px(available - 2 * GAP - mapW - sideW);
      if (sideW < sideMinimum || thirdW < sideMinimum ||
          mapW < map.minWidth) return null;
    }
    const measuredMap = targetHeight(map, mapW, H, density);
    rects.map = {x: MARGIN, y: MARGIN, width: mapW,
                 height: Math.max(measuredMap, Math.min(740, H - MARGIN * 2))};
    if (columns === 1) {
      // Prioritized notices/critical controls remain in document, before the map
      // on narrow viewports. The map is never collapsed.
      let y = MARGIN;
      const urgent = others.filter(p => p.critical);
      for (const p of urgent) {
        rects[p.id] = {x: MARGIN, y, width: available,
                       height: targetHeight(p, available, H, density)};
        y += rects[p.id].height + GAP;
      }
      rects.map.y = y;
      y += rects.map.height + GAP;
      for (const p of others.filter(p => !p.critical)) {
        rects[p.id] = {x: MARGIN, y, width: available,
                       height: targetHeight(p, available, H, density)};
        y += rects[p.id].height + GAP;
      }
    } else if (columns === 2) {
      let y = MARGIN;
      for (const p of others) {
        rects[p.id] = {x: MARGIN + mapW + GAP, y, width: sideW,
                       height: targetHeight(p, sideW, H, density)};
        y += rects[p.id].height + GAP;
      }
    } else {
      const x = [MARGIN + mapW + GAP, MARGIN + mapW + 2 * GAP + sideW];
      const widths = [sideW, thirdW];
      const ys = [MARGIN, MARGIN];
      // Bounded deterministic shortest-column assignment. The first critical
      // card is always immediately visible in the first rail.
      for (const p of others) {
        const rail = ys[0] <= ys[1] ? 0 : 1;
        rects[p.id] = {x: x[rail], y: ys[rail], width: widths[rail],
                       height: targetHeight(p, widths[rail], H, density)};
        ys[rail] += rects[p.id].height + GAP;
      }
    }
    const bottom = Math.max(...Object.values(rects).map(r => r.y + r.height)) + MARGIN;
    const plan = {width: W, viewportHeight: H, contentHeight: px(bottom),
                  columns, rects, order: Object.entries(rects)
                    .sort((a, b) => a[1].y - b[1].y || a[1].x - b[1].x)
                    .map(([id]) => id)};
    const validation = verify(plan, input.panels);
    if (!validation.ok) return null;
    const over = Math.max(0, bottom - H);
    const priorityDelay = others.reduce((sum, p) =>
      sum + Math.min(1200, rects[p.id].y) * importance(p) / 200, 0);
    const geometricPenalty = columns === 1 ? Math.max(0, W - 660) * 1.7 :
      columns === 2 ? Math.max(0, W - 1450) * 0.18 :
      Math.max(0, 1450 - W) * 1.6;
    const userPenalty = prefs.focusGraph ? (columns === 3 ? 210 : 0) : 0;
    // Minimize vertical scroll, high-priority delay, inappropriate column counts.
    plan.score = px(over * 0.38 + priorityDelay * 0.12 +
                    geometricPenalty + userPenalty);
    plan.constraints = validation;
    plan.decisions = {
      objective: "SCROLL_PLUS_PRIORITY_LATENCY_PLUS_GEOMETRY",
      focusGraph: !!prefs.focusGraph, mapShare:prefs.mapShare,
      density, scrollOverflowPx: px(over),
      chosenColumns: columns,
      fixedBreakpointsUsed: false,
      totalPanels: input.panels.length
    };
    return plan;
  }

  function optimize(input) {
    validateInput(input);
    const prefs = {focusGraph: !!input.focusGraph,
                   mapShare:sane(input.mapShare,0.63,0.48,0.76)};
    const candidates = [1, 2, 3]
      .map(columns => buildCandidate(input, columns, prefs))
      .filter(Boolean).sort((a, b) => a.score - b.score || a.columns - b.columns);
    if (!candidates.length) throw Error("no feasible arrangement");
    return {...candidates[0], candidates: candidates.map(c => ({
      columns: c.columns, score: c.score, scrollOverflowPx: c.decisions.scrollOverflowPx
    }))};
  }

  function verify(plan, panels) {
    const violations = [];
    const spec = new Map(panels.map(p => [p.id, p]));
    for (const [id, r] of Object.entries(plan.rects)) {
      const p = spec.get(id);
      if (!p || ![r.x, r.y, r.width, r.height].every(finite) ||
          r.width + 0.1 < p.minWidth || r.height + 0.1 < p.minHeight)
        violations.push({code:"MIN_SIZE", panel:id});
      if (r.x < 0 || r.y < 0 || r.x + r.width > plan.width + 0.1 ||
          r.y + r.height > plan.contentHeight + 0.1)
        violations.push({code:"OUTSIDE_CANVAS", panel:id});
    }
    if (Object.keys(plan.rects).length !== panels.length)
      violations.push({code:"INACCESSIBLE_PANEL", missing:panels
        .filter(p => !plan.rects[p.id]).map(p => p.id)});
    const ids = Object.keys(plan.rects);
    for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++) {
      const a = plan.rects[ids[i]], b = plan.rects[ids[j]];
      if (a.x < b.x+b.width-0.1 && a.x+a.width > b.x+0.1 &&
          a.y < b.y+b.height-0.1 && a.y+a.height > b.y+0.1)
        violations.push({code:"OVERLAP", panels:[ids[i], ids[j]]});
    }
    return {ok: violations.length === 0, violations};
  }

  function verifyDOM(plan, rootElement, opts) {
    const violations = [...plan.constraints.violations];
    const tolerance = opts && finite(opts.tolerance) ? opts.tolerance : 1.5;
    if (!rootElement || !rootElement.ownerDocument) {
      return {ok:false, violations:[{code:"DOM_NOT_AVAILABLE"}]};
    }
    const rootRect = rootElement.getBoundingClientRect();
    for (const id of Object.keys(plan.rects)) {
      const node = rootElement.ownerDocument.getElementById(id);
      if (!node) {violations.push({code:"DOM_PANEL_MISSING",panel:id}); continue;}
      const box = node.getBoundingClientRect(), expected = plan.rects[id];
      if (Math.abs((box.left-rootRect.left)-expected.x)>tolerance ||
          Math.abs((box.top-rootRect.top)-expected.y)>tolerance ||
          Math.abs(box.width-expected.width)>tolerance ||
          Math.abs(box.height-expected.height)>tolerance)
        violations.push({code:"DOM_GEOMETRY_DRIFT", panel:id});
      const style = rootElement.ownerDocument.defaultView?.getComputedStyle(node);
      if (node.scrollHeight > node.clientHeight + 2 &&
          !["auto","scroll"].includes(style?.overflowY))
        violations.push({code:"CONTENT_CLIPPED",panel:id,
          scrollHeight:node.scrollHeight,clientHeight:node.clientHeight});
      if (node.scrollWidth > node.clientWidth + 2 &&
          !["auto","scroll"].includes(style?.overflowX))
        violations.push({code:"HORIZONTAL_CONTENT_CLIPPED",panel:id});
    }
    return {ok:violations.length===0,violations};
  }

  return Object.freeze({optimize,verify,verifyDOM,MAX_PANELS});
});
