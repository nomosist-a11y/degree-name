# 研究メモ: パソコンから書いても音が変わらないものの原因と、安全な実験手順

対象: CorOS 4.0.1(firmware d14e)、pyquadcortex 0.40.0。2026-10-27 作成。
この文書の実験は、**あとでユーザーの Mac 上で行う前提**で書いています。この環境(クラウド)では実機に触れていないので、ここに書いた仮説はどれも未確認です。

## 0. 記号の意味

根拠の強さを混ぜないように、各項目に次の印を付けます。

| 印 | 意味 |
|---|---|
| **[実測]** | ユーザーの実機で確認済みの事実(前回のセッション) |
| **[記載]** | pyquadcortex の docstring に「実機で確認した」と書いてあること(ここでは再確認していない) |
| **[定義]** | pyquadcortex のソースコードと protobuf 定義から読み取れること(本体の挙動ではなく、メッセージの形) |
| **[検索]** | Web 検索で見つけた情報(出典つき。本文を読めなかったものはそう書く) |
| **[仮説]** | ここでの推測。実験で確かめるまで事実として扱わない |

## 1. 現象のまとめ [実測]

パソコンから次のものを書き込むと、**書き込みは成功し、読み戻しても正しく見えるのに、音は変わらない**。本体の画面で同じものを手で読み込めば鳴る。

1. Neural Capture ブロックの `file_name`(`set_capture`)と、そのブロックの GAIN などのパラメーター
2. キャビの `ir selector`(マイクの選択)などの文字列パラメーター
3. 未所有のプラグインのブロック(Plini, Gojira, Nolly, Petrucci, John Mayer, Rabea, Misha, Tim Henson, Fortin, Soldano SLO-100 X)。置けるし値も読み戻せるが、音は素通し(DI のまま)。Cory Wong の The Amp Snob と Dumbbell ODS は鳴った

あわせて、キャプチャーのモデル(14000 / 14001)の `set_block` は確認待ちがタイムアウトして `BlockRefused` になるが、実際には置かれている [実測]。

## 2. 調べたもの

- pyquadcortex 0.40.0 のソース: `client.py`(`set_param`、`set_capture`、`set_ir`、`set_block`、`show_capture_dialog`、`read_preset`、`save_current_preset`、接続時の購読リスト `_SUBSCRIBE_TYPES`)、`transport.py`(受信、振り分け、collector)、`registry.py`、`framing.py`
- protobuf 定義(`ProductionAutomation_pb2`、`Preset_pb2`)。メッセージの種類は `CortexMessageType` に 72 個(`Undefined` と `NumberOfMessageTypes` を含む)。関係しそうなものの項目を 8 章の表にまとめた
- pyquadcortex の `docs/`(protocol.md、capture.md、troubleshooting.md など)は、**PyPI のパッケージには入っていない**(METADATA に GitHub へのリンクがあるだけ)。このセッションは別のリポジトリを読まない決まりなので、**読めていない**。手元で読むと、ここの仮説を補強または否定できる可能性がある
- Web 検索(Neural DSP のリリースノート、フォーラム)。neuraldsp.com と unity.neuraldsp.com の本文は、この環境からは取得できなかった(検索結果の要約だけ)

## 3. わかったこと

### 3.1 書き込みの経路は、数値も文字列も同じ [定義]

- `set_param` は、行と列を指定した疎な `Grid` UPDATE を 1 通送るだけ。文字列のときは `param_values[].string_value`、数値のときは `float_value` に入るだけで、それ以外の違いはない
- `set_capture` は専用の命令ではなく、`set_block`(モデルを置く)と、パラメーター 5 番(`file_name` = 64 文字のハッシュ + 表示名)への文字列の `set_param` を組み合わせたもの。`set_ir` も同じで、IR PATH(2 / 10 番)と IR NAME(22 / 23 番)への文字列の書き込み
- 書き込みに対して本体は基本的に返事をしない(USB の書き込みは毎回 STALL で終わるが、実際には届いている)[記載]。返事を待つのは `set_block` だけで、置いたセルを示す `Grid` のエコーを 5 秒待つ [記載]
- `GridMessage.update_type` という列挙はあるが、値は `METER_UPDATE` しか定義されていない [定義]。「資産を読み込み直せ」のような種類を指定する手段は、この定義には見当たらない

