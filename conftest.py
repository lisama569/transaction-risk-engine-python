# 根级 conftest：pytest 会把本文件所在目录加入 sys.path，
# 从而可以 import risk_engine 与 tests 内的日志辅助。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "tests"))
