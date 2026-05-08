#!/usr/bin/env python3
"""
doc.inexbot.com 爬虫
=====================
使用 Playwright headless browser 抓取 VitePress SPA，
替换所有相对路径为完整 URL，存入 KB。

用法：
  python3 crawl.py              # 全量爬取（首次）
  python3 crawl.py --diff       # 增量：只爬有变化的页面
  python3 crawl.py --page URL  # 爬单个页面
"""

import os
import re
import sys
import json
import time
import urllib.parse
from pathlib import Path
from datetime import datetime

from playwright.sync_api import sync_playwright

# ── 路径配置 ────────────────────────────────────────────────────────────────

KB_DIR    = Path.home() / ".hermes" / "kb" / "inexbot"
RAW_DIR   = KB_DIR / "raw"
MD_DIR    = KB_DIR / "md"
INDEX_F   = KB_DIR / "index.json"
URL_MAP_F = KB_DIR / "url_map.json"
META_F    = KB_DIR / "meta.yaml"

BASE_URL = "https://doc.inexbot.com"

# ── Playwright 爬取 ─────────────────────────────────────────────────────────

def fetch_page(url: str, timeout: int = 30000) -> str:
    """用 Playwright 打开页面，等待 JS 渲染，返回完整 HTML"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=timeout)
        # 额外等一下动态内容
        page.wait_for_timeout(2000)
        html = page.content()
        browser.close()
    return html


def parse_sitemap() -> list[str]:
    """从 sitemap.xml 获取所有页面 URL"""
    import urllib.request
    req = urllib.request.Request(
        f"{BASE_URL}/sitemap.xml",
        headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        xml = resp.read().decode("utf-8")

    urls = re.findall(r"<loc>(https?://[^<]+)</loc>", xml)
    # 只保留 doc.inexbot.com 的，排除 index
    urls = [u for u in urls if u.startswith(BASE_URL) and "/index" not in u]
    return urls


def page_path(url: str) -> str:
    """从 URL 反推 path：https://doc.inexbot.com/产品资料/控制器/T30.html → /产品资料/控制器/T30"""
    path = url.replace(BASE_URL, "").rstrip("/")
    path = re.sub(r"\.html?$", "", path)
    if not path:
        path = "/"
    return path


def slugify_path(path: str) -> str:
    """/产品资料/控制器/T30示教器 → 产品资料-控制器-T30示教器"""
    return path.strip("/").replace("/", "-")


def make_absolute(html: str, page_url: str) -> str:
    """把所有相对路径替换为完整 URL"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(["a", "area"]):
        href = tag.get("href", "")
        if not href or href.startswith(("http://", "https://", "#", "javascript:", "data:", "mailto:")):
            continue
        if href.startswith("/"):
            tag["href"] = BASE_URL + href
        else:
            tag["href"] = urllib.parse.urljoin(page_url, href)

    for tag in soup.find_all(["img", "video", "audio", "source", "track", "embed", "iframe"]):
        src = tag.get("src", "")
        if not src or src.startswith(("http://", "https://", "data:", "javascript:")):
            continue
        if src.startswith("/"):
            tag["src"] = BASE_URL + src
        else:
            tag["src"] = urllib.parse.urljoin(page_url, src)

    # meta og:image 等
    for tag in soup.find_all(["meta"]):
        if tag.get("property") == "og:image":
            content = tag.get("content", "")
            if content and not content.startswith("http"):
                if content.startswith("/"):
                    tag["content"] = BASE_URL + content
                else:
                    tag["content"] = urllib.parse.urljoin(page_url, content)

    return str(soup)


def extract_content(html: str, path: str) -> dict:
    """从 HTML 提取正文并转为 Markdown"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    # 找内容区：VitePress 用 <main> 下的 article，main 优先级最高
    article = (
        soup.find("main")
        or soup.find("article", class_="page")
        or soup.find("div", class_="vp-doc")
        or soup.find("div", class_="content")
    )

    if not article:
        # 兜底：body > div#app > div
        app = soup.find("div", id="app")
        if app:
            article = app
        else:
            article = soup.body if soup.body else soup

    # 清理：去掉导航、侧边栏、评论、脚本
    for sel in [
        "nav", "aside", "footer", "header",
        ".nav", ".sidebar", ".comments", ".social-share",
        "[role='navigation']", "[role='complementary']",
        "script", "style", "noscript",
    ]:
        for el in article.select(sel):
            el.decompose()

    # 标题
    h1 = article.find(["h1", "h2"])
    title = h1.get_text(strip=True) if h1 else path.strip("/").split("/")[-1]

    # 描述：取第一段非空 p 或 > 引用
    description = ""
    for el in article.find_all(["p", "blockquote"]):
        txt = el.get_text(strip=True)
        if len(txt) > 30:
            description = txt[:200]
            break

    # 转 md
    import html2text
    h = html2text.HTML2Text()
    h.body_width = 0
    h.ignore_images = False
    h.ignore_links = False
    h.ignore_emphasis = False
    h.unicode_snob = True
    h.escape_snob = ["html"]

    md_raw = h.handle(str(article))

    # 清理 md 里的残留相对路径（理论上 make_absolute 已处理完，这里加一层兜底）
    def fix_md_links(md: str) -> str:
        # 处理 ![alt](/assets/...)  和  [text](/path/...)
        md = re.sub(
            r'!\[([^\]]*)\]\((/[^\(\)\n]+)\)',
            lambda m: f"![{m.group(1)}](https://doc.inexbot.com/{m.group(2).lstrip('/')})",
            md
        )
        md = re.sub(
            r'(?<!!)\[([^\]]+)\]\((/[^\(\)\n]+)\)',
            lambda m: f"[{m.group(1)}](https://doc.inexbot.com/{m.group(2).lstrip('/')})",
            md
        )
        return md

    md_content = fix_md_links(md_raw)

    return {
        "title": title,
        "description": description,
        "content_md": md_content,
    }


