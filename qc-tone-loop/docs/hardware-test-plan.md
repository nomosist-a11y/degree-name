# qcsuite 実機テスト手順書

この手順書は、Mac 上のローカル Claude Code に「これを順番に実行して」と渡すためのものです。
qcsuite はクラウド環境で、モック(FakeQuadCortex)だけを相手に作りました。**どの機能も、実機では一度も動かしていません**。
この手順で、初めて実機での挙動を確かめます。

## 渡し方

1. Mac で `qc-tone-loop` フォルダを開き、`claude` を起動します。
2. 次のように頼みます。

   > `docs/hardware-test-plan.md` を最初から順番に実行して。各手順の「期待する結果」と比べて、違ったら「失敗したときの対処」に従って。書き込みは sandbox だけ。分からないことは止まって私に聞いて。結果は手順書の最後の「記録のしかた」に沿ってまとめて。

3. まず対話式のランナー `qcsuite selftest --hardware` を実行します。手順 0〜5 の主な項目(24 手順)を順に実行し、
   結果を `~/.qctone/selftest/report-日時.md` に書きます。途中で止めたときは、`--start-at 番号` で続きから再開できます。
   ランナーに入っていない手順(この手順書で「手で」と書いたもの)は、そのあとで実行します。

## 安全の約束(Claude Code はこれを守る)

- 書き込み先は `_qcsuite_sandbox` だけにする。My Presets / Favorite / kami / Cory Wong / Factory Library には書き込まない
  (qcsuite も拒否します)。Live / Artist / Song / Workshop にも、この手順書では書き込まない。
- 削除・上書き・移動は、qcsuite の計画(plan)を通してだけ行う。`--apply` を付ける前に、計画をユーザーに読み上げる。
- 本体全体の設定(I/O、Global EQ、マスターボリューム、ファームウェア)には触らない。キャプチャーのダイアログには応答しない。
- リアンプの前には毎回、モニターとヘッドホンの音量を下げてもらう。
- プリセットの読み込み(バックアップ、音量そろえ、一括リアンプなど)は、本体の音を切り替え、保存していない編集を消す。実行前にそう伝える。
- 失敗しても自分で直そうとしない。記録して止まり、ユーザーに聞く。

## 準備

| 項目 | 内容 |
|---|---|
| 環境 | macOS、Python 3.11 以上、`brew install hidapi`、`export DYLD_LIBRARY_PATH=/opt/homebrew/lib` |
| インストール | `cd qc-tone-loop && python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[device,dev]'` |
| モックの確認 | `pytest -q` がすべて通ること、`qcsuite --mock selftest` が 24 手順すべて PASS で終わること |
| 本体 | CorOS 4.0.1(firmware d14e)。**Cortex Control を終了しておく**(USB の制御を独占するため) |
| 音 | モニターとヘッドホンの音量を下げる。リアンプでは本体から音が出る |
| DI | 12 秒前後の DI を `qcsuite di import 自分のDI.wav --name main` で登録しておく(合成 DI でも動くが、聴いて判断するときは本物を使う) |
| 時間 | ランナーだけで 20〜40 分(バックアップ 1 フォルダ分の読み込みを含む)。手での手順を含めると 2〜3 時間 |

---

## 手順 0: 接続と基準の記録(読み取りのみ)

| # | 実行 | 期待する結果 | 失敗したときの対処 |
|---|---|---|---|
| 0-1 | `qctone doctor` | USB control OK、firmware d14e、USB audio OK | Cortex Control を終了し、USB を挿し直す。`hid` のエラーなら hidapi と `DYLD_LIBRARY_PATH` を確認 |
| 0-2 | `qcsuite status` | フォルダ数が My Presets 51 / Favorite 21 / kami 20 / Cory Wong 15 / Live・Artist・Song・Workshop 0、Factory Library 256 | 数が違えば本体の画面と見比べる。違っていても、最後の照合は開始時の実際の数を基準にする |

## 手順 1: 最初の確認(これが通るまで、ほかの手順に進まない)

### 1-1. カスタムセットリストの上限

| 項目 | 内容 |
|---|---|
| 背景 | Neural DSP のサポート情報では「カスタムのセットリストは最大 12 個」。My Presets を数に含むかどうかは書かれていない。qcsuite は小さいほうの読み方(My Presets を含めて 12 個)で止まるようにしてある |
| 実行 | `qcsuite library usage` で今の数を見る(8 個のはず)。本体のマニュアルか画面で、上限を確認する |
| 期待 | フォルダ 8 個 + sandbox 1 個 = 9 個が、上限以内 |
| 余裕がないとき | **sandbox を作らずに止まる**(qcsuite も止まります)。手順 3 と 4 の書き込みは行わない |
| 上限が 12 以外とわかったら | `export QCSUITE_SETLIST_LIMIT=実際の数`(My Presets を含む総数)。ランナーは手順の中で入力を求めます |

