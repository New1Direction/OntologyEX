//! What code declares, read from syntax trees: routes, operations, tables and jobs in
//! Python, JavaScript/TypeScript and Go (`ontologyex-discover surface`).
//!
//! `code_surface.py` finds the same declarations with regular expressions. A syntax tree
//! tells code from comments and strings, reads calls and decorators that span lines, and
//! follows what the expressions say: a router's own prefix (FastAPI `APIRouter(prefix=)`,
//! Flask `Blueprint(url_prefix=)`, Express `app.use("/api", router)`, gin/echo `Group`,
//! chi `Route`, gorilla `PathPrefix`), chained routes (`router.route("/x").get().post()`),
//! Fastify's `route({method, url})`, gorilla's `.Methods("POST")`, and re-exported Next.js
//! handlers (`export { handler as GET }`). For each file it returns facts with their lines;
//! `code_surface.py` places them (file routes, other languages) exactly as before.
//!
//! Parsing only: grammars are compiled in, nothing is run, imported or downloaded.
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::path::Path;
use tree_sitter::{Language, Node, Parser};

pub const MAX_FILES: usize = 50_000;
pub const MAX_FILE: u64 = 4 * 1024 * 1024;
pub const MAX_TOTAL: u64 = 96 * 1024 * 1024;
const METHODS: [&str; 7] = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"];
const JS_VERBS: [&str; 6] = ["get", "post", "put", "patch", "delete", "all"];
const PY_VERBS: [&str; 7] = ["route", "get", "post", "put", "patch", "delete", "api_route"];
const GO_VERBS: [&str; 14] = ["GET", "POST", "PUT", "PATCH", "DELETE", "Get", "Post", "Put", "Patch", "Delete",
                              "Handle", "HandleFunc", "Any", "Method"];

#[derive(Default)]
struct Facts {
    routes: Vec<(usize, Value)>,  // (byte offset of the declaration, for source order) and the route
    operations: Vec<Value>,
    tables: Vec<Value>,
    jobs: Vec<Value>,
    exports: Vec<Value>,
}

impl Facts {
    fn route(&mut self, at: Node, method: &str, path: String, framework: &str) {
        self.routes.push((at.start_byte(), json!({"method": method.to_uppercase(), "path": path, "line": line(at),
                                                  "framework": framework})));
    }
    fn table(&mut self, name: &str, line: usize, source: &str) {
        if is_word(name) {
            self.tables.push(json!({"name": name, "line": line, "source": source}));
        }
    }
    fn job(&mut self, name: String, line: usize, kind: &str) {
        self.jobs.push(json!({"name": name, "line": line, "kind": kind}));
    }
    fn value(mut self) -> Value {
        self.routes.sort_by_key(|(at, _)| *at);  // stable: one declaration's methods keep their order
        let routes: Vec<Value> = self.routes.into_iter().map(|(_, r)| r).collect();
        json!({"routes": routes, "operations": self.operations, "tables": self.tables, "jobs": self.jobs,
               "exports": self.exports})
    }
}

fn is_word(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| c.is_alphanumeric() || c == '_')
}

fn text<'a>(node: Node, src: &'a str) -> &'a str {
    &src[node.byte_range()]
}

fn line(node: Node) -> usize {
    node.start_position().row + 1
}

fn kids(node: Node) -> Vec<Node> {
    let mut cursor = node.walk();
    node.named_children(&mut cursor).collect()
}

fn field<'t>(node: Node<'t>, name: &str) -> Option<Node<'t>> {
    node.child_by_field_name(name)
}

/// Every node of the tree in source order (iterative: deep trees do not overflow the stack).
fn nodes(root: Node) -> Vec<Node> {
    let mut out = Vec::new();
    let mut stack = vec![root];
    while let Some(node) = stack.pop() {
        out.push(node);
        let mut children = kids(node);
        children.reverse();
        stack.extend(children);
    }
    out
}

/// A router's prefix and a route's path, as the frameworks join them: `/items` + `/` is `/items/`.
fn join(prefix: &str, path: &str) -> String {
    if path.is_empty() {
        prefix.to_string()
    } else {
        format!("{}{}", prefix.trim_end_matches('/'), path)
    }
}

// ---------------------------------------------------------------- Python

/// A string literal's value; None for an f-string with an interpolation or anything else.
fn py_str(node: Node, src: &str) -> Option<String> {
    if node.kind() != "string" {
        return None;
    }
    let parts = kids(node);
    if parts.iter().any(|k| k.kind() == "interpolation") {
        return None;
    }
    let start = parts.iter().find(|k| k.kind() == "string_start")?.end_byte();
    let end = parts.iter().find(|k| k.kind() == "string_end")?.start_byte();
    (start <= end).then(|| src[start..end].to_string())
}

fn py_keyword<'t>(args: Node<'t>, name: &str, src: &str) -> Option<Node<'t>> {
    kids(args).into_iter().find(|k| {
        k.kind() == "keyword_argument" && field(*k, "name").is_some_and(|n| text(n, src) == name)
    }).and_then(|k| field(k, "value"))
}

fn py_positional<'t>(args: Node<'t>) -> Vec<Node<'t>> {
    kids(args).into_iter().filter(|k| !matches!(k.kind(), "keyword_argument" | "comment")).collect()
}

