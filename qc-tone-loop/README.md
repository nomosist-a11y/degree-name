# qc-tone-loop

目標の音を決めると、Quad Cortexの音作りから比較、修正までを繰り返すシステムです。Mac上のClaude Codeが次の流れを回します。

```
目標を決める(参照音源と言葉)
      │
      ▼
┌─► Quad Cortexのグリッドを編集する(USB経由、pyquadcortexを使用)
│     │
│     ▼
│   DIを再生してリアンプし、Quad CortexのUSB出力を録音する
│     │
│     ▼
│   特徴量の解析と比較(スコア、不一致の帯域、スペクトログラム画像)
│     │
│     ▼
│   Claudeが判定して次の修正パッチを作る ◄── あなたが聴いた感想
└─────┘ (納得するまで繰り返す)
      │
      ▼
   sandboxフォルダの空きスロットに保存する(明示的に指示したときだけ)
```

同じフォルダに、日々の操作の手間を減らす **qcsuite** も入っています(下の「qcsuite」を参照)。

## 必要なもの

- Mac、Quad Cortex(CorOS 4.x)、USBケーブル
- Python 3.11以上、`brew install hidapi`
- Claude Code

## セットアップ

```bash
cd qc-tone-loop
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[device,dev]'
export DYLD_LIBRARY_PATH=/opt/homebrew/lib   # hidapiを読み込むために必要
pytest                                        # 実機なしで動くテスト
```

## 使い方

1. **Cortex Controlを終了します**(USBの制御を独占するため、同時には使えません)。
2. 出発点にするプリセットを本体で読み込みます。
3. `qctone doctor --reamp-test` を実行します。FAILが出たら表示される修正方法に従ってください。テスト音が鳴るので、モニターの音量は下げておいてください。
4. このフォルダで `claude` を起動し、例えば次のように頼みます。

   > `~/Music/ref.wav` の0:30から15秒を目標にして。言葉では「タイトで中域が前に出た、ジャリつかない2000年代メタルコアのリフ」。DIはこれから録る。

Claude Codeは `CLAUDE.md` の手順に従って、`qctone init`、`record-di`、`step` の繰り返し、`feedback`、`save` を進めます。

手動で操作するときのコマンド一覧は `qctone -h` で表示できます。

| コマンド | 内容 |
|---|---|
| `qctone doctor --reamp-test` | 接続、ルーティング、音の往復を最初に確認する |
| `qctone init NAME --ref ref.wav --desc "..."` | 目標を登録する |
| `qctone record-di NAME` | Input 1のドライ信号をUSB経由で録音する |
| `qctone grid` | 現在のグリッドをJSONで表示する |
| `qctone catalog --search 2203` | ブロックのモデルとパラメータを調べる |
| `qctone apply '{"ops":[...]}'` | パッチを適用する(録音や解析はしない) |
| `qctone step NAME --patch p.json` | パッチ適用、リアンプ、解析を1回分まとめて行う |
| `qctone import NAME bounce.wav` | Logic Proで書き出した音を解析する |
| `qctone feedback NAME "..."` | 聴いた感想を記録する |
| `qctone restore NAME [--iter N]` | 以前の状態に戻す |
| `qctone save NAME --slot 30A --title "..."` | sandbox(`_qcsuite_sandbox`)の空きスロットに保存する。自分のフォルダへの保存と上書きはしない |

## qcsuite

練習・ライブ準備・録音・プリセット管理のための道具箱です。考え方は「**音の良し悪しの判断は人、手間のかかる操作・比較・整理・記録は機械**」。
ブラウザの画面(`qcsuite serve`、127.0.0.1だけで待ち受け)とコマンド(`qcsuite ...`)の両方から使えます。

> **状態:** すべての機能はモック(実機を真似た FakeQuadCortex)で開発・テストしたもので、**実機では未確認**です。
> 使い始める前に [docs/hardware-test-plan.md](docs/hardware-test-plan.md) を実行してください(`qcsuite selftest --hardware` が対話式に進めます)。

