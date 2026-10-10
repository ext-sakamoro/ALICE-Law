//! Reads "requirement<TAB>version" lines and prints 1 (accepts), 0 (refuses) or E (unreadable)
use std::io::BufRead;
fn main() {
    for line in std::io::stdin().lock().lines() {
        let l = line.unwrap();
        let (r, v) = l.split_once('\t').unwrap();
        let out = match (semver::VersionReq::parse(r), semver::Version::parse(v)) {
            (Ok(r), Ok(v)) => if r.matches(&v) { "1" } else { "0" },
            _ => "E",
        };
        println!("{out}");
    }
}
