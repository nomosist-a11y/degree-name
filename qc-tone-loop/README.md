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
   User Presetに保存する(明示的に指示したときだけ)
```

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
| `qctone step NAME --patch p.json` | パッチ適用、リアンプ、解析を1回分まとめて行う |
| `qctone import NAME bounce.wav` | Logic Proで書き出した音を解析する |
| `qctone feedback NAME "..."` | 聴いた感想を記録する |
| `qctone restore NAME [--iter N]` | 以前の状態に戻す |
| `qctone save NAME --slot 30A --title "..."` | User Presetに保存する(上書き) |

## 注意

- **pyquadcortexは非公式ライブラリです。** Neural DSPとは無関係で、動作確認はCorOS 4.0.1のみです。CorOSを更新すると動かなくなる可能性があります。
- **USBの音声チャンネルの初期設定**は、PCから本体への出力が5(グリッドのUSB 5入力)、本体からPCへの録音が3/4(処理後の音)と1(ドライDI)です。本体の設定と違う場合は、環境変数 `QCTONE_REAMP_OUT=5`、`QCTONE_WET_IN=3,4`、`QCTONE_DRY_IN=1` で変更できます。`qctone doctor --reamp-test` を実行すると、音が戻ってくるかを確かめられます。
- **参照音源はギターだけのものが理想です。** フルミックスの場合、低域と超高域の比較は参考程度にしてください。
- スコアは周波数バランスの近さだけを測る指標です。最後の判断は必ず耳で行ってください。
