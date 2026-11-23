#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量给已下载目录命名：<主题>_<作者>_<日期>

用法：
    python3 rename_folders.py <媒体根目录>                 # 预览
    python3 rename_folders.py <媒体根目录> --apply         # 执行
    python3 rename_folders.py <媒体根目录> --fetch         # 重新抓页面元数据（不缓存）

原理：目录原名是 video_id → 打开抖音页面取 meta description
      「<标题> - <作者>于YYYYMMDD发布在抖音…」→ 推导主题/作者/日期。
      目录内写 meta.json 保留 video_id，可追溯、可回滚。
"""
import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import naming

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def fetch_meta(vid: str) -> dict:
    """抓单条视频的 og:title / description。

    同时做**页面身份校验**：canonical / location 必须仍指向目标 vid，
    否则页面渲染的是**别的**视频，它的 description 属于另一个作品
    （实测 2026-10-28：并发共用 profile 时会渲染成另一条视频）。
    校验不过就把 meta 清空 —— 宁可目录名退回 video_id，也不能名不副实。
    """
    from playwright.sync_api import sync_playwright
    out = {"og_title": "", "desc": "", "identity_ok": False,
           "canonical": "", "location": ""}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
        pg = ctx.new_page()
        try:
            pg.goto(f"https://www.douyin.com/video/{vid}",
                    wait_until="commit", timeout=60000)
            pg.wait_for_timeout(9000)
            m = pg.evaluate("""() => { const o = {}; document.querySelectorAll('meta')
                .forEach(x => { const k = x.getAttribute('property')
                 || x.getAttribute('name');
                 if (k) o[k] = x.getAttribute('content') || ''; });
                const c = document.querySelector('link[rel="canonical"]');
                o.__canonical = c ? c.href : '';
                o.__location = location.href; return o; }""")
            out["og_title"] = m.get("og:title", "") or ""
            out["desc"] = m.get("description", "") or ""
            out["canonical"] = m.get("__canonical", "") or ""
            out["location"] = m.get("__location", "") or ""
            # 判据同 transcribe.py：og:url 实测为空，不可用
            out["identity_ok"] = (vid in out["canonical"]) or (vid in out["location"])
            if not out["identity_ok"]:
                out["og_title"] = ""
                out["desc"] = ""
        except Exception as e:
            out["err"] = str(e)
        finally:
            pg.close()
            b.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="媒体根目录（含各视频子目录）")
    ap.add_argument("--apply", action="store_true", help="真正执行重命名（默认仅预览）")
    ap.add_argument("--fetch", action="store_true", help="重新抓页面元数据（忽略缓存）")
    ap.add_argument("--cache", default="/tmp/meta16.json", help="元数据缓存文件")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(f"✗ 目录不存在: {root}")
        return 2

    cache = {}
    cf = Path(args.cache)
    if cf.exists() and not args.fetch:
        try:
            cache = json.loads(cf.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    dirs = [d for d in sorted(root.iterdir())
            if d.is_dir() and not d.name.startswith((".", "_"))]

    plan = []
    for d in dirs:
        # 已命名过的目录：meta.json 里有原始 video_id 与元数据，直接复用（幂等）
        local = {}
        mj = d / "meta.json"
        if mj.exists():
            try:
                local = json.loads(mj.read_text(encoding="utf-8"))
            except Exception:
                local = {}

        vid = local.get("video_id") or (d.name if d.name.isdigit() else "")
        title = local.get("title") or ""
        author = local.get("author") or ""
        date = local.get("date") or ""

        meta = {}
        if vid and not title:
            if vid not in cache or args.fetch:
                meta = fetch_meta(vid)
                cache[vid] = meta
            else:
                meta = cache[vid]

        desc = meta.get("desc", "") or ""
        og = meta.get("og_title", "") or ""
        # 页面身份校验失败 → 不采信任何 meta（fetch_meta 已把 desc/og_title 清空，
        # 这里再兜一层：缓存里的旧数据可能是校验失败前抓的）
        identity_ok = meta.get("identity_ok", True) if meta else True
        if meta and not identity_ok:
            desc = og = ""
        if not title:
            title = naming.derive_title(og, desc)
        if not author:
            author = naming.extract_author(desc)
        if not date:
            date = naming.extract_date(desc)

        # 回退：目录里已有的 frontmatter title
        if not title:
            for md in sorted(d.glob("*.md")):
                t = md.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r"^title:\s*(.+)$", t, re.M)
                if m and m.group(1).strip() != vid:
                    title = naming.sanitize(m.group(1).strip(), naming.MAX_TITLE)
                    break

        base = naming.folder_base(title, author, date, fallback=vid or d.name)
        target = root / base
        if target == d:
            continue
        if target.exists():
            base = naming.folder_base(title, author, date,
                                      fallback=(vid or d.name))
            target = naming.unique_dir(root, base)
        plan.append((d, target, vid, title, author, date, identity_ok))

    print(f"{'原目录':<26}{'→ 新目录'}")
    print("-" * 100)
    for d, t, vid, title, author, date, identity_ok in plan:
        print(f"{d.name:<26}{t.name}")
        print(f"{'':<26}  主题={title or '—'}  作者={author or '—'}  日期={date or '—'}  id={vid or '—'}")
        if not identity_ok:
            print(f"{'':<26}  ⚠️ 页面身份校验失败，未采信页面元数据"
                  f"（目录名可能退回 video_id）")

    if not plan:
        print("\n✓ 无需重命名")
        return 0

    if not args.apply:
        print(f"\n（预览模式，共 {len(plan)} 个目录待重命名；加 --apply 执行）")
        return 0

    ok = 0
    for d, t, vid, title, author, date, identity_ok in plan:
        try:
            shutil.move(str(d), str(t))
        except Exception as e:
            print(f"✗ {d.name} → {t.name}: {e}")
            continue
        try:
            naming.write_meta(t, video_id=vid, title=title, author=author,
                              date=date,
                              source=(f"https://www.douyin.com/video/{vid}"
                                      if vid else ""),
                              extra={"renamed_from": d.name,
                                     "renamed_at": __import__("datetime").date.today().isoformat()})
        except Exception as e:
            print(f"⚠️ {t.name} 写 meta.json 失败: {e}")
        print(f"✓ {d.name} → {t.name}")
        ok += 1
    print(f"\n完成 {ok}/{len(plan)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