### 3.2 本体はホストの文字列の書き込みに「何か」反応している [記載]

pyquadcortex の `set_capture` の説明に、「キャプチャーを読み込むと、そのブロックのほかのパラメーターが、キャプチャー自身の既定値に黙ってリセットされる(読み込み前に書いた VOLUME 0.56 が 0.5 に戻った)」とある。つまり、**ホストからの `file_name` の書き込みで、本体のデータ側はパラメーターのリセットまで行っている**。それでも音が変わらない [実測] のは、「データは更新されたが、音を作るエンジンには読み込み直しが伝わっていない」と考えると説明がつく(→ 仮説 H1)。

### 3.3 プリセットの呼び出しはエンジンを作り直す [記載]

`read_preset` の説明に「プリセットの読み込みはエンジンを読み込み直す(だから毎回音が途切れる)」とある。呼び出しのときには、保存されたプリセット全体からエンジンが組み立て直される、という読みが自然(→ 仮説 H2 の根拠)。

### 3.4 関係しそうなメッセージ [定義]

| メッセージ | 主な項目 | 関係しそうな点 |
|---|---|---|
| `NeuralCapture` | `try_to_show_dialog`、`show_dialog`、`state`、`model_ab` など | キャプチャーの「作成」の流れ。本体はダイアログの表示をホストに問い合わせ、ホストが答えないと止まる [記載]。既存のキャプチャーの読み込みとは別物と思われる |
| `NeuralCapture2` | `job_id`、`cabsim`、`open_dialog`、`start_from_scratch` | V2 キャプチャーの作成の流れ |
| `DefaultParameters` | `row`、`column`、`all_defaults[]{id, model}` | セル単位の既定値。キャプチャー読み込み時のパラメーターのリセットと関係があるかもしれない |
| `ModuleStats` | `stats[]`、`core_stats[]`、`valid_id[]{id, valid}` | **モデル ID ごとの「有効」フラグ**がある。未所有プラグインのモデルが `valid: false` かどうかは読み取りだけで確かめられる |
| `License` | `licenses[]{plugin_id, id, name, license_type, expiration_date}`、`supported_plugin_ids`、`new_plugin_ids`、`is_enabled` | プラグインのライセンス。ただし本体からの `License` は protobuf ではない生の本文で届くことがあり、pyquadcortex は読めずに捨てている(`transport.py` のコメント)|
| `CompilerInhibitedModules` | `global_gate`、`global_eq` | 名前に反して、プラグインとは無関係。DSP の余裕がないときに本体が Global EQ / Gate を止めた通知 [記載] |
| `NewModels`、`PinnedModels` | `models[]`、`captures[]` | 新しく使えるようになったモデル、ピン留め |
| `Confirmation` | `title`、`msg`、`actions[]`、`response` | 本体からホストへの確認の問い合わせ。返事がないと処理が保留される種類のものがあるかもしれない |
| `GenericError` | `error_code` | 本体からのエラー通知 |
| `File` | `type`(0 = プリセット、1 = IR、2 = キャプチャー [記載])、`preset_payload`、`ir_payload` | ファイル単位の操作。プリセットを丸ごと送る道(`preset_payload`)は定義上ある |
| `ProductData`(一覧の各項目)| `product_dependencies[]`、`factory_dependencies[]`(`hash`、`missing`、`visible`、`published`、`name`、`product_type`)| **プリセットごとの依存関係の記録**。キャプチャーやプラグインへの依存がここに載り、読み込み時の手がかりになっている可能性 |
| `BinaryPreset` | `recompile_code_1/2`、`layout_code_1/2`、`model_update_notifications[]{row, column}`、`Model.notify_flag` | 意味は不明。DSP の組み立て直し(コンパイル)を判断する値かもしれない |

### 3.5 プラグインは、有効なライセンスがあるときだけ読み込まれる [検索]

