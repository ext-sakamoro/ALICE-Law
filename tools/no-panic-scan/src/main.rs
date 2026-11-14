//! Count the panic sites of a repository's production Rust code from the syntax tree.
//!
//! ```text
//! no-panic-scan <repo-dir> [--sites]
//! ```
//!
//! Production code is every `.rs` file under a `src/` directory of the repository (the root
//! package and workspace members, binaries included), except under `tests/`, `benches/`,
//! `examples/`, `fuzz/` or `target/`, a `build.rs`, and the development crates under a
//! top-level `scripts/`, `tools/`, `xtask/` or `ci/`. Inside a file, items under
//! `#[cfg(test)]` or `#[cfg(all(test, ..))]` and `#[test]` functions are skipped; an item under
//! `#[cfg(any(test, ..))]` is production code too (it is compiled when the other predicate holds)
//! and is counted. A file that is a module declared under `#[cfg(test)]` elsewhere
//! (`#[cfg(test)] mod tests;` loading `tests.rs`, `tests/mod.rs`, a `#[path]`, or a file under
//! that module's directory) is test code too: the module tree is resolved before counting.
//!
//! Counted, as one site each:
//!
//! * `unwrap`: a method call `.unwrap()` (no argument)
//! * `expect`: a method call `.expect(..)` with one argument
//! * `panic`, `todo`, `unimplemented`, `unreachable`: an invocation of that macro (the last path
//!   segment), also inside the arguments of another macro (`assert!(x.unwrap())`, `format!`)
//! * `assert`: an invocation of `assert!`, `assert_eq!` or `assert_ne!` (they panic in a release
//!   build when the condition fails, so each one is a panic site of production code); the
//!   `debug_assert` forms are not counted, since a release build compiles them out
//! * `allow`: an attribute `allow(..)` or `expect(..)` that names `clippy::unwrap_used`,
//!   `clippy::expect_used`, `clippy::panic`, `clippy::todo`, `clippy::unimplemented` or
//!   `clippy::unreachable` — one per lint named, counted under that lint: a deny of the lint is
//!   void where such an attribute stands, so it is a site of the lint.
//!
//! Without the types the scan cannot tell `Option::expect` from another method named `expect`
//! with one argument; it counts both (over-counting, never under).
//!
//! Output (tab-separated): `files <n>`, `unparsed <n>`, then one line per lint with its count.
//! `--sites` also prints `site <path>:<line> <lint>` for every site. Exit 0, or 2 when the
//! directory cannot be read.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use proc_macro2::TokenStream;
use quote::ToTokens;
use syn::visit::{self, Visit};
use syn::Attribute;

const LINTS: [&str; 7] = [
    "unwrap",
    "expect",
    "panic",
    "todo",
    "unimplemented",
    "unreachable",
    "assert",
];

/// the lint a macro invocation counts under, by the last segment of its path
fn macro_lint(name: &str) -> Option<&'static str> {
    match name {
        "panic" => Some("panic"),
        "todo" => Some("todo"),
        "unimplemented" => Some("unimplemented"),
        "unreachable" => Some("unreachable"),
        "assert" | "assert_eq" | "assert_ne" => Some("assert"),
        _ => None,
    }
}

/// clippy lint name → counted lint
const CLIPPY: [(&str, &str); 6] = [
    ("unwrap_used", "unwrap"),
    ("expect_used", "expect"),
    ("panic", "panic"),
    ("todo", "todo"),
    ("unimplemented", "unimplemented"),
    ("unreachable", "unreachable"),
];

