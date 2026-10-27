#!/usr/bin/env python3
"""OntologyEX Atlas: bounded, read-only GitHub repository inventory.

Reads committed GitHub metadata/manifests only. Never executes repository code,
changes GitHub resources, stores credentials, or asserts runtime health.
"""
from __future__ import annotations
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_REPOS = 16
MAX_TREE_ENTRIES = 20000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 192 * 1024
MAX_MANIFESTS = 18
MAX_SCRIPTS_PER_FILE = 30
MAX_MODULES = 20
MAX_WORKFLOWS = 32
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
TECH = {'react','next','vite','typescript','zod','pg','drizzle-orm','viem',
        'lightweight-charts','@solana/web3.js','@coral-xyz/anchor',
        'playwright-core','@earendil-works/pi-durable','@tauri-apps/api',
        'prisma','@prisma/client','fastify','express','hono','@modelcontextprotocol/server'}
SKIP_DIR = {'node_modules','vendor','dist','build','target','coverage','backup','spikes',
            '.next','.git','.venv','.agents','.claude'}

class ScanError(ValueError):
    pass


def access_token():
    token=os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if token:
        return token.strip()
    gh=shutil.which('gh')
    if gh:
        try:
            result=subprocess.run([gh,'auth','token'],capture_output=True,text=True,timeout=7,check=False)
            if result.returncode==0:
                return result.stdout.strip()
        except (OSError,subprocess.TimeoutExpired):
            pass
    return ''


def request(path,token='',allow_bytes=MAX_RESPONSE_BYTES):
    """Only sends requests to api.github.com with fixed path prefixes."""
    if not path.startswith('/repos/') or '?' in path.split('/')[2]:
        raise ScanError('API request path must be repository scoped')
    url='https://api.github.com'+path
    headers={'Accept':'application/vnd.github+json','User-Agent':'OntologyEX-Atlas-ReadOnly/0.1',
             'X-GitHub-Api-Version':'2022-11-28'}
    if token:
        headers['Authorization']='Bearer '+token
    try:
        req=urllib.request.Request(url,headers=headers,method='GET')
        with urllib.request.urlopen(req,timeout=18) as response:
            raw=response.read(allow_bytes+1)
            if len(raw)>allow_bytes:
                raise ScanError('GitHub response exceeds size budget')
    except urllib.error.HTTPError as exc:
        # Never include response text: upstream errors can contain sensitive echoes.
        raise ScanError(f'GitHub API HTTP {exc.code}; check repository visibility and rate limits') from None
    except (urllib.error.URLError,TimeoutError) as exc:
        raise ScanError(f'GitHub API inaccessible: {type(exc).__name__}') from None
    try:
        return json.loads(raw)
    except (UnicodeError,ValueError) as exc:
        raise ScanError('GitHub returned malformed JSON') from exc


def read_manifest(repo,path,commit,api):
    url='/repos/'+repo+'/contents/'+urllib.parse.quote(path,safe='/')+'?ref='+commit
    record=api(url)
    if not isinstance(record,dict) or record.get('encoding')!='base64':
        raise ScanError('Unsupported manifest response')
    if record.get('size',0)>MAX_MANIFEST_BYTES:
        raise ScanError('Manifest exceeds byte budget')
    try:
        data=base64.b64decode(''.join(record['content'].split()),validate=True)
        if len(data)>MAX_MANIFEST_BYTES:
            raise ScanError('Decoded manifest exceeds byte budget')
        return data
    except (KeyError,ValueError) as exc:
        raise ScanError('Malformed encoded manifest') from exc


def safe_json(raw):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:
                raise ScanError('Duplicate JSON key')
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=unique)


def manifests(paths):
    """Recognize only bounded config formats, not arbitrary application files."""
    pkgs=[p for p in paths if p=='package.json' or p.endswith('/package.json') and
          p.count('/')<=2 and not any(s in SKIP_DIR for s in p.split('/'))]
    cargo=[p for p in paths if p in ('Cargo.toml','runner/Cargo.toml','server/Cargo.toml')]
    compose=[p for p in paths if p.split('/')[-1] in ('compose.yaml','compose.yml','docker-compose.yml','docker-compose.yaml') and p.count('/')<=3]
    return (sorted(pkgs)+sorted(cargo)+sorted(compose))[:MAX_MANIFESTS]


