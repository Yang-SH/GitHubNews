import sys
from pathlib import Path

# 让测试可以直接 import scripts/ 下的模块
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