/// `#[cfg(test)]` or `#[cfg(all(test, ..))]` (`test` a term of the `all` itself, whatever its other terms); `cfg(any(test, ..))`,
/// `not(test)` and an `all` whose `test` is only inside a `not(..)` or `any(..)` term are not
fn is_cfg_test(attrs: &[Attribute]) -> bool {
    attrs.iter().any(|a| {
        if !a.path().is_ident("cfg") {
            return false;
        }
        let s = a.meta.to_token_stream().to_string().replace(' ', "");
        let Some(inner) = s.strip_prefix("cfg(").and_then(|x| x.strip_suffix(')')) else {
            return false;
        };
        if inner == "test" {
            return true;
        }
        inner
            .strip_prefix("all(")
            .and_then(|x| x.strip_suffix(')'))
            .is_some_and(|terms| top_level_terms(terms).any(|p| p == "test"))
    })
}

/// The comma-separated terms of a cfg predicate list at its own level (a `not(..)` or `any(..)` term stays whole),
/// so `all(test, not(x))` has the terms `test` and `not(x)`, and `all(not(test), x)` has no `test` term
fn top_level_terms(list: &str) -> impl Iterator<Item = &str> {
    let mut depth = 0usize;
    let mut start = 0usize;
    let mut out = Vec::new();
    for (i, c) in list.char_indices() {
        match c {
            '(' => depth += 1,
            ')' => depth = depth.saturating_sub(1),
            ',' if depth == 0 => {
                out.push(&list[start..i]);
                start = i + 1;
            }
            _ => {}
        }
    }
    out.push(&list[start..]);
    out.into_iter()
}

/// `#[test]` (also `#[tokio::test]` and friends: a path whose last segment is `test`)
fn is_test_fn(attrs: &[Attribute]) -> bool {
    attrs
        .iter()
        .any(|a| a.path().segments.last().is_some_and(|s| s.ident == "test"))
}

fn skipped(attrs: &[Attribute]) -> bool {
    is_cfg_test(attrs) || is_test_fn(attrs)
}

struct Scan<'a> {
    path: &'a str,
    counts: &'a mut BTreeMap<&'static str, usize>,
    sites: &'a mut Vec<String>,
}

impl Scan<'_> {
    fn hit(&mut self, lint: &'static str, line: usize) {
        *self.counts.entry(lint).or_default() += 1;
        self.sites.push(format!("site {}:{} {}", self.path, line, lint));
    }

    fn macro_tokens(&mut self, mac: &syn::Macro) {
        let name = mac
            .path
            .segments
            .last()
            .map(|s| s.ident.to_string())
            .unwrap_or_default();
        if let Some(lint) = macro_lint(&name) {
            let line = mac.path.segments[0].ident.span().start().line;
            self.hit(lint, line);
        }
        self.scan_tokens(&mac.tokens);
    }

    /// The sites inside a macro's tokens, read from the token sequence (the tokens of `quote!`,
    /// `macro_rules!` bodies and `format!` arguments do not all parse as expressions): `. unwrap ()`,
    /// `. expect (..)` with a non-empty group, and `name !` for the four macros, nested groups
    /// included. String literals are single tokens, so their text is never read.
    fn scan_tokens(&mut self, tokens: &TokenStream) {
        use proc_macro2::{Delimiter, TokenTree};
        let tts: Vec<TokenTree> = tokens.clone().into_iter().collect();
        for (i, tt) in tts.iter().enumerate() {
            match tt {
                TokenTree::Group(g) => self.scan_tokens(&g.stream()),
                TokenTree::Ident(id) => {
                    let dot_before = i > 0
                        && matches!(&tts[i - 1], TokenTree::Punct(p) if p.as_char() == '.');
                    let next = tts.get(i + 1);
                    let paren = match next {
                        Some(TokenTree::Group(g)) if g.delimiter() == Delimiter::Parenthesis => {
                            Some(g.stream().is_empty())
                        }
                        _ => None,
                    };
                    let line = id.span().start().line;
                    let name = id.to_string();
                    if dot_before && name == "unwrap" && paren == Some(true) {
                        self.hit("unwrap", line);
                    } else if dot_before && name == "expect" && paren == Some(false) {
                        self.hit("expect", line);
                    } else if matches!(next, Some(TokenTree::Punct(p)) if p.as_char() == '!') {
                        if let Some(lint) = macro_lint(&name) {
                            self.hit(lint, line);
                        }
                    }
                }
                _ => {}
            }
        }
    }
}

