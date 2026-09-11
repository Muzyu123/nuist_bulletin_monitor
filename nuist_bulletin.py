#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
爬取南京信息工程大学「信息公告栏」https://bulletin.nuist.edu.cn/ 的通知标题。

列表页规律：
  第 1 页   -> https://bulletin.nuist.edu.cn/
  第 N 页   -> https://bulletin.nuist.edu.cn/index/{TOTAL_PAGES + 1 - N}.htm
  共 274 页，每页 50 条，合计约 13694 条（站点自报）

只抓公开列表页的标题/日期/栏目/发布单位，不进入正文，单线程 + 延时。
"""

import argparse
import csv
import html
import re
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

BASE = "https://bulletin.nuist.edu.cn/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

ITEM_RE = re.compile(r'<li\b[^>]*class="[^"]*news[^"]*"[^>]*>(.*?)</li>', re.S)
TITLE_RE = re.compile(r'<span class="btt">\s*<a\s+href="([^"]+)"[^>]*?title="([^"]*)"', re.S)
TITLE_FALLBACK_RE = re.compile(r'<span class="btt">\s*<a\s+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
DATE_RE = re.compile(r'<span class="news_date">\s*<span[^>]*>([\d\-\./]+)</span>', re.S)
CATE_RE = re.compile(r'<span class="wjj">\s*<a[^>]*>(.*?)</a>', re.S)
DEPT_RE = re.compile(r"fn\('([^']*)'\)", re.S)
TOTAL_RE = re.compile(r'总共\s*(\d+)')
PERPAGE_RE = re.compile(r'每页\s*(?:&nbsp;|\s)*(\d+)')
PAGE_TOTAL_RE = re.compile(r'<span class="p_t">1/(\d+)</span>')
NEWSID_RE = re.compile(r'wbnewsid=(\d+)')


def get_total_pages(page_html):
    """从任意一页列表 HTML 中读出总页数（站点分页会随后台增删记录而变，需每次重新读取）"""
    m = PAGE_TOTAL_RE.search(page_html)
    return int(m.group(1)) if m else 1


def get_total_records(page_html):
    m = TOTAL_RE.search(page_html)
    return int(m.group(1)) if m else 0


def item_id(record):
    """给一条通知生成稳定 ID。覆盖列表页两种 URL 形式，"""
    m = NEWSID_RE.search(record.get("url", ""))
    return "n" + m.group(1) if m else record.get("url", "")


def fetch(url, retries=3, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8-sig", errors="replace")
        except Exception as e:                       # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"抓取失败 {url}: {last}")


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def parse_list(page_html):
    """从一页列表 HTML 中提取所有通知。"""
    rows = []
    for block in ITEM_RE.findall(page_html):
        m = TITLE_RE.search(block) or TITLE_FALLBACK_RE.search(block)
        if not m:
            continue
        href, title = m.group(1), html.unescape(m.group(2)).strip()
        if not title and m.re.groups == 2:
            title = strip_tags(m.group(2))
        if not title:
            continue
        d = DATE_RE.search(block)
        c = CATE_RE.search(block)
        p = DEPT_RE.search(block)
        rows.append({
            "title": title,
            "url": urljoin(BASE, href),
            "date": d.group(1) if d else "",
            "category": strip_tags(c.group(1)).strip("[] ") if c else "",
            "department": html.unescape(p.group(1)).strip() if p else "",
        })
    return rows


def page_url(n, total_pages):
    """第 n 页（从 1 开始）的 URL。"""
    return BASE if n == 1 else urljoin(BASE, f"index/{total_pages + 1 - n}.htm")


def main():
    ap = argparse.ArgumentParser(description="爬取 NUIST 信息公告栏通知标题")
    ap.add_argument("-n", "--pages", type=int, default=1,
                    help="抓取页数，从第 1 页（最新）开始；0 = 全部")
    ap.add_argument("-o", "--out", default="Run/nuist_notices.csv",
                    help="输出 CSV 路径（默认 Run/nuist_notices.csv）")
    ap.add_argument("--delay", type=float, default=0.4, help="每次请求间隔秒数")
    ap.add_argument("--start", type=int, default=1, help="起始页（默认 1 = 最新）")
    args = ap.parse_args()

    first = fetch(page_url(1, 1))
    total_records = get_total_records(first)
    total_pages = get_total_pages(first)

    n_pages = total_pages if args.pages == 0 else min(args.pages, total_pages)
    print(f"站点共 {total_records} 条 / {total_pages} 页，本次抓取第 "
          f"{args.start}–{args.start + n_pages - 1} 页", file=sys.stderr)

    all_rows, seen = [], set()
    for i, n in enumerate(range(args.start, args.start + n_pages)):
        body = first if n == 1 else fetch(page_url(n, total_pages))
        rows = parse_list(body)
        for r in rows:
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            all_rows.append(r)
        print(f"  [{i + 1}/{n_pages}] 第 {n} 页 -> {len(rows)} 条（累计 {len(all_rows)}）",
              file=sys.stderr)
        if args.delay and i + 1 < n_pages:
            time.sleep(args.delay)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["date", "title", "category", "department", "url"])
        w.writeheader()
        w.writerows(all_rows)
    print(f"完成：{len(all_rows)} 条 -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