Neural DSP の CorOS 3.0.0 のリリースノートに、対応する X プラグインのデバイスは「有効なライセンスがあれば Quad Cortex で読み込める」とあり、CorOS 3.2.0 のノートにも「ライセンスを持っていれば使える」とある(下の出典)。未所有のプラグインが素通しになる [実測] のは、**ライセンスによる製品の仕様**と考えるのが自然。

### 3.6 公式エディターでも、マイクの変更が本体で開くまで反映されなかったという報告がある [検索]

Neural DSP のフォーラムに「Changing MIC/IR in Cortex Control does not work」という題のスレッドがあり、検索結果の要約では、CorOS 2.2.2 のころ、Cortex Control でマイクや IR を変えても何も起きず、**本体でキャブのブロックを開くと反映された**、という内容だった(**本文は取得できず、要約のみ**。現在の版で直っているかは不明)。事実 1 とよく似ている。また別のフォーラムの返信では、キャブのブロックは「IR の間をモーフィングする高機能な IR ローダー」のように説明されていた(要約のみ)。

## 4. 仮説(確からしい順)

### H1: 資産の読み込みは、本体の画面操作か、プリセット全体の読み込みのときにしか起きない(キャプチャーとマイク)[仮説]

キャプチャーのモデルデータやキャブの IR の読み込みは、本体の UI の操作(ブロックを開く、リストから選ぶ)や、プリセットの呼び出し・組み立て直しの経路でだけ行われる。ホストからの疎な `Grid` UPDATE は保存データを書き換え(パラメーターのリセットまで起きる)、読み戻しにも反映されるが、DSP エンジンに「読み込み直せ」とは伝わらない。

- 根拠: 3.1(専用の命令がない)、3.2(データは反応している)、3.3(呼び出しはエンジンを作り直す)、3.6(公式エディターでも同じ症状の報告)
- 反証になる結果: E1 で、手の操作のときに本体が出すメッセージが、ホストが送るものと形も中身も同じ(=違いが本体の内部にしかない)なら、ホストから直す手段はない。E2 で保存→呼び出しでも鳴らなければ、H1 の「呼び出しでは読み込む」の部分が誤り

### H2: 保存して呼び出し直せば鳴る(H1 の帰結。実用上いちばん重要)[仮説]

H1 が正しければ、ホストで書いた内容を sandbox に保存し、そのスロットを呼び出せば、エンジンが保存データから組み立て直されて鳴るはず。正しければ「キャプチャーやマイクを切り替えて比べる」スイープを、1 構成ごとに保存と呼び出しを挟むことで作れる(1 構成あたり保存・呼び出しの時間が増える)。

- モックの既定は、安全側に倒して「呼び出しても鳴らない」(`FakeConfig.strings_apply_on_recall=False`)。E2 の結果で切り替える
- 気をつける点: 呼び出しで本体の音は途切れる [記載]。保存は sandbox にしか行わない

### H3: 未所有プラグインの素通しはライセンスの仕様で、パソコン側の問題ではない [仮説・強い]

3.5 のとおり、プラグインのデバイスは有効なライセンスがあるときだけ読み込まれる。カタログにはモデルとして載っていて(`sku` / `plugin_id` を持つ)、グリッドにも置けるが、エンジンは処理せず素通しにする、という作りと考えられる。Cory Wong が鳴ったのは、そのプラグインを所有しているから(**所有状況はユーザーに確認が必要**)。

- 確かめ方: E5 で `ModuleStats.valid_id` と `License` を読む(読み取りだけ)
- 結論が正しければ、**パソコンから回避する方法を探すべきではない**。qcsuite は「未所有のプラグイン」として表示・除外を続ける

### H4: マイクは文字列 1 つではなく、複数のパラメーターの組で決まる [仮説]

キャブのパラメーターは内部向けの `ir selector` が 2 つあり(マイク A / B)、ほかにも位置や距離などがある。本体の UI は、マイクを選ぶときに複数の値をまとめて書き、エンジンはそのうち数値のほう(インデックスやハッシュ)を見ている可能性がある。文字列だけを書くと、表示は変わるが組み合わせが不整合で無視される。