/// The callee's last name: `APIRouter` for `fastapi.APIRouter(...)`.
fn callee_name<'a>(call: Node, src: &'a str) -> Option<&'a str> {
    let function = field(call, "function")?;
    match function.kind() {
        "identifier" => Some(text(function, src)),
        "attribute" => field(function, "attribute").map(|a| text(a, src)),
        _ => None,
    }
}

fn python(root: Node, src: &str, rel: &str, facts: &mut Facts) {
    let all = nodes(root);
    let name = rel.rsplit('/').next().unwrap_or(rel);
    // Routers and their prefixes: router = APIRouter(prefix=...), bp = Blueprint(..., url_prefix=...),
    // and app.include_router(router, prefix=...) / app.register_blueprint(bp, url_prefix=...) in the same file.
    let mut prefixes: HashMap<String, String> = HashMap::new();
    let mut mounts: Vec<(String, String)> = Vec::new();
    for node in &all {
        if node.kind() == "assignment" {
            let (Some(left), Some(right)) = (field(*node, "left"), field(*node, "right")) else { continue };
            if left.kind() == "identifier" && right.kind() == "call"
                && matches!(callee_name(right, src), Some("APIRouter" | "Blueprint")) {
                if let Some(args) = field(right, "arguments") {
                    let value = py_keyword(args, "prefix", src).or_else(|| py_keyword(args, "url_prefix", src));
                    if let Some(prefix) = value.and_then(|v| py_str(v, src)) {
                        prefixes.insert(text(left, src).to_string(), prefix.trim_end_matches('/').to_string());
                    }
                }
            }
        } else if node.kind() == "call" && matches!(callee_name(*node, src), Some("include_router" | "register_blueprint")) {
            let Some(args) = field(*node, "arguments") else { continue };
            let target = py_positional(args).into_iter().next().filter(|a| a.kind() == "identifier");
            let value = py_keyword(args, "prefix", src).or_else(|| py_keyword(args, "url_prefix", src));
            if let (Some(target), Some(prefix)) = (target, value.and_then(|v| py_str(v, src))) {
                mounts.push((text(target, src).to_string(), prefix.trim_end_matches('/').to_string()));
            }
        }
    }
    let mut mounted: Vec<String> = Vec::new();
    for (router, prefix) in mounts {  // a router mounted twice (one name in two functions) keeps the first
        if mounted.contains(&router) {
            continue;
        }
        let own = prefixes.get(&router).cloned().unwrap_or_default();
        prefixes.insert(router.clone(), format!("{prefix}{own}"));
        mounted.push(router);
    }
    let django = (name == "models.py" || format!("/{rel}").contains("/models/")) && src.contains("django");
    for node in &all {
        match node.kind() {
            "decorated_definition" => py_decorated(*node, src, &prefixes, facts),
            "call" if name == "urls.py" => {
                let Some(function) = field(*node, "function") else { continue };
                if function.kind() == "identifier" && matches!(text(function, src), "path" | "re_path") {
                    let first = field(*node, "arguments").and_then(|a| py_positional(a).into_iter().next());
                    if let Some(path) = first.and_then(|f| py_str(f, src)) {
                        let path = path.trim_start_matches('^').trim_start_matches('/');
                        facts.route(*node, "ANY", format!("/{path}"), "django");
                    }
                }
            }
            "class_definition" if node.parent().is_some_and(|p| p.kind() == "module")
                || node.parent().is_some_and(|p| p.kind() == "decorated_definition"
                                             && p.parent().is_some_and(|q| q.kind() == "module")) => {
                let (Some(class_name), Some(bases)) = (field(*node, "name"), field(*node, "superclasses")) else { continue };
                let class_name = text(class_name, src);
                let bases_text = text(bases, src);
                let table_true = py_keyword(bases, "table", src).is_some_and(|v| text(v, src) == "True");
                if table_true {
                    facts.table(class_name, line(*node), "sqlmodel");
                } else if django && bases_text.contains("Model") && class_name != "Meta"
                    && !["BaseModel", "SQLModel", "RootModel"].iter().any(|b| has_word(bases_text, b)) {
                    facts.table(class_name, line(*node), "django");
                }
            }
            "assignment" => {
                let (Some(left), Some(right)) = (field(*node, "left"), field(*node, "right")) else { continue };
                if left.kind() == "identifier" && text(left, src) == "__tablename__" {
                    if let Some(table) = py_str(right, src) {
                        facts.table(&table, line(*node), "sqlalchemy");
                    }
                }
            }
            "pair" => {  // Celery beat: "name": {"task": "module.task", ...}
                let (Some(key), Some(value)) = (field(*node, "key"), field(*node, "value")) else { continue };
                let (Some(key), true) = (py_str(key, src), value.kind() == "dictionary") else { continue };
                let task = kids(value).into_iter().filter(|p| p.kind() == "pair").find_map(|p| {
                    (field(p, "key").and_then(|k| py_str(k, src)).as_deref() == Some("task"))
                        .then(|| field(p, "value").and_then(|v| py_str(v, src))).flatten()
                });
                if let Some(task) = task {
                    let ok = |s: &str, extra: &str| !s.is_empty()
                        && s.chars().all(|c| c.is_alphanumeric() || c == '_' || extra.contains(c));
                    if ok(&key, ".:-") && ok(&task, ".") {
                        facts.job(format!("{key} -> {task}"), line(*node), "celery beat schedule");
                    }
                }
            }
            _ => {}
        }
    }
}

