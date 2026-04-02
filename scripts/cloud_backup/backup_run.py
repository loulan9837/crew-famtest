#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冷备单次执行入口：供 GitHub Actions / cron 调用，不经 Streamlit。

用法：
  export DATABASE_URL_BACKUP=...
  export R2_ACCOUNT_ID=...  # 或 R2_ENDPOINT_URL
  export R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET_NAME=...
  python scripts/cloud_backup/backup_run.py

退出码：0 成功，1 失败，2 跳过（未配置云端密钥）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 与 backup_lib 同目录
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from backup_lib import config_from_environ, run_backup  # noqa: E402


def main() -> None:
    def log(msg: str) -> None:
        print(msg, flush=True)

    code = run_backup(config_from_environ(), log)
    # GitHub Actions：未配置 Secrets 时跳过，避免 fork/未接云端库时 job 失败
    if code == 2 and os.environ.get("GITHUB_ACTIONS") == "true":
        code = 0
    raise SystemExit(code)


if __name__ == "__main__":
    main()
