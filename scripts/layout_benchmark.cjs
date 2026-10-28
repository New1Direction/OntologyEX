"use strict";
const {performance}=require("node:perf_hooks");
const L=require("../ontology-extraction/scripts/layout_engine.js");
const input={
 width:1440,height:900,density:401,panels:[
 {id:"map",minWidth:240,minHeight:310,preferredHeight:540,priority:78},
 {id:"incidents",minWidth:200,minHeight:116,preferredHeight:145,priority:98,critical:true,items:25},
 {id:"integrity",minWidth:200,minHeight:82,preferredHeight:92,priority:96,critical:true},
 {id:"details-panel",minWidth:200,minHeight:180,preferredHeight:235,priority:85},
 {id:"coverage-panel",minWidth:200,minHeight:142,preferredHeight:180,priority:62},
 {id:"versions-panel",minWidth:200,minHeight:95,preferredHeight:108,priority:35},
 {id:"boundary-panel",minWidth:200,minHeight:104,preferredHeight:124,priority:32}
 ]};
const iterations=2500;
const timings=[];
for(let i=0;i<iterations;i++){
 const t=performance.now();const p=L.optimize(input);timings.push(performance.now()-t);
 if(!p.constraints.ok)throw Error("invalid layout");
}
timings.sort((a,b)=>a-b);
const median=timings[Math.floor(timings.length*.5)];
const p95=timings[Math.floor(timings.length*.95)];
// Exact old code: max(550,...cols.map(a=>a.length*76+135))
// Compare worst-case 401 nodes in one column; new cap=90, same worst case.
// Rendering budget reduction is not a CPU speedup claim or a browser benchmark.
const legacyWorstSvgHeight=Math.max(550,401*76+135);
const boundedWorstSvgHeight=Math.max(350,90*64+88);
const report={
 input:{viewport:"1440x900",entities:401,panels:7,iterations},
 solver:{medianMs:Number(median.toFixed(4)),p95Ms:Number(p95.toFixed(4))},
 previousGraph:{maxRenderedNodes:401,worstCaseSvgHeightPx:legacyWorstSvgHeight},
 newGraph:{maxRenderedNodes:90,worstCaseSvgHeightPx:boundedWorstSvgHeight},
 worstCaseGraphHeightReductionPct:Number((100*(1-boundedWorstSvgHeight/legacyWorstSvgHeight)).toFixed(2)),
 caveats:["Node CPU benchmark, not browser paint/layout time",
 "Worst-case SVG height comparison, not a measurement of the actual repo.ing canvas",
 "Runtime data shown remains source-reported; no production health proof"]
};
console.log(JSON.stringify(report,null,2));
if(process.argv.includes("--check") &&
 (!Number.isFinite(p95)||!L.optimize(input).constraints.ok||
 boundedWorstSvgHeight>=legacyWorstSvgHeight))
 process.exit(1);