fn has_word(haystack: &str, word: &str) -> bool {
    haystack.match_indices(word).any(|(i, _)| {
        let before = haystack[..i].chars().next_back();
        let after = haystack[i + word.len()..].chars().next();
        !before.is_some_and(|c| c.is_alphanumeric() || c == '_') && !after.is_some_and(|c| c.is_alphanumeric() || c == '_')
    })
}

fn py_decorated(node: Node, src: &str, prefixes: &HashMap<String, String>, facts: &mut Facts) {
    let definition = field(node, "definition");
    let function_name = definition.filter(|d| d.kind() == "function_definition")
        .and_then(|d| field(d, "name")).map(|n| text(n, src));
    for decorator in kids(node).into_iter().filter(|k| k.kind() == "decorator") {
        let Some(expr) = kids(decorator).into_iter().next() else { continue };
        // Celery: @shared_task, @app.task, @celery.task(bind=True)
        let target = if expr.kind() == "call" { field(expr, "function") } else { Some(expr) };
        let last = target.and_then(|t| match t.kind() {
            "identifier" => Some(text(t, src)),
            "attribute" => field(t, "attribute").map(|a| text(a, src)),
            _ => None,
        });
        if matches!(last, Some("shared_task" | "task")) {
            if let Some(function_name) = function_name {
                facts.job(function_name.to_string(), line(decorator), "celery task");
            }
            continue;
        }
        // Routes: @app.get("/x"), @router.post(path="/x"), @bp.route("/x", methods=["GET", "POST"])
        if expr.kind() != "call" {
            continue;
        }
        let Some(function) = field(expr, "function").filter(|f| f.kind() == "attribute") else { continue };
        let Some(verb) = field(function, "attribute").map(|a| text(a, src)).filter(|v| PY_VERBS.contains(v)) else { continue };
        let Some(args) = field(expr, "arguments") else { continue };
        let owner = field(function, "object").filter(|o| o.kind() == "identifier").map(|o| text(o, src));
        let prefix = owner.and_then(|o| prefixes.get(o)).cloned().unwrap_or_default();
        let path = py_positional(args).into_iter().next().and_then(|a| py_str(a, src))
            .or_else(|| py_keyword(args, "path", src).and_then(|v| py_str(v, src)));
        let Some(path) = path.filter(|p| p.starts_with('/') || (p.is_empty() && !prefix.is_empty())) else { continue };
        let verbs: Vec<String> = if matches!(verb, "route" | "api_route") {
            let listed = py_keyword(args, "methods", src).map(|m| {
                kids(m).into_iter().filter_map(|k| py_str(k, src)).collect::<Vec<_>>()
            });
            listed.filter(|l| !l.is_empty()).unwrap_or_else(|| vec!["GET".to_string()])
        } else {
            vec![verb.to_string()]
        };
        for method in verbs {
            facts.route(decorator, &method, join(&prefix, &path), "python");
        }
    }
}

// ---------------------------------------------------------------- JavaScript / TypeScript

fn js_str(node: Node, src: &str) -> Option<String> {
    match node.kind() {
        "string" => {
            let raw = text(node, src);
            (raw.len() >= 2).then(|| raw[1..raw.len() - 1].to_string())
        }
        "template_string" if !kids(node).iter().any(|k| k.kind() == "template_substitution") => {
            let raw = text(node, src);
            (raw.len() >= 2).then(|| raw[1..raw.len() - 1].to_string())
        }
        _ => None,
    }
}

fn js_args<'t>(call: Node<'t>) -> Vec<Node<'t>> {
    field(call, "arguments").map(|a| kids(a).into_iter().filter(|k| k.kind() != "comment").collect()).unwrap_or_default()
}

/// `obj.prop` of a member expression: (object, property name).
fn js_member<'t>(node: Node<'t>, src: &str) -> Option<(Node<'t>, String)> {
    (node.kind() == "member_expression").then_some(())?;
    Some((field(node, "object")?, text(field(node, "property")?, src).to_string()))
}

fn js_pair_str<'t>(object: Node<'t>, key: &str, src: &str) -> Option<Node<'t>> {
    kids(object).into_iter().filter(|p| p.kind() == "pair")
        .find(|p| field(*p, "key").is_some_and(|k| text(k, src).trim_matches(|c| c == '"' || c == '\'') == key))
        .and_then(|p| field(p, "value"))
}

/// The names `code_surface.py` treats as servers and routers: app, router, server, api, route(s), fastify,
/// hono, r, v1.., *Router.
fn router_name(name: &str) -> bool {
    matches!(name, "app" | "router" | "server" | "api" | "route" | "routes" | "fastify" | "hono" | "r")
        || (name.len() > 1 && name.starts_with('v') && name[1..].chars().all(|c| c.is_ascii_digit()))
        || (name.len() > 6 && name.ends_with("Router") && is_word(name))
}

/// The variable an expression names: `app`, or `this.app` -> `app`.
fn js_name(node: Node, src: &str) -> Option<String> {
    match node.kind() {
        "identifier" => Some(text(node, src).to_string()),
        "member_expression" => field(node, "property").map(|p| text(p, src).to_string()),
        _ => None,
    }
}

