"use strict";
const assert = require("node:assert/strict");
const {test} = require("node:test");
const {performance} = require("node:perf_hooks");
const L = require("../ontology-extraction/scripts/layout_engine.js");

function panels(urgent=0){
  return [
    {id:"map",minWidth:240,minHeight:310,preferredHeight:540,priority:78},
    {id:"incidents",minWidth:200,minHeight:116,preferredHeight:145,priority:98,critical:true,items:urgent},
    {id:"integrity",minWidth:200,minHeight:82,preferredHeight:92,priority:96,critical:true},
    {id:"details-panel",minWidth:200,minHeight:180,preferredHeight:235,priority:85},
    {id:"coverage-panel",minWidth:200,minHeight:142,preferredHeight:180,priority:62},
    {id:"versions-panel",minWidth:200,minHeight:95,preferredHeight:108,priority:35},
    {id:"boundary-panel",minWidth:200,minHeight:104,preferredHeight:124,priority:32},
  ];
}
const model=(w,h,density=401,urgent=0)=>({width:w,height:h,density,panels:panels(urgent)});

for(const [width,height,name] of [[320,568,"mobile-small"],[390,844,"mobile"],
    [768,1024,"tablet"],[1024,768,"laptop"],[1440,900,"desktop"],[1920,1080,"wide"]]){
  test("feasible accessible "+name,()=>{
    const input=model(width,height,401,15);
    const plan=L.optimize(input);
    assert.ok(L.verify(plan,input.panels).ok);
    assert.equal(Object.keys(plan.rects).length,7);
    assert.ok(plan.contentHeight>=height || plan.contentHeight>0);
    for(const [id,r] of Object.entries(plan.rects)){
      assert.ok(r.x>=0 && r.x+r.width <= width+0.1,id);
      assert.ok(r.y>=0 && r.y+r.height <= plan.contentHeight+0.1,id);
    }
    assert.ok(plan.candidates.length>=1);
    assert.ok(plan.candidates.every(c=>c.score>=0));
    if(width<=390){
      assert.equal(plan.columns,1);
      assert.ok(plan.rects.incidents.y < plan.rects.map.y,"critical incidents before map");
      assert.ok(plan.rects.integrity.y < plan.rects.map.y,"integrity accessible");
    }
  });
}

test("deterministic across repeated recomputations and hundreds of entities",()=>{
  const input=model(1600,900,2000,100);
  const a=L.optimize(input);
  for(let i=0;i<30;i++)assert.deepEqual(L.optimize(input),a);
  assert.equal(Object.keys(a.rects).length,7);
  assert.ok(a.decisions.density===2000);
});

test("critical panel is not removed or compressed below minimum",()=>{
 const input=model(320,600,10000,5000);
 const plan=L.optimize(input);
 for(const p of input.panels)assert.ok(plan.rects[p.id].height>=p.minHeight,p.id);
 assert.ok(plan.rects.incidents.y<plan.rects.map.y);
});

test("user-selected graph preference preserved",()=>{
 const normal=L.optimize(model(1050,850,401,0));
 const focused=L.optimize({...model(1050,850,401,0),focusGraph:true});
 assert.ok(focused.decisions.focusGraph);
 assert.ok(!normal.decisions.focusGraph);
 if(normal.columns===focused.columns && normal.columns>1)
   assert.ok(focused.rects.map.width>=normal.rects.map.width);
});

test("changes in content density can expand map, but do not hide controls",()=>{
 const sparse=L.optimize(model(1250,900,3,1));
 const dense=L.optimize(model(1250,900,2000,25));
 assert.ok(dense.rects.map.height>=sparse.rects.map.height);
 assert.ok(dense.rects.incidents.height>=sparse.rects.incidents.height);
 assert.ok(dense.rects["boundary-panel"],"boundary remains reachable");
});

test("invalid input fails closed rather than creating broken rectangles",()=>{
 for(const width of [0,100,-30,NaN,Infinity,20000])
   assert.throws(()=>L.optimize(model(width,800)),/invalid viewport/);
 const p=model(1200,850);p.panels[2].id="incidents";
 assert.throws(()=>L.optimize(p),/duplicate/);
});

test("geometry verifier catches both overlap and missing controls",()=>{
 const input=model(1200,800);
 const p=L.optimize(input);
 const tampered=JSON.parse(JSON.stringify(p));
 tampered.rects.integrity={...tampered.rects.map};
 assert.ok(L.verify(tampered,input.panels).violations.some(v=>v.code==="OVERLAP"));
 delete tampered.rects.incidents;
 assert.ok(L.verify(tampered,input.panels).violations.some(v=>v.code==="INACCESSIBLE_PANEL"));
});

test("DOM verifier catches clipping, drift, and missing accessible nodes",()=>{
 const input=model(1280,850);
 const p=L.optimize(input);
 const nodes={};
 for(const [id,r] of Object.entries(p.rects)){
   nodes[id]={
     scrollWidth:r.width,clientWidth:r.width,
     scrollHeight:r.height,clientHeight:r.height,
     getBoundingClientRect:()=>({left:r.x,top:r.y,width:r.width,height:r.height})
   };
 }
 const ownerDocument={
   getElementById:id=>nodes[id],
   defaultView:{getComputedStyle:()=>({overflowY:"auto",overflowX:"auto"})}
 };
 const root={ownerDocument,getBoundingClientRect:()=>({left:0,top:0})};
 assert.equal(L.verifyDOM(p,root).ok,true);
 nodes.incidents.scrollHeight+=90;
 ownerDocument.defaultView.getComputedStyle=()=>({overflowY:"hidden",overflowX:"auto"});
 assert.ok(L.verifyDOM(p,root).violations.some(v=>v.code==="CONTENT_CLIPPED"));
 ownerDocument.defaultView.getComputedStyle=()=>({overflowY:"auto",overflowX:"auto"});
 nodes.map.getBoundingClientRect=()=>({left:80,top:0,width:10,height:10});
 assert.ok(L.verifyDOM(p,root).violations.some(v=>v.code==="DOM_GEOMETRY_DRIFT"));
 delete nodes.integrity;
 assert.ok(L.verifyDOM(p,root).violations.some(v=>v.code==="DOM_PANEL_MISSING"));
});

test("performance budget under deterministic high-content stress",()=>{
 const input=model(1920,1080,2000,300);
 const warm=L.optimize(input);
 const t0=performance.now();
 for(let i=0;i<1500;i++)assert.ok(L.optimize(input).constraints.ok);
 const elapsed=performance.now()-t0;
 assert.ok(elapsed<5000,"1500 evaluations exceeded 5 second cold CI budget: "+elapsed);
 assert.ok(warm.contentHeight<15000);
});
