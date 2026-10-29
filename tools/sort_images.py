"""学習データ作り用: 画像を1枚ずつ表示して、キー操作で person / vehicle / delete フォルダに振り分ける。

    python tools/sort_images.py --src 振り分ける画像のフォルダ --dst 振り分け先のフォルダ

  ← または a : person へ移動
  → または d : vehicle へ移動
  ↓ または s : delete へ移動
  Esc       : 終了

画像はコピーではなく「移動」されます。必要なら事前にバックアップを取ってください。
"""

import argparse
import os
import shutil

import cv2
import numpy as np

# cv2.waitKeyEx の矢印キーのコードは OS によって違うので両方受け付ける（Windows / Linux）
KEYS_PERSON = {2424832, 65361, 65430, ord("a")}
KEYS_VEHICLE = {2555904, 65363, 65432, ord("d")}
KEYS_DELETE = {2621440, 65364, 65433, ord("s")}
KEY_ESC = 27


def imread_jp(path):
    """日本語を含むパスでも読めるようにする（cv2.imread は Windows で日本語パスが苦手）。"""
    try:
        return cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
    except Exception as e:  # noqa: BLE001
        print(f"読み込みエラー: {e}")
        return None


def manual_sort(src_dir, dst_base, display_width=1280):
    if not os.path.isdir(src_dir):
        print(f"エラー: フォルダが見つかりません: {src_dir}")
        return

    dirs = {name: os.path.join(dst_base, name) for name in ("person", "vehicle", "delete")}
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)

    files = sorted(f for f in os.listdir(src_dir) if f.lower().endswith((".png", ".jpg", ".jpeg")))
    if not files:
        print("処理する画像がありません。")
        return

    print(__doc__)
    title = "Sorting (Left:Person, Right:Vehicle, Down:Delete, ESC:Exit)"
    for i, filename in enumerate(files, 1):
        img_path = os.path.join(src_dir, filename)
        img = imread_jp(img_path)
        if img is None:
            print(f"読み込めません: {filename}")
            continue

        h, w = img.shape[:2]
        display = cv2.resize(img, (display_width, int(h * display_width / w)))
        cv2.imshow(title, display)
        print(f"[{i}/{len(files)}] {filename}")

        while True:
            key = cv2.waitKeyEx(0)
            if key in KEYS_PERSON:
                dst = dirs["person"]
            elif key in KEYS_VEHICLE:
                dst = dirs["vehicle"]
            elif key in KEYS_DELETE:
                dst = dirs["delete"]
            elif key == KEY_ESC:
                cv2.destroyAllWindows()
                return
            else:
                continue
            shutil.move(img_path, os.path.join(dst, filename))
            break

    cv2.destroyAllWindows()
    print("全ての処理が完了しました。")


def main():
    parser = argparse.ArgumentParser(description="画像を person / vehicle / delete に手作業で振り分ける")
    parser.add_argument("--src", required=True, help="振り分ける画像が入っているフォルダ")
    parser.add_argument("--dst", required=True, help="振り分け先（この中に person / vehicle / delete を作る）")
    parser.add_argument("--width", type=int, default=1280, help="表示幅（ピクセル）")
    args = parser.parse_args()
    manual_sort(args.src, args.dst, args.width)


if __name__ == "__main__":
    main()