fn javascript(root: Node, src: &str, facts: &mut Facts) {
    let all = nodes(root);
    // Pass 1: which variables are servers or routers, and the prefix each is mounted under in this file.
    let mut routers: Vec<String> = Vec::new();
    let mut prefixes: HashMap<String, String> = HashMap::new();
    let mut mounts: Vec<(String, String, String)> = Vec::new();  // (parent, prefix, child)
    for node in &all {
        match node.kind() {
            "variable_declarator" => {
                let (Some(name), Some(value)) = (field(*node, "name"), field(*node, "value")) else { continue };
                let made = match value.kind() {
                    "call_expression" | "new_expression" => {
                        let callee = field(value, "function").or_else(|| field(value, "constructor"));
                        callee.and_then(|c| js_name(c, src)).is_some_and(|c| {
                            matches!(c.as_str(), "express" | "Router" | "Hono" | "Fastify" | "fastify" | "Elysia")
                        })
                    }
                    _ => false,
                };
                if made && name.kind() == "identifier" {
                    routers.push(text(name, src).to_string());
                }
            }
            "call_expression" => {
                let Some((object, property)) = field(*node, "function").and_then(|f| js_member(f, src)) else { continue };
                if matches!(property.as_str(), "use" | "route" | "register") {
                    let args = js_args(*node);
                    if let (Some(prefix), Some(child)) = (args.first().and_then(|a| js_str(*a, src)),
                                                          args.get(1).filter(|a| a.kind() == "identifier")) {
                        if let Some(parent) = js_name(object, src) {
                            if prefix.starts_with('/') {
                                mounts.push((parent, prefix.trim_end_matches('/').to_string(), text(*child, src).to_string()));
                            }
                        }
                    }
                }
            }
            _ => {}
        }
    }
    for (parent, prefix, child) in &mounts {
        if routers.contains(child) || router_name(child) {
            let above = prefixes.get(parent).cloned().unwrap_or_default();
            prefixes.insert(child.clone(), format!("{above}{prefix}"));
        }
    }
    let is_router = |name: &str| router_name(name) || routers.iter().any(|r| r == name);
    // Pass 2: routes, exports, operations, tables and jobs.
    for node in &all {
        match node.kind() {
            "call_expression" => js_call(*node, src, &is_router, &prefixes, facts),
            "new_expression" => {
                let constructor = field(*node, "constructor").map(|c| text(c, src));
                if matches!(constructor, Some("Queue" | "Worker")) {
                    if let Some(name) = js_args(*node).first().and_then(|a| js_str(*a, src)) {
                        if !name.is_empty() && name.chars().all(|c| c.is_alphanumeric() || "_:.-".contains(c)) {
                            facts.job(name, line(*node), "bullmq queue");
                        }
                    }
                }
            }
            "variable_declarator" | "pair" => {
                let key = if node.kind() == "pair" { field(*node, "key") } else { field(*node, "name") };
                let (Some(key), Some(value)) = (key, field(*node, "value")) else { continue };
                if !matches!(key.kind(), "identifier" | "property_identifier") {
                    continue;
                }
                let name = text(key, src);
                if !name.to_lowercase().ends_with("procedure") && trpc_procedure(value, src) {
                    facts.operations.push(json!({"kind": "rpc", "name": name, "line": line(key)}));
                }
            }
            "class_declaration" | "export_statement" => {
                if node.kind() == "export_statement" && node.parent().is_some_and(|p| p.kind() == "program") {
                    js_export(*node, src, facts);  // Next.js route handlers, Remix loader and action
                }
                // TypeORM: @Entity("name") class X  (the decorator sits on the export when the class is exported)
                let class = if node.kind() == "class_declaration" { Some(*node) } else {
                    field(*node, "declaration").filter(|d| d.kind() == "class_declaration")
                };
                let Some(class) = class else { continue };
                for decorator in kids(*node).into_iter().filter(|k| k.kind() == "decorator") {
                    let Some(expr) = kids(decorator).into_iter().next() else { continue };
                    let (callee, args) = if expr.kind() == "call_expression" {
                        (field(expr, "function"), js_args(expr))
                    } else {
                        (Some(expr), Vec::new())
                    };
                    if callee.map(|c| text(c, src)) == Some("Entity") {
                        let named = args.first().and_then(|a| js_str(*a, src));
                        let class_name = field(class, "name").map(|n| text(n, src).to_string());
                        if let Some(table) = named.or(class_name) {
                            facts.table(&table, line(decorator), "typeorm");
                        }
                    }
                }
            }
            _ => {}
        }
    }
}

/// tRPC: the value is built from a procedure (`publicProcedure.query(...)`, `t.procedure.input(...).mutation(...)`).
fn trpc_procedure(value: Node, src: &str) -> bool {
    let mut node = value;
    loop {
        match node.kind() {
            "call_expression" => match field(node, "function") {
                Some(f) => node = f,
                None => return false,
            },
            "member_expression" => {
                let Some(object) = field(node, "object") else { return false };
                if object.kind() == "identifier" && field(node, "property").map(|p| text(p, src)) == Some("procedure") {
                    return true;
                }
                node = object;
            }
            "identifier" => {
                let name = text(node, src);
                return name.ends_with("procedure") || name.ends_with("Procedure");
            }
            _ => return false,
        }
    }
}

