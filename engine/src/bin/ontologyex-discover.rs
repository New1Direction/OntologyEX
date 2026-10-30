//! Offline manifest discovery; prints the same JSON as `discover_repo.py --repo REPO`.
//! `ontologyex-discover surface` reads `{"repo": DIR, "files": [...]}` on stdin and prints what
//! the code in those files declares, read from syntax trees (see `surface.rs`).
//! Reads files only: no code execution, network, or credentials.
use ontologyex_core::discover;
use std::path::PathBuf;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    #[cfg(feature = "surface")]
    if args.len() == 2 && args[1] == "surface" {
        use std::io::Read;
        let mut input = String::new();
        if std::io::stdin().take(64 * 1024 * 1024).read_to_string(&mut input).is_err() {
            eprintln!("surface: could not read the request");
            std::process::exit(2);
        }
        let (text, code) = ontologyex_core::surface::cli(&input);
        println!("{text}");
        std::process::exit(code);
    }
    let repo = match args.as_slice() {
        [_, flag, repo] if flag == "--repo" => PathBuf::from(repo),
        _ => {
            eprintln!("usage: ontologyex-discover --repo REPO | ontologyex-discover surface < REQUEST");
            std::process::exit(2);
        }
    };
    let (text, code) = discover::cli_output(&repo);
    println!("{text}");
    std::process::exit(code);
}
