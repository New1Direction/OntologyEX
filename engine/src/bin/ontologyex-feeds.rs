//! Live observability feeds for the control room, in Rust: HTTP probes,
//! Prometheus instant queries, and an OTLP/HTTP trace receiver. Reads the same
//! `ONTOLOGYEX_FEEDS` configuration and writes the same observations file as
//! `feeds.py`, so `control_room.py --observations` reads either.
//!
//!   ontologyex-feeds run      --config FEEDS.json
//!   ontologyex-feeds once     --config FEEDS.json
//!   ontologyex-feeds check    --config FEEDS.json --repo REPO
//!
//! Diagnostics (pure functions, used by the parity tests):
//!   validate --config C | decode --content-type T --content-encoding E < BODY |
//!   prom-value < BODY | window-replay < SCENARIO | store-add --output F --keep K < ROWS
use chrono::DateTime;
use ontologyex_core::feeds::{self, Otlp, SpanWindow};
use ontologyex_core::feeds_net::Runner;
use ontologyex_core::{discover, pyjson};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;

const MAX_STDIN: u64 = 64 * 1024 * 1024;

fn option(args: &[String], name: &str) -> Result<String, String> {
    args.windows(2).find(|w| w[0] == name).map(|w| w[1].clone()).ok_or(format!("missing {name}"))
}

fn stdin() -> Result<Vec<u8>, String> {
    let mut data = Vec::new();
    std::io::stdin().take(MAX_STDIN).read_to_end(&mut data).map_err(|e| e.to_string())?;
    Ok(data)
}

fn window_replay(input: &Value) -> Result<Value, String> {
    let o = &input["otlp"];
    let services: BTreeMap<String, String> = serde_json::from_value(o["services"].clone()).map_err(|e| e.to_string())?;
    let otlp = Otlp {
        services, bind: String::new(), port: 0, token_env: String::new(),
        window: o["window_seconds"].as_i64().unwrap_or(60), emit: o["emit_seconds"].as_i64().unwrap_or(15),
        min_requests: o["min_requests"].as_u64().unwrap_or(5) as usize,
        warning: feeds::thresholds(o.get("warning"), "w")?, error: feeds::thresholds(o.get("error"), "e")?,
    };
    let moment = DateTime::from_timestamp(input["moment"].as_i64().unwrap_or(0), 0).ok_or("bad moment")?;
    let mut window = SpanWindow::new(&otlp);
    let mut emitted = Vec::new();
    for step in input["steps"].as_array().ok_or("steps")? {
        let at = step["at"].as_f64().ok_or("at")?;
        if step.get("emit").is_some() {
            let rows: Vec<String> = window.observations(at, moment).iter().map(|r| r.line()).collect();
            emitted.push(json!(rows));
        } else {
            let spans: Vec<feeds::Span> = step["spans"].as_array().ok_or("spans")?.iter().map(|s| {
                (s[0].as_str().unwrap_or("").to_string(), s[1].as_u64().unwrap_or(0) as u128,
                 s[2].as_u64().unwrap_or(0) as u128, s[3].as_f64().unwrap_or(0.0))
            }).collect();
            window.ingest(&spans, at);
        }
    }
    Ok(json!({"emitted": emitted, "accepted": window.accepted, "unmapped": window.unmapped}))
}

fn run() -> Result<i32, String> {
    let args: Vec<String> = std::env::args().collect();
    let command = args.get(1).map(String::as_str).unwrap_or("");
    match command {
        "run" | "once" | "check" | "validate" => {
            let config = feeds::load_config(Path::new(&option(&args, "--config")?))?;
            match command {
                "validate" => println!("{}", pyjson::dumps(&config.describe(), None, true)),
                "check" => {
                    let repo = PathBuf::from(option(&args, "--repo")?);
                    let known: BTreeSet<String> = discover::discover(&repo)?["nodes"].as_array().unwrap().iter()
                        .filter_map(|n| n["id"].as_str().map(str::to_string)).collect();
                    let mut used: BTreeSet<String> = config.probes.iter().map(|p| p.component.clone()).collect();
                    used.extend(config.prometheus.iter().flat_map(|p| p.targets.iter().map(|t| t.component.clone())));
                    used.extend(config.otlp.iter().flat_map(|o| o.services.values().cloned()));
                    let unknown: Vec<&String> = used.difference(&known).collect();
                    let status = if unknown.is_empty() { "OK" } else { "UNKNOWN_COMPONENTS" };
                    println!("{{\n  \"status\": \"{status}\",\n  \"components\": {},\n  \"unknown\": {}\n}}", used.len(),
                             pyjson::dumps(&json!(unknown), Some(2), true).replace('\n', "\n  "));
                    return Ok(if unknown.is_empty() { 0 } else { 2 });
                }
                "once" => {
                    let output = config.output.to_string_lossy().into_owned();
                    let mut runner = Runner::new(config);
                    let written = runner.poll_once()?;
                    println!("{{\n  \"written\": {written},\n  \"output\": {},\n  \"feeds\": {}\n}}",
                             pyjson::dumps(&Value::String(output), None, true), runner.status.ordered_json(1));
                }
                _ => {
                    println!("OntologyEX feeds writing {}", config.output.display());
                    let mut runner = Runner::new(config);
                    if runner.window.is_some() {
                        println!("OTLP/HTTP traces: http://{}/v1/traces", runner.start_otlp()?);
                    }
                    runner.run(&AtomicBool::new(false))?;
                }
            }
            Ok(0)
        }
        "decode" => {
            let (code, spans) = feeds::decode_request(&stdin()?, &option(&args, "--content-type").unwrap_or_default(),
                                                      &option(&args, "--content-encoding").unwrap_or_default());
            let spans: Vec<Value> = spans.iter().map(|(s, k, c, d)| json!([s, *k == feeds::SERVER_KIND, *c == feeds::ERROR_CODE, d])).collect();
            println!("{}", pyjson::dumps(&json!({"code": code, "spans": spans}), None, true));
            Ok(0)
        }
        "prom-value" => {
            let out = match feeds::prometheus_number(&stdin()?) {
                Ok(v) => json!({"value": v}),
                Err(e) => json!({"error": e}),
            };
            println!("{}", pyjson::dumps(&out, None, true));
            Ok(0)
        }
        "window-replay" => {
            let input: Value = serde_json::from_slice(&stdin()?).map_err(|e| e.to_string())?;
            println!("{}", pyjson::dumps(&window_replay(&input)?, None, true));
            Ok(0)
        }
        "conformance" => {
            println!("{}", pyjson::dumps(&feeds::conformance_table(), None, true));
            Ok(0)
        }
        "store-add" => {
            let rows: Value = serde_json::from_slice(&stdin()?).map_err(|e| e.to_string())?;
            let rows = rows.as_array().ok_or("rows")?.iter().map(feeds::row_from).collect::<Result<Vec<_>, _>>()?;
            let keep: usize = option(&args, "--keep")?.parse().map_err(|_| "keep")?;
            let written = feeds::store_add(Path::new(&option(&args, "--output")?), keep, rows)?;
            println!("{written}");
            Ok(0)
        }
        _ => Err("usage: ontologyex-feeds run|once|check|validate --config FEEDS.json [--repo REPO]".into()),
    }
}

fn main() {
    match run() {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            // json.dumps({"status": "ERROR", "error": str(exc)})
            println!("{{\"status\": \"ERROR\", \"error\": {}}}", pyjson::dumps(&Value::String(error), None, true));
            std::process::exit(1);
        }
    }
}