```bash
qcsuite --mock selftest      # 実機なしで全機能をひと通り動かす
qcsuite --mock serve         # 実機なしで画面を試す
qcsuite serve                # 実機(Cortex Controlは終了しておく)
```

| 機能 | コマンド | 内容 |
|---|---|---|
| プリセット管理 | `qcsuite library` | 全フォルダの一覧・検索・重複の検出、別フォルダへのコピーの計画、キャプチャー/IR/カタログの検索、お気に入りとFavoriteフォルダの比較(読むだけ) |
| バックアップ | `qcsuite backup` | 全プリセットを正規化したJSONにしてgitで履歴管理、差分表示、sandboxへの復元 |
| 音量そろえ | `qcsuite level` | 同じDIでLUFSを測り、補正案を作り、sandboxのコピーにだけ当てて測り直す |
| ブラインド比較 | `qcsuite tournament` | 音量をそろえ、名前を伏せて2つずつ比べ、Eloで順位を付ける |
| 言葉で操作 | `qcsuite panel` | 「少し暗く」などを操作に変えてプレビューしてから送る。取り消しとA/B切り替え |
| スイープ | `qcsuite sweep` | ノブやモデルの組み合わせを総当たりでレンダリングし、素通しを除外して記録 |
| 資料 | `qcsuite sheets` | チェーン図・ノブ表・シーン早見表のHTML/PDF、共有パック |
| セットリスト | `qcsuite setlist` | 曲順から配置の計画とMIDIのプログラムチェンジ表を作る |
| 一括リアンプ | `qcsuite batch` | DI×プリセットを鳴らし、Logic Pro用にそろえたステムを書き出す。音の地図 |
| 健康診断 | `qcsuite health` | 入力がUSBのまま、未所有プラグインなどの検出と、ライブ前のチェックリスト |
| 編集ウォッチャー | `qcsuite watch` | 本体で回したノブを記録し、DIとウェットの同時録りをその記録に紐づける |
| DIライブラリ | `qcsuite di` | リアンプに使うDIの登録・録音・切り出し |
| 研究 | `qcsuite research` | パソコンから効かない操作を調べるための読み取り専用の道具 |

安全のしくみ(要点):

- 書き込みの既定の行き先は、qcsuiteが作る `_qcsuite_sandbox` フォルダだけです。My Presets / Favorite / kami / Cory Wong / Factory Library はコードで拒否します。Live / Artist / Song / Workshop は `--allow-folder` で明示したときだけ書き込めます。
- 保存・コピー・削除・移動は計画(plan)として表示し、既定はdry-runです。破壊的な操作の前にはバックアップを取り、実行後に読み戻して確かめます。
- 本体へのアクセスは `~/.qctone/device.lock` で1プロセスに限ります(qctoneも同じロックを使います)。本体全体の設定(I/O、Global EQ、ファームウェア)は呼びません。
- リアンプの前には、毎回モニターの音量を下げるよう求めます。

使い方の詳細は [docs/user-guide.md](docs/user-guide.md)、Claude Codeが守るルールは `CLAUDE.md`、パソコンから効かない操作の調査は [docs/research-host-limits.md](docs/research-host-limits.md)、アイデアの一覧は [docs/ideas.md](docs/ideas.md) にあります。

## 注意

- **pyquadcortexは非公式ライブラリです。** Neural DSPとは無関係で、動作確認はCorOS 4.0.1のみです。CorOSを更新すると動かなくなる可能性があります。
- **USBの音声チャンネルの初期設定**は、PCから本体への出力が5(グリッドのUSB 5入力)、本体からPCへの録音が3/4(処理後の音)と1(ドライDI)です。本体の設定と違う場合は、環境変数 `QCTONE_REAMP_OUT=5`、`QCTONE_WET_IN=3,4`、`QCTONE_DRY_IN=1` で変更できます。`qctone doctor --reamp-test` を実行すると、音が戻ってくるかを確かめられます。
- **参照音源はギターだけのものが理想です。** フルミックスの場合、低域と超高域の比較は参考程度にしてください。
- スコアは周波数バランスの近さだけを測る指標です。最後の判断は必ず耳で行ってください。
