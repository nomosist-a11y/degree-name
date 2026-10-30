# qcsuite ユーザーガイド

qcsuite は、Quad Cortex(以下 QC)を USB で操作して、練習・ライブ準備・録音・プリセット管理の手間を減らすツール群です。
ブラウザの画面(`qcsuite serve`)とターミナルのコマンド(`qcsuite ...`)の両方から使えます。

> **今の状態:** すべての機能は、実機を真似たモック(FakeQuadCortex)で開発・テストしたものです。
> **実機では一度も動かしていません**(モックで確認済み・実機は未確認)。使い始める前に
> [実機テスト手順書](hardware-test-plan.md) を実行してください。

## 1. 考え方: 判断は人、手間は機械

前回、AI が主導してトーンを合わせ込むやり方は品質が低かったので、qcsuite は音の良し悪しを決めません。

| 機械がやること | 人がやること |
|---|---|
| 同じ DI・同じ音量にそろえて鳴らす | どちらが良いかを聴いて決める |
| 名前を伏せて 2 つずつ並べる | 投票する |
| 本体で回したノブを記録する | 耳でノブを回す |
| コピー・バックアップ・整理の計画を作る | 計画を読んで、実行するかを決める |
| 数値(LUFS、特徴量)を測って並べる | 数値を参考に、聴いて決める |

スイープの「スコア」や音の地図の「近さ」は、聴く順番を決めるための目安です。良い音であることの証明ではありません。

## 2. 準備と起動

```bash
cd qc-tone-loop
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[device,dev]'                 # Claude API を使うなら '.[device,dev,claude]'
brew install hidapi && export DYLD_LIBRARY_PATH=/opt/homebrew/lib
pytest -q                                       # 実機なしのテスト
qcsuite --mock selftest                         # モックで全機能をひと通り動かす
```

1. **Cortex Control を終了します。** USB の制御を独占するため、同時には使えません。
2. 画面で使うなら `qcsuite serve`(ブラウザが開きます。`http://127.0.0.1:8765/`)。実機なしで試すなら `qcsuite --mock serve`。
3. コマンドで使うなら `qcsuite -h` で一覧が出ます。共通のオプションはコマンド名の前に書きます。

| オプション | 意味 |
|---|---|
| `--mock` | 実機の代わりにモックを使う(状態は `~/.qctone/mock/` に残る。`qcsuite --mock mock-reset` で初期化) |
| `--allow-folder Live` | Live / Artist / Song / Workshop への書き込みを、このコマンドに限り許可する |
| `--yes` | 確認の入力を省く(`--apply` が無い計画は実行されない。実機の自己テストでは無視される) |
| `--json` | 結果を JSON で出す |

サーバーは 127.0.0.1 だけで待ち受けます。LAN やスマホからの操作は、今回は作っていません(保留の「将来の案」)。

## 3. 安全のしくみ

| しくみ | 内容 |
|---|---|
| 書き込み先 | 既定は qcsuite が作る `_qcsuite_sandbox` だけ。Live / Artist / Song / Workshop は明示的に許可したときだけ |
| 保護フォルダ | My Presets / Favorite / kami / Cory Wong は書き込めない(許可リストにも入れられない)。Factory Library は読み取り専用 |
| 計画(plan) | プリセットの保存・コピー・削除・移動・上書き・フォルダの作成と削除は、計画を作って表示 → 確認 → 実行。**既定は dry-run**(CLI は `--apply`、画面は「実行」と入力) |
| バックアップ | 削除・上書き・移動・フォルダ削除の前に、対象のプリセットを JSON に保存する(`~/.qctone/plans/<ID>/backup/`) |
| 読み戻し | 実行のたびに一覧を読み直し、保存された名前が頼んだ名前(または本体の重複回避の形)かを確かめる |
| 音が変わる操作 | プリセットの読み込み(バックアップ・音量そろえ・一括リアンプなど)は本体の音を切り替え、保存していない編集を消す。実行前に知らせる |
| リアンプの警告 | リアンプでは本体から音が出るので、毎回モニターの音量を下げるよう求める |
| ロック | 本体を使うのは 1 プロセスだけ(`~/.qctone/device.lock`)。qctone も同じロックを使う |
| 読み取り専用モード | `QCSUITE_READONLY=1` で、保存もグリッドの編集もすべて拒否 |
| セットリストの上限 | ユーザーのフォルダが上限(12 個と想定、My Presets を含む)に達していたら、sandbox を作らずに止まる |
| 本体全体の設定 | I/O、Global EQ、マスターボリューム、ファームウェア関係は呼ばない |
| 画面の保護 | 127.0.0.1 以外の Host、ほかのサイトからの要求、qcsuite の画面を通さない要求は受け付けない |