def scan_repository(repo,api):
    if not REPO_PATTERN.fullmatch(repo):
        raise ScanError('Repository must be owner/name')
    safe='/'.join(urllib.parse.quote(part,safe='-._~') for part in repo.split('/'))
    info=api('/repos/'+safe)
    if not isinstance(info,dict) or info.get('full_name','').lower()!=repo.lower():
        raise ScanError('Repository metadata mismatch')
    branch=info.get('default_branch','main')
    if not isinstance(branch,str) or len(branch)>150:
        raise ScanError('Invalid branch')
    b=api('/repos/'+safe+'/branches/'+urllib.parse.quote(branch,safe=''))
    sha=b.get('commit',{}).get('sha')
    if not isinstance(sha,str) or not re.fullmatch(r'[0-9a-f]{40}',sha):
        raise ScanError('Missing pinned commit SHA')
    tr=api('/repos/'+safe+'/git/trees/'+sha+'?recursive=1')
    tree=tr.get('tree',[])
    if tr.get('truncated') or not isinstance(tree,list) or len(tree)>MAX_TREE_ENTRIES:
        raise ScanError('Git tree incomplete or too large; inspect with a scoped local scan')
    blobs={x['path'] for x in tree if x.get('type')=='blob' and isinstance(x.get('path'),str)}
    dirs=sorted({x['path'] for x in tree if x.get('type')=='tree' and
                 isinstance(x.get('path'),str) and '/' not in x['path'] and
                 not x['path'].startswith('.') and x['path'] not in SKIP_DIR})[:MAX_MODULES]
    pid='repo:'+repo
    project={ 'id':repo,'label':info.get('name',repo.split('/')[1]),'category':'GitHub source repository',
              'description':info.get('description') or 'GitHub source and manifest inventory',
              'private':bool(info.get('private',True)),'branch':branch,'commit':sha,
              'url':'https://github.com/'+repo,'tree_entries':len(tree),'modules':dirs}
    nodes=[{'id':pid,'kind':'project','label':project['label'],'project':repo,
            'detail':project['description'],'origin':'github_metadata'}]
    edges=[]; insights=[]; gaps=[]; evidence=[]
    def node(id,kind,label,path='',detail=''):
        n={'id':id,'kind':kind,'label':str(label)[:200],'project':repo,'origin':'committed_source'}
        if path:n['path']=path
        if detail:n['detail']=detail
        nodes.append(n)
        edges.append({'source':pid,'target':id,'relation':'CONTAINS' if kind=='module' else 'DECLARES_'+kind.upper(),
                      'basis':'declared','path':path})
        return id
    for d in dirs:
        node('module:'+repo+':'+d,'module',d,d,'Directory visible at pinned Git commit')
    workflows=sorted(p for p in blobs if p.startswith('.github/workflows/') and p.endswith(('.yaml','.yml')))[:MAX_WORKFLOWS]
    for path in workflows:
        node('workflow:'+repo+':'+path,'workflow',path.split('/')[-1],path,'Workflow file exists; no execution status observed')
    tech_links=[]; compose_services={}
    selected=manifests(blobs)
    if len(manifests(blobs))>=MAX_MANIFESTS:
        gaps.append({'source':repo,'reason':'MANIFEST_BUDGET_LIMIT'})
    for path in selected:
        savepoint=(len(nodes),len(edges),len(insights),len(tech_links))
        try:
            raw=read_manifest(safe,path,sha,api)
            digest=hashlib.sha256(raw).hexdigest()
            evidence.append({'path':path,'sha256':digest,'bytes':len(raw),'commit':sha,'kind':'GITHUB_COMMITTED_MANIFEST'})
            if path.endswith('package.json'):
                pkg=safe_json(raw)
                if not isinstance(pkg,dict):
                    raise ScanError('Package manifest is not an object')
                scripts=pkg.get('scripts') or {}
                if not isinstance(scripts,dict):
                    raise ScanError('Scripts not a mapping')
                if len(scripts)>MAX_SCRIPTS_PER_FILE:
                    gaps.append({'source':path,'reason':'SCRIPT_DISCOVERY_LIMIT','visible':MAX_SCRIPTS_PER_FILE})
                for key in sorted(scripts)[:MAX_SCRIPTS_PER_FILE]:
                    if not isinstance(key,str) or len(key)>100:
                        continue
                    node('script:'+repo+':'+path+':'+key,'script',key,path,'Package script declared, not executed')
                dependencies=pkg.get('dependencies') or {}
                if not isinstance(dependencies,dict):
                    raise ScanError('Dependencies not a mapping')
                for item in sorted(dependencies):
                    if item in TECH:
                        tech_links.append((item,path))
            elif path.endswith('Cargo.toml'):
                import tomllib
                parsed=tomllib.loads(raw.decode('utf-8'))
                if isinstance(parsed.get('package'),dict):
                    name=parsed['package'].get('name')
                    if isinstance(name,str) and len(name)<120:
                        node('service:'+repo+':'+path,'service',name,path,'Rust crate declaration, not a deployed service')
                if isinstance(parsed.get('dependencies'),dict):
                    for name,item in parsed['dependencies'].items():
                        if isinstance(item,dict) and isinstance(item.get('path'),str):
                            insights.append({'project':repo,'severity':'info','title':'Local crate dependency',
                                'description':f'{path} depends on local crate {name} at {item["path"]}; inspect build and verification boundaries.',
                                'evidence':path})
            else:
                try:
                    import yaml
                except ImportError:
                    gaps.append({'source':path,'reason':'COMPOSE_REQUIRES_PYYAML'})
                    continue
                # SafeLoader allows common Compose merge anchors but no code constructors.
                if raw.count(b'&')+raw.count(b'*')>150:
                    raise ScanError('Compose YAML alias budget exceeded')
                compose=yaml.safe_load(raw.decode('utf-8'))
                services=compose.get('services') if isinstance(compose,dict) else None
                if not isinstance(services,dict):
                    raise ScanError('Compose missing services map')
                if len(services)>100:
                    raise ScanError('Compose services limit exceeded')
                for name,item in services.items():
                    if not isinstance(item,dict):
                        raise ScanError('Compose service entry must be a mapping')
                    if not isinstance(name,str) or len(name)>100:
                        raise ScanError('Invalid Compose service name')
                    sid='service:'+repo+':'+path+':'+name
                    node(sid,'service',name,path,'Docker Compose service declaration, runtime unknown')
                    compose_services[(path,name)]=sid
                for name,item in services.items():
                    if not isinstance(item,dict):
                        continue
                    deps=item.get('depends_on',[])
                    if isinstance(deps,dict):deps=list(deps)
                    if not isinstance(deps,list):
                        gaps.append({'source':path,'reason':'MALFORMED_DEPENDS_ON'})
                        continue
                    for dep in deps[:100]:
                        a=compose_services.get((path,name));b=compose_services.get((path,dep))
                        if a and b:
                            edges.append({'source':a,'target':b,'relation':'DEPENDS_ON','path':path,'basis':'compose_startup_declaration'})
                        else:
                            gaps.append({'source':path,'reason':'UNRESOLVED_COMPOSE_DEPENDENCY','from':name,'target':str(dep)[:120]})
        except (Exception,) as exc:  # invalid untrusted source is a visible gap, never a successful partial declaration
            nodes[:],edges[:],insights[:],tech_links[:]=(nodes[:savepoint[0]],edges[:savepoint[1]],insights[:savepoint[2]],tech_links[:savepoint[3]])
            gaps.append({'source':path,'reason':'MANIFEST_PARSE_FAILED','error_type':type(exc).__name__})
    # Technology identifiers are stable across repos; dedupe in final aggregate.
    for name,path in sorted(set(tech_links)):
        tid='tech:'+name
        nodes.append({'id':tid,'kind':'technology','label':name,
                      'detail':'Package declared in a checked manifest; not runtime health'})
        edges.append({'source':pid,'target':tid,'relation':'USES_TECH','basis':'declared','path':path})
    monitored=[n['label'] for n in nodes if n['kind']=='script' and any(x in n['label'] for x in ('index','worker','collect','health','ops','keeper','reconcile'))]
    if monitored:
        insights.append({'project':repo,'severity':'focus','title':'Background-job visibility opportunity',
                         'description':'Declared operational scripts: '+', '.join(monitored[:7])+'. Connect heartbeats and progress counters before treating these as live jobs.',
                         'evidence':next((e['path'] for e in evidence if e['path'].endswith('package.json')),'README.md')})
    return project,nodes,edges,insights,evidence,gaps


