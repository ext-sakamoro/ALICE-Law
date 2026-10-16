# ALICE-Law

[English](README.md)

release train (`release-train.toml`) に載る crate をまたいだ統合の検査: ALICE-LOL、ALICE-DetMath、ALICE-SDF、ALICE-Physics、ALICE-DB、ALICE-Zip、ALICE-Crypto、ALICE-Analytics、ALICE-Edge、ALICE-ML、ALICE-LLM、ALICE-Kinematics、ALICE-Sync この文が roster より遅れると、この file 自身の検査 `scripts/readme_roster_check.py` が失敗する

ここにあるものはすべて crates.io に公開された版から build し、path 依存を使わない 検査は 2 種類:

- 依存の整合 (この場で): すべての `alice-*` crate が `Cargo.lock` でちょうど 1 つの版に解決され、`scripts/dep_consistency.py` 自身の `CORE` 一覧が挙げる crate がすべてあり、各 `alice-*` の要求が、その要求が名指す crate の最新の公開版を受ける
- end to end: law を読み、計算し、保存し、復元し、すべての値を bit で比べる

core crate の release train (計画と、各公開の前後に走らせる script: [runbook](#runbook-crate-ごと)) と、この repository が公開する text の検査も持つ

目次: [依存の整合](#依存の整合) · [Release train](#release-train) · [公開する text](#公開する-text) · [Conformance](#conformance) · [End to end](#end-to-end) · [公開まで失敗する step](#公開まで失敗する-step) · [状態](#状態) · [ライセンス](#ライセンス)

## 依存の整合

`Cargo.toml` は各 core crate の最新の公開版に、互いへの依存を有効にする feature つきで依存するので、lock file に crate 同士のすべての繋がりが現れる

```sh
cargo generate-lockfile                 # resolve against crates.io as it is today
python3 scripts/dep_consistency.py      # Markdown report; exit 1 on a problem, 2 when a check compared nothing
```

検査:

1. crate ごとに 1 つの版: 1 つの lock file に同じ crate が 2 つの版であれば、その型と算術の写しが 2 つあることになり、一方を通して保存した値は、もう一方が読み戻す値と同じ型ではない
2. 揃っていること: `CORE` が挙げる crate がすべて lock file にある
3. 要求と最新版: この repository と、lock file が解決した版の各 core crate の `alice-*` の要求が、名指す crate の最新の公開版 (最新の安定版、安定版の無い crate では最新の pre-release) を受けること 受けない要求は、利用者の lock file に 2 つ目の版が入る経路になる

まだ来ない公開で消える問題は、`release-train.toml` の `[pending.<name>]` にその公開 (`until = "<crate> <version>"`) と、それで消える問題 (`linked = [<crate>, ...]`、`requires = ["<crate> <version> -> <dependency> <requirement>", ...]`) とともに載せる (CI のように) `--roster release-train.toml` を付けると、report はそれらを entry ごとに予期したものとして数える 載っていない問題は run を失敗させ、roster がその公開を `published` にした entry (古い: 外して、その公開で許される要求に上げる) と、どの問題にも当たらない項目 (期限切れ) も失敗させる この repository 自身の要求と欠けた core crate は載せられない: 待つのでなく上げるか加える したがって red の run は直すべき問題であって、まだ来ない公開ではない

各検査は比べた件数を報告し、1 件も比べなかった検査は run を失敗させる (exit 2) この repository が `alice-*` crate に path や git で依存していても同じ この repository 自身の package は検査する crate に含まれない

要求の照合は Cargo の規則 (caret、tilde、範囲、`1.2.*` や `1.x` のような wildcard、pre-release) に従う `scripts/semver_differential.py` が、生成した要求と版の組と、すべての演算子と要求の形を隣り合う版と突き合わせる固定の格子で、[semver](https://crates.io/crates/semver) crate (`tools/semver-oracle`) と比べ、食い違いがあれば失敗する (片方だけが読める要求も食い違いに数える)

CI (`.github/workflows/dependency-consistency.yml`) は試験、semver crate との比較と検査を、毎週月曜、手動、検査を変える push で走らせる report は job summary にある

## Release train

`release-train.toml` は core crate をまとめて公開する計画: crate ごとに、公開する版、順序の中の位置 (crate は自分が要求するものすべての後)、状態 (`planned`、`prepared-local`、`prepared`、`published`、この repository は `exempt`)、repository、repository に crate が 2 つ以上ある時は manifest の path と tag の名前 (`tag`、既定は `v{version}`) を持つ `[train] owners` はすべての公開に必要な crates.io の owner を挙げる crate が他に何を要求するかはここに書かない: 公開する checkout の crate 自身の `Cargo.toml` から読むので、出所は 1 つ crate は `must-contain` (公開する commit が含むべき commit)、`changelog-has` (CHANGELOG の `## [<version>]` の節が含むべき行)、`must-contain-patch` (公開する commit が、直近の merge でない 2000 commit の中に、変更そのものか rebase した写しとして持つべき変更の `git patch-id --stable` の id)、`required-ci` (`{ workflow, job, legs, since }`; `since` のある entry はその crate のその版から効く: 公開する commit に対するその workflow の最新の GitHub Actions run で、job の leg `<job>` / `<job> (...)` が、期待どおり (件数か正確な名前) で、すべて success で完了していること 欠けた leg、余分な leg、実行中・skip・失敗の leg は拒否、CI が読めないのも拒否) も持てる

### Runbook: crate ごと

```sh
S=scripts/release_train.py
python3 $S next                                                      # 1. the crate to publish now
git clone <its repository> /tmp/rel && git -C /tmp/rel checkout <sha> # 2. a clean checkout of the release commit
python3 $S publish-gate <crate> --repo /tmp/rel --ref <sha>          # 3. exit 0 = go; prints the .crate sha256 and the publish command
(cd /tmp/rel && cargo publish --locked --manifest-path Cargo.toml)    # 4. the command the gate printed, run by a person
git -C /tmp/rel tag -a <tag> <sha> -m "<crate> <version>" && git -C /tmp/rel push origin <tag>   # 5. the tag at the published commit
python3 $S roster-mark published <crate> --repo /tmp/rel --ref <sha> --expect-sha256 <from 3>   # 6. verify, then mark the roster
python3 $S followups <crate> --repos-root <dir with every roster checkout>                      # 7. who has to raise a requirement
```

その後 `release-train.toml` を commit し、`followups` が挙げる要求の引き上げを取り込む (順序の次の crate はその 1 つ)

1. `next` は順序の中で公開されていない最初の crate を名指す
2. `cargo publish` は working tree を package にするので、gate は作業中の写しでなく、その commit の clone で走らせる
3. `publish-gate <crate> --repo <checkout> --ref <sha> [--manifest-path <path>]` はその commit の crate の manifest (と repository の根の `Cargo.toml`) を読み、次の時に拒否する:
   - checkout がその commit でない、または追跡している file に変更がある
   - crate が `prepared` でない、または manifest が roster と別の crate か版
   - manifest が `alice-*` crate を上書きする (`[patch.*]`、`[replace]`) か、別の registry から取る
   - その版がすでに crates.io にある
   - manifest の `alice-*` 依存 (normal か build、optional か否か、どの target か、改名の有無、名前の `-` / `_` を問わず) に crates.io の版の要求が無い、または roster で `published` でない crate を名指す、crates.io に無いか yank されている、要求に受けられない (Cargo の規則: `"0.4"` は 0.5.0 を受けない) roster の外の依存はその最新の公開版と照らす
   - `must-contain` の commit がその commit の祖先でない、CHANGELOG に `## [<version>] - YYYY-MM-DD` の公開の見出し (実在する日付で、明日より後でない; すべての crate で確かめる) が無い、その節に `changelog-has` の行が無い
   - 直近の commit のどれも `must-contain-patch` の patch を持たない
   - `required-ci` の job が、その commit に対する workflow の最新の run で期待どおりの leg を持たない、または leg が success で完了していない
   - repository の Actions secrets (repository と environment、GitHub から読む; 何も読めなければ拒否) に、roster の `[secrets."<repository>"]` が用途とともに宣言していない名前がある (または roster が宣言した名前が repository に無い)、または公開の credential が必須の reviewer を持つ environment の外にある; release、tag、dispatch、schedule の trigger で、そのような environment の外で job が secret (`GITHUB_TOKEN` 以外の `secrets.<name>`、`secrets: inherit`、OIDC token) を受ける (受ける secret がすべて公開に使わないと宣言されている時を除く); `secrets: inherit` は local の workflow にだけ渡せ、その workflow も読む; repository の Actions variables (repository と environment) は `[variables."<repository>"]` に用途とともに宣言する (variable は credential を持たない)、Dependabot の secret は `[secrets]` に `dependabot:<name>` として宣言する、どの一覧も page ごとに読み、GitHub が数える件数と同じ数の名前を持つこと; release、tag、dispatch、schedule の trigger で self-hosted runner で走りうる job (runner group、GitHub-hosted の image の正確な label でない label、repository に登録された runner の label (GitHub から読む)、literal の値がすべて GitHub-hosted の image である matrix の値以外の式、または式が選ぶ literal の一覧がすべてそうでない式) は、必須の reviewer を持つ environment の下で走るか、`[self-hosted."<repository>"]` に宣言する (`"<workflow>:<job>"`、`registry-credentials-on-host = false`)
   - その commit の repository の workflow が、この gate の外で公開できる (tag の push で gate を通らず公開されうる): job ごとに、comment を除き継続行をつないで読み、crates.io の credential (secret として、または `CARGO_REGISTRY_TOKEN` / `CARGO_REGISTRIES_<name>_TOKEN` にある crates.io の token、crates.io の auth action、cargo を走らせる job の OIDC token) か crates.io の公開の command (`--dry-run` の無い `cargo … publish`、release-plz、publish-crates) がどこかにある; `environment` に必須の reviewer の無い job に PyPI / npm の token、OIDC token、公開の command がある (GitHub から読む); workflow を 1 つも読まなければ何も比べていない
   - package (`cargo package --list`) が `.github/`、`scripts/`、`docs/`、`logs/` の下の file や、非公開のメモ (file と directory の名前を script の中で sha256 として持つ) を含む: repository の道具とメモであって crate ではない manifest に `include` を置く

   go の時は `cargo package` (`cargo publish` と同じく package にした crate を build し、`Cargo.lock` が commit されていれば `--locked`) を走らせ、commit、`.crate` の sha256、正確な `cargo publish` の command を表示する package にした後、package のすべての file を `scripts/public_text_check.py` で読み (path は repository の path に戻し、その `.public-text.toml` と許可の一覧を使う; 内部の text を含む package は拒否、file が 1 つも無ければ何も比べていない)、checkout をもう一度確かめる (`cargo publish` は working tree を package にするので、package を作ることで追跡している file が変わってはならない: たとえば sibling の checkout が別の版を持つために lock が書き換わる場合) go の前に、その `.crate` を crates.io の利用者と同じように build もする: 上に `Cargo.lock`、`Cargo.toml`、`.cargo/config` の無い新しい directory に展開し、空の `CARGO_HOME` (registry の cache は gate 自身の directory) と最小の環境 (機械の `RUSTFLAGS`、`RUSTC_WRAPPER`、`CARGO_*` を持ち込まない) で、空の crate の path 依存として、既定の feature で 1 回、feature 無しで 1 回、すべての feature で 1 回 (同時に有効にできない feature は拒否する、ただし roster が `consumer-features` で build する組を挙げた時を除く; Python extension のように別の道具が link する library の feature は `consumer-check-features` から `cargo check` で compile する; `consumer-features-why` に理由を書き、ある組の既知の失敗は `consumer-expected-failure` に、それが示すべき error (`expect = {code, file, text, count, label?, message?}`: cargo の JSON 診断にある crate 自身のすべての error がその code、file、強調された source の text と、与えた時は主 span の label ("expected `…`, found `…`") を持ち、ちょうど `count` 件あること なので隣の別の error、同じ file の同じ code の別の error、同じものがもう 1 件、comment の中にだけある code は拒否する) と、それを直す公開とともに載せ、その後は拒否する) (`consumer-features-why` に、外した feature と理由: Python extension のように別の道具が build する feature、ある platform の hardware のための feature) library の無い crate はそれだけを、その外の directory から build する; Cargo の設定 (`.cargo/config`、`.cargo/config.toml`) を含む `.crate` は拒否する build の失敗、または crate の library (か binary) を 1 つも compile しなかった build は拒否する (`cargo package` は repository の中で検証するので、crate の上の lock を読む build script は repository の lock を見つけてしまう) `Cargo.lock` が commit されていれば、`.crate` は `cargo publish` が upload するものと同じ byte になる; 無ければ cargo はその時点で package に入れる lock を解決するので、gate の直後に公開する
4. 公開は script が行わない唯一の段
5. `publish-verify` は remote (`--remote`、既定は `origin`) から tag を読む
6. `roster-mark published` は `publish-verify` を走らせ、すべての検査が通った時だけ crate の `state = "prepared"` の行を `published` に変える `publish-verify` (書き換えをしない同じ検査) は次の時に拒否する:
   - その版が crates.io に無い、または yank されている
   - その checksum が gate の表示した sha256 でない
   - その license が manifest のものでない、または owner が `[train] owners` でない
   - download した `.crate` の hash がその checksum にならない、またはその `.cargo_vcs_info.json` が別の commit、dirty な checkout、別の directory を名指す
   - tag が remote に無い、または別の commit を名指す

   docs.rs の build の状態も情報として表示する (docs.rs は公開の後に build する)
7. `followups <crate> --repos-root <dir> [--at origin/main]` は、roster のすべての repository の checkout (先に fetch する) で `--at` のすべての `Cargo.toml` を読み、その crate への要求 (normal、dev、build、target、workspace) のうち roster の版を受けないものと、crate 自身の repository の外にある版の無いものを挙げる 上げるべきものがある間は exit 1

「crates.io に無い」と数えるのは HTTP 404 だけ; それ以外の応答 (error の本文、頼んだのと別の版、切れた接続) は command を exit 2 で止める 何も比べなかった command (roster の crate が無い、package の file が無い、manifest を読めない、`[train] owners` が無い) も同じ 要求の照合は `scripts/dep_consistency.py` のものを使う CI は試験 (`scripts/test_release_train.py`、crates.io、cargo、remote は fixture に置き換える) を走らせ、`next` を表示する

## 公開する text

この repository は公開されており、それが公開するもの (file の内容、path、commit message) もすべて公開される `scripts/public_text_check.py` はそれらに内部の語彙が無いかを検査する (開発の進め方の語、内部の tracker やメモの名前、指示の出所、機材の名前 (所有する機材、大文字小文字を問わず、組み込み board の系統を含む; CPU の系統、GPU の memory の分類、wheel の target、CI の runner の architecture のような platform や architecture の分類は技術的な text であって機材ではないので、意図して一覧に入れていない) とその SDK / firmware の詳細 (版を伴う SDK の名前、board support package の名前、機材を名指す行かその隣にある R<major>.<minor> の形の release の文字列)、非公開の機械の host 名 (SHA-256 として持つ)、非公開の network の address、SHA-256 の hash としてだけ持つ非公開の名前、review / session を名指す語 (前後の `レビュー`、`review`、`指摘`、`検証`、`session` / `セッション`) の隣にある id のような token (2 桁の hex)、review として名指された F と番号という review の指摘の label; 2 桁の id の一覧は hash にしても公開されてしまうので、id は一覧に持たない); 作業の単位として使う作業期間の語 (日本語の text の直後、日本語の text の前の行頭、その multi- の形、文字を伴う形、2 行の comment に分かれた「次の」の句として書かれたもの)、underscore か hyphen を含むすべての `[[name]]` link (`.toml` file か `Cargo.lock` の行頭の TOML の array-of-tables の見出しを除く)、文中の非公開のメモの名前 (メモの置き場を表す語の後、`<prefix>_<name>.md`、メモの frontmatter の key)、利用者の home directory (path の始まりか `file://` の後の `/Users/<name>/`、`/home/<name>/`; web の URL の中は除く; CI の runner の home と `<you>` や `$USER` のような placeholder は名前ではない)、非公開の repository への home directory の path (`~/<repo>/`、`/Users/<user>/<repo>/`; repository の名前は script の中に SHA-256 として持つので、script だけを取ってくる repository にも届く; 名前の集合が空なら、何も比べないのでなく exit 2 で検査を止める) 自分の instruction の package を 1 つの directory に置く repository では、その directory で始まる path (その repository のどの file でも、`./` か `../` の後でも) は package の名前であって agent の語ではない; 各 entry は `sha256(<repository> NUL <path prefix>)` で、それ以外の同じ形の path、別の repository の同じ path、文中の語は検出のまま Cloudflare の runtime の crate の名前でもある agent の語は、その crate の場所 (`crates/<that name>/`、`migrations/`、`wrangler.toml`、`Cargo.lock` の package の行) と、repository が `.public-text.toml` に挙げる `technical-paths` の下でだけ検出にしない; その file は `upstream-fork = true` も宣言できる (`upstream-fork-why` と、fork した点の完全な SHA で、検査する commit の祖先である `upstream-base` を伴う): fork では `--range` は fork した点より後のすべての commit を誰が書いたかを問わず読み、`--tree` はその後に足された・変えられた file を読む 同じ file で、ある種類の検出を、ある範囲で、固定した行数だけ、理由とともに許せる (語彙の linter 自身の語の一覧と fixture): `[public-text]` の下の `allow` の array of tables で、`path` (file か `/` で終わる directory)、`class` (agent process、work session、session name、session id、internal tracker、note link、internal note、instruction source、device、private address; session name、session id、device、private address はそれを定義する linter という単一の file にだけ; private name と private path は決して許さない)、`count`、`why` を持つ; `--tree` では別の行数なら失敗し、`--suggest-allow <ref> <scope>` はそこで見つかった件数つきの entry を表示する `--range` では `.public-text.toml` を変える commit は検出 ("policy config changed"、key つき) になり、summary は効いている設定を表示する binary の file は、6 文字以上の印字可能な並びとして ASCII と UTF-16 で読み、4 文字以上の token を残し (PNG の zTXt chunk や PDF の stream のような圧縮された text は展開しない)、ALICE-Physics の `scripts/docs_lint.py` の語彙で照合する 検出は場所と語の種類を名指し、語そのものは名指さない 他社の製品を機能として名指す製品 (固定された agent の指示の file 名、plugin の manifest の名前、extension の入口) は、script の `ALLOWED` で repository ごとに許す: 各 entry は `sha256(<scope> NUL <name, agent word or [[link]], lower case>)` とそれが許す行数で、scope は repository からの相対の file か `/` で終わる directory の prefix (path の component 単位)、`--allow-hash <scope> <name>` で作る なので製品を名指さず、他の場所では許さない; `--tree` では件数 (1 以上) と別の行数を許す entry は失敗する (新しい・消えた言及、綴り違いの entry)、commit message は決して許さず、開発の進め方の言及は一覧に載せない `--range` は読んだ file を commit の隣に報告する

```sh
python3 scripts/public_text_check.py --tree                     # every tracked file at HEAD
python3 scripts/public_text_check.py --range origin/main..HEAD  # the messages, added lines and added paths of the commits
scripts/preflight.sh                                            # the script tests and both scans, before a push
```

CI (`.github/workflows/public-text.yml`) はすべての push と pull request で試験と両方の走査を走らせる 何も比べなかった走査 (file も commit も無い) は失敗する

`README_JP.md` は `README.md` の日本語の text 本文は手で訳す; `scripts/readme_parity_check.py` は、訳しても変わってはならないものが 2 つで違えば失敗する: 見出し (`scripts/readme_headings.toml` に順に対で載せるので、片方だけで動いた・消えた・名前を変えた節が見つかる)、各節の段落と list の項目の数、code block、inline code、値、名前、link 文の意味は比べない: 言い換えた文、足した否定、場所を保った段落の中で変えた 1 桁の数や語で書いた数は通る

## Conformance

`docs/CONFORMANCE.md` は、Law を担う crate の算術が保証すべきことを人が読む形で書いた仕様 — 14 の gate があり、platform の超越関数を直接呼ぶことの禁止 (G1) から、浮動小数点の意味論を変える build の flag の禁止 (G14) まで `conformance/gates.toml` は同じ 14 の gate を生成器が読む形で持つ: id、名前、1 行の定義、通る crate が挙げるべき証拠の種類

release train の各 crate は自分の `alice-law-conformance.toml` を repository の根に持ち、gate ごとに 1 entry (`status`: `pass` / `fail-known` / `unverified` / `n/a`、`evidence`、`mutant`、`since`、`note`、`reference_repo`); この repository はそれらの manifest を持たず、それらが答える gate だけを持つ `scripts/conformance_roster_check.py` は release train のすべての crate の manifest をその main で読み (`gh api` で: file の tree と manifest)、次の時に失敗する: manifest の無い crate、entry の無い gate、未知の gate か別の形で gate のように綴った key (`g15`、`G01`)、gate の語彙の外の status (大文字小文字を区別)、先頭の語が main に無い path である evidence の entry (backtick、`label:` の接頭辞、先頭の `./`、`:<line>` / `::<name>` の接尾辞を外した後; 裸の file 名はちょうど 1 つの file に当たること)、文字列でない mutant、場所を示さない `fail-known` の entry: その note か evidence が、main にある file を path で (directory でも、`Cargo.toml` や `README.md` のような crate を説明する根の file でもなく)、または 1 つの file にだけ当たり後ろに行番号のある裸の名前で、または 1 つの workflow を名指す裸の名前で (step や leg が欠けている場所) 名指すこと; 別の repository のものとして書いた path (`ALICE-Law's scripts/...`) は数えず、行番号を file の長さと照らすことはしない `pass` / `n/a` の entry は、evidence の entry が main にある path を名指し、mutant が空でない文字列である時だけ通り、そうでなければ `unverified` に数える manifest がまだ書かれていない crate は `conformance/missing-manifests.toml` に名前で (manifest を足す repository とともに) 載せ、manifest が入った entry は古いものとして失敗する 各 repository の manifest の status の件数と、それを読む crate (repository を共有する crate はその 1 つの manifest を共有する) は `conformance/manifest-state.toml` にあり、`--write` で作り直す; main と違えば検査が失敗する 何も比べなければ exit 2

## End to end

`e2e/` は別の crate で、試験は 1 つ、`tests/law_to_storage_roundtrip.rs` が core crate を公開 API で通り抜ける:

1. 読む: text で書いた Newton の冷却の法則を alice-lol の研究 law の reader で読み、時間の格子で評価する
2. 計算する: alice-lol が検査する熱の場の law (alice-physics が解く)、`alice_physics::transient_thermal` で冷える棒、`PhysicsWorld` で落ちる 2 つの物体; alice-physics (`SdfUnion`、`ScalarField3D::max_value`) と alice-sdf (重なる箱の union、intersection、subtraction) の公開の経路で、等しい 0 (+0 と -0) が min や max に出会う所 (`f32::max` / `f32::min` は符号を target に任せ、x86_64 と aarch64 で違う); alice-edge が整数の sensor の窓に当てる固定小数点の直線と、それから作る `SignalLaw` (`alice_edge::law::linear_law`)
3. 当てはめる: 冷却の曲線への alice-zip の `SignalLaw` と、alice-analytics によるその残差の要約
4. 保存する: すべての値を alice-db の exact series (`AliceDB::series`、当てはめた model の store ではない) を通して、両方の law (冷却の law と alice-edge のもの) をその law store を通して、directory に
5. 復元する: directory を開き直してすべてを読み戻す
6. 比べる: すべての値を `to_bits` で、law の係数、定義域、record、識別子、残差の要約のすべての field、run 全体の alice-crypto の digest

crate が 1 つの算術で build されたこと (alice-zip、alice-db、alice-analytics、alice-edge、alice-lol から見た alice-det-math の `SEMANTICS_ID` が、`KNOWN_SEMANTICS` に記録した、link した alice-det-math の公開の識別子に等しく、そこにある以前の公開の識別子を `alice_det_math::check_semantics` が検証済みの以前のものとして受けること)、すべての `alice-*` crate が 1 つの版に lock されていること、名指す 9 つの core crate が build にあること (`e2e/tests/law_to_storage_roundtrip.rs`; roster から導かないので、train に加わった repository は自動ではこの検査に届かない) も確かめる 各比較は何も比べなければ失敗する 計算した run の golden digest (`e2e/golden/run.digest`、公開ごとに 1 行 `<alice-det-math version> <digest>`: law の record は算術から導いた識別子を持つので、公開ごとに digest がある) は、crate が記録した時と同じものを今も計算しているかを示し、往復は保存と復元で何も失われないことを示す `.github/workflows/e2e-main.yml` では同じ run を aarch64 と x86_64 の runner で行い、それぞれが digest を書き (`ALICE_LAW_E2E_DIGEST_OUT`)、最後の job が 2 つが互いに等しく、その alice-det-math の公開の golden の行に等しいことを求める

```sh
cd e2e
cargo test --release -- --nocapture             # published releases (resolved against crates.io)
ALICE_LAW_E2E_BLESS=1 cargo test --release      # record the golden digest of the linked alice-det-math release again
```

2 つの mode が同じ試験を走らせる:

| mode | crate | workflow | 意味 |
|---|---|---|---|
| published | crates.io の公開版 | `e2e.yml` | gate: 必要なすべての crate が互いに合う版で公開されるまで red |
| main | main の branch を並べ、`scripts/main_mode.py` で patch として差し込む | `e2e-main.yml` | 早期の警告: 次の公開で何が変わるか、版が他と合わないために main のどの crate が取り込まれないか |

`scripts/e2e_teeth.py` (main mode、`e2e-main.yml` の teeth の job) は checkout の 1 行ずつを変え、試験が期待した message で失敗することを求める: 1 ulp ずれて decode した law の係数、f32 を通して保存した f64、何も返さない復元、反転した digest の byte、platform の libm の `exp`、わずかに長い熱の step、別の算術の識別子、1 ulp ずれた残差、build に入った 2 つ目の alice-zip の版

## 公開まで失敗する step

公開の前に通れない CI の step (たとえば、公開前の crate の版を crates.io から解決する end to end の crate や、一方がまだ他方の 1 つ前の major を要求している main の branch を一緒に差し込む場合) は `scripts/expected_failure.py <step> -- <command>` で包み、`release-train.toml` の `[expected-failure.<name>]` に、待つ公開 (`until`)、その失敗が持つ error の行の text (`expect`)、理由 (`why`) とともに載せる 失敗はそれ自身の error の行から読む (端末の色の code を除いた後、大文字小文字を問わず `error` で始まり `:`、`[...]`、空白、行末が続く行、`fatal:`、Rust の panic; entry は `exit` で終了の status も名指せる): どの行も `expect` の text を含み、どの `expect` の text もどれかの行にあること なので 2 つ目の無関係な error や、text を引用するだけの log の行は通らない するとそれは予期したものとして報告され、それを要する step は飛ばされる; どの run も有効な entry (step、until、why) を job summary に挙げ、`expected_failure.py --check` はどの workflow も走らせない entry と、`exit = 101` 無しで `cargo` の command を包む entry で失敗する step は、別の形で失敗した時、roster で公開が `published` になった後も失敗する時、まだ載っているのに通った時 (entry を外す) に red になる したがって red の run は直すべき問題

## 状態

<!-- law-status:begin -->

`scripts/gen_law_status.py` が `release-train.toml`・`e2e/golden/run.digest`・`conformance/` から生成する (手で編集しない、生成結果と違えば CI が失敗する) CI の結果は各 workflow の run を見る

### Release train

| crate | 版 | 状態 | 順 | repository | tag |
|---|---|---|---|---|---|
| alice-det-math | 0.5.1 | published | 0 | ALICE-DetMath | v0.5.1 |
| alice-crypto | 0.4.1 | published | 1 | ALICE-Crypto | v0.4.1 |
| alice-zip | 0.10.0 | published | 2 | ALICE-Zip | v0.10.0 |
| alice-zip-cli | 3.1.0 | published | 3 | ALICE-Zip | alice-zip-cli-v3.1.0 |
| alice-analytics | 0.6.0 | published | 4 | ALICE-Analytics | v0.6.0 |
| alice-db | 0.5.1 | published | 5 | ALICE-DB | v0.5.1 |
| alice-physics | 3.1.0 | published | 6 | ALICE-Physics | v3.1.0 |
| alice-sdf | 6.1.0 | prepared | 7 | ALICE-SDF | v6.1.0 |
| alice-llm | 2.0.0 | published | 8 | ALICE-LLM | v2.0.0 |
| alice-lol-macro | 0.3.0 | published | 9 | ALICE-LOL | alice-lol-macro-v0.3.0 |
| alice-lol | 0.4.2 | published | 10 | ALICE-LOL | alice-lol-v0.4.2 |
| alice-edge | 0.3.0 | published | 11 | ALICE-Edge | v0.3.0 |
| alice-law-integration | 0.0.0 | exempt | 12 | ALICE-Law | (なし) |
| alice-ml | 0.2.0 | published | 13 | ALICE-ML | v0.2.0 |
| alice-kinematics | 0.2.1 | published | 14 | ALICE-Kinematics | alice-kinematics-v0.2.1 |
| alice-sync | 0.6.1 | published | 15 | ALICE-Sync | v0.6.1 |

### 公開まで失敗を予期する step

なし

### alice-det-math の版ごとの end to end の run digest

| alice-det-math | run digest |
|---|---|
| 0.5.0 | `966a78784bb4e3341ac6627d519db896f398176b71c2402b188294db6c8e3728` |
| 0.5.1 | `9f7cf676708a272e3817ebbf0727c610f391e1547747af4307ee44d5523267c2` |

### Conformance manifest (各 repository の main の manifest における 14 gate の状態の件数)

repository ごとに 1 行: repository を共有する crate は根にある 1 つの manifest を共有する (「読む crate」の列) ので、各 manifest は 1 回だけ数える; 13 の manifest × 14 の gate = 182 の gate の entry

| repository | 読む crate | pass | fail-known | unverified | n/a |
|---|---|---|---|---|---|
| ALICE-Analytics | alice-analytics | 4 | 6 | 4 | 0 |
| ALICE-Crypto | alice-crypto | 0 | 11 | 3 | 0 |
| ALICE-DB | alice-db | 4 | 7 | 3 | 0 |
| ALICE-DetMath | alice-det-math | 6 | 6 | 0 | 2 |
| ALICE-Edge | alice-edge | 4 | 5 | 5 | 0 |
| ALICE-Kinematics | alice-kinematics | 1 | 10 | 3 | 0 |
| ALICE-LLM | alice-llm | 0 | 12 | 2 | 0 |
| ALICE-LOL | alice-lol, alice-lol-macro | 4 | 6 | 4 | 0 |
| ALICE-ML | alice-ml | 2 | 8 | 4 | 0 |
| ALICE-Physics | alice-physics | 7 | 6 | 1 | 0 |
| ALICE-SDF | alice-sdf | 1 | 9 | 4 | 0 |
| ALICE-Sync | alice-sync | 7 | 4 | 3 | 0 |
| ALICE-Zip | alice-zip, alice-zip-cli | 5 | 9 | 0 | 0 |

### manifest がまだ無い crate

なし

<!-- law-status:end -->

## ライセンス

`AGPL-3.0-or-later OR LicenseRef-Commercial`: 二重 license で、どちらかを選ぶ

| 選択 | 条件 | 使う時 |
|--------|-------|-------------|
| **AGPL-3.0-or-later** | [LICENSE-AGPL](LICENSE-AGPL)、無料 | project そのものが AGPL と両立する open source である、または検査を内部で走らせる |
| **Commercial License** | [LICENSE-COMMERCIAL.md](LICENSE-COMMERCIAL.md)、有料、copyleft を外す | closed source の製品や pipeline、独自の SaaS、独自の道具での再配布 |

AGPL は強い copyleft: これらの検査を組み込み、配布するか利用者に提供する製品、pipeline、
service も AGPL で公開しなければならない end to end の試験の crate が link する core crate
のうち 3 つが同じ二重 license: alice-physics、alice-db、alice-crypto alice-lol、alice-zip、
alice-analytics は `Apache-2.0`、alice-sdf は `MIT OR Apache-2.0`; alice-det-math は試験が
build する公開版の 0.4.0 では `MIT OR Apache-2.0`、0.5.0 から `Apache-2.0`

商用 license の問い合わせ: <contact@extoria.co.jp>