## 4. パソコンからできないこと(実機で確認済みの事実)

- **キャプチャー**: `file_name` とキャプチャーブロックの GAIN などは、書けて読み戻せるが**音に反映されない**。本体の画面で読み込めば鳴る。
- **キャビのマイク**などの文字列パラメーターも同じ。
- **未所有のプラグイン**(Plini, Gojira, Nolly, Petrucci, John Mayer, Rabea, Misha, Tim Henson, Fortin, Soldano SLO-100 X)は、置けるが音は素通し(DI のまま)。Cory Wong の The Amp Snob と Dumbbell ODS は鳴った。
- **名前の変更**はできない(コピーして保存し直すしかない)。コピーでは**タグが消え**、同じ名前があると本体が 20 文字以内に切り詰めて `_N` を付ける。
- qcsuite はこれらを含むレンダリングを比較から外し、理由を表示します。原因の仮説と実験は [研究メモ](research-host-limits.md) にあります。

## 5. 機能ごとの使い方

画面はすべて `qcsuite serve` の上部のメニューから開けます。コマンドは `qcsuite <コマンド> -h` で詳しい使い方が出ます。
プリセットの指定は `フォルダ:スロット`(例 `kami:1E`、`"Factory Library:1A"`)。プリセット管理とバックアップでは `フォルダ/1A` や `フォルダ/名前` も使えます。

### DI ライブラリ(ホーム画面)

リアンプに使う DI を管理します。`qcsuite di list | import FILE --name main | record NAME --seconds 12 | trim NAME NEW --start 2 --dur 10`。
合成 DI(`synthetic-riff`)は試す用で、実際の演奏ではありません。聴いて判断するときは自分の DI を使ってください。

### 1. プリセット管理(プリセット管理画面 / `qcsuite library`)

- `list` / `search 語` — 全フォルダの一覧と検索(フォルダの一覧だけを読むので、音は変わりません)。
- `dupes [--markdown 整理案.md]` — 名前の重複(複数のフォルダにある同じ名前)と、中身の重複(バックアップの指紋で比較)。整理案は提案で、実行は本体で行います(削除機能はありません)。
- `copy 'Cory Wong/1A' ... [--to Live] [--rename ...] [--apply]` — 別フォルダへのコピーの計画。既定の行き先は sandbox。
- `show REF [--load|--current]` — チェーンとノブ。既定はバックアップから(読み込みなし)。
- `graft 元 先 [--blocks amp,cab] [--pair 行,列=行,列] [--name 名前] [--load] [--apply]` — 「元」のアンプとキャブを、「先」のシーンの仕組み(シーンごとのオン/オフ、シーン名、ほかのブロック)はそのままに組み込んだコピーを sandbox に保存する計画。どちらの元のプリセットも変更しません。キャブのマイクとキャプチャーはパソコンからでは鳴らないので、保存後に本体で選び直して本体で保存します。置き換えたブロックにあったシーンごとのノブの値はなくなります。
- `captures 語 [--export 候補.md]` / `irs` / `catalog` — キャプチャー(約 2,078 件)・IR・ブロックの検索。キャプチャーはパソコンから読み込んでも鳴らないので、「本体で手で読み込む候補リスト」を作れます。
- `usage` — フォルダの使用数とセットリストの上限。
- `favorites` — 本体のお気に入り(ハート)と「Favorite」フォルダの比較。**読むだけ**で、ハートの追加・削除はしません。

### 2. バックアップと変更履歴(バックアップ画面 / `qcsuite backup`)

