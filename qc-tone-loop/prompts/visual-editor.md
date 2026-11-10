リポジトリ nomosist-a11y/degree-name（この Mac の ~/degree-name）の、ブランチ claude/qc-suite にある qc-tone-loop/ の qcsuite に、**Cortex Control 風の見た目で操作できる「エディター」タブ**を、設計から実装・テスト・ドキュメント・最終報告まで作ってください。作業は claude/qc-suite の最新（`origin/claude/qc-suite`。ローカルより 1 コミット進んでいる）から新しいブランチを切って行い、コミットはそのブランチに積んでください。プッシュと PR は、私に聞いてからにしてください。私の判断が必要なことは保留リストに積んで、最終報告でまとめて聞いてください。

# 前提: この Mac のローカルセッション。実機はつながっているが、開発はモックで

Quad Cortex（以下 QC）はこの Mac に USB でつながっている。
- 開発とテストは、すべてモック（qcsuite/device/fake.py の FakeQuadCortex と fake_data.py、`--mock`）で行う。
- **実機には、私が「実機で試して」と言うまで触らない**。私が演奏や Cortex Control で本体を使っていることがあるし、本体の音が勝手に変わると困る。実機で確かめる手順は「実機テスト手順書」にまとめておき、私の合図で一緒に実行する。
- 例外として、実機の生の ModelRepo.xml は ~/Library/Mobile Documents/com~apple~CloudDocs/開発/quad-cortex-cc-build/ModelRepo.xml に取得済み（CorOS 4.1.0、読み取り専用で取ったもの）。パーサーの確認にはこれを使ってよい（リポジトリには入れない）。同じフォルダの build_cc.py は、この XML から静的なパラメーター表を作ったスクリプトで、属性の扱いの参考になる。
- 実機で確認していないことを「動く」と書かない。最終報告では「モックで確認済み・実機は未確認」と正直に書く。
- qc-tone-loop/CLAUDE.md の qcsuite のルールに従う。
- `~/degree-name/qc-tone-loop` の作業ツリーには、別件の未コミットの修正（qcsuite/device/core.py ほか）がある。そこには触らず、自分のブランチ（worktree ならその中）で作業する。venv が無ければ自分の作業場所に作る（`pip install -e 'qc-tone-loop[device,dev]'`、DYLD_LIBRARY_PATH=/opt/homebrew/lib が必要）。
- docs/visual-editor/（cc.css、cc.js）とこのプロンプトはまだコミットされていない。~/degree-name/qc-tone-loop/docs/visual-editor/ と ~/degree-name/qc-tone-loop/prompts/visual-editor.md から自分のブランチにコピーして、最初のコミットに含める。

# 作るもの

qcsuite の Web UI（`qcsuite serve`、127.0.0.1:8765）に「エディター」タブを足す。
1. **グリッド表示**: 今本体で鳴っているプリセット（保存前の編集を含む）のグリッドを、Cortex Control と同じ見た目で表示する。行（Row 1〜4）ごとに、ブロックを角丸の正方形で並べ、信号線でつなぐ。ブロックの枠の色はカテゴリの色（下の「見た目の仕様」）で、中に白い線のカテゴリアイコンを描く。バイパス中のブロックは暗くする。
2. **パラメーター画面**: ブロックを選ぶと、下にそのブロックのパラメーター画面が開く。docs/visual-editor/cc.js と cc.css が、Cortex Control の画面を見ながら作った描画コード（読み取り専用の静的ページ用）なので、これを土台にして操作できるように作り直す。
3. **操作**: つまみ（縦ドラッグ、ホイール、ダブルクリックで初期値に戻す、フォーカス中は矢印キー）、スイッチ、プルダウン、ブロックのバイパス切り替え。
4. **書き込み**: 操作は本体の今のグリッドに書き込む（保存はしない）。ドラッグ中は 100 ms 程度に間引いて送り、離したときに最終値を必ず送る。1 回のドラッグ（またはクリック）を、取り消しの 1 単位にする。
5. **取り消し・やり直し・A/B**: 既存の「言葉で操作するパネル」（qcsuite/features/panel.py）の apply / undo / redo / history / A/B をそのまま使い、エディターの操作も同じ履歴に入るようにする。パネルとエディターのどちらで変えても、両方の画面と履歴に反映されること。
6. **保存しない**: 保存は既存の「sandbox に保存」（plan 経由、dry run が先）だけにする。画面に常に「保存していません。本体でプリセットを呼び出し直すと元に戻ります」と出す。
7. **第 2 段階（余力があれば）**: 左側にデバイス一覧（カテゴリ別、Cortex Control と同じく名前だけのリスト）を出し、選んだブロックのモデルを差し替える（set_block）。set_block は確認待ちがタイムアウトしても実際には置かれていることがあるので、必ず読み戻して確かめる。

