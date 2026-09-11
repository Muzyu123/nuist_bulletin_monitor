#!/usr/bin/env bash
# 定时任务入口。cron 的环境变量极简（PATH 通常只有 /usr/bin:/bin），
# 所以这里显式定位目录和解释器，不依赖登录 shell 的任何配置。
#
# 解释器优先级：
#   1. 环境变量 NUIST_PYTHON —— 显式指定，比如 conda 环境里的 python
#   2. PATH 里的 python3
#   3. 常见绝对路径兜底
#
# 覆盖示例（写进 crontab 或 systemd unit 的 Environment=）：
#   NUIST_PYTHON=/opt/conda/envs/myenv/bin/python ./run.sh

set -euo pipefail

# readlink -f 是 GNU 扩展，macOS 上没有；用这份可移植写法
DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
cd "$DIR" || exit 1

PYTHON="${NUIST_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for cand in python3 /usr/bin/python3 /usr/local/bin/python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      PYTHON="$cand"
      break
    fi
  done
fi

if [ -z "$PYTHON" ]; then
  echo "run.sh: 找不到 python3。请设置 NUIST_PYTHON=/path/to/python3" >&2
  exit 127
fi

exec "$PYTHON" monitor.py --config "${NUIST_CONFIG:-config.json}" "$@"