- `run [--folders "Cory Wong,kami"] [--include-factory] [--resume ジョブID]` — プリセットを 1 つずつ読み込んで(**音が切り替わる**)正規化した JSON にし、`~/.qctone/backup/`(専用の git リポジトリ)にコミット。1 件 10〜25 秒ほど。中断しても再開できます。
- `status` / `log [--preset REF]` / `diff [A] [B] [--preset REF]` / `show REF [--commit C]` — 履歴と差分(ノブの値を実単位で表示)。
- `restore REF [--commit C] [--to Live] [--slot 3A] [--apply]` — **sandbox(または許可したフォルダ)にだけ**組み立て直して保存します。元のフォルダには書きません。キャプチャー・マイク・未所有プラグインは鳴らず、タグは戻りません。

### 3. 音量そろえ(音量そろえ画面 / `qcsuite level`)

1. `check-lane --preset "Factory Library:1A"` — 行の出力レベルをパソコンから書いて、音量が本当に変わるかを確かめます(実機テストの最初の確認)。
2. `measure --preset kami:1E --preset ... --di main [--scenes labelled]` — 同じ DI で鳴らして LUFS を測ります(シーンごとも可)。
3. `propose [--target -18]` — 補正案(既定の目標は測ったプリセットの中央値)。
4. `apply [--method lane|gain_block|amp_level] [--apply]` — **sandbox のコピーにだけ**補正を書き込みます。元のプリセットは変えません。行の出力レベルが効かないときは `gain_block`(行の最後に Gain ブロックを足す)。`amp_level` はアンプの MASTER/VOLUME を動かすので音色も変わります。
5. `verify` — コピーを測り直し、目標から ±1 LU 以内かを確かめます。

### 4. ブラインド比較(ブラインド比較画面 / `qcsuite tournament`)

`from-presets --preset ... --di main`(プリセットを同じ DI で鳴らす)か `new FILE...`(音声ファイル)で作ります。
候補は音量をそろえ、名前を伏せて 2 つずつ出ます。画面では A/B を同じ再生位置で切り替えられます(キー 1 / 2 / 0)。
投票から Elo で順位を付け、最後に `reveal` で名前を表示します。素通しや鳴らないブロックを含む候補は、理由付きで外されます。

### 5. 言葉で操作するパネル(言葉で操作画面 / `qcsuite panel`)

- `show` — グリッドとノブ(読み取りのみ)。パソコンから効かないブロックには印が付きます。
- `say "少し暗く"`(プレビュー)→ `say "少し暗く" --apply` — 言葉を操作(パッチ)に変えて送ります。**保存はしません**。音の良し悪しは判断しません。
  例: 暗く / 明るく / 歪みを増やして / ゲインを 6 に / 低音を減らして / 中域を出して / ブースト追加 / リバーブを外して / ディレイを足して / BRIGHT をオンに。
  わからない部分があれば、何も送らずに理由を表示します。スイッチは選択肢の名前でしか動かしません。
- `--engine claude` — `ANTHROPIC_API_KEY` があれば Claude API でも変換できます(グリッドの内容が API に送られます)。結果は同じ検査を通り、送る前に表示されます。
- `undo` / `redo` / `history` — 取り消しとやり直し。
- `ab capture A|B` / `ab last` / `ab switch A|B` / `ab diff` — 2 つの状態を、呼び出し直しなしで行き来します(弾きながら耳で比べる)。
- `save 名前 [--apply]` — 今のグリッドを sandbox の空きスロットに保存する計画。

### 6. スイープ(スイープ画面 / `qcsuite sweep`)

「どのブロックの、どのモデルとノブを、どの範囲で」を JSON で書いて総当たりします(書式は `qcsuite/features/sweep.py` の先頭)。

```json
{"title": "2203 gain", "di": "main", "seconds": 12,
 "base": {"preset": {"folder": "kami", "slot": "1E"}},
 "cells": [{"row": 0, "col": 1, "params": {"gain": [0.2, 0.5, 0.8], "TREBLE": {"from": 0.3, "to": 0.7, "steps": 3}}}],
 "mode": "grid"}
```