### 1-2. Lane Output VOLUME をパソコンから書いて、音量が本当に変わるか

| 項目 | 内容 |
|---|---|
| 背景 | キャプチャーやマイクのように「書けるのに効かない」可能性がある。音量そろえ(機能 3)は、この値で補正する |
| 実行 | `qcsuite level check-lane --preset "Factory Library:1A"`(モニターを下げる)。工場出荷のプリセットを読み込み、行の入力を USB 5 にして、出力レベルを −6 dB 動かす前後でリアンプする。保存はしない |
| 期待 | LUFS が約 −6 dB 動く(±1.5 dB)。判定は「効く」。終わると値は元に戻る |
| 効かないとき | 事実として記録する。音量そろえは `--method gain_block`(行の最後に Utility の Gain ブロックを足して補正する)に切り替える。モック(`FakeConfig.lane_volume_inert`)にも反映する |

## 手順 2: 読み取り系(本体のプリセットは変更しない)

| # | 実行 | 期待する結果 | 失敗したときの対処 |
|---|---|---|---|
| 2-1 | `qcsuite library list`、`qcsuite library dupes` | 件数が 0-2 と同じ。本体の音は変わらない。同じ名前が複数のフォルダにある組が一覧に出る(実機では 20 組の想定) | 一覧の取得がタイムアウトしたら再実行(遅れて届くことがある) |
| 2-2 | `qcsuite library captures "Corn Vixen"`、`qcsuite library irs`、`qcsuite library catalog --help` を見てから catalog の検索 | Corn Vixen 1〜4 が出る。キャプチャーは約 2,078 件 | 0 件なら一覧の取得の遅れなので再実行 |
| 2-3 | `qcsuite library favorites` | 本体のハートの画面と一致する。書き込みはしない | 違う項目の名前とフォルダを記録する |
| 2-4 | `qcsuite grid` を 2 回 | 2 回とも同じ内容。**本体の音は途切れない**(読み取りに副作用がない) | 音が途切れたら記録して止める |
| 2-5 | `qcsuite backup run --folders "Cory Wong"`(音量を下げる。15 件を順に読み込むので音が切り替わる) | 15 件が JSON になり、git に 1 コミット。1 件 10〜25 秒。`qcsuite backup show "Cory Wong/1A"` が本体の表示と一致 | 途中で止まったら `--resume ジョブID`。失敗した件は `qcsuite jobs ジョブID` |
| 2-6 | 本体で 1A のノブを 1 つ変えて保存 → もう一度 2-5 → `qcsuite backup diff` | そのノブだけが差分に出る | 余計な差分が出たら、その項目を記録(正規化の調整が必要) |
| 2-7 | `qcsuite health scan --from-backup`、`qcsuite health report` | 読み込みなしで診断が出る。行の入力が USB 5 のまま保存されたプリセットがあれば「要対応」 | 誤判定はプリセット名と理由を記録 |
| 2-8 | `qcsuite sheets pdf --from-backup --out ~/Desktop/qcsuite-sheets` と `sheets html` | PDF と HTML ができ、日本語(ヒラギノ)とチェーン図が読める | 日本語が □ になるならフォント名を記録 |
| 2-9(手で) | `qcsuite research dependencies "My Presets"`、`qcsuite research read-state ModuleStats` | 読み取りだけで結果が出る(研究メモの E0・E5) | エラーは記録して次へ |

## 手順 3: sandbox への書き込み(手順 1 が通ったときだけ)