# まず読むもの

- qc-tone-loop/CLAUDE.md、README.md の qcsuite の節、docs/ 以下の qcsuite のガイドと実機テスト計画
- qcsuite/features/panel.py、panel_rules.py（特に switch_violations）、qcsuite/web/routes/panel.py と static/panel.html・panel.js。エディターはこの仕組みに乗せる。
- qcsuite/device/core.py（Device: ロック、再試行、grid()、apply_patch()、フォルダ一覧のキャッシュ、version(cached=True)）
- qcsuite/web/app.py、static/common.js、style.css（セッションの確認、ヘッダー、接続状態の表示）
- qctone/device.py（パッチ形式と検証、snapshot の形）
- pyquadcortex の client.py の docstring（set_param、set_block、バイパス、param_options、catalog、`_fetch_model_repo`）。使うメソッドは必ず docstring を読む。
- docs/visual-editor/cc.js・cc.css（見た目の土台）

# 実機で確認済みの事実（モックにも反映する）

1. **本体からは書き込めても音が変わらないもの**: Neural Capture ブロックのパラメーターと file_name、キャビの "ir selector"（マイク）などの文字列パラメーター、未所有プラグイン（Plini、Gojira、Nolly、Petrucci、John Mayer、Rabea、Misha、Tim Henson、Fortin、Soldano SLO-100 X）のブロック。値は読み戻せるので、成功したように見える。エディターではこれらのつまみに「読み戻せるが音は変わらない」と表示し、文字列パラメーターは表示だけにする（パネルも同じ扱いをしている）。
2. **スイッチ（選択肢を持つもの）は value で書かない**。既存コードと同じく選択肢の名前で書く。comboBox の選択肢の名前は catalog に無く、プリセットの dynamic_steps にある（pyquadcortex の param_options）。
3. **本体は 1 つの要求を処理している間、ほかの要求を待たせる**。フォルダ一覧は 20 秒かかる。以前、GUI がタブごとに重い問い合わせをして、接続状態が「確認中…」のまま返らなくなった（Device にキャッシュを足して直した）。エディターは grid の読み取りを最小限にし、重い問い合わせを定期的に投げないこと。書き込みが詰まったら、古い値を捨てて最新の値だけ送る。
4. USB 制御は Cortex Control と排他。
5. ブロックをクリックしただけでは本体は変わらない。ブロックの × は削除なので、エディターでは削除は確認つきにするか、作らない。

# カタログの追加情報（pyquadcortex が捨てている属性）

本体が送ってくるモデル一覧（ModelRepo.xml、gzip した tar に入っている）には、画面を再現するのに必要な属性があるが、pyquadcortex の catalog.parse_model_repo は捨てている。qcsuite 側で生の XML を読むパーサーを作る（`qc._fetch_model_repo()` は非公開 API なので、失敗したら属性なしで動くようにフォールバックする）。取得した XML は ~/.qctone/catalog/ にファームウェアのバージョンごとにキャッシュする。

**本物の ModelRepo.xml はリポジトリに入れない**（リポジトリは公開で、中身は Neural DSP のデータ）。テストとモック用には、下の形をまねた小さな XML を手で書く。