fn js_export(node: Node, src: &str, facts: &mut Facts) {
    let mut names = Vec::new();
    if let Some(declaration) = field(node, "declaration") {
        match declaration.kind() {
            "function_declaration" | "generator_function_declaration" => names.extend(field(declaration, "name")),
            "lexical_declaration" | "variable_declaration" => {
                names.extend(kids(declaration).into_iter().filter(|k| k.kind() == "variable_declarator")
                    .filter_map(|d| field(d, "name")));
            }
            _ => {}
        }
    }
    for clause in kids(node).into_iter().filter(|k| k.kind() == "export_clause") {
        for spec in kids(clause).into_iter().filter(|k| k.kind() == "export_specifier") {
            names.extend(field(spec, "alias").or_else(|| field(spec, "name")));
        }
    }
    for name in names {
        let name = text(name, src);
        if METHODS.contains(&name) || matches!(name, "loader" | "action") {
            facts.exports.push(json!({"name": name, "line": line(node)}));
        }
    }
}

fn js_call(node: Node, src: &str, is_router: &dyn Fn(&str) -> bool, prefixes: &HashMap<String, String>,
           facts: &mut Facts) {
    let Some(function) = field(node, "function") else { return };
    let args = js_args(node);
    let first_str = args.first().and_then(|a| js_str(*a, src));
    if let Some((object, property)) = js_member(function, src) {
        if JS_VERBS.contains(&property.as_str()) {
            // router.get("/x"), or a chain on router.route("/x"): .get(h).post(h)
            let mut base = object;
            while base.kind() == "call_expression" {
                match field(base, "function").and_then(|f| js_member(f, src)) {
                    Some((inner, p)) if JS_VERBS.contains(&p.as_str()) => base = inner,
                    _ => break,
                }
            }
            let chained = (base.kind() == "call_expression").then(|| {
                field(base, "function").and_then(|f| js_member(f, src)).filter(|(_, p)| p == "route")
                    .and_then(|(owner, _)| Some((owner, js_args(base).first().and_then(|a| js_str(*a, src))?)))
            }).flatten();
            let (owner, path) = match chained {
                Some((owner, path)) => (Some(owner), Some(path)),
                None => (Some(base), if base.kind() == "call_expression" { None } else { first_str.clone() }),
            };
            if let (Some(owner), Some(path)) = (owner, path) {
                if let Some(name) = js_name(owner, src).filter(|n| is_router(n)) {
                    if path.starts_with('/') {
                        let prefix = prefixes.get(&name).cloned().unwrap_or_default();
                        let at = field(function, "property").unwrap_or(node);
                        facts.route(at, &property, join(&prefix, &path), "node");
                    }
                }
            }
            return;
        }
        // Fastify: fastify.route({ method: "GET" | ["GET", "POST"], url: "/x" })
        if property == "route" && args.first().is_some_and(|a| a.kind() == "object") {
            if let Some(name) = js_name(object, src).filter(|n| is_router(n)) {
                let spec = args[0];
                let url = js_pair_str(spec, "url", src).or_else(|| js_pair_str(spec, "path", src))
                    .and_then(|u| js_str(u, src));
                let methods = js_pair_str(spec, "method", src).map(|m| {
                    if m.kind() == "array" { kids(m).into_iter().filter_map(|k| js_str(k, src)).collect() }
                    else { js_str(m, src).into_iter().collect::<Vec<_>>() }
                }).unwrap_or_default();
                if let Some(url) = url.filter(|u| u.starts_with('/')) {
                    let prefix = prefixes.get(&name).cloned().unwrap_or_default();
                    for method in methods {
                        facts.route(node, &method, join(&prefix, &url), "node");
                    }
                }
            }
            return;
        }
        match property.as_str() {
            "define" => {
                if let Some(name) = first_str { facts.table(&name, line(node), "sequelize"); }
            }
            "model" if args.len() >= 2 => {
                if let Some(name) = first_str { facts.table(&name, line(node), "mongoose"); }
            }
            "pgTable" | "mysqlTable" | "sqliteTable" => {
                if let Some(name) = first_str { facts.table(&name, line(node), "drizzle"); }
            }
            "schedule" if js_name(object, src).as_deref() == Some("cron") => {
                if let Some(expr) = first_str.filter(|e| !e.is_empty()) { facts.job(expr, line(node), "cron schedule"); }
            }
            "createFunction" => {
                if let Some(id) = args.first().filter(|a| a.kind() == "object").and_then(|o| js_pair_str(*o, "id", src))
                    .and_then(|v| js_str(v, src)) {
                    facts.job(id, line(node), "inngest function");
                }
            }
            "task" if js_name(object, src).as_deref() == Some("schedules") => trigger_task(node, &args, src, facts),
            _ => {}
        }
        return;
    }
    if function.kind() == "identifier" {
        match text(function, src) {
            "pgTable" | "mysqlTable" | "sqliteTable" => {
                if let Some(name) = first_str { facts.table(&name, line(node), "drizzle"); }
            }
            "model" if args.len() >= 2 => {
                if let Some(name) = first_str { facts.table(&name, line(node), "mongoose"); }
            }
            "task" => trigger_task(node, &args, src, facts),
            _ => {}
        }
    }
}

