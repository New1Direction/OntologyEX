//! Offline manifest discovery; prints the same JSON as `discover_repo.py --repo REPO`.
//! Reads manifests only: no code execution, network, or credentials.
use ontologyex_core::discover;
use std::path::PathBuf;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let repo = match args.as_slice() {
        [_, flag, repo] if flag == "--repo" => PathBuf::from(repo),
        _ => {
            eprintln!("usage: ontologyex-discover --repo REPO");
            std::process::exit(2);
        }
    };
    let (text, code) = discover::cli_output(&repo);
    println!("{text}");
    std::process::exit(code);
}