- `preview SPEC` — 件数と見込み時間(実機では 1 通り約 16 秒)。ベースのプリセットを読み込むときは確認します。
- `run SPEC [--resume ジョブID]` — 各構成を鳴らし、素通しと鳴らないブロックを除外し、FLAC と JSONL に記録します。`reference` を書けば参考音源との距離で並べられます(重みは推測なので、聴く順番の目安です)。
- `results JOB [--sort ...]` / `to-tournament JOB --top 8` — 結果をブラインド比較に送れます。
- `sensitivity --row 1 --col 2 --param gain` — 1 つのノブを動かしたときの測定値の変化(効き方のグラフ)。

ノブの名前はブロックごとに違います(GAIN / DRIVE / VOLUME / INPUT GAIN / GAIN 1・2 など)。`gain` `bass` `mid` `treble` `presence` `depth` `level` と書けば、ブロックに合わせて読み替えます。

### 7. 資料(資料画面 / `qcsuite sheets`)

`html` / `pdf`(`--preset ...`、`--folder`、`--current`、`--from-backup`)で、チェーン図・ノブの表・シーン早見表(シーンごとのバイパスと値の違い)を印刷できる形にします。
`pack` は共有パック(JSON + 資料 + 音 + README の zip)。受け取った人は本体に直接は取り込めないので、README に手で組み直す方法を書いています。

### 8. セットリストと MIDI(セットリスト画面 / `qcsuite setlist`)

1. 曲のリスト(1 行 1 曲。`曲名 | プリセット名` や `曲名 | kami:1E` も可)を用意して `plan songs.txt --name gig [--folder Live] [--start 1A]`。
   同じ名前が複数のフォルダにあるときは推測せずに候補を出すので、`choose gig 番号 kami:1E` で決めます。
2. 配置は sandbox(または許可したフォルダ)へのコピーの計画です。`--apply` で実行し、`refresh gig` で保存された名前を記録します。コピーせずに今の場所のまま使う `--map` もあります。
3. `midi gig --format csv|md|json` — CC#0(バンク)・CC#32(セットリスト番号)・PC(0 始まりと 1 始まりの両方)の表。
   カスタムのフォルダの CC#32 番号は分かっていないので、実機で確かめて `midi-map --set Live=5` で設定します。

### 9. 一括リアンプ(一括リアンプ画面 / `qcsuite batch`)

`run --di main --preset kami:1E --preset ... [--format wav|flac]` で、DI × プリセットを順に鳴らし、Logic Pro 用のステムを書き出します。
48 kHz / 24 bit、すべて同じ長さで DI の先頭にそろえてあります(往復の遅延約 312 ms は毎回測って取り除きます)。
`00_<DI>__DI.wav` と `NN_<DI>__<プリセット>.wav`、一覧の `stems.csv` ができます。除外されたものは `__EXCLUDED` が付きます。
`map JOB [--near プリセット名]` は「音の地図」(測定値が似ているプリセット)。似ているのは数値で、良し悪しではありません。

### A. 健康診断とライブ前チェック(健康診断画面 / `qcsuite health`)

`scan --from-backup`(読み込みなし)/ `--folder 名前` / `--current` で、次のようなものを「要対応 / 注意 / 情報」に分けて出します。
入力が USB のまま保存されたプリセット(ギターを挿しても鳴らない)、出力に届かない行、ミュートされた行、未所有プラグイン、キャプチャー、重い構成、名前のないシーン、重複。
`gig セットリスト名` はライブ前のチェックリストです(全曲のプリセットがあるか、要対応がないか、音量差が ±1 LU 以内か、入力が Input 1 か)。

### D. 編集ウォッチャーと録音(編集ウォッチャー画面 / `qcsuite watch`)

