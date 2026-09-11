#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
南信大「信息公告栏」新公告监控 —— 单次轮询，设计为由 cron 定时调用。

每次运行：
  1. 抓取列表页前 N 页，解析出全部通知
  2. 与 state.json 里已见过的 ID 比对，得到新增通知
  3. 新增通知里命中关键词的 → 汇总成一封邮件发出
  4. 未命中的直接记为已见（不发信）
ps.发送失败时不把这些公告记为已见，下一轮会重试，避免漏报。

用法：
  python3 monitor.py                    # 正常轮询一次
  python3 monitor.py --dry-run          # 只打印，不写状态、不发信
  python3 monitor.py --test-mail        # 给配置里的收件人发一封测试邮件
  python3 monitor.py --check-config     # 只校验配置
"""

import argparse
import html as htmllib
import json
import logging
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timezone, timedelta
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

from nuist_bulletin import (  # noqa: E402
    BASE, fetch, parse_list, page_url, get_total_pages, get_total_records, item_id,
)

CST = timezone(timedelta(hours=8))       # 运行服务器时区未必是东八区，邮件统一用北京时间
log = logging.getLogger("nuist-monitor")

DEFAULTS = {
    "smtp": {
        "host": "smtp.163.com",
        "port": 465,
        "ssl": True,
        "user": "",
        "password": "",
        "sender_name": "南信大公告监控",
    },
    "mail": {
        "to": [],
        "subject_prefix": "[南信大公告]",
    },
    "keywords": [],
    "notify_all": False,
    "keyword_match": {
        "fields": ["title", "category", "department"],
        "case_sensitive": False,
    },
    "fetch": {"pages": 2, "delay": 0.4, "timeout": 20, "retries": 3},
    "state_file": "Run/state.json",
    "log_file": "Run/monitor.log",
    "log_max_bytes": 2097152,
    "log_backup_count": 5,
    "notify_on_first_run": True,
    "max_seen": 5000,
}


#  配置 / 日志

def deep_merge(base, override):
    """把用户配置合并到默认配置上，只递归 dict，其余类型整体覆盖。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    cfg = deep_merge(DEFAULTS, raw)
    cfg["_dir"] = Path(path).resolve().parent
    return cfg


def resolve(cfg, key):
    """相对路径按配置文件所在目录解析，绝对路径原样返回。"""
    p = Path(cfg[key])
    return p if p.is_absolute() else cfg["_dir"] / p


def setup_logging(cfg, verbose):
    logfile = resolve(cfg, "log_file")
    logfile.parent.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()        # 同进程里被多次调用时不叠加 handler
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = RotatingFileHandler(logfile, maxBytes=cfg["log_max_bytes"],
                             backupCount=cfg["log_backup_count"], encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)


def check_config(cfg):
    """返回配置问题列表；空列表表示可用。"""
    problems = []
    s = cfg["smtp"]
    if not s["user"] or "@" not in s["user"] or s["user"] == "你的邮箱@163.com":
        problems.append("smtp.user 未填写或不是完整邮箱地址")
    if not s["password"] or s["password"] == "在这里填写授权码":
        problems.append("smtp.password 未填写（163 需要的是 SMTP 授权码，不是登录密码）")
    if not cfg["mail"]["to"] or any("example.com" in t for t in cfg["mail"]["to"]):
        problems.append("mail.to 收件人列表为空/配置仍为示例地址")
    if not cfg["keywords"] and not cfg["notify_all"]:
        problems.append("keywords 为空且 notify_all=false")

    bad_re = [k for k in cfg["keywords"]
              if isinstance(k, str) and k.lower().startswith("re:")
              and _bad_regex(k[3:])]
    if bad_re:
        problems.append(f"以下正则关键词无法编译：{bad_re}")
    return problems


def _bad_regex(pattern):
    try:
        re.compile(pattern)
        return False
    except re.error:
        return True


#  状态

def load_state(path):
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("seen", {}) if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        log.error("state 文件损坏，按空状态处理（本轮会被当作首次运行）：%s", e)
        return {}