- 確かめ方: E1 で、本体でマイクを変えたときに何個のパラメーターが変わるかを記録する

### H5: 本体がホストに確認を求め、返事がないため保留されている [仮説・弱い]

キャプチャーの作成では、本体がホストにダイアログ表示を問い合わせ、返事がないと何も起きない [記載]。同じように、キャプチャーの読み込みやライセンスのないブロックで、本体が `Confirmation` や `GenericError` を送っている可能性がある。

- 確かめ方: E1 のホスト側の記録で、書き込み直後に本体から届くメッセージを見る。**届いても返事はしない**(何に同意することになるか分からないため)

### H6: プリセットの依存情報やコンパイル用の値が手がかりになっている [仮説・弱い]

`ProductData.product_dependencies`(`missing` などの項目つき)や `BinaryPreset.recompile_code_*` / `layout_code_*` が、読み込むべき資産や組み立て直しの判断に使われていて、ホストの書き込みではそれらが更新されない、という可能性。根拠は項目名だけ。

- 確かめ方: E0 で、本体で作ったキャプチャー入りのプリセットと、ホストで書いたものを保存した sandbox のプリセットの依存情報を比べる(読み取りだけ)

### 捨てた仮説

- 「`CompilerInhibitedModules` がプラグインを止めている」: 項目は `global_gate` と `global_eq` だけで、pyquadcortex の説明では DSP の余裕がないときに Global EQ を止めた通知。プラグインとは無関係と判断した
- 「`Grid` の `update_type` で読み込みを指示できる」: 値は `METER_UPDATE` しかない

## 5. 実験手順(リスクの低い順)

### 5.1 すべての実験に共通の前提

- Cortex Control を終了しておく(USB の制御は排他)
- 書き込みを伴う実験は、**sandbox(`_qcsuite_sandbox`)に作ったコピーだけ**で行う。My Presets / Favorite / kami / Cory Wong / Factory Library には書かない(コードでも拒否される)
- sandbox を作る前に、実機テスト手順書の最初の 2 項目(セットリストの上限、Lane VOLUME が効くか)を済ませる
- 音が出る測定(`qcsuite research measure`)の前に、**モニターとヘッドホンの音量を下げる**
- 本体からキャプチャー作成のダイアログの問い合わせが来ても**答えない**(`show_capture_dialog` は呼ばない)
- 全体設定(I/O、Global EQ、マスター音量、ファームウェア)には触らない
- 生データの保存(`--raw-dir`)にはライセンス情報が含まれる可能性があるので、手元に置くだけにして共有しない

「鳴っているか」の判定には、同じ DI でのリアンプの測定を使う。

```bash
qcsuite research measure --label before          # 今のグリッドを測る(本体から音が出る)
qcsuite research measure --label after
qcsuite research measure --compare before after   # 形の差 1 dB 以上か音量差 1 LU 以上で「変わった」(目安)
```

`measure` は、ギター入力の行を USB 5 に付け替えてから測る(保存はしない。次の呼び出しで元に戻る)。最後の判断は耳で行う。

Python で書く手順は、qc-tone-loop のフォルダで `source .venv/bin/activate` したあと `python` で実行する。どれも同じ書き出し(`rig`、`d`)を使う。

```python
import time
from pathlib import Path
from types import SimpleNamespace
from qcsuite.device.session import open_rig
from qcsuite.features import research

rig_cm = open_rig(mock=False)       # USB を排他で開く(終わったら rig_cm.__exit__(None, None, None))
rig = rig_cm.__enter__()
d = rig.device
SANDBOX_KEY = d.ensure_sandbox()    # 上限の確認を済ませてから(セットリストを 1 つ使う)
# POS: 実験に使う sandbox のコピーのスロット番号、ROW / COL: 対象のブロックの位置(0 始まり)
```

Python のセッションを開いている間は、USB とロックをそのセッションが持っているので、`qcsuite` の CLI は「使用中」になる。CLI の手順の前に、Python 側を閉じる(`rig_cm.__exit__(None, None, None)`)。DI は `--di` / `research.measure(..., di_name=...)` で自分の DI を指定するのがよい(既定は合成 DI)。

