"""pytest設定。

テスト実行時に `import domain`(server側) や `import ghost_sensehat`(raspi側) など
フラットなモジュールを解決できるよう、コンポーネント各ディレクトリを import path に
追加する。pytestはこのconftestをテスト収集前に読み込むので、これだけで tests/ 配下から
server/・raspi/ の各モジュールを import できる。
"""
import os
import sys

_ROOT = os.path.dirname(__file__)
for _component in ("server", "raspi"):
    sys.path.insert(0, os.path.join(_ROOT, _component))
