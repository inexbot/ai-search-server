#!/usr/bin/env python3
"""
KB 数据迁移脚本
================
把现有 KB 里所有的相对路径替换为 doc.inexbot.com 完整 URL，
重新生成 md 文件和 index.json，输出 url_map.json。

使用现有 raw HTML，不重新爬网站。

运行方式：
  python3 migrate.py [--force]

--force : 强制重新处理所有文件（默认跳过已有 md）
"""

import os
import re
import sys
import json
import time
import urllib.parse
from pathlib import Path

# 添加 crawler 目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from crawl import (
    BASE_URL, KB_DIR, RAW_DIR, MD_DIR, INDEX_PATH, URL_MAP_PATH, META_PATH,
    make_links_absolute, extract_content, build_search_index,
    slugify_path
)

# ── 步骤1：从 index.json 构建 path → url 映射 ────────────────────────────────

def build_url_map_from_index(index_path: str) -> dict:
    """从 index.json 读取所有 path，构建 url_map"""
    with open(index_path, encoding="utf-8") as f:
        index = json.load(f)

    url_map = {}
    for path in index.keys():
        clean = path.strip("/")
        if not clean:
            clean = "index"
        clean = re.sub(r'\.html$', '', clean)
        encoded = urllib.parse.quote(clean, safe="/")
        url = f"{BASE_URL}/{encoded}.html"
        url_map[path] = url
        url_map[f"/{clean}"] = url

    return url_map, index


# ── 步骤2：建立 filename → path 映射 ───────────────────────────────────────

def build_filename_to_path_index(index: dict) -> dict:
    """从 index.json 建立 filename → path 的反向索引（支持 URL 解码）"""
    mapping = {}
    for path in index.keys():
        slug = slugify_path(path)
        mapping[slug] = path
        mapping[f"{slug}.html"] = path
        mapping[f"{slug}.md"] = path
        # URL 编码版本（有些 raw 文件名是 URL 编码的）
        encoded_slug = urllib.parse.quote(path.lstrip("/"), safe="-")
        mapping[encoded_slug] = path
        mapping[urllib.parse.quote(path.lstrip("/").replace("/", "-"), safe="")] = path
    return mapping


def slug_to_path(slug: str, fname_to_path: dict, raw_dir: str) -> str:
    """
    从 raw 文件名反查 index.json 中的 path。
    尝试：直接匹配 → URL 解码 → 去除 .html 末尾
    """
    # 原始
    if slug in fname_to_path:
        return fname_to_path[slug]

    # URL 解码（处理 %E4%BA%A7... 格式）
    decoded = urllib.parse.unquote(slug)
    if decoded in fname_to_path:
        return fname_to_path[decoded]

    # 去除 .html 再解码
    no_ext = slug.replace(".html", "").replace(".md", "")
    decoded2 = urllib.parse.unquote(no_ext)
    if decoded2 in fname_to_path:
        return fname_to_path[decoded2]

    # 在 fname_to_path 键中搜索末尾匹配（因为 raw 命名可能带.html）
    for key, val in fname_to_path.items():
        if key.endswith(slug) or key.endswith(no_ext):
            return val

    return None


# ── 步骤3：迁移单个文件 ─────────────────────────────────────────────────────

def migrate_file(raw_file: str, url_map: dict, fname_to_path: dict, force: bool = False) -> dict:
    """处理单个 raw HTML 文件，返回 page_data"""
    basename = os.path.basename(raw_file)
    slug = basename.replace(".html", "")

    # 查找对应 path
    path = slug_to_path(slug, fname_to_path, RAW_DIR)
    if not path:
        return None

    md_file = os.path.join(MD_DIR, slug + ".md")

    # 增量：已有则跳过（除非 force）
    if not force and os.path.exists(md_file):
        # 但仍需返回 page_data（用于重建索引）
        try:
            with open(md_file, encoding="utf-8") as f:
                md_content = f.read()
            # 快速提取标题
            h_match = re.search(r"^#\s+(.+)$", md_content, re.MULTILINE)
            title = h_match.group(1).strip() if h_match else ""
            desc_match = re.search(r"^>\s*(.+)$", md_content, re.MULTILINE)
            description = desc_match.group(1).strip() if desc_match else ""
            return {
                "path": path,
                "title": title,
                "description": description,
                "content_md": md_content,
                "url": url_map.get(path, f"{BASE_URL}{path}.html"),
            }
        except Exception:
            pass

    # 读取 HTML
    with open(raw_file, encoding="utf-8") as f:
        html = f.read()

    # 替换链接为绝对 URL
    page_url = url_map.get(path, f"{BASE_URL}{path}.html")
    abs_html = make_links_absolute(html, url_map, page_url)

    # 保存修复后的 raw HTML
    with open(raw_file, "w", encoding="utf-8") as f:
        f.write(abs_html)

    # 提取正文并转为 md
    page_data = extract_content(abs_html, path)

    # 保存 md
    md_content = f"# {page_data['title']}\n\n"
    if page_data["description"]:
        md_content += f">{page_data['description']}\n\n"
    md_content += page_data["content_md"]

    with open(md_file, "w", encoding="utf-8") as f:
        f.write(md_content)

    page_data["url"] = page_url
    return page_data