### E0: プリセットの依存情報を読む(リスクなし)

- 目的: H6。本体で作ったプリセットに、キャプチャーやプラグインへの依存が記録されているか
- 手順: `qcsuite --json research dependencies "My Presets" > deps_my.json`(kami なども同様)
- 記録: キャプチャー入りのプリセット(本体で作ったもの)の `product_dependencies` の中身
- 判定: 依存が空なら H6 は弱まる。キャプチャー名やプラグインが載っていれば、E2 で保存した sandbox のプリセットとも比べる(依存が付かないなら H6 が強まる)
- 元に戻す: 不要(読み取りだけ)

### E5: ライセンスとモジュールの有効フラグを読む(リスクなし)

- 目的: H3
- 手順:
  ```bash
  qcsuite research read-state ModuleStats --out modulestats.jsonl
  qcsuite research read-state License --raw-dir ~/qc-research/license-raw --out license.jsonl
  ```
  プラグインのモデル ID は Python で調べる: `[(m.id, m.name, m.plugin_id, m.sku) for m in d.catalog if m.plugin_id or m.sku]`
  どちらも、接続のときに pyquadcortex がすでに送っている READ をもう一度送るだけ
- 記録: `ModuleStats` の `valid_id` で、Plini などのモデル ID が `valid: false`、The Amp Snob などが `valid: true` になっているか。`License` が読めた場合は、所有しているプラグインの一覧
- 判定: 未所有だけが無効なら H3 が強く支持される。`License` が生の本文(`kind: raw`)でしか届かない場合は、中身の解読はしない(長さとハッシュだけ記録)
- 元に戻す: 不要

### E1: 手の操作と、ホストの書き込みで、本体が出すメッセージを比べる(手の操作の記録はリスクなし)

- 目的: H1・H4・H5。本体の画面で操作したときと、ホストから書いたときで、本体が出すメッセージに違いがあるか
- 準備: sandbox に、キャプチャー入りのプリセットのコピーを 1 つ作っておき、本体でそれを読み込んでおく(コピーは計画の確認つきで、sandbox にだけ作る)
- 手順(手の操作。このツールは何も送らない):
  ```bash
  qcsuite research sniff --seconds 40 --out hand-capture.jsonl
  #   ↑ 実行中に、本体の画面でキャプチャーのブロックを開き、別のキャプチャーを選ぶ
  qcsuite research sniff --seconds 40 --out hand-mic.jsonl
  #   ↑ 実行中に、本体でキャブのマイクを変える
  qcsuite research sniff --seconds 40 --out hand-idle.jsonl   # 何もしないときの基準
  ```
  本体の画面での変更は保存しない(終わったらプリセットを呼び出し直して破棄)
- 手順(ホストの書き込み。低リスク: 保存しない編集だけ。同じ sandbox のコピーで):
  ```python
  d.read_stored(SANDBOX_KEY, POS)          # sandbox のコピーを読み込む(音が切り替わる)
  cap = next(c for c in d.captures() if c["name"] == "手で選んだのと同じキャプチャー名")
  with research.Sniffer(d.qc) as s:
      d.qc.set_capture(ROW, COL, SimpleNamespace(**cap), model=None)   # 既存のブロックの差し替え
      time.sleep(15)
  research.write_log(Path("host-capture.jsonl"), s.records)
  ```
  あわせて、何が送られるかは本体なしでも組み立てられる: `qcsuite research outbound set-capture --row ROW --col COL --key KEY --name NAME --out host-out.jsonl`
- 比較:
  ```bash
  qcsuite research diff hand-capture.jsonl host-capture.jsonl
  qcsuite research diff hand-capture.jsonl hand-idle.jsonl
  qcsuite research diff hand-mic.jsonl host-out.jsonl --type Grid
  ```