使う属性:
- Model: `id`, `name`, `tm`（"Based on ..."）, `hidden`, `internal`, `plugin_id`, `sku`, `clones`（キャビは "Default Cabsim"（id 12000、hidden）の複製で、自分では "ir selector" しか持たない。パラメーターは複製元から引く）
- Parameter: `name`, `type`（float / int / fader / floatWithLed はつまみ、switch / toggleButton / rotarySwitch / comboBox は選択、string / empty / meter / grMeter は表示しない）, `min`, `max`, `defaultValue`, `units`, `skew`（数値なら JUCE の skew。"LIN_SKEW" は 1）, `steps`, `stepNames`（カンマ区切り）, `displayPos`（画面での並び順。**送信時の index とは違う**）, `hidden="true"`（表示しない）, `min_string`（最小値のときの表示。例 "OFF"）, `showAsInteger`, `toggleOn` / `toggleOff`
- 名前が小文字の bypass / phi / ir selector / file_name は内部用で、表示しない。キャビは同じ名前のパラメーターがマイク B 用にもう 1 組あるので、最初の組だけ出す。

実機の XML の抜粋（形の見本。モック用の XML はこれをまねる）:

```xml
<Category id="1" name="Guitar Amplifier">
  <Model id="..." name="Brit 2203" tm="Based on Marshall® JCM800®">
    <!-- 送信順は GAIN BASS MID TREBLE MASTER PRESENCE OUTPUT。displayPos により画面では
         GAIN BASS MID TREBLE PRESENCE / MASTER OUTPUT の順に並ぶ（実機の画面で確認済み）。
         初期値は 5.0 が 5 つ、MASTER 3.0、OUTPUT 0.0 dB -->
  </Model>
</Category>
<Model id="2" name="Obsessive Drive" tm="Based on Fulltone® OCD®">
  <Parameter defaultValue="5" max="10" min="0" name="DRIVE" type="float" units=""/>
  <Parameter defaultValue="0" max="1" min="0" name="PEAK" stepNames="LP,HP" steps="2" type="switch" units=""/>
  <Parameter defaultValue="5" max="10" min="0" name="TONE" type="float" units=""/>
  <Parameter defaultValue="5" max="10" min="0" name="VOLUME" type="float" units=""/>
</Model>
<Model id="5001" name="Legendary 87 (M)" tm="Based on Universal Audio® 1176®">
  <Parameter defaultValue="-15.0" max="0.0" min="-48.0" name="INPUT" skew="LIN_SKEW" steps="481" type="float" units="dB"/>
  <Parameter defaultValue="2" max="4" min="0" name="RATIO" stepNames="4,8,12,20,All" steps="5" type="rotarySwitch" units=""/>
  <Parameter defaultValue="0.8" max="0.8" min="0.016" name="ATTACK" skew="0.6" steps="300" type="float" units="ms"/>
  <Parameter defaultValue="1.0" max="1.1" min="0.056" name="RELEASE" skew="0.6" steps="300" type="float" units="s"/>
  <Parameter defaultValue="4.0" max="48.0" min="-48.0" name="MAKEUP" skew="LIN_SKEW" steps="481" type="float" units="dB"/>
  <Parameter defaultValue="100" max="100" min="0" name="MIX" skew="LIN_SKEW" steps="1001" type="float" units="%"/>
  <Parameter defaultValue="0" max="0" min="-50" name="GAIN REDUCTION" steps="180" type="grMeter" units="dB"/>
</Model>
<Model clones="12000" id="12006" name="412 Brit 60B GB 71 (M)" tm="Based on Marshall® 1960B® ...">
  <Parameter defaultValue="ML_412 Brit 60B GB 71_Dynamic 57" max="999" min="0" name="ir selector" steps="1000" type="string" units=""/>
  <Parameter defaultValue="ML_412 Brit 60B GB 71_Ribbon 160" max="999" min="0" name="ir selector" steps="1000" type="string" units=""/>
</Model>
```

キャビのマイク名は ir selector の値の最後の "_" より後ろ（例 "Dynamic 57"）。MIC A / MIC B として表示だけする。

