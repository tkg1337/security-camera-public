# 防犯カメラシステム（試作）

RTSP 対応のネットワークカメラの映像を YOLO で解析し、**動いている人・車**を見つけたら前後の映像を録画して、スマホにプッシュ通知を送るシステムです。録画の確認や検出範囲の設定はブラウザ（PWA）から行えます。

- メインストリームを**再エンコードせず**に一時セグメントへ溜め、検知した前後だけを mp4 にまとめるので高画質・低負荷
- YOLO の検出に加えてフレーム差分で「動いているか」を見るので、停まっている車などでは録画しない
- 検出範囲（ROI）と感度を Web 画面から変更可能
- 録画フォルダが上限を超えたら古いものから消すループ録画
- サブカメラ（録画のみ）を 1 台追加可能
- Web Push 通知（サムネイル付き）、パスキー（WebAuthn）による管理者ログイン

> 試作段階のプロジェクトです。

## フォルダ構成

```
.
├── run.py                  起動用スクリプト（python run.py）
├── config.example.yaml     設定ファイルの雛形 → config.yaml にコピーして使う
├── requirements.txt
├── camera_server/          本体
│   ├── main.py             起動処理・コマンドライン
│   ├── config.py           config.yaml の読み込み
│   ├── detector.py         YOLO による検知
│   ├── recorder.py         ffmpeg によるセグメント録画・mp4 化・ループ録画
│   ├── notify.py           Web Push 通知
│   ├── state.py            ROI・感度（Web から変更できる設定）
│   ├── web.py              Web 画面と API（Flask）
│   ├── auth.py             パスキー認証
│   ├── templates/          HTML
│   └── static/             Service Worker・アイコン
├── tools/
│   ├── gen_vapid_keys.py   通知用の鍵を作る
│   └── sort_images.py      学習用画像を手作業で振り分ける
└── data/                   実行時に自動で作られる（録画・設定の保存先。Git 管理外）
```

## 必要なもの

- Python 3.10 以上（3.13 で動作確認）
- [ffmpeg](https://ffmpeg.org/)（PATH を通すか、`recording.ffmpeg_path` にフルパスを書く）
- RTSP で映像を出せるネットワークカメラ
- NVIDIA GPU（推奨。CPU でも動きますが `detection.fps` を下げてください）

## セットアップ

```bash
# 1. 仮想環境を作って有効化（Windows の例）
python -m venv .venv
.venv\Scripts\activate

# 2. GPU を使う場合は、先に CUDA 版 PyTorch を入れる
#    コマンドは https://pytorch.org/get-started/locally/ で環境に合わせて確認

# 3. ライブラリを入れる
pip install -r requirements.txt

# 4. 設定ファイルを作る
copy config.example.yaml config.yaml      # macOS / Linux は cp
```

`config.yaml` を開いて、最低限 **カメラの RTSP アドレス** を書き換えます。

```yaml
cameras:
  main:
    url: "rtsp://192.168.1.100:554/stream1"
    username: "camera_user"
    password: "your-password"
```

RTSP アドレスの形はカメラによって違います。説明書やメーカーのサポートページで確認してください（例: TP-Link Tapo は `rtsp://<IP>:554/stream1`（高画質）/ `stream2`（低画質）で、カメラ用アカウントをアプリで作成）。

## 設定の確認

```bash
python run.py --check-config    # 設定の内容を表示（パスワードは伏せ字）
python run.py --test-cameras    # カメラから 1 コマ取れるか確認 → data/camera_test_main.jpg
```

## 起動

```bash
python run.py
```

起動すると URL が表示されます。

- 同じ PC から: `http://localhost:8080/`
- 同じ Wi-Fi のスマホなどから: `http://<この PC の IP アドレス>:8080/`

別の設定ファイルを使うときは `python run.py -c path/to/config.yaml`（または環境変数 `CAMERA_CONFIG`）。

## 主な設定項目

すべての項目は `config.example.yaml` にコメント付きで載っています。

| 項目 | 内容 |
| --- | --- |
| `cameras.main` / `cameras.sub` | RTSP アドレス・ユーザー名・パスワード。サブカメラは `enabled: true` で有効 |
| `server.port` / `server.https` | 待ち受けポートと HTTPS の有無 |
| `auth.enabled` | パスキー認証。`false` の間は全員が管理者扱い（`server.local_network_only` で LAN 内に限定） |
| `paths.recordings_dir` | 録画の保存先 |
| `recording.max_gb` | 録画フォルダの上限（超えると古いものから削除） |
| `recording.buffer_before` / `buffer_after` | 検知の何秒前から・何秒後まで録画するか |
| `live.hls_enabled` | ブラウザでのライブ表示 |
| `detection.*` | YOLO のモデル・FPS・検知サイズ・感度の初期値 |
| `notifications.*` | Web Push 用の VAPID 鍵 |

パスワードなどを設定ファイルに直接書きたくない場合は、`password: "${MAIN_CAMERA_PASSWORD}"` のように書くと環境変数から読み込みます。

## プッシュ通知を使う

1. `python tools/gen_vapid_keys.py` で鍵を作り、表示された 2 行を `config.yaml` の `notifications:` に貼り付ける
2. サーバーを起動し、スマホでホーム画面の「通知を許可する」を押す

ブラウザの仕様で、通知の登録は **HTTPS か localhost** でアクセスしたときだけできます。

## パスキー認証を使う（外部公開する場合）

1. ドメインと TLS 証明書を用意し、`server.https: true`、`ssl_cert` / `ssl_key` を設定
2. `auth.enabled: true`、`auth.rp_id` / `rp_origin` をそのドメインに、`auth.invite_code` を推測されにくい文字列に設定
3. `server.secret_key` にランダムな文字列を設定（再起動してもログインが切れないように）
4. ホーム画面の「管理者登録」で招待コードを入力してパスキーを登録

## 録画データ

- 録画: `data/recordings/person/record_YYYYMMDD_HHMMSS.mp4`（`car/` も同様）と同名の `.jpg`（サムネイル）
- サブカメラの録画: `..._cam2.mp4`（録画一覧ではメインと並べて再生）
- Web で変更した ROI・感度: `data/roi_settings.json` / `data/sensitivity.json`

## 学習用ツール

自前のモデルを学習させるための画像振り分けツールです。

```bash
python tools/sort_images.py --src 画像フォルダ --dst 振り分け先フォルダ
```

学習したモデルは `detection.model` にパスを書けば使えます。

## 注意

- 防犯カメラの映像には個人情報が含まれます。撮影範囲（他人の敷地・公道など）や録画データの管理には注意してください。
- 認証オフのまま外部に公開しないでください。
- 本システムは YOLO に [Ultralytics](https://github.com/ultralytics/ultralytics)（AGPL-3.0）を使用しています。