fn trigger_task(node: Node, args: &[Node], src: &str, facts: &mut Facts) {
    if let Some(id) = args.first().filter(|a| a.kind() == "object").and_then(|o| js_pair_str(*o, "id", src))
        .and_then(|v| js_str(v, src)) {
        facts.job(id, line(node), "trigger.dev task");
    }
}

// ---------------------------------------------------------------- Go

fn go_str(node: Node, src: &str) -> Option<String> {
    match node.kind() {
        "interpreted_string_literal" | "raw_string_literal" => {
            let raw = text(node, src);
            (raw.len() >= 2).then(|| raw[1..raw.len() - 1].to_string())
        }
        _ => None,
    }
}

fn go_args<'t>(call: Node<'t>) -> Vec<Node<'t>> {
    field(call, "arguments").map(|a| kids(a).into_iter().filter(|k| k.kind() != "comment").collect()).unwrap_or_default()
}

fn go_selector<'t>(node: Node<'t>, src: &str) -> Option<(Node<'t>, String)> {
    (node.kind() == "selector_expression").then_some(())?;
    Some((field(node, "operand")?, text(field(node, "field")?, src).to_string()))
}

/// The prefix a router expression carries: a variable's, `r.Group("/x")`, `r.PathPrefix("/x").Subrouter()`.
fn go_prefix(node: Node, src: &str, scope: &[(String, String)]) -> String {
    match node.kind() {
        "identifier" => {
            let name = text(node, src);
            scope.iter().rev().find(|(n, _)| n == name).map(|(_, p)| p.clone()).unwrap_or_default()
        }
        "call_expression" => {
            let Some((operand, method)) = field(node, "function").and_then(|f| go_selector(f, src)) else { return String::new() };
            let base = go_prefix(operand, src, scope);
            match method.as_str() {
                "Group" | "PathPrefix" | "Route" => {
                    let path = go_args(node).first().and_then(|a| go_str(*a, src)).unwrap_or_default();
                    format!("{}{}", base, path.trim_end_matches('/'))
                }
                "Subrouter" | "With" | "Use" => base,
                _ => String::new(),
            }
        }
        _ => String::new(),
    }
}

fn go(root: Node, src: &str, facts: &mut Facts) {
    // Depth-first in source order, carrying the router prefixes in scope: variables assigned a group, and
    // the router parameter of a chi r.Route("/x", func(r chi.Router) {...}).
    let mut stack: Vec<(Node, std::rc::Rc<Vec<(String, String)>>)> = vec![(root, std::rc::Rc::new(Vec::new()))];
    let mut assigned: Vec<(String, String)> = Vec::new();
    while let Some((node, scope)) = stack.pop() {
        let mut child_scope = scope.clone();
        let visible = || -> Vec<(String, String)> {
            let mut all = scope.as_ref().clone();
            all.extend(assigned.iter().cloned());
            all
        };
        match node.kind() {
            "short_var_declaration" | "assignment_statement" | "var_spec" => {
                let left = field(node, "left").or_else(|| field(node, "name"));
                let right = field(node, "right").or_else(|| field(node, "value"));
                if let (Some(left), Some(right)) = (left, right) {
                    let visible = visible();
                    let names: Vec<Node> = if left.kind() == "expression_list" { kids(left) } else { vec![left] };
                    let values: Vec<Node> = if right.kind() == "expression_list" { kids(right) } else { vec![right] };
                    for (name, value) in names.iter().zip(values.iter()) {
                        let prefix = go_prefix(*value, src, &visible);
                        if name.kind() == "identifier" && !prefix.is_empty() {
                            assigned.push((text(*name, src).to_string(), prefix));
                        }
                    }
                }
            }
            "call_expression" => {
                if let Some((operand, method)) = field(node, "function").and_then(|f| go_selector(f, src))
                    .filter(|(_, m)| m == "Route" || GO_VERBS.contains(&m.as_str())) {
                    let visible = visible();
                    if method == "Route" {
                        // chi: r.Route("/x", func(r chi.Router) { r.Get(...) }) -- the inner r carries the prefix
                        let args = go_args(node);
                        if let (Some(path), Some(func)) = (args.first().and_then(|a| go_str(*a, src)),
                                                           args.get(1).filter(|a| a.kind() == "func_literal")) {
                            let param = field(*func, "parameters").and_then(|p| kids(p).into_iter().next())
                                .and_then(|d| field(d, "name")).map(|n| text(n, src).to_string());
                            if let Some(param) = param {
                                let prefix = format!("{}{}", go_prefix(operand, src, &visible), path.trim_end_matches('/'));
                                let mut inner = scope.as_ref().clone();
                                inner.push((param, prefix));
                                child_scope = std::rc::Rc::new(inner);
                            }
                        }
                    }
                    if GO_VERBS.contains(&method.as_str()) {
                        go_route(node, operand, &method, src, &visible, facts);
                    }
                }
            }
            _ => {}
        }
        let mut children = kids(node);
        children.reverse();
        for child in children {
            stack.push((child, child_scope.clone()));
        }
    }
}