impl<'ast> Visit<'ast> for Scan<'_> {
    fn visit_attribute(&mut self, a: &'ast Attribute) {
        let is_allow = a.path().is_ident("allow") || a.path().is_ident("expect");
        if is_allow {
            let s = a.meta.to_token_stream().to_string().replace(' ', "");
            for (clippy, lint) in CLIPPY {
                let needle = format!("clippy::{clippy}");
                let named = s
                    .match_indices(&needle)
                    .any(|(i, _)| {
                        let after = s[i + needle.len()..].chars().next();
                        !after.is_some_and(|c| c.is_alphanumeric() || c == '_')
                    });
                if named {
                    self.hit(lint, a.pound_token.span.start().line);
                }
            }
        }
        visit::visit_attribute(self, a);
    }
    fn visit_item_mod(&mut self, m: &'ast syn::ItemMod) {
        if !skipped(&m.attrs) {
            visit::visit_item_mod(self, m);
        }
    }
    fn visit_item_impl(&mut self, i: &'ast syn::ItemImpl) {
        if !skipped(&i.attrs) {
            visit::visit_item_impl(self, i);
        }
    }
    fn visit_item_fn(&mut self, f: &'ast syn::ItemFn) {
        if !skipped(&f.attrs) {
            visit::visit_item_fn(self, f);
        }
    }
    fn visit_impl_item_fn(&mut self, f: &'ast syn::ImplItemFn) {
        if !skipped(&f.attrs) {
            visit::visit_impl_item_fn(self, f);
        }
    }
    fn visit_trait_item_fn(&mut self, f: &'ast syn::TraitItemFn) {
        if !skipped(&f.attrs) {
            visit::visit_trait_item_fn(self, f);
        }
    }
    fn visit_item_const(&mut self, c: &'ast syn::ItemConst) {
        if !skipped(&c.attrs) {
            visit::visit_item_const(self, c);
        }
    }
    fn visit_item_static(&mut self, c: &'ast syn::ItemStatic) {
        if !skipped(&c.attrs) {
            visit::visit_item_static(self, c);
        }
    }
    fn visit_item_use(&mut self, _u: &'ast syn::ItemUse) {}
    fn visit_stmt(&mut self, s: &'ast syn::Stmt) {
        if let syn::Stmt::Local(l) = s {
            if skipped(&l.attrs) {
                return;
            }
        }
        visit::visit_stmt(self, s);
    }
    fn visit_expr_method_call(&mut self, m: &'ast syn::ExprMethodCall) {
        let line = m.method.span().start().line;
        if m.method == "unwrap" && m.args.is_empty() {
            self.hit("unwrap", line);
        } else if m.method == "expect" && m.args.len() == 1 {
            self.hit("expect", line);
        }
        visit::visit_expr_method_call(self, m);
    }
    fn visit_macro(&mut self, mac: &'ast syn::Macro) {
        self.macro_tokens(mac);
    }
}

/// Whether `rel` (a path relative to the repository root, `/`-separated) is production code
fn production(rel: &str) -> bool {
    if !rel.ends_with(".rs") {
        return false;
    }
    let parts: Vec<&str> = rel.split('/').collect();
    if parts
        .iter()
        .any(|p| matches!(*p, "tests" | "benches" | "examples" | "fuzz" | "target"))
    {
        return false;
    }
    if parts.last() == Some(&"build.rs") {
        return false;
    }
    if matches!(parts[0], "scripts" | "tools" | "xtask" | "ci" | ".github") {
        return false;
    }
    parts.contains(&"src")
}

fn walk(root: &Path, dir: &Path, out: &mut Vec<PathBuf>) -> std::io::Result<()> {
    let mut entries: Vec<_> = fs::read_dir(dir)?.collect::<Result<_, _>>()?;
    entries.sort_by_key(std::fs::DirEntry::file_name);
    for e in entries {
        let p = e.path();
        let name = e.file_name();
        if name == ".git" || name == "target" || name == "node_modules" {
            continue;
        }
        let ft = e.file_type()?;
        if ft.is_dir() {
            walk(root, &p, out)?;
        } else if ft.is_file() {
            out.push(p);
        }
    }
    Ok(())
}