- 記録: 手の操作だけに出るメッセージの種類(`DefaultParameters`、`ModuleStats`、`RecallPreset`、`Confirmation` など)、`Grid` の中の項目の違い(`notify_flag`、`model_update_notifications`、マイクのときに変わるパラメーターの数)
- 判定と予想:
  - 手の操作のときだけ、追加のメッセージや項目が出る → H1・H4 の具体的な中身の手がかり。**それをそのまま送り返す実験はしない**(6 章)。どう使うかはユーザーと相談
  - 違いがない → 違いは本体の内部だけ。H2(保存→呼び出し)が唯一の道
  - ホストの書き込みの直後に `Confirmation` や `GenericError` が届く → H5
- 元に戻す: sandbox のコピーを呼び出し直す(保存していないので元に戻る)

### E6: Lane Output VOLUME が効くか(参考。実機テスト手順書の 2 番目の項目)

- 目的: 音量そろえの方法を決める。キャプチャーやマイクと同じく「書けるのに効かない」可能性がある
- 手順(保存しない編集):
  ```python
  d.read_stored(SANDBOX_KEY, POS)
  research.measure(rig, "lane-before")
  d.set_lane_volume_db(0, d.lane_volume_db(0) - 6.0)
  research.measure(rig, "lane-minus6", route=False)
  print(research.compare_measurements(False, "lane-before", "lane-minus6"))
  ```
  (音量そろえの機能の側にも同じ確認の関数を用意する予定。手順書ではそちらを使う)
- 判定: 音量差が約 −6 LU なら効いている。ほぼ 0 なら効いていない → 音量そろえは別の方法(Utility の Gain ブロックなど)に切り替える
- 元に戻す: プリセットを呼び出し直す

### E3: 保存せずに「読み込み直し」を起こせるか(低リスク: 保存しない編集だけ)

- 目的: H1 の範囲を絞る。呼び出し以外に、エンジンの組み立て直しを起こす操作があるか
- 手順: sandbox のコピーで、ホストからキャプチャーを差し替えたあと(E1 と同じ書き込み)、次を 1 つずつ試し、そのたびに測る
  1. シーンを切り替えて戻す(`d.switch_scene(1); d.switch_scene(0)`)→ `research.measure(rig, "e3-scene", route=False)`
  2. そのブロックのバイパスを入れて戻す(`d.qc.set_bypass(ROW, COL, True); d.qc.set_bypass(ROW, COL, False)`)→ 測る
  3. 空いているセルに軽いブロック(Utility の Gain、ID 16005)を置いて消す(`d.place_block(R, C, 16005); d.qc.remove_block(R, C)`)→ 測る
  4. 基準として、本体の画面で同じキャプチャーを手で選んだ状態を測る(`e3-by-hand`)
- 判定: どれかで `--compare` が「変わった」になり、手で選んだ状態に近づけば(形の差 1 dB 未満)、それが保存なしの読み込み直しの方法の候補
- 元に戻す: sandbox のコピーを呼び出し直す

### E4: IR ローダーの `set_ir` は鳴るか(低リスク)

- 目的: pyquadcortex は「ホストで指定した IR が本体に警告なしで表示された」と書いている [記載] が、音が変わるかは書いていない。キャブのマイクと同じかを確かめる
- 準備: sandbox のコピーに、本体の画面で IR ローダー(Single M など)を置き、IR「X」を選んで、そのコピーを本体で保存しておく(sandbox の中だけ)
- 手順:
  ```python
  d.read_stored(SANDBOX_KEY, POS)
  research.measure(rig, "ir-x")
  y = next(i for i in d.irs() if i["name"] == "IR Y の名前")
  d.qc.set_ir(ROW, COL, SimpleNamespace(**y), model=None)
  research.measure(rig, "ir-y-host", route=False)
  print(research.compare_measurements(False, "ir-x", "ir-y-host"))
  ```
  基準として、本体の画面で IR Y を選んだ状態(`ir-y-hand`)も測る
- 判定: `ir-y-host` が `ir-y-hand` に近ければ、IR ローダーはホストから切り替えられる(キャブのマイクの代わりに使える)。`ir-x` と同じなら、文字列パラメーターと同じ扱い
- 元に戻す: コピーを呼び出し直す

