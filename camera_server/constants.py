"""コード全体で共通の定数（設定ファイルで変える必要のないもの）。"""

import re

# 録画カテゴリ（録画フォルダ名・URL にも使う）
CATEGORIES = ("person", "car")

# COCO データセットのクラス ID
PERSON_CLASSES = {0}                 # person
CAR_CLASSES = {1, 2, 3, 5, 7}        # bicycle, car, motorcycle, bus, truck

# ffmpeg が書き出す一時セグメントのファイル名
SEGMENT_RE = re.compile(r"seg_(\d{8}_\d{6})\.ts")

FINALIZE_WAIT = 6       # 録画終了後、最後のセグメントが書き終わるのを待つ秒数
SEGMENT_MARGIN = 2      # 録画の前後に足す余裕（秒）