/// The files and directories that `#[cfg(test)] mod name;` declarations of `file` (at `rel`) load:
/// `name.rs` and `name/` beside a `lib.rs` / `main.rs` / `mod.rs`, under `<stem>/` otherwise,
/// or the `#[path = ".."]` given (relative to the declaring file's directory)
fn test_module_paths(rel: &str, file: &syn::File) -> Vec<String> {
    let dir = rel.rsplit_once('/').map_or("", |(d, _)| d);
    let name = rel.rsplit('/').next().unwrap_or(rel);
    let base = if matches!(name, "lib.rs" | "main.rs" | "mod.rs") {
        dir.to_string()
    } else {
        let stem = name.trim_end_matches(".rs");
        if dir.is_empty() {
            stem.to_string()
        } else {
            format!("{dir}/{stem}")
        }
    };
    let join = |d: &str, f: &str| if d.is_empty() { f.to_string() } else { format!("{d}/{f}") };
    let mut out = Vec::new();
    let mut visit = |items: &[syn::Item]| {
        for item in items {
            let syn::Item::Mod(m) = item else { continue };
            if m.content.is_some() || !is_cfg_test(&m.attrs) {
                continue;
            }
            let path_attr = m.attrs.iter().find_map(|a| {
                if !a.path().is_ident("path") {
                    return None;
                }
                let syn::Meta::NameValue(nv) = &a.meta else { return None };
                let syn::Expr::Lit(syn::ExprLit { lit: syn::Lit::Str(s), .. }) = &nv.value else {
                    return None;
                };
                Some(s.value())
            });
            if let Some(p) = path_attr {
                out.push(join(dir, &p));
            } else {
                let n = m.ident.to_string();
                out.push(join(&base, &format!("{n}.rs")));
                out.push(join(&base, &format!("{n}/")));
            }
        }
    };
    visit(&file.items);
    out
}

