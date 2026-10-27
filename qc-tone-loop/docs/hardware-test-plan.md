# qcsuite 実機テスト手順書

この手順書は、Mac 上のローカル Claude Code に「これを順番に実行して」と渡すためのものです。
qcsuite はクラウド環境でモック(FakeQuadCortex)だけを相手に作られ、**どの機能も実機では一度も動かしていません**。
ここに書いた手順で、初めて実機の挙動を確かめます。

## 渡し方

1. Mac で `qc-tone-loop` フォルダを開き、`claude` を起動します。
2. 次のように頼みます。

   > `docs/hardware-test-plan.md` を最初から順番に実行して。各手順の「期待する結果」と比べて、違ったら「失敗したときの対処」に従って。書き込みは sandbox だけ。分からないことは止まって私に聞いて。

3. 対話式のランナーを使う場合は、`qcsuite selftest --hardware` を実行します(共通部分の手順を順に実行し、
   `~/.qctone/selftest/report-日時.md` に結果を書きます)。途中で止めたら `--start-at 番号` で続きから再開できます。
   機能ごとの手順(この手順書の「4」以降)は、ランナーに入っていないものを手で実行します。

## 安全の約束(Claude Code はこれを守る)

- 書き込み先は `_qcsuite_sandbox` だけ。My Presets / Favorite / kami / Cory Wong / Factory Library には書き込まない
  (qcsuite も拒否します)。Live / Artist / Song / Workshop には、この手順書では書き込まない。
- 破壊的な操作(削除・上書き・移動)は qcsuite の計画(plan)経由だけ。`--apply` を付ける前に計画を読み上げる。
- 本体全体の設定(I/O、Global EQ、ファームウェア、マスターボリューム)は触らない。`show_capture_dialog` に応答しない。
- リアンプの前に、毎回モニターとヘッドホンの音量を下げてもらう。
- `read_preset`(プリセットの読み込み)は本体の音を切り替え、保存していない編集を消す。実行前にそう伝える。
- 失敗しても自分で直そうとせず、記録して止まり、ユーザーに聞く。

## 準備

| 項目 | 内容 |
|---|---|
| 環境 | macOS、Python 3.11 以上、`brew install hidapi`、`export DYLD_LIBRARY_PATH=/opt/homebrew/lib` |
| インストール | `cd qc-tone-loop && python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[device,dev]'` |
| モックの確認 | `pytest -q` がすべて通ること、`qcsuite --mock selftest` が PASS で終わること |
| 本体 | CorOS 4.0.1(firmware d14e)。**Cortex Control を終了**(USB の制御を独占するため) |
| 音 | モニター・ヘッドホンの音量を下げる。リアンプでは本体から音が出る |
| DI | 12 秒前後の DI を `qcsuite di import 自分のDI.wav --name main` で登録しておく(合成 DI でも動くが、判断には本物を使う) |

<!-- 手順の詳細は、各機能の実装が終わってからこの下に追記する -->
