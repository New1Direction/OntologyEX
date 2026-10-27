#!/usr/bin/env python3
"""Build an evidence-aware business X-Ray from declarative project blueprints.

All evidence is author-declared, not independently authenticated by this tool.
Offline/read-only, standard library only. Never executes workflows or permissions.
"""
import argparse
import os
import json
import re
from pathlib import Path
from urllib.parse import quote

MAX_BYTES = 4 * 1024 * 1024
SLUG = re.compile(r'^[a-zA-Z][a-zA-Z0-9_.-]{0,80}$')
SHA = re.compile(r'^[0-9a-f]{40}$')
PROJECT = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
KINDS = {'observed_in_source', 'derived_from_source', 'operator_declared', 'proposed', 'unknown'}
ACTOR_ROLES = {'customer', 'creator', 'owner', 'operator', 'builder', 'trader', 'participant', 'agent', 'team', 'external', 'user'}
FLOW_STATES = {'described', 'deployed_claim', 'gated', 'planned', 'unknown'}

class BlueprintError(ValueError):
    pass

def check(condition, message):
    if not condition:
        raise BlueprintError(message)

def read(path):
    raw=Path(path).read_bytes()
    check(len(raw) <= MAX_BYTES, f'too large: {path}')
    def pairs(items):
        d={}
        for k,v in items:
            check(k not in d,f'duplicate JSON key: {k}')
            d[k]=v
        return d
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda x: (_ for _ in ()).throw(BlueprintError(f'non-finite: {x}')))

def require_string(s, where, max_length=1000):
    check(type(s) is str and bool(s.strip()) and len(s)<=max_length, f'{where}: invalid string')
    return s

def evidence_url(ev):
    repo=ev['repo']
    return f'https://github.com/{repo}/blob/{ev["commit"]}/{quote(ev["path"], safe="/._-")}#L{ev["start"]}-L{ev["end"]}'