- `start [--interval 2]` — 本体で回したノブを、読み取りだけで記録します(本体の音は途切れません)。端末では `n メモ`、`t 名前 秒`(録音)、`q`(終了)が使えます。
- `list` / `show 記録ID` / `export 記録ID --md|--patch` / `apply 記録ID` / `to-sandbox 記録ID --source kami:1E [--apply]` — 記録をまとめたり、パッチとして再現したり、sandbox に保存したりします。`--source` を付けると元のプリセットに変更を当てて保存するので、キャプチャーやマイクも鳴ったままです。
- `take 名前 --seconds 20` — 弾いた音を、DI(USB 1)とウェット(USB 3/4)の 1 本のストリームで同時に録り、DI ライブラリに登録して、そのときのプリセットとノブに紐づけます。行の入力が INPUT_1 のときだけ録ります(リアンプの直後は USB_5 のままのことがあるので、プリセットを呼び直してください)。

### 研究ツール(`qcsuite research`)

`sniff`(本体から届くメッセージを記録するだけ、何も送らない)、`diff`、`dependencies`、`read-state`、`outbound`、`measure`。
使い方は [研究メモ](research-host-limits.md) の実験手順にあります。

### 自己テスト(`qcsuite selftest`)

`qcsuite --mock selftest` はモックで全機能をひと通り動かします。`qcsuite selftest --hardware` は [実機テスト手順書](hardware-test-plan.md) を対話式に実行します。

### 従来のトーン合わせ(`qctone`)

参照音源に近づけるループです(Claude Code が回す)。使い方は README と CLAUDE.md にあります。
`qctone save` の保存先は、安全ルールに合わせて sandbox の空きスロットに変えました(自分のフォルダへの保存と上書きはしません)。

## 6. データの置き場所

すべて `~/.qctone/`(モックは `~/.qctone/mock/`、`QCSUITE_HOME` で変更可)。

| フォルダ | 中身 |
|---|---|
| `backup/` | バックアップ(正規化した JSON、git リポジトリ) |
| `plans/<ID>/` | 計画、実行前のバックアップ、実行記録 `report.jsonl` |
| `jobs/<ID>/` | 長い処理の記録(`results.jsonl`、再開用) |
| `di/` | DI ライブラリ |
| `sweeps/`、`batch/`、`tournaments/`、`leveler/` | レンダリングした音と結果 |
| `watch/`、`takes/` | 編集の記録と録音 |
| `sheets/`、`selftest/` | 資料、自己テストのレポート |
| `device.lock` | 本体のロック |
| `inert_plugins.json` | 未所有プラグインの一覧(書き換え可。無ければ既定の 10 種) |

## 7. 困ったとき

| 症状 | 対処 |
|---|---|
| 「Quad Cortex に USB で接続できません」 | Cortex Control を終了、USB を挿し直す、本体の起動を待つ。hidapi と `DYLD_LIBRARY_PATH` を確認 |
| 「別のプロセスが使用中です」 | 表示されたプロセス(qcsuite serve や qctone)を終了する |
| リアンプが無音 | 行の入力が USB 5 か(`qcsuite grid`)、`qctone doctor --reamp-test`、`QCTONE_REAMP_OUT` / `QCTONE_WET_IN` |
| 一覧やカタログがタイムアウト | 再試行が自動で入ります。続くときは本体を再起動 |
| 保存したのに名前が違う | 本体の重複回避(20 文字 + `_N`)。計画の記録に実際の名前が残ります |
| 画面に「開き直してください」 | ブラウザで `http://127.0.0.1:8765/` を開き直す(セッションの確認) |
| ジョブが止まった | `qcsuite jobs ID` で状態とエラーを見て、各コマンドの `--resume ID` で再開 |

## 8. 実機で確認できていないこと

ここに挙げたものは、実機テスト手順書で確かめるまで推測です。

- Lane Output VOLUME をパソコンから書いて音量が変わるか(音量そろえの前提)
- カスタムセットリストの上限(12 個と想定、My Presets を含むか)
- 素通し判定のしきい値(flatness < 0.08、形の差 ≤ 1.0 dB、クレスト差 ≤ 1.5 dB)
- 名前の重複回避の細部(同じスロットに同じ名前で保存したとき、など)
- MIDI の CC#32 のフォルダ番号と、PC が 0 始まりか 1 始まりか
- 保存して呼び直すと、パソコンから書いたキャプチャーやマイクが鳴るか(研究メモの E2)
- IR ローダーの `set_ir` が鳴るか
- Claude API での変換(API に対しては実行していません)
