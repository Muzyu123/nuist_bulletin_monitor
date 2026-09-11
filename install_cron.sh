#!/bin/bash
# 把监控挂进当前用户的 crontab（每小时一次），重启后自动恢复。
#
#   ./install_cron.sh           安装 / 更新
#   ./install_cron.sh --remove  卸载
#   ./install_cron.sh --status  查看当前状态

set -euo pipefail # -e 表示出错时（非零退出状态）退出，-u 表示使用未定义变量时退出，-o pipefail 表示管道中有一个命令出错时退出

HERE="$(cd -- "$(dirname -- "$0")" && pwd -P)" # 获取脚本所在目录的绝对路径
MARKER="# nuist-bulletin-monitor" # 用于标记 crontab 中的任务
MINUTE="${NUIST_CRON_MINUTE:-7}" # 默认在第 7 分钟运行，防止定时任务扎堆
LINE="$MINUTE * * * * $HERE/run.sh >> $HERE/Run/cron.log 2>&1 $MARKER" # crontab 中的完整任务行

crontab_current() { crontab -l 2>/dev/null || true; } # 获取当前用户的 crontab 内容，如果没有 crontab 则返回空
crontab_strip()   { crontab_current | grep -vF "$MARKER" || true; } # 去掉当前用户 crontab 中的监控任务行

case "${1:-install}" in
  --remove)
    crontab_strip | crontab -
    echo "已卸载监控任务。"
    ;;

  --status)
    if crontab_current | grep -qF "$MARKER"; then
      echo "监控任务正在运行："
      crontab_current | grep -F "$MARKER" | sed 's/^/  /'
      echo
      echo "最近日志："
      tail -n 10 "$HERE/Run/monitor.log" 2>/dev/null | sed 's/^/  /' || echo "  （还没有日志）"
    else
      echo "监控任务未安装。"
    fi
    ;;

  install|*)
    [[ -f "$HERE/config.json" ]] || {
      echo "错误：$HERE/config.json 不存在。可以复制填写 config.example.json 并更名为 config.json" >&2
      exit 1
    }
    chmod +x "$HERE/run.sh"
    mkdir -p "$HERE/Run"

    echo "配置检查 ..."
    if ! "$HERE/run.sh" --check-config; then
      echo >&2
      echo "配置出错，环境未安装 cron。以上错误日志 log.error 即为缺失部件" >&2
      exit 1
    fi

    crontab_strip | crontab -
    { crontab_current | grep -vF "$MARKER" || true; echo "$LINE"; } | crontab -

    echo "已安装："
    echo "  $LINE"
    echo
    echo "下一次触发在每小时的第 $MINUTE 分钟。查看状态：$HERE/install_cron.sh --status"
    ;;
esac