### E2: 保存して呼び出し直せば鳴るか(低リスク: sandbox への保存)

- 目的: H2。実用上いちばん重要
- 手順:
  ```python
  d.read_stored(SANDBOX_KEY, POS)                      # 本体で作ったキャプチャー入りのコピー
  research.measure(rig, "e2-base")
  cap = next(c for c in d.captures() if c["name"] == "別のキャプチャー名")
  d.qc.set_capture(ROW, COL, SimpleNamespace(**cap), model=None)
  research.measure(rig, "e2-host", route=False)         # 事実 1 どおりなら変わらない
  pos2 = d.free_position(SANDBOX_KEY)
  d.save_current(SANDBOX_KEY, pos2, "E2 saved")         # sandbox の空きスロットへ保存
  d.read_stored(SANDBOX_KEY, pos2)                      # 呼び出し直す(音が途切れる)
  research.measure(rig, "e2-recalled", route=False)
  for a, b in [("e2-base", "e2-host"), ("e2-base", "e2-recalled")]:
      print(research.compare_measurements(False, a, b))
  ```
  基準として、元のコピーを読み込み、本体の画面で同じキャプチャーを手で選んだ状態を `e2-by-hand` として測り、`e2-recalled` と比べる。マイク(`d.set_text(ROW, COL, 0, "...")` で手で選んだときと同じ文字列)と、ホストで新しく置いたキャプチャーのブロックでも同じことを行う
- 注意: `measure` は行の入力を USB 5 に付け替えるので、そのあと保存したコピーは入力が USB 5 のままになる(sandbox の中だけの話。健康診断で指摘される)
- 判定と予想:
  - `e2-recalled` が `e2-by-hand` に近い(形の差 1 dB 未満)→ **H2 が正しい**。モックの `strings_apply_on_recall` を True にし、「保存→呼び出し」を挟むキャプチャー・マイクの比較を作れる(保留事項として相談)
  - `e2-recalled` が `e2-base` と同じ → 呼び出しでも読み込まれない。手で読み込む候補リスト(1 の機能)が唯一の方法
- 元に戻す: sandbox のプリセットは、計画の確認つきで削除できる(sandbox 以外には触れていない)

### E7(任意・上級): Cortex Control の通信を USB で記録する

pyquadcortex の作者は、Windows で USBPcap を使って Cortex Control 4.0.1 の通信を記録している(`framing.py` の説明)。現在の Cortex Control がキャプチャーやマイクを切り替えられて、それが鳴るなら、その送信内容が答えになる。ただし USB の記録の道具が必要で、qcsuite の範囲外。方法は pyquadcortex の `docs/capture.md`(GitHub。この環境では未読)にあるとされる。

## 6. やってはいけないこと

- E1 で見つけた、本体が出すメッセージを、意味を確かめずにそのまま送り返す(リプレイ)。DSP の組み立て直し、ファイル操作、全体設定の変更などを意図せず起こす恐れがある。送り返す実験をするなら、その前にユーザーと相談し、sandbox のコピーで 1 通だけにする
- キャプチャー作成の流れのメッセージ(`NeuralCapture`、`NeuralCapture2`、`EnableCaptureOut`)を送る、`show_capture_dialog` に答える
- 保守・工場向けのメッセージを送る: `GainCalibration`、`Updater` / `UpdaterForward`、`Serialization`、`ProductionTest`、`TestFarm`、`LoadAutomatedTestPreset`、`GenerateTestPreset`、`SetTestPreset*`、`PresetSpeedTest`、`SuspendConnection`、`LocalBackup`、`CloudBackup`、`ProcessDownloadsQueue`、`ProductForward`、`ProductionAutomationMode`、`Diagnostics`
- 全体設定(I/O、Global EQ、マスター音量、モード)を変える
- `File` の `preset_payload` や `ir_payload` でプリセットや IR を丸ごと送る(形式が分かっておらず、壊れたファイルを作る恐れがある)
- 保護フォルダへの保存。ライセンス(`License`)への READ 以外の送信
- `--raw-dir` で保存した生データを他人に渡す