def validate(p):
    check(type(p) is dict and p.get('schema_version')==1, 'blueprint schema_version=1 required')
    for key in ('id','name','purpose','stage','audiences','value','money_flows','journeys','systems','evidence','risks','unknowns','improvements'):
        check(key in p,f'missing required field {key}')
    check(PROJECT.fullmatch(p['id']) is not None, 'invalid project id')
    for s in ('name','purpose','stage'): require_string(p[s],s)
    evidence={}
    check(type(p['evidence']) is list and len(p['evidence']) <= 200, 'invalid evidence set')
    for e in p['evidence']:
        check(type(e) is dict and SLUG.fullmatch(e.get('id','')), 'invalid evidence id')
        check(e['id'] not in evidence, 'duplicate evidence id')
        require_string(e.get('repo'),'repo',200)
        check(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',e['repo']) is not None,'bad repo')
        check(SHA.fullmatch(e.get('commit','')) is not None, 'must pin source commit')
        require_string(e.get('path'),'path',500)
        check(not e['path'].startswith('/') and not any(x in ('.','..') for x in e['path'].split('/')),'unsafe evidence path')
        check(type(e.get('start')) is int and type(e.get('end')) is int and 0<e['start']<=e['end']<=100000,'invalid line range')
        evidence[e['id']]=e
    objects=[]
    for field in ('audiences','value','money_flows','journeys','systems','risks','unknowns','improvements'):
        items=p[field];check(type(items) is list and len(items) <= 100,f'{field}: invalid list')
        for item in items:
            check(type(item) is dict and SLUG.fullmatch(item.get('id','')),f'{field}: id required')
            if field=='journeys':
                steps=item.get('steps')
                check(type(steps) is list and 1<=len(steps)<=20,'journey steps 1..20 required')
                for step in steps: objects.append((f'journeys.{item["id"]}.step',step))
            objects.append((field,item))
    seen=set()
    for field,item in objects:
        uid=f'{field}:{item["id"]}'
        check(uid not in seen,f'duplicate {uid}')
        seen.add(uid)
        evs=item.get('evidence',[])
        check(type(evs) is list and len(evs)<=16,'invalid evidence refs')
        kind=item.get('basis','operator_declared')
        check(kind in KINDS,'unknown evidence basis')
        if kind in ('observed_in_source','derived_from_source'):
            check(bool(evs),f'{uid}: source-backed claim must link evidence')
        for ref in evs:check(ref in evidence, f'{uid}: missing evidence {ref}')
        if field.startswith('journeys') and '.step' in field:
            require_string(item.get('name'),f'{uid}.name')
            check(item.get('state','described') in FLOW_STATES,'unknown step state')
        elif field in ('money_flows','systems','risks','improvements'):
            require_string(item.get('name'),f'{uid}.name')
    for j in p['journeys']:
        require_string(j.get('name'),'journey.name')
        require_string(j.get('outcome'),'journey.outcome')
        require_string(j.get('actor'),'journey.actor')
        for step in j['steps']:
            for rid in step.get('risk_ids',[]):
                check(any(r['id']==rid for r in p['risks']),f'unknown risk: {rid}')
    for i in p['improvements']:
        for rid in i.get('addresses',[]):check(any(r['id']==rid for r in p['risks']),f'unknown targeted risk: {rid}')
    return p

def summarize(p):
    steps=[s for j in p['journeys'] for s in j['steps']]
    states={s:sum(x.get('state','described')==s for x in steps) for s in sorted(FLOW_STATES)}
    source_backed=sum(bool(x.get('evidence')) for x in steps)
    return {'journeys':len(p['journeys']),'steps':len(steps),'steps_with_source_links':source_backed,
            'money_flows':len(p['money_flows']),'risks':len(p['risks']),'unknowns':len(p['unknowns']),
            'candidate_improvements':len(p['improvements']),'states':states,
            'runtime_coverage':'NOT_MEASURED','business_completeness':'UNKNOWN'}

def compile_blueprints(projects, inventory=None):
    check(1<=len(projects)<=30,'expected 1-30 projects')
    checked=[validate(p) for p in projects]
    ids=[p['id'] for p in checked]
    check(len(ids)==len(set(ids)),'duplicate project id')
    inventory_projects={p.get('id'):p for p in (inventory or {}).get('projects',[])}
    output=[]
    for p in checked:
        base={**p,'summary':summarize(p)}
        base['evidence']=[{**ev,'url':evidence_url(ev)} for ev in p['evidence']]
        scoped_nodes = {n['id'] for n in (inventory or {}).get('nodes',[])
                        if n.get('project') == p['id'] and type(n.get('id')) is str}
        base['inventory']={
          'available':p['id'] in inventory_projects,
          'node_count':len(scoped_nodes),
          'links_count':sum(1 for edge in (inventory or {}).get('edges',[])
                             if edge.get('source') in scoped_nodes or edge.get('target') in scoped_nodes),
        }
        output.append(base)
    return {'schema_version':1,'kind':'BUSINESS_XRAY_DECLARED_BLUEPRINT','trust':'AUTHOR_DECLARED_SOURCE_LINKS_NOT_INDEPENDENTLY_VERIFIED',
            'execution_authorized':False,'generated_for':'LOCAL_INSPECTION',
            'projects':output,'totals':{
                'projects':len(output),
                'journeys':sum(p['summary']['journeys'] for p in output),
                'steps':sum(p['summary']['steps'] for p in output),
                'risks':sum(p['summary']['risks'] for p in output),
                'questions':sum(p['summary']['unknowns'] for p in output),
            }}

def render_html(compiled, template_path):
    template=Path(template_path).read_text(encoding='utf-8')
    marker='__XRAY_DATA__'
    check(template.count(marker)==1,'HTML template marker missing or repeated')
    safe_json=json.dumps(compiled,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    return template.replace(marker,safe_json)

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('blueprints',nargs='+',help='authored, source-linked blueprint JSON')
    ap.add_argument('--inventory',help='optional existing Project Atlas snapshot JSON')
    ap.add_argument('--template',default=str(Path(__file__).with_name('business_xray_template.html')))
    ap.add_argument('--out',required=True)
    ap.add_argument('--json-out')
    args=ap.parse_args()
    projects=[read(f) for f in args.blueprints]
    inv=read(args.inventory) if args.inventory else None
    result=compile_blueprints(projects,inv)
    outputs = [(Path(args.out), render_html(result,args.template))]
    if args.json_out:
        outputs.append((Path(args.json_out), json.dumps(result,indent=2,ensure_ascii=False)+'\n'))
    resolved = [path.absolute() for path, _ in outputs]
    check(len(set(resolved)) == len(resolved), 'HTML and JSON output paths must differ')
    for path, _ in outputs:
        check(not path.exists() and not path.is_symlink(), f'refuse overwrite of existing output: {path}')
        check(path.parent.is_dir() and not path.parent.is_symlink(), f'output directory unavailable or symlinked: {path.parent}')
    written = []
    try:
        for path, contents in outputs:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
            fd = os.open(path, flags, 0o600)
            written.append(path)
            with os.fdopen(fd, 'w', encoding='utf-8') as writer:
                writer.write(contents)
    except BaseException:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    print(json.dumps(result['totals'],sort_keys=True))

if __name__=='__main__':
    main()