/// Count the sites of one file's text (the unit the tests drive)
fn scan_text(
    path: &str,
    text: &str,
    counts: &mut BTreeMap<&'static str, usize>,
    sites: &mut Vec<String>,
) -> bool {
    let Ok(file) = syn::parse_file(text) else {
        return false;
    };
    let mut s = Scan {
        path,
        counts,
        sites,
    };
    // inner attributes of the file (`#![allow(..)]`) are part of `file.attrs`
    s.visit_file(&file);
    true
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(dir) = args.iter().find(|a| !a.starts_with("--")) else {
        eprintln!("usage: no-panic-scan <repo-dir> [--sites]");
        std::process::exit(2);
    };
    let want_sites = args.iter().any(|a| a == "--sites");
    let root = Path::new(dir);
    let mut files = Vec::new();
    if let Err(e) = walk(root, root, &mut files) {
        eprintln!("no-panic-scan: cannot read {dir}: {e}");
        std::process::exit(2);
    }
    let mut counts: BTreeMap<&'static str, usize> = LINTS.iter().map(|l| (*l, 0)).collect();
    let mut sites = Vec::new();
    let (mut scanned, mut unparsed) = (0usize, 0usize);
    // read and parse every production file first, so the test modules they declare are known
    let mut parsed: Vec<(String, Option<String>)> = Vec::new();
    let mut test_paths: Vec<String> = Vec::new();
    for p in files {
        let Ok(rel) = p.strip_prefix(root) else {
            continue;
        };
        let rel = rel.to_string_lossy().replace('\\', "/");
        if !production(&rel) {
            continue;
        }
        let text = fs::read_to_string(&p).ok();
        if let Some(f) = text.as_deref().and_then(|t| syn::parse_file(t).ok()) {
            test_paths.extend(test_module_paths(&rel, &f));
        }
        parsed.push((rel, text));
    }
    let is_test_file = |rel: &str| {
        test_paths
            .iter()
            .any(|t| if t.ends_with('/') { rel.starts_with(t.as_str()) } else { rel == t })
    };
    for (rel, text) in parsed {
        if is_test_file(&rel) {
            continue;
        }
        let Some(text) = text else {
            unparsed += 1;
            continue;
        };
        scanned += 1;
        if !scan_text(&rel, &text, &mut counts, &mut sites) {
            unparsed += 1;
            println!("unparsed-file\t{rel}");
        }
    }
    println!("files\t{scanned}");
    println!("unparsed\t{unparsed}");
    for l in LINTS {
        println!("{l}\t{}", counts[l]);
    }
    if want_sites {
        for s in sites {
            println!("{s}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn count(text: &str) -> BTreeMap<&'static str, usize> {
        let mut c = BTreeMap::new();
        let mut s = Vec::new();
        assert!(scan_text("src/x.rs", text, &mut c, &mut s), "parses");
        c
    }

    fn n(c: &BTreeMap<&'static str, usize>, k: &str) -> usize {
        c.get(k).copied().unwrap_or(0)
    }

    #[test]
    fn production_sites_are_counted() {
        let c = count(r#"fn f() { x.unwrap(); y.expect("a"); panic!("b"); todo!(); unimplemented!(); unreachable!(); assert!(z); }"#);
        for l in LINTS {
            assert_eq!(n(&c, l), 1, "{l}");
        }
    }

    #[test]
    fn release_asserts_count_and_debug_asserts_do_not() {
        let c = count(
            "fn f() { assert!(a); assert_eq!(b, 1); std::assert_ne!(c, 2); debug_assert!(d); \
             debug_assert_eq!(e, 3); debug_assert_ne!(f, 4); format!(\"{}\", { assert!(g); 1 }); }\n\
             macro_rules! m { () => { assert_eq!(1, 1) }; }",
        );
        // assert!, assert_eq!, std::assert_ne!, the one inside format!'s tokens, the one in a macro_rules! body
        assert_eq!(n(&c, "assert"), 5);
        assert_eq!(n(&c, "panic"), 0);
    }

    #[test]
    fn test_code_is_skipped() {
        let c = count(
            "fn f() { a.unwrap(); }\n#[cfg(test)]\nmod tests { fn g() { b.unwrap(); panic!(); } }\n\
             #[test]\nfn t() { c.unwrap(); }\n#[cfg(all(test, feature = \"x\"))]\nfn h() { d.unwrap(); }",
        );
        assert_eq!(n(&c, "unwrap"), 1);
        assert_eq!(n(&c, "panic"), 0);
    }

    #[test]
    fn cfg_all_test_with_a_negated_other_predicate_is_test() {
        // `all` needs `test`, whatever else it says: a `not(..)` of another predicate keeps it test-only
        let c = count(
            "fn f() { a.unwrap(); }\n#[cfg(all(test, not(target_arch = \"wasm32\")))]\nfn g() { b.unwrap(); panic!(); }\n\
             #[cfg(all(not(target_os = \"windows\"), test))]\nfn h() { c.unwrap(); }",
        );
        assert_eq!(n(&c, "unwrap"), 1);
        assert_eq!(n(&c, "panic"), 0);
    }

    #[test]
    fn cfg_all_without_a_top_level_test_is_production() {
        // `test` only inside a `not(..)` or an `any(..)` of the `all`: compiled without test too
        let c = count(
            "#[cfg(all(not(test), feature = \"x\"))]\nfn f() { a.unwrap(); }\n\
             #[cfg(all(feature = \"x\", any(test, feature = \"y\")))]\nfn g() { b.unwrap(); }",
        );
        assert_eq!(n(&c, "unwrap"), 2);
    }

    #[test]
    fn cfg_any_test_and_not_test_are_production() {
        let c = count(
            "#[cfg(not(test))]\nfn f() { e.unwrap(); }\n#[cfg(any(test, feature = \"x\"))]\nfn g() { e.unwrap(); }",
        );
        assert_eq!(n(&c, "unwrap"), 2);
    }

    #[test]
    fn comments_strings_and_lookalikes_are_not_sites() {
        let c = count(
            "// x.unwrap()\nfn f() { let s = \"panic!\"; let r = r#\"y.unwrap()\"#; a.unwrap_or(1); \
             b.unwrap_or_default(); c.expect_err(\"\"); d.expect(); e.unwrap(1); }",
        );
        for l in LINTS {
            assert_eq!(n(&c, l), 0, "{l}");
        }
    }

    #[test]
    fn macro_arguments_are_scanned() {
        let c = count("fn f() { assert!(x.unwrap() > 0); println!(\"{}\", y.expect(\"z\")); }");
        assert_eq!((n(&c, "unwrap"), n(&c, "expect")), (1, 1));
        // tokens that do not parse as expressions: quote! with interpolations, macro_rules! bodies,
        // nested macros; a string literal's text is not read
        let c = count(
            "fn g() -> T { quote! { #a.unwrap_or_else(|e| panic!(\"{e}\")); #b.expect(\"x\") } }\n\
             macro_rules! m { ($e:expr) => { $e.unwrap(); todo!() }; }\n\
             fn h() { vec![format!(\"{}\", a.unwrap())]; println!(\"x.unwrap() panic!\"); }",
        );
        assert_eq!(n(&c, "panic"), 1);
        assert_eq!(n(&c, "expect"), 1);
        assert_eq!(n(&c, "unwrap"), 2);
        assert_eq!(n(&c, "todo"), 1);
    }

    #[test]
    fn allow_attributes_count_as_sites_of_their_lint() {
        let c = count(
            "#![allow(clippy::expect_used, clippy::module_name_repetitions)]\n\
             #[expect(clippy::unwrap_used, reason = \"r\")]\nfn f() {}\n\
             #[allow(clippy::panic_in_result_fn)]\nfn g() {}\n#[allow(clippy::todo, clippy::unreachable)]\nfn h() {}",
        );
        assert_eq!(n(&c, "expect"), 1);
        assert_eq!(n(&c, "unwrap"), 1);
        assert_eq!(n(&c, "panic"), 0, "panic_in_result_fn is another lint");
        assert_eq!((n(&c, "todo"), n(&c, "unreachable")), (1, 1));
    }

    #[test]
    fn test_modules_declared_without_a_body_are_resolved_to_their_files() {
        let f = syn::parse_file(
            "#[cfg(test)]\nmod tests;\nmod real;\n#[cfg(test)]\n#[path = \"t/x.rs\"]\nmod x;\n#[cfg(any(test, feature = \"f\"))]\nmod y;",
        )
        .expect("parses");
        let mut got = test_module_paths("src/chunked.rs", &f);
        got.sort();
        assert_eq!(got, ["src/chunked/tests.rs", "src/chunked/tests/", "src/t/x.rs"]);
        let lib = syn::parse_file("#[cfg(test)]\nmod tests;").expect("parses");
        let mut got = test_module_paths("crates/a/src/lib.rs", &lib);
        got.sort();
        assert_eq!(got, ["crates/a/src/tests.rs", "crates/a/src/tests/"]);
    }

    #[test]
    fn the_production_filter() {
        for (p, want) in [
            ("src/lib.rs", true),
            ("crates/a/src/x.rs", true),
            ("src/bin/main.rs", true),
            ("tests/t.rs", false),
            ("crates/a/tests/t.rs", false),
            ("benches/b.rs", false),
            ("examples/e/src/main.rs", false),
            ("build.rs", false),
            ("scripts/tool/src/main.rs", false),
            ("tools/x/src/lib.rs", false),
            ("README.md", false),
            ("lib.rs", false),
        ] {
            assert_eq!(production(p), want, "{p}");
        }
    }
}
