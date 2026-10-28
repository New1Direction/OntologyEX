//! Offline test harness for the Rust projection core.
//! This does not connect to GitHub/cloud, run a daemon or collect telemetry.
use ontologyex_core::{project, Observation, Topology};
use std::env;
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::Path;

const MAX_TOPOLOGY_BYTES: u64 = 2 * 1024 * 1024;
const MAX_OBSERVATIONS_BYTES: u64 = 8 * 1024 * 1024;

fn read_bounded(path: &str, limit: u64) -> Result<Vec<u8>, String> {
    let p = Path::new(path);
    let meta = fs::symlink_metadata(p).map_err(|e| e.to_string())?;
    if meta.file_type().is_symlink() || !meta.is_file() || meta.len() > limit {
        return Err("input must be a regular file under the byte limit".into());
    }
    let contents = fs::read(p).map_err(|e| e.to_string())?;
    if contents.len() as u64 > limit {
        return Err("input changed or exceeds byte limit".into());
    }
    Ok(contents)
}

fn execute() -> Result<(), String> {
    let argv: Vec<String> = env::args().collect();
    if argv.len() != 4 {
        return Err("usage: ontologyex-core TOPOLOGY.json OBSERVATIONS.jsonl AS_OF_UNIX_MS".into());
    }
    let now_ms: i64 = argv[3].parse().map_err(|_| "invalid as-of unix millisecond timestamp")?;
    let topology: Topology =
        serde_json::from_slice(&read_bounded(&argv[1], MAX_TOPOLOGY_BYTES)?)
            .map_err(|e| format!("invalid topology: {e}"))?;
    let file = read_bounded(&argv[2], MAX_OBSERVATIONS_BYTES)?;
    let reader = BufReader::new(file.as_slice());
    let mut events: Vec<Observation> = Vec::new();
    for line in reader.lines() {
        let line = line.map_err(|e| e.to_string())?;
        if line.trim().is_empty() {
            continue;
        }
        if line.len() > 8192 || events.len() >= ontologyex_core::MAX_OBSERVATIONS {
            return Err("observation row or count budget exceeded".into());
        }
        events.push(
            serde_json::from_str(&line).map_err(|e| format!("invalid observation: {e}"))?,
        );
    }
    let output = project(&topology, &events, now_ms).map_err(|e| e.to_string())?;
    println!("{}", serde_json::to_string(&output).map_err(|e| e.to_string())?);
    Ok(())
}

fn main() {
    if let Err(err) = execute() {
        eprintln!("ontologyex-core refused input: {err}");
        std::process::exit(1);
    }
}