## 7. 結果の記録と、qcsuite 側の対応

実験ごとに次を残す: 日時、CorOS のバージョン、使ったプリセット(sandbox のスロット)、コマンド、`measure` の結果(`--compare` の出力)、耳での印象、記録ファイル(`*.jsonl`)。

| 結果 | qcsuite 側でやること |
|---|---|
| E2 で鳴った(H2 が正しい)| モックの `strings_apply_on_recall` を True に。キャプチャー・マイクを「保存→呼び出し」で切り替えるスイープを作るかどうか相談 |
| E2 で鳴らない | 手で読み込む候補リスト(プリセット管理の検索)を主な手段にする。表示の文言はそのまま |
| E3 のどれかで鳴った | その操作を挟む方法を、sandbox 限定で設計して相談 |
| E4 で IR ローダーが鳴った | キャブのマイクの代わりに IR ローダーで比べる案を相談。モックの `ir_is_inert` を False に |
| E5 で H3 を支持 | 健康診断の表示を「ライセンスがないため素通し」に。回避は探さない |
| E6 で Lane VOLUME が効かない | 音量そろえを別の方法に切り替える |
| どれも当てはまらない | 記録ファイルを残して止める。推測で次の書き込みを試さない |

## 8. 付録

### 8.1 研究用のコマンド(`qcsuite research`)

| コマンド | 内容 | 本体への送信 |
|---|---|---|
| `sniff --seconds N [--out F] [--raw-dir D] [--include-meters]` | 本体から届くメッセージを N 秒記録する。読めなかったもの(License など)も種類・長さ・ハッシュで残す | なし(接続中のキープアライブのみ)|
| `outbound set-capture\|set-text\|set-block\|set-value ...` | pyquadcortex がその操作で送るメッセージを、本体なしで組み立てる | なし(本体を使わない)|
| `diff A.jsonl B.jsonl [--type Grid]` | 2 つの記録を、種類・項目・値で比べる | なし |
| `dependencies FOLDER` | 一覧に付いている依存情報を表示 | 一覧の READ(普段と同じ)|
| `read-state License\|ModuleStats\|CompilerInhibitedModules\|NewModels\|PinnedModels` | 接続時にも読む状態を読み直して記録 | その READ を 1 通 |
| `measure --label L` / `measure --compare A B` | 今のグリッドをリアンプして測り、2 つの測定を比べる | 行の入力の付け替え(保存しない)とリアンプ。**本体から音が出る** |

`--mock` では本体の通信はなく、記録はモックのメソッド呼び出し(`"synthetic": true`)になる。道具の動作確認用で、本体の通信ではない。

記録の仕組みは、pyquadcortex 0.40.0 の `Transport` の内部(`_handle_message`、`_dispatch`、`send`)を、記録中だけ差し替えるもので、公開 API ではない。別のバージョンでは使えないことがあり、その場合ははっきりエラーになる。

### 8.2 出典

- Neural DSP, CorOS 3.0.0 and Cortex Control 1.1.0 are now available: https://neuraldsp.com/quad-cortex-updates/coros-3-0-0-and-cortex-control-1-1-0-are-now-available (検索結果の要約で確認)
- Neural DSP, CorOS 3.2.0 and Cortex Control 1.3.0 are now available: https://neuraldsp.com/news/coros-3-2-0-and-cortex-control-1-3-0-are-now-available (検索結果の要約で確認)
- Neural DSP Unity forum, "Changing MIC/IR in Cortex Control does not work": https://unity.neuraldsp.com/t/changing-mic-ir-in-cortex-control-does-not-work/12839 (本文は取得できず、検索結果の要約のみ)
- Neural DSP Unity forum, "Impulse Response/Cab combination": https://unity.neuraldsp.com/t/impulse-response-cab-combination/8355 (検索結果の要約のみ)
- pyquadcortex 0.40.0(PyPI のパッケージに含まれるソースと docstring)。docs/ は同梱されておらず未読: https://github.com/stokes-audio/pyquadcortex (METADATA に記載のリンク)