def build_search_index(pages: list) -> dict:
    """从页面列表构建 index.json"""
    import jieba
    jieba.setLogLevel("WARNING")
    jieba.initialize()

    index = {}
    for page in pages:
        text = f"{page['title']} {page['description']} {page['content_md']}"
        words = list(jieba.cut(text))
        # 简单关键词：取标题词 + 描述词（去停用词后）
        stop = {"的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一", "一个",
                "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好",
                "自己", "这", "那", "他", "她", "它", "们", "之", "与", "及", "或", "等", "为",
                "而", "且", "但", "以", "于", "被", "由", "对", "中", "或", "其", "所", "用",
                "于", "将", "可", "如", "因", "此", "能", "而", "则", "又", "及", "与", "或",
                "个", "来", "出", "后", "里", "得", "还", "得", "把", "让", "向", "往",
                "如", "若", "使", "则", "即", "非", "与", "和", "及", "或", "而", "但",
                "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "·", "—", "–", "–"}
        words = [w for w in words if len(w) >= 2 and w not in stop]
        index[page["path"]] = {
            "title": page["title"],
            "description": page["description"],
            "keywords": list(set(words[:30])),
            "url": page["url"],
        }

    return index


# ── 主爬取流程 ─────────────────────────────────────────────────────────────

def crawl_all():
    """全量爬取"""
    print(f"目标网站: {BASE_URL}")
    print(f"KB 目录:  {KB_DIR}")
    print()

    # 确保目录存在
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    MD_DIR.mkdir(parents=True, exist_ok=True)

    # 解析 sitemap
    print("[1/5] 解析 sitemap.xml ...")
    urls = parse_sitemap()
    print(f"  共 {len(urls)} 个页面")
    print()

    # 构建 url_map
    url_map = {}
    for url in urls:
        path = page_path(url)
        url_map[path] = url
        slug = slugify_path(path)
        url_map[slug] = url

    # 保存 url_map
    with open(URL_MAP_F, "w", encoding="utf-8") as f:
        json.dump(url_map, f, ensure_ascii=False, indent=2)
    print(f"[2/5] url_map.json 已保存（{len(url_map)} 条）")

    # 遍历爬取
    pages = []
    failed = []
    total = len(urls)

    print(f"[3/5] 开始爬取 {total} 个页面 ...")
    for i, url in enumerate(urls, 1):
        path = page_path(url)
        slug = slugify_path(path)
        raw_file = RAW_DIR / f"{slug}.html"
        md_file = MD_DIR / f"{slug}.md"

        # 增量：已爬且文件未变则跳过
        if raw_file.exists() and md_file.exists():
            # 读已有内容验证
            try:
                with open(raw_file, encoding="utf-8") as f:
                    existing = f.read()
                if f"doc.inexbot.com" in existing and "content" in existing:
                    print(f"  [{i}/{total}] 跳过（已存在）: {path}")
                    # 仍加入 pages 供索引重建
                    with open(md_file, encoding="utf-8") as f:
                        md_content = f.read()
                    h_match = re.search(r"^#\s+(.+)$", md_content, re.MULTILINE)
                    title = h_match.group(1).strip() if h_match else ""
                    desc_match = re.search(r"^>\s*(.+)$", md_content, re.MULTILINE)
                    description = desc_match.group(1).strip() if desc_match else ""
                    pages.append({
                        "path": path, "title": title,
                        "description": description,
                        "content_md": md_content,
                        "url": url,
                    })
                    continue
            except Exception:
                pass

        try:
            print(f"  [{i}/{total}] 爬取: {url}")
            html = fetch_page(url)
            abs_html = make_absolute(html, url)

            # 保存 raw HTML
            with open(raw_file, "w", encoding="utf-8") as f:
                f.write(abs_html)

            # 提取正文
            page_data = extract_content(abs_html, path)
            page_data["path"] = path
            page_data["url"] = url
            pages.append(page_data)

            # 保存 md
            md_content = f"# {page_data['title']}\n\n"
            if page_data["description"]:
                md_content += f">{page_data['description']}\n\n"
            md_content += page_data["content_md"]
            with open(md_file, "w", encoding="utf-8") as f:
                f.write(md_content)

            time.sleep(0.5)  # 礼貌限速

        except Exception as e:
            print(f"  [{i}/{total}] ✗ 失败: {url} — {e}")
            failed.append(url)

        if i % 10 == 0:
            print(f"  进度 {i}/{total}，成功 {len(pages)}，失败 {len(failed)}")

    print()
    print(f"  爬取完成：{len(pages)} 成功，{len(failed)} 失败")

    # 重建索引
    print(f"\n[4/5] 重建 index.json ...")
    index = build_search_index(pages)
    with open(INDEX_F, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    print(f"  index.json 已保存（{len(index)} 条）")

    # 更新 meta
    print(f"\n[5/5] 更新 meta.yaml ...")
    import yaml
    meta = {
        "version": datetime.now().strftime("%Y%m%d"),
        "updated_at": datetime.now().isoformat(),
        "pages_total": len(pages),
        "pages_failed": len(failed),
        "failed_urls": failed[:20],
    }
    with open(META_F, "w", encoding="utf-8") as f:
        yaml.safe_dump(meta, f, allow_unicode=True, default_flow_style=False)

    print(f"\n✓ 完成！")
    print(f"  成功: {len(pages)}")
    print(f"  失败: {len(failed)}")
    if failed:
        for u in failed[:5]:
            print(f"    - {u}")


def crawl_page(url: str):
    """爬单个页面"""
    print(f"爬取: {url}")
    try:
        html = fetch_page(url)
        path = page_path(url)
        slug = slugify_path(path)
        abs_html = make_absolute(html, url)

        raw_file = RAW_DIR / f"{slug}.html"
        with open(raw_file, "w", encoding="utf-8") as f:
            f.write(abs_html)
        print(f"  保存: {raw_file}")

        page_data = extract_content(abs_html, path)
        page_data["path"] = path
        page_data["url"] = url

        md_content = f"# {page_data['title']}\n\n>{page_data['description']}\n\n{page_data['content_md']}"
        md_file = MD_DIR / f"{slug}.md"
        with open(md_file, "w", encoding="utf-8") as f:
            f.write(md_content)
        print(f"  保存: {md_file}")

        print(f"\n标题: {page_data['title']}")
        print(f"描述: {page_data['description'][:100]}")
        print(f"路径: {path}")
        print(f"URL:  {url}")

    except Exception as e:
        print(f"✗ 失败: {e}")


if __name__ == "__main__":
    if "--page" in sys.argv:
        idx = sys.argv.index("--page")
        crawl_page(sys.argv[idx + 1])
    elif "--diff" in sys.argv:
        print("增量模式（当前行为同全量，需要自行判断哪些页面变了）")
        crawl_all()
    else:
        crawl_all()