# 見た目の仕様（Cortex Control v4.1.0 の実画面から採ったもの）

- 全体は黒（#000）。パラメーター画面のヘッダーは #1b1b1b で、左に「⋮」、続けて「カテゴリ名（灰色） デバイス名（カテゴリの色）」、右端にページ番号のボタン（1 2 …）。
- パラメーターは 1 ページ 10 個、5 列 × 2 段。セルは #202020、セルの間は 1 px の暗い線。左上に名前（大文字）、左下に値、右下につまみ。単位は値の後ろに灰色で付ける（"0.0 dB"、"80 Hz"、"17 %"）。
- つまみ: 7 時から 5 時までの 270° の弧。最小値から今の値までをカテゴリの色で塗り、残りは黒。中は暗い円で、白い指針。位置は JUCE と同じく proportion = ((v - min) / (max - min)) ^ skew。
- 値の表示: showAsInteger、%、BPM は整数。Hz と ms は 10 以上なら整数。絶対値が 1 未満なら小数 2 桁。それ以外は小数 1 桁。最小値で min_string があればそれを出す。
- 選択肢が 3 つ以上はプルダウン（暗い角丸の箱に名前と ∨）、2 つまではボタンを 2 つ並べて、選ばれている方をカテゴリの色で塗る。stepNames が無いスイッチは toggleOff / toggleOn、それも無ければ OFF / ON。
- カテゴリの色: アンプ #E5483D、ドライブ #E07A3A、キャブと IR #6E5EF0、ディレイ #62D2C2、リバーブ #4DB8A3、コンプ・EQ・フィルター・ワウ #6CC56E、ピッチ・モーフ #E8BE48、モジュレーション・シンセ #D25FA4、Neural Capture #F0F0F0、ユーティリティ・ルーパー・FX ループ #9AA5AE。
- スマホ幅では 2〜3 列に折り返す。横スクロールを出さない。
- Cortex Control のロゴや名前、Neural DSP のロゴは画面に使わない（qcsuite の既存のヘッダーのまま）。

# 安全ルール（既存の qcsuite と同じ。最優先）

- 保存しない。エディターから保存・コピー・削除・移動はしない（保存は既存の sandbox 保存だけ）。
- 書き込みはすべて Device のロックと再試行を通す。API を直接叩かない。
- 1 回の操作で変える値は、そのつまみ 1 つだけ。勝手にほかの値を整えない。
- 本体の画面で同時に編集されることがある。書き込みの前に必要なら読み直し、食い違ったら上書きせずに画面を更新して知らせる。

# テスト

- 生の XML のパーサー（displayPos の並び、hidden、clones、stepNames、skew、min_string）
- 値の表示と、つまみの角度の計算（Python 側に同じ規則を持たせてテストするか、Node が使えるなら JS をテストする）
- API ルート（モックで、操作 → 読み戻し → 履歴 → undo / redo）。ドラッグ中の間引きと「最後の値は必ず送る」
- 既存のテストを壊さない（`pytest -q` が全部通ること）
- 画面はヘッドレスブラウザで開いてスクリーンショットを撮り、デスクトップ幅とスマホ幅で崩れていないことを確かめる（使える環境なら）

# ドキュメントと最終報告

- 日本語のユーザーガイドにエディターの節を足す。
- 実機テスト手順書: 私の合図のあと、私と一緒に順番に実行できる形で書く（sandbox のプリセットを呼び出して操作 → 本体の画面と音で確認 → undo で戻る → Cortex Control で見比べる）。
- 最終報告: できたこと、モックでの確認範囲、実機で確かめるべきこと、保留リスト、ブランチ名。

# やらないこと

- docs のハードウェアテストで見つかった既存のバグ（遅延の測定ずれ、sandbox 経由の復元でバイパスが消える件、plan.execute の JSON TypeError など）は別に直すので、触らない。
- LAN 公開（--host）やスマホからの操作は作らない。サーバーは 127.0.0.1 固定のまま。