def scan(repos,api):
    if not repos or len(repos)>MAX_REPOS or len(set(repos))!=len(repos):
        raise ScanError('Provide 1-16 distinct owner/repo names')
    projects=[];nodes=[];edges=[];insights=[];evidence=[];gaps=[]
    for repo in repos:
        try:
            p,n,e,i,ev,g=scan_repository(repo,api)
            projects.append(p);nodes.extend(n);edges.extend(e);insights.extend(i)
            evidence.extend([{'repo':repo,**x} for x in ev]);gaps.extend([{'repo':repo,**x} for x in g])
        except ScanError as exc:
            gaps.append({'repo':repo,'source':'repository','reason':'REPOSITORY_SCAN_FAILED','detail':str(exc)})
    if not projects:
        raise ScanError('No readable repositories. Check access or use public repositories.')
    by_id={}
    for node in nodes:
        if node['id'] not in by_id:
            by_id[node['id']]=node
    links={(e['source'],e['target'],e['relation'],e.get('path','')):e for e in edges}
    return {'schema_version':1,'kind':'ONTOLOGYEX_PROJECT_ATLAS_SNAPSHOT',
            'mode':'GITHUB_DECLARATIONS_NO_RUNTIME_HEALTH','generated_at':dt.datetime.now(dt.timezone.utc).date().isoformat(),
            'title':'Read-only project atlas','projects':projects,
            'nodes':[by_id[k] for k in sorted(by_id)],
            'edges':[links[k] for k in sorted(links)],'insights':insights,'gaps':gaps,'source_evidence':evidence,
            'coverage':{'repositories_accessible':len(projects),'private_repositories':sum(bool(p['private']) for p in projects),
                         'runtime_sources_connected':0,'method':'Pinned GitHub trees and bounded source manifests; not runtime verification'},
            'disclaimer':'Declared source topology only. Unknown runtime health; no policy execution or deployments.'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repos',nargs='+',help='1-16 GitHub repositories, owner/repo')
    parser.add_argument('--out',type=Path,default=Path('ontologyex-atlas.json'),help='new local JSON output file (refuses overwrites)')
    args=parser.parse_args(argv)
    token=access_token()
    try:
        result=scan(args.repos,lambda path:request(path,token))
        target=args.out
        if target.is_symlink():
            raise ScanError('Output must not be a symlink')
        # Private metadata is for the local operator only; owner read/write.
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as out:
                json.dump(result,out,ensure_ascii=False,sort_keys=True,indent=2)
                out.write('\n')
        except Exception:
            target.unlink(missing_ok=True)
            raise
        print(f'Atlas written: {target} | {len(result["projects"])} projects, {len(result["nodes"])} nodes, {len(result["edges"])} links, {len(result["gaps"])} gaps',file=sys.stderr)
        return 0
    except (OSError,ScanError,ValueError) as exc:
        print('Scan refused: '+str(exc),file=sys.stderr)
        return 1

if __name__=='__main__':
    raise SystemExit(main())