def save_state(path, seen, max_seen):
    """按插入顺序保留最近 max_seen 条，避免状态文件无限膨胀。"""
    if len(seen) > max_seen:
        for k in list(seen)[:len(seen) - max_seen]:
            seen.pop(k, None)
    path.parent.mkdir(parents=True, exist_ok=True)   # 首次运行时 Run/ 可能还不存在
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"updated": now_cst().isoformat(), "seen": seen}, f,
                  ensure_ascii=False, indent=1)
    tmp.replace(path) # 原子替换，避免 cron 被杀时留下半截文件


def now_cst():
    return datetime.now(CST)


#  匹配

def match_item(item, keywords, km):
    """命中返回匹配到的关键词，未命中返回 None。"""
    fields = km.get("fields") or ["title"]
    parts = [(item.get(f) or "").strip() for f in fields]
    text = " ".join(p for p in parts if p)
    cs = bool(km.get("case_sensitive"))
    hay = text if cs else text.lower()
    flags = 0 if cs else re.IGNORECASE

    for kw in keywords:
        if not isinstance(kw, str) or not kw:
            continue
        if kw.lower().startswith("re:"):
            try:
                if re.search(kw[3:], text, flags):
                    return kw
            except re.error as e:
                log.warning("正则关键词 %r 无法编译，已跳过：%s", kw, e)
        else:
            if (kw if cs else kw.lower()) in hay:
                return kw
    return None


#  邮件

def render_text(hits, when):
    lines = [f"南信大信息公告栏 —— 发现 {len(hits)} 条匹配的新公告", f"检查时间：{when:%Y-%m-%d %H:%M:%S} (UTC+8)", ""]
    for i, (it, kw) in enumerate(hits, 1):
        lines += [
            f"{i}. {it['title']}",
            f"   发布单位：{it.get('department') or '-'}    栏目：{it.get('category') or '-'}    日期：{it.get('date') or '-'}",
            f"   命中关键词：{kw or '（全部新公告）'}",
            f"   {it['url']}",
            "",
        ]
    lines.append("—— 由 nuist_bulletin/monitor.py 自动发送")
    return "\n".join(lines)


def render_html(hits, when, prefix):
    esc = htmllib.escape

    def row(i, it, kw):
        return f"""
      <tr>
        <td style="padding:10px 12px;border-bottom:1px solid #eceff3;color:#8a94a6;font-size:13px;white-space:nowrap;vertical-align:top">{esc(str(i))}</td>
        <td style="padding:10px 12px;border-bottom:1px solid #eceff3;vertical-align:top">
          <a href="{esc(it['url'])}" style="color:#1a56db;text-decoration:none;font-weight:600;font-size:15px">{esc(it['title'])}</a>
          <div style="margin-top:5px;color:#6b7280;font-size:13px;line-height:1.6">
            {esc(it.get('department') or '—')}<br>
            日期 {esc(it.get('date') or '—')} &nbsp;·&nbsp; 栏目 {esc(it.get('category') or '—')} &nbsp;·&nbsp; 命中 <span style="color:#b45309">{esc(kw or '全部新公告')}</span>
          </div>
        </td>
      </tr>"""

    rows = "".join(row(i, it, kw) for i, (it, kw) in enumerate(hits, 1))
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:20px;background:#f5f6f8;font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif">
  <div style="max-width:760px;margin:0 auto;background:#fff;border-radius:10px;overflow:hidden">
    <div style="padding:18px 20px;border-bottom:2px solid #1a56db">
      <div style="font-size:17px;font-weight:700;color:#111827">{esc(prefix)} 发现 {len(hits)} 条匹配的新公告</div>
      <div style="margin-top:4px;color:#6b7280;font-size:13px">检查时间 {when:%Y-%m-%d %H:%M:%S} (UTC+8)</div>
    </div>
    <table style="width:100%;border-collapse:collapse">{rows}</table>
    <div style="padding:14px 20px;color:#9ca3af;font-size:12px;border-top:1px solid #eceff3">
      由 nuist_bulletin/monitor.py 自动发送 · <a href="{esc(BASE)}" style="color:#9ca3af">打开公告栏</a>
    </div>
  </div>