fn go_route(node: Node, operand: Node, method: &str, src: &str, scope: &[(String, String)], facts: &mut Facts) {
    let args = go_args(node);
    let (mut verb, raw) = if method == "Method" {
        match (args.first().and_then(|a| go_str(*a, src)), args.get(1).and_then(|a| go_str(*a, src))) {
            (Some(v), Some(p)) if METHODS.contains(&v.as_str()) => (v, p),
            (Some(p), _) => ("ANY".to_string(), p),
            _ => return,
        }
    } else {
        match args.first().and_then(|a| go_str(*a, src)) {
            Some(p) => (method.to_string(), p),
            None => return,
        }
    };
    let mut path = raw;
    if let Some((first, rest)) = path.split_once(char::is_whitespace) {
        if METHODS.contains(&first) && rest.trim_start().starts_with('/') {  // Go 1.22: "GET /path"
            verb = first.to_string();
            path = rest.trim_start().to_string();
        }
    }
    if !path.starts_with('/') {
        return;
    }
    // gorilla: r.HandleFunc("/x", h).Methods("POST", "PUT")
    let mut verbs = vec![if METHODS.contains(&verb.to_uppercase().as_str()) { verb.to_uppercase() } else { "ANY".into() }];
    if let Some(parent) = node.parent().filter(|p| p.kind() == "selector_expression") {
        if field(parent, "field").map(|f| text(f, src)) == Some("Methods") {
            if let Some(call) = parent.parent().filter(|c| c.kind() == "call_expression") {
                let listed: Vec<String> = go_args(call).iter().filter_map(|a| go_str(*a, src))
                    .map(|m| m.to_uppercase()).filter(|m| METHODS.contains(&m.as_str())).collect();
                if !listed.is_empty() {
                    verbs = listed;
                }
            }
        }
    }
    let prefix = go_prefix(operand, src, scope);
    let at = field(node, "function").and_then(|f| field(f, "field")).unwrap_or(node);
    for verb in verbs {
        facts.route(at, &verb, join(&prefix, &path), "go");
    }
}

// ---------------------------------------------------------------- files

fn language(rel: &str) -> Option<(Language, &'static str)> {
    let ext = rel.rsplit_once('.').map(|(_, e)| e.to_ascii_lowercase())?;
    Some(match ext.as_str() {
        "py" => (tree_sitter_python::LANGUAGE.into(), "python"),
        "js" | "jsx" | "mjs" | "cjs" => (tree_sitter_javascript::LANGUAGE.into(), "javascript"),
        "ts" | "mts" | "cts" => (tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into(), "typescript"),
        "tsx" => (tree_sitter_typescript::LANGUAGE_TSX.into(), "tsx"),
        "go" => (tree_sitter_go::LANGUAGE.into(), "go"),
        _ => return None,
    })
}

/// The facts of one file, or None when no grammar reads its kind (code_surface.py reads it instead).
pub fn file_facts(rel: &str, source: &str) -> Option<Value> {
    let (language, name) = language(rel)?;
    let mut parser = Parser::new();
    parser.set_language(&language).ok()?;
    let tree = parser.parse(source, None)?;
    let mut facts = Facts::default();
    match name {
        "python" => python(tree.root_node(), source, rel, &mut facts),
        "go" => go(tree.root_node(), source, &mut facts),
        _ => javascript(tree.root_node(), source, &mut facts),
    }
    Some(facts.value())
}

fn safe_rel(rel: &str) -> bool {
    !rel.is_empty() && !rel.starts_with('/') && !rel.contains('\\') && !rel.split('/').any(|p| p.is_empty() || p == "..")
}