| # | 実行 | 期待する結果 | 失敗したときの対処 |
|---|---|---|---|
| 3-1 | `qcsuite library copy "Cory Wong/1A"`(dry-run)→ 計画を読んでから `--apply` | `_qcsuite_sandbox` が 0/256 で作られ、コピーが 1A に入る。元のプリセットは変わらない | 確認がタイムアウトしても、本体の画面で sandbox を確認する |
| 3-2 | 同じものをもう一度コピー | 2 回目は本体が名前を 20 文字以内に切り詰めて `_1` を付ける | 規則が違えば記録(モックの規則を実機に合わせて直す) |
| 3-3 | `qcsuite library copy kami/1A --to Favorite --apply` | **何も送らずに拒否される** | 拒否されなければ直ちに止めて報告 |
| 3-4 | `qcsuite backup restore "Cory Wong/1B" --apply` | sandbox に組み立て直したプリセットが入り、数値のノブが一致する。キャプチャーとマイクは鳴らない(既知の制約、計画に警告が出る) | 違ったノブを記録 |
| 3-5 | `qcsuite panel show` → `qcsuite panel say "少し暗く"`(--apply なし)→ `--apply` → `qcsuite panel undo` | 表示が本体の画面と一致。--apply なしでは何も変わらない。適用で TREBLE/PRESENCE が半段下がって暗くなり、undo で戻る | 画面は変わるのに音が変わらなければ、そのブロック名を記録 |
| 3-6 | `qcsuite panel ab last`、`ab switch A`、`ab switch B` | 呼び出し直しなしで A と B を行き来でき、違いが聴こえる | 切り替えで音が途切れるなら記録 |
| 3-7 | スイッチのあるアンプで `qcsuite panel say "BRIGHT をオンに" --apply` | 本体の表示が On になる | 違う選択肢になったら、本体の選択肢の名前を記録して止める |
| 3-8 | アンプの前に空きセルがあるプリセットで `panel say "ブースト追加" --apply` | ドライブが有効(バイパスでない)で置かれる | バイパスで置かれたら、セルのシーン設定を引き継いだもの。本体で有効にして記録 |
| 3-9 | `qcsuite watch start --interval 2` → 本体で GAIN を回す → バイパス → シーン切り替え → 別のプリセット → キャビのマイクを変える → Ctrl-C | GAIN の変化が 1 行(値が本体の表示と一致)、バイパス、シーン X→Y、新しい区間、※印付きのマイク変更が記録される。音は途切れない | 何も出なければ `--interval 5`。シーンが出なければ `active_scene` の応答の問題として記録 |
| 3-10 | `qcsuite watch to-sandbox latest --source <元のフォルダ:スロット>`(dry-run)→ `--apply` → 本体で sandbox のプリセットを呼ぶ | 記録した数値が本体の画面と一致する | 計画の `report.jsonl` と本体の値を記録 |
| 3-11 | `qcsuite setlist plan songs.txt --name test`(曲 3 つ、プリセットは自分のもの)→ `--apply` → `qcsuite setlist refresh test` → `qcsuite setlist midi test --format md` | sandbox の連続したスロットに入り、保存された名前(`_N` 付きを含む)が記録される | 計画が出力する `report.jsonl` と `qcsuite library list` を確認 |

## 手順 4: リアンプ系(本体から音が出る。毎回モニターを下げる)

| # | 実行 | 期待する結果 | 失敗したときの対処 |
|---|---|---|---|
| 4-1 | `qctone doctor --reamp-test` | テスト音が戻り、往復の遅延が約 312 ms | 無音なら USB のチャンネル割り当て(`QCTONE_REAMP_OUT` / `QCTONE_WET_IN`)を確認 |
| 4-2 | ランナーの「往復遅延と素通し検出」 | 遅延が約 312 ms。未所有プラグイン(Plini)だけの行は素通しと判定され、flatness < 0.08、DI との形の差が小さい | 遅延が大きく違えば `QCSUITE_LATENCY_MS` で固定。**素通しの判定値(flatness < 0.08、形の差 ≤ 1.0 dB、クレスト差 ≤ 1.5 dB)は推測値**なので、実機の数値を記録して調整する |
| 4-3 | `qcsuite level measure --preset "Cory Wong:1A" --preset "Cory Wong:1B" --preset "kami:1A" --di main` → `level propose` | プリセットごとに LUFS が出て、耳の印象と矛盾しない | 除外されたプリセットは、本当に未所有プラグインやキャプチャーがあるか確認 |
| 4-4 | `level apply`(dry-run)→ `--apply` → `level verify` | コピーは sandbox にだけ入り、元は変わらない(`backup diff` で確認)。各コピーが目標から ±1 LU 以内 | 外れたら `--method gain_block` で再試行。それでも外れたら残差を記録 |
| 4-5 | `qcsuite tournament from-presets --preset … ×3〜4 --di main` → Web(`qcsuite serve`)のブラインド比較で投票 | 音量がそろって聴こえ、最後まで名前が出ない。A/B の切り替えで再生位置が保たれる | 音量差を感じたら各候補の LUFS を記録 |
| 4-6 | `qcsuite sweep preview spec.json`(2〜4 構成)→ `sweep run spec.json` | preview は音を出さずに件数と見込み時間を出す。1 構成あたり約 16 秒(12 秒の DI)。ゲインを上げると flatness が上がりクレストが下がる | 無音なら 4-1 へ。きれいな音が素通し扱いなら `shape_diff_db` と `flatness` を記録 |
| 4-7 | スイープ後に `qcsuite grid` | ベースの状態(入力は USB_5、保存はしていない)。本体でプリセットを呼び直すと元に戻る | 保存されていたら直ちに報告 |
| 4-8 | 「Plini Lead」をスイープするスペック | すべての構成が理由付きで除外され、音は DI と同じ | 除外されなければ素通し判定の数値を記録 |
| 4-9 | `qcsuite batch run --di main --preset "Cory Wong:1A" --preset "kami:1A"` → DI のフォルダを Logic に 1 小節目からドロップ | ステムのアタックが DI のステムとそろう。`stems.csv` の遅延が約 312 ms で、ばらつきの警告がない | ずれていれば `stems.csv` の遅延と、耳と目で見たずれを記録 |
| 4-10 | ステムの形式を確認 | 48 kHz / 24 bit、すべて同じ長さ。`stems.csv` が Numbers で開ける | |
| 4-11 | 中身が同じプリセット 2〜3 個を一括リアンプ → `qcsuite batch map ジョブID` | 互いに最も近い(距離ほぼ 0) | 離れていれば、そのプリセットの差分(`backup diff`)を確認 |
| 4-12 | 一括リアンプの途中で Ctrl-C → `--resume ジョブID` | 同じステムを二度レンダリングしない | |
| 4-13 | `qcsuite watch take take1 --seconds 10`(弾きながら) | DI にギターが入り、ウェットは本体の音のまま。ずれは数 ms(312 ms ではない) | ウェットが無音なら行の入力が INPUT_1 か確認。DI が無音なら `QCTONE_DRY_IN` |
| 4-14 | リアンプの直後(入力が USB_5 のまま)に `watch take` | INPUT_1 が必要というメッセージで止まり、録音しない | 録音してしまったら報告 |
| 4-15 | `qcsuite sheets pack --preset "kami:1A" --render-di main` | zip に `audio/*.flac` が入り、除外されたものは `manifest.json` に印が付く | |