</body></html>"""


def send_mail(cfg, subject, text_body, html_body):
    s = cfg["smtp"]
    to = list(cfg["mail"]["to"])

    # EmailMessage 走的是新的 EmailPolicy，header 一律赋 str，
    # 中文由 Address/策略自动编码；
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = Address(display_name=s["sender_name"], addr_spec=s["user"])
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text_body, subtype="plain", charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")

    ctx = ssl.create_default_context()
    if s["ssl"]:
        with smtplib.SMTP_SSL(s["host"], s["port"], timeout=30, context=ctx) as c:
            c.login(s["user"], s["password"])
            c.send_message(msg)
    else:
        with smtplib.SMTP(s["host"], s["port"], timeout=30) as c:
            c.ehlo()
            c.starttls(context=ctx)
            c.ehlo()
            c.login(s["user"], s["password"])
            c.send_message(msg)
    log.info("邮件已发送 -> %s | %s", ", ".join(to), subject)


#  主流程

def gather(cfg, fresh_first_html=None):
    """抓取前 N 页并返回去重后的通知列表。"""
    f = cfg["fetch"]
    first = fresh_first_html if fresh_first_html is not None else fetch(
        page_url(1, 1), retries=f["retries"], timeout=f["timeout"])
    total_pages = get_total_pages(first)
    total_records = get_total_records(first)

    out, seen_ids = [], set()
    for n in range(1, max(1, f["pages"]) + 1):
        if n > total_pages:
            break
        body = first if n == 1 else fetch(page_url(n, total_pages),
                                          retries=f["retries"], timeout=f["timeout"])
        for r in parse_list(body):
            rid = item_id(r)
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            r["id"] = rid
            out.append(r)
        if f["delay"] and n < f["pages"]:
            time.sleep(f["delay"])
    return out, total_pages, total_records


def run(args):
    cfg = load_config(args.config)
    setup_logging(cfg, args.verbose)

    problems = check_config(cfg)
    if problems:
        for p in problems:
            log.error("配置问题：%s", p)
        if not (args.dry_run or args.preview):
            return 2
        log.warning("空跑/预览模式，忽略上述配置问题继续跑")

    state_path = resolve(cfg, "state_file")

    if args.test_mail:
        when = now_cst()
        hits = [({"title": "这是一封测试邮件", "url": BASE, "department": "monitor.py",
                  "category": "测试", "date": f"{when:%Y-%m-%d}"}, "test")]
        send_mail(cfg,
                  f"{cfg['mail']['subject_prefix']} 测试邮件",
                  render_text(hits, when),
                  render_html(hits, when, cfg["mail"]["subject_prefix"]))
        return 0

    try:
        items, total_pages, total_records = gather(cfg)
    except Exception as e:                                   # noqa: BLE001
        log.error("抓取失败，本轮不更新状态，下轮重试：%s", e)
        return 1

    if args.preview:
        # 拿当前配置的关键词去匹配「整个当前列表」，忽略 state —— 调关键词时预览用。
        # 不读 state、不写 state、不发信
        # --dry-run 只匹配新增公告
        hits = [(it, match_item(it, cfg["keywords"], cfg["keyword_match"])) for it in items]
        hits = [(it, kw) for it, kw in hits if kw or cfg["notify_all"]]
        log.info("预览：当前列表 %d 条，命中 %d 条（按 config.json 当前关键词，忽略已读状态）",
                 len(items), len(hits))
        for it, kw in hits:
            log.info("  命中[%s]  %s  %s", kw or "全部", it.get("date", ""), it["title"])
        if not hits:
            log.info("  未命中 —— 关键词可能太窄，或公告使用其他措辞")
        log.info("预览模式：未读写 state.json，不发信")
        return 0

    seen = load_state(state_path)
    first_run = not seen
    new_items = [it for it in items if it["id"] not in seen]

    log.info("列表 %d 条（站点共 %d 条/%d 页），新增 %d 条，已知 %d 条",
             len(items), total_records, total_pages, len(new_items), len(seen))

    if first_run:
        log.info("首次运行：将现有 %d 条全部记为已见，避免刷屏", len(items))
        for it in items:
            seen[it["id"]] = {"t": it["title"], "d": it.get("date", ""),
                              "f": now_cst().isoformat(timespec="seconds")}
        if args.dry_run:
            log.info("--dry-run，不写状态、不发信")
            return 0
        save_state(state_path, seen, cfg["max_seen"])
        if cfg["notify_on_first_run"]:
            when = now_cst()
            hits = [({"title": f"监控已启动，当前列表有 {len(items)} 条公告",
                      "url": BASE, "department": "monitor.py", "category": "启动通知",
                      "date": f"{when:%Y-%m-%d %H:%M}"}, "启动")]
            try:
                send_mail(cfg, f"{cfg['mail']['subject_prefix']} 监控已启动",
                          render_text(hits, when),
                          render_html(hits, when, cfg["mail"]["subject_prefix"]))
            except Exception as e:                           # noqa: BLE001
                log.error("启动通知发送失败（不影响监控业务）：%s", e)
        return 0

    if not new_items:
        if not args.dry_run:
            save_state(state_path, seen, cfg["max_seen"])
        return 0

    hits, misses = [], []
    for it in new_items:
        kw = match_item(it, cfg["keywords"], cfg["keyword_match"])
        if kw or cfg["notify_all"]:
            hits.append((it, kw))
        else:
            misses.append(it)

    log.info("新增 %d 条：命中 %d 条，未命中 %d 条", len(new_items), len(hits), len(misses))

    if args.dry_run:
        for it in new_items:
            kw = match_item(it, cfg["keywords"], cfg["keyword_match"])
            mark = f"命中[{kw}]" if kw else "未命中"
            log.info("  %s  %s  %s", mark, it.get("date", ""), it["title"])
        log.info("--dry-run，不写状态、不发信")
        return 0

    # 未命中的先记为已见；命中的等发信成功再记，发失败下轮会重试
    for it in misses:
        seen[it["id"]] = {"t": it["title"], "d": it.get("date", ""),
                          "f": now_cst().isoformat(timespec="seconds")}

    if hits:
        when = now_cst()
        subject = f"{cfg['mail']['subject_prefix']} {len(hits)} 条新公告 " \
                  f"{when:%m-%d %H:%M}"
        try:
            send_mail(cfg, subject, render_text(hits, when),
                      render_html(hits, when, cfg["mail"]["subject_prefix"]))
        except Exception as e:                               # noqa: BLE001
            log.error("邮件发送失败，这 %d 条不记为已见，下轮重试：%s", len(hits), e)
            save_state(state_path, seen, cfg["max_seen"])     # 至少把未命中的存了
            return 1
        for it, _ in hits:
            seen[it["id"]] = {"t": it["title"], "d": it.get("date", ""),
                              "f": now_cst().isoformat(timespec="seconds")}

    save_state(state_path, seen, cfg["max_seen"])
    return 0


def main():
    ap = argparse.ArgumentParser(description="南信大公告栏新公告监控（单次轮询）")
    ap.add_argument("-c", "--config", default=str(HERE / "config.json"),
                    help="配置文件路径（默认同目录 config.json）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只抓取并打印新增公告的命中判定，不写状态、不发邮件")
    ap.add_argument("--preview", action="store_true",
                    help="用当前关键词匹配整个当前列表（忽略已读状态），调关键词时用；"
                         "不读写状态、不发邮件")
    ap.add_argument("--test-mail", action="store_true", help="发送一封测试邮件后退出")
    ap.add_argument("--check-config", action="store_true", help="只校验配置")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.check_config:
        cfg = load_config(args.config)
        setup_logging(cfg, True)
        problems = check_config(cfg)
        for p in problems:
            log.error("配置问题：%s", p)
        log.info("配置检查%s", "通过" if not problems else f"发现 {len(problems)} 个问题")
        return 1 if problems else 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