# ── 步骤4：主迁移流程 ───────────────────────────────────────────────────────

def migrate(force=False):
    print(f"KB 目录: {KB_DIR}")
    print(f"RAW: {RAW_DIR}")
    print(f"MD:  {MD_DIR}")
    print()

    # 步骤 A：建立 url_map
    print("[1/4] 从 index.json 构建 url_map...")
    url_map, old_index = build_url_map_from_index(INDEX_PATH)
    print(f"  url_map: {len(url_map)} 条映射")

    # 同时构建反向索引：filename → path
    fname_to_path = build_filename_to_path_index(old_index)
    print(f"  filename → path: {len(fname_to_path)} 条")

    # 保存 url_map（新增）
    with open(URL_MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(url_map, f, ensure_ascii=False, indent=2)
    print(f"  已保存 url_map.json")

    # 步骤 B：遍历 raw HTML，修复链接并重建 md
    print(f"\n[2/4] 遍历 raw HTML，修复链接...")
    raw_files = sorted([
        os.path.join(RAW_DIR, f)
        for f in os.listdir(RAW_DIR)
        if f.endswith(".html")
    ])
    print(f"  共 {len(raw_files)} 个 raw 文件")

    pages = []
    skipped = 0
    failed = []

    for i, raw_file in enumerate(raw_files, 1):
        basename = os.path.basename(raw_file)

        # 查找对应 path
        slug = basename.replace(".html", "")
        path = slug_to_path(slug, fname_to_path, RAW_DIR)

        if not path:
            print(f"  [{i}/{len(raw_files)}] ✗ 找不到 path: {basename}")
            failed.append(basename)
            continue

        md_file = os.path.join(MD_DIR, slug + ".md")
        if not force and os.path.exists(md_file):
            skipped += 1
            # 仍返回 page_data 用于索引重建
            try:
                with open(md_file, encoding="utf-8") as f:
                    md_content = f.read()
                h_match = re.search(r"^#\s+(.+)$", md_content, re.MULTILINE)
                title = h_match.group(1).strip() if h_match else ""
                desc_match = re.search(r"^>\s*(.+)$", md_content, re.MULTILINE)
                description = desc_match.group(1).strip() if desc_match else ""
                pages.append({
                    "path": path,
                    "title": title,
                    "description": description,
                    "content_md": md_content,
                    "url": url_map.get(path, f"{BASE_URL}{path}.html"),
                })
            except Exception:
                pass
            if i % 20 == 0:
                print(f"  进度 {i}/{len(raw_files)}（跳过 {skipped} 个已有文件）")
            continue

        try:
            page_data = migrate_file(raw_file, url_map, fname_to_path, force=True)
            if page_data:
                pages.append(page_data)
                print(f"  [{i}/{len(raw_files)}] ✓ {path}")
            else:
                failed.append(basename)
        except Exception as e:
            print(f"  [{i}/{len(raw_files)}] ✗ {basename}: {e}")
            failed.append(basename)

        if i % 10 == 0:
            print(f"  进度 {i}/{len(raw_files)}")

    print(f"\n  处理完成: {len(pages)} 成功, {len(failed)} 失败, {skipped} 跳过")

    # 步骤 C：重建 index.json
    print(f"\n[3/4] 重建 index.json...")
    new_index = build_search_index(pages)
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(new_index, f, ensure_ascii=False, indent=2)
    print(f"  已保存，共 {len(new_index)} 条记录")

    # 步骤 D：更新 meta.yaml
    print(f"\n[4/4] 更新 meta.yaml...")
    import datetime, yaml
    meta = {
        "version": datetime.date.today().strftime("%Y%m%d"),
        "migrated_at": datetime.datetime.now().isoformat(),
        "pages_total": len(pages),
        "pages_failed": len(failed),
        "pages_skipped": skipped,
        "failed_files": failed,
    }
    with open(META_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(meta, f, allow_unicode=True, default_flow_style=False)

    print(f"\n✓ 迁移完成！")
    print(f"  成功: {len(pages)}")
    print(f"  失败: {len(failed)}")
    print(f"  跳过: {skipped}")
    if failed:
        print(f"\n失败文件:")
        for f in failed[:10]:
            print(f"  - {f}")
    print(f"\n输出文件:")
    print(f"  url_map.json: {URL_MAP_PATH}")
    print(f"  index.json:   {INDEX_PATH}")
    print(f"  meta.yaml:    {META_PATH}")

    return pages, new_index, meta


if __name__ == "__main__":
    force = "--force" in sys.argv
    if force:
        print("强制模式：重新处理所有文件\n")
    migrate(force=force)