## 手順 5: 研究の実験(任意。研究メモの順で)

`docs/research-host-limits.md` の実験 E0〜E7 を、読み取りだけのものから順に行います。どれも sandbox のコピーだけを使い、
保存しない操作から始めます。要点は次のとおりです。

1. E1(手での操作): `qcsuite research sniff --seconds 30` を 3 回 — 何もしないとき / 本体の画面でキャプチャーを切り替えるとき / マイクを変えるとき。何も送らない。
2. E1(パソコン側): sandbox のコピーで `set_capture` を行う前後を sniff し、`qcsuite research diff` で手での記録と比べる。
3. E3: パソコンからキャプチャーを変えたあと、シーン切り替え・バイパスの切り替え・ブロックの追加と削除のそれぞれで、`qcsuite research measure` で鳴り方が変わるかを測る。
4. E4: IR ローダーの `set_ir` と、同じ IR を手で選んだ場合を比べる。
5. E2: パソコンから書く → sandbox の空きスロットに保存 → 呼び出す → 測る。手で読み込んだ場合と比べる。キャプチャー・マイク・新しく置いたキャプチャーブロックで繰り返す。

## 手順 6: 後片付け

| # | 実行 | 期待する結果 | 失敗したときの対処 |
|---|---|---|---|
| 6-1 | ランナーの最後の手順(または `qcsuite plan` で sandbox の `delete_folder` を実行) | sandbox が消える | 消えなければ本体の画面で確認 |
| 6-2 | `qcsuite status` | フォルダと件数が開始時と完全に一致する(My Presets 51 / Favorite 21 / kami 20 / Cory Wong 15 / Live・Artist・Song・Workshop 0) | 一致しなければ差分を報告し、本体の画面で確認する(保護フォルダには qcsuite から書けないはず) |
| 6-3 | 本体で、ふだん使うプリセットを呼び直す | 普段どおりに鳴る(リアンプで USB 5 にした入力は保存していないので、呼び直せば戻る) | |

## 記録のしかた

- ランナーのレポート(`~/.qctone/selftest/report-日時.md`)と、手での手順の結果を 1 つの Markdown にまとめる。
- 各手順の結果は PASS / FAIL / SKIP と数値(LUFS、遅延、flatness、形の差、保存された名前など)で書く。
- モックと実機が違った点は、「モックの直し方の提案」として別に書く(例: 名前の重複回避の規則、Lane VOLUME が効くかどうか、
  素通しの判定値、セットリストの上限、MIDI の CC#32 の番号)。テストやモックを、実機の結果に合わせて勝手に書き換えない。