/// `{"repo": DIR, "files": [rel, ...]}` in, the facts of each file the grammars read out.
pub fn cli(input: &str) -> (String, i32) {
    let error = |message: &str| (json!({"status": "ERROR", "error": message}).to_string(), 1);
    let Ok(request) = serde_json::from_str::<Value>(input) else { return error("expected {\"repo\": ..., \"files\": [...]}") };
    let (Some(repo), Some(files)) = (request["repo"].as_str(), request["files"].as_array()) else {
        return error("expected {\"repo\": ..., \"files\": [...]}");
    };
    if files.len() > MAX_FILES {
        return error("too many files");
    }
    let repo = Path::new(repo);
    let mut out = Map::new();
    let mut skipped = Vec::new();
    let mut total = 0u64;
    for rel in files {
        let Some(rel) = rel.as_str().filter(|r| safe_rel(r)) else { return error("invalid file path") };
        if language(rel).is_none() {
            continue;
        }
        let path = repo.join(rel);
        let Ok(meta) = std::fs::symlink_metadata(&path) else { skipped.push(rel.to_string()); continue };
        if !meta.is_file() || meta.len() > MAX_FILE || total + meta.len() > MAX_TOTAL {
            skipped.push(rel.to_string());
            continue;
        }
        total += meta.len();
        let Ok(bytes) = std::fs::read(&path) else { skipped.push(rel.to_string()); continue };
        let source = String::from_utf8_lossy(&bytes);
        match file_facts(rel, &source) {
            Some(facts) => { out.insert(rel.to_string(), facts); }
            None => skipped.push(rel.to_string()),
        }
    }
    (json!({"schema_version": 1, "kind": "ONTOLOGYEX_CODE_SURFACE_FACTS", "parser": "tree-sitter",
            "languages": ["python", "javascript", "typescript", "tsx", "go"], "files": out, "not_parsed": skipped,
            "semantics": "SYNTAX_CANDIDATES_NOT_VERIFIED_ENDPOINTS_OR_TABLES"}).to_string(), 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn facts(rel: &str, source: &str) -> Value {
        file_facts(rel, source).unwrap()
    }

    fn routes(value: &Value) -> Vec<(String, String)> {
        value["routes"].as_array().unwrap().iter()
            .map(|r| (r["method"].as_str().unwrap().to_string(), r["path"].as_str().unwrap().to_string())).collect()
    }

    #[test]
    fn python_routes_follow_each_router_and_skip_strings() {
        let src = "users = APIRouter(prefix=\"/users\")\nadmin = APIRouter(prefix='/admin')\n\"\"\"\n@app.get('/doc')\n\"\"\"\n\
                   @users.get(\n    \"/{id}\",\n)\ndef read(id): ...\n@admin.post(\"\")\ndef make(): ...\n\
                   @bp.route('/x', methods=('PUT', 'PATCH'))\ndef x(): ...\napp.include_router(admin, prefix='/v1')\n";
        let found = facts("api/routes.py", src);
        assert_eq!(routes(&found), vec![("GET".into(), "/users/{id}".into()), ("POST".into(), "/v1/admin".into()),
                                        ("PUT".into(), "/x".into()), ("PATCH".into(), "/x".into())]);
        assert_eq!(found["routes"][0]["line"], 6);
    }

    #[test]
    fn javascript_mounts_chains_fastify_and_exports() {
        let src = "const billing = express.Router();\n// app.get('/commented', f)\nbilling.route('/invoices').get(list).post(make);\n\
                   app.use('/api/billing', billing);\nfastify.route({ method: ['GET', 'HEAD'], url: '/f' });\n\
                   axios.get('/not-a-route');\nexport { handler as POST };\nexport const GET = () => 1;\n";
        let found = facts("server.ts", src);
        assert_eq!(routes(&found), vec![("GET".into(), "/api/billing/invoices".into()),
                                        ("POST".into(), "/api/billing/invoices".into()),
                                        ("GET".into(), "/f".into()), ("HEAD".into(), "/f".into())]);
        let exports: Vec<&str> = found["exports"].as_array().unwrap().iter().map(|e| e["name"].as_str().unwrap()).collect();
        assert_eq!(exports, vec!["POST", "GET"]);
    }

    #[test]
    fn go_groups_chi_routes_and_gorilla_methods() {
        let src = "package m\nfunc main() {\n v1 := r.Group(\"/v1\")\n admin := v1.Group(\"/admin\")\n admin.GET(`/users`, h)\n\
                   r.Route(\"/a\", func(r chi.Router) { r.Get(\"/b\", h) })\n s := r.PathPrefix(\"/api\").Subrouter()\n\
                   s.HandleFunc(\"/x\", h).Methods(\"POST\", \"PUT\")\n // r.Get(\"/commented\", h)\n}\n";
        let found = facts("main.go", src);
        assert_eq!(routes(&found), vec![("GET".into(), "/v1/admin/users".into()), ("GET".into(), "/a/b".into()),
                                        ("POST".into(), "/api/x".into()), ("PUT".into(), "/api/x".into())]);
    }

    #[test]
    fn tables_jobs_and_operations() {
        let py = facts("app/models.py", "import django\nclass Order(models.Model):\n    class Meta: ...\n\
                                         class S(BaseModel): ...\nclass U(SQLModel, table=True): ...\n\
                                         class R(Base):\n    __tablename__ = 'refunds'\n@shared_task\ndef send(): ...\n\
                                         beat = {'nightly': {'schedule': 1, 'task': 'shop.tasks.expire'}}\n");
        let tables: Vec<&str> = py["tables"].as_array().unwrap().iter().map(|t| t["name"].as_str().unwrap()).collect();
        assert_eq!(tables, vec!["Order", "U", "refunds"]);
        let jobs: Vec<&str> = py["jobs"].as_array().unwrap().iter().map(|j| j["name"].as_str().unwrap()).collect();
        assert_eq!(jobs, vec!["send", "nightly -> shop.tasks.expire"]);
        let ts = facts("db.ts", "@Entity()\nexport class Plan {}\nconst t = pgTable('plans', {});\n\
                                 const r = router({ getPlan: publicProcedure.query(f), make: t.procedure.mutation(f) });\n\
                                 const q = new Queue('emails');\n");
        let names: Vec<&str> = ts["tables"].as_array().unwrap().iter().map(|t| t["name"].as_str().unwrap()).collect();
        assert_eq!(names, vec!["Plan", "plans"]);
        let ops: Vec<&str> = ts["operations"].as_array().unwrap().iter().map(|o| o["name"].as_str().unwrap()).collect();
        assert_eq!(ops, vec!["getPlan", "make"]);
        assert_eq!(ts["jobs"][0]["name"], "emails");
    }

    #[test]
    fn cli_refuses_paths_out_of_the_repository() {
        assert_eq!(cli(r#"{"repo": "/", "files": ["../etc/passwd"]}"#).1, 1);
        assert_eq!(cli(r#"{"repo": "/", "files": ["/etc/passwd"]}"#).1, 1);
        let (out, code) = cli(r#"{"repo": "/nonexistent", "files": ["a.py", "b.rb"]}"#);
        assert_eq!(code, 0);
        let value: Value = serde_json::from_str(&out).unwrap();
        assert_eq!(value["not_parsed"], json!(["a.py"]));
    }
}
