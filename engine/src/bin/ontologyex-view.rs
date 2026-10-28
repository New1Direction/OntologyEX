//! Offline Rust read-only graph view protocol, bounded JSON on stdin.
//! No listener, connector, file access, execution, credentials or tenant auth.
//! The Python control room is unchanged until parity and performance review.
use ontologyex_core::{business, incident, view};
use serde_json::Value;
use std::env;
use std::io::{self, Read};

const MAX_INPUT_BYTES: u64 = 16 * 1024 * 1024;

fn decimal(input: &str) -> Result<usize, String> {
    if input.is_empty() || (input.len() > 1 && input.starts_with('0'))
        || !input.bytes().all(|c| c.is_ascii_digit()) {
        return Err("invalid decimal argument".into());
    }
    input.parse::<usize>().map_err(|_| "invalid numerical argument".into())
}
fn run() -> Result<(), String> {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {return Err("usage: ontologyex-view COMMAND [ARGS]".into());}
    let mut input = Vec::new();
    io::stdin().lock().take(MAX_INPUT_BYTES + 1)
        .read_to_end(&mut input).map_err(|_|"cannot read input")?;
    if input.len() as u64 > MAX_INPUT_BYTES {
        return Err("graph state exceeds byte limit".into());
    }
    let full: Value = serde_json::from_slice(&input)
        .map_err(|_|"invalid source graph JSON")?;
    let output = match args[1].as_str() {
        "summary" if args.len() == 2 => view::summary(&full)?,
        "page" if args.len() == 5 =>
            view::page(&full,&args[2],&args[3],decimal(&args[4])?)?,
        "neighborhood" if args.len() == 4 =>
            view::neighborhood(&full,&args[2],decimal(&args[3])?)?,
        "node" if args.len() == 3 => view::node_detail(&full,&args[2])?,
        "incident" if args.len() == 3 => {
            let graph = full.get("full").ok_or("incident requires full graph")?;
            let rows = full.get("rows").and_then(Value::as_array)
                .ok_or("incident requires observation rows")?;
            incident::project(graph, rows, &args[2])?
        },
        "suggest-links" if args.len() == 2 => business::suggest_from(&full)?,
        "conformance" if args.len() == 2 => business::conformance_table(),
        _ => return Err("unsupported view command or argument count".into()),
    };
    println!("{}",serde_json::to_string(&output)
        .map_err(|_|"cannot serialize view")?);
    Ok(())
}
fn main() {
    if let Err(reason)=run(){
        eprintln!("ontologyex-view rejected request: {reason}");
        std::process::exit(2);
    }
}
