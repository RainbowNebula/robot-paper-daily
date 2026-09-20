#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
arXiv 机器人领域论文汇总 - HTML 生成器

修改点：
1. 日期直接从 JSON 中取最近 5 个有数据的 arXiv 日期，不再按日历日倒推。
2. llm_score 为 0 时，会从 llm_summary 再解析一次评分。
   因此历史记录里“【相关性】分数： 5分”即使 llm_score=0，也会正常显示 ★★★★★。
3. 排序也使用修复后的 effective score。
"""

import json
import re
import sys
import argparse
import logging
import os
from typing import Dict, List

# -------------------------- 日志配置 --------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)

# -------------------------- 默认配置 --------------------------

DEFAULT_JSON_PATH = "arxiv_cs_ro_papers_final.json"
DEFAULT_TEMPLATE_PATH = "template.html"
DEFAULT_OUTPUT_PATH = "index.html"
DEFAULT_RECENT_DAYS = 5

LLM_SCORE_PATTERN = re.compile(
    r"(?:【?\s*相关性\s*】?\s*)?"
    r"(?:相关性\s*)?(?:评分|分数)"
    r"\s*[：:]\s*\**\s*([1-5])\s*\**\s*(?:分|/5)?",
    re.IGNORECASE,
)

LLM_SCORE_FALLBACK_PATTERN = re.compile(
    r"分数\s*[：:]\s*\**\s*([1-5])\s*\**\s*分",
    re.IGNORECASE,
)

# -------------------------- 工具函数 --------------------------


def html_escape(text: str, preserve_newlines: bool = False) -> str:
    """转义 HTML 特殊字符。"""
    if not text:
        return ""

    text = str(text)
    text = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )

    if not preserve_newlines:
        text = text.replace("\n", " ")

    return text


def parse_score_from_summary(summary: str) -> int:
    """从 summary 回退解析评分。"""
    if not summary:
        return 0

    for pattern in (
        LLM_SCORE_PATTERN,
        LLM_SCORE_FALLBACK_PATTERN,
    ):
        match = pattern.search(summary)
        if match:
            try:
                score = int(match.group(1))
                if 1 <= score <= 5:
                    return score
            except (TypeError, ValueError):
                pass

    return 0


def get_effective_score(paper: Dict) -> int:
    """
    首选 JSON 的 llm_score；
    llm_score 无效时，从 llm_summary 自动恢复。
    """
    score = paper.get("llm_score", 0)

    try:
        score = int(score)
    except (TypeError, ValueError):
        score = 0

    if 1 <= score <= 5:
        return score

    return parse_score_from_summary(
        str(paper.get("llm_summary", "") or "")
    )


def extract_arxiv_id(url: str) -> str:
    """从 abs / pdf / html 链接中提取 arXiv ID。"""
    if not url:
        return ""

    patterns = [
        r"arxiv\.org/abs/([^/?#]+)",
        r"arxiv\.org/pdf/([^/?#]+)",
        r"arxiv\.org/html/([^/?#]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            arxiv_id = match.group(1).replace(".pdf", "")
            arxiv_id = re.sub(r"v\d+$", "", arxiv_id)
            return arxiv_id

    return ""


def get_first_author(authors_str: str) -> str:
    """提取第一作者姓名。"""
    if not authors_str:
        return "未知作者"

    first = authors_str.split(",")[0].strip()

    if ";" in authors_str:
        first = authors_str.split(";")[0].strip().split(",")[0].strip()

    return first if first else "未知作者"


def get_latest_data_dates(
    date_papers: Dict[str, List[Dict]],
    limit: int = 5,
) -> List[str]:
    """
    取 JSON 中最近 N 个“有数据日期”。

    不再使用 datetime.now() - timedelta(days=i)，
    因此周末不会把 Friday / Thursday 等有效批次挤掉。
    """
    valid_dates = [
        date
        for date, papers in date_papers.items()
        if papers and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)
    ]

    valid_dates.sort(reverse=True)
    return valid_dates[:limit]


def generate_stars_html(score: int) -> str:
    """生成星星评分 HTML。"""
    if not isinstance(score, int) or not (1 <= score <= 5):
        return '<span class="stars">-</span>'

    filled = "★" * score
    empty = "☆" * (5 - score)

    return (
        f'<span class="stars" '
        f'title="相关性评分：{score}/5">{filled}{empty}</span>'
    )


def generate_paper_row(paper: Dict) -> str:
    """生成单篇论文表格行。"""
    title = html_escape(paper.get("title", "未知标题"))
    authors = paper.get("authors", "未知作者")
    first_author = html_escape(get_first_author(authors))

    comment = html_escape(paper.get("comment", ""))
    pdf_link = str(paper.get("pdf_link", "") or "")
    code = str(paper.get("code", "") or "")
    abs_link = str(paper.get("arxiv_abs_link", "") or "")

    llm_summary = str(paper.get("llm_summary", "") or "")
    llm_error = str(paper.get("llm_error", "") or "")

    # 关键修改：旧 llm_score=0 时，从 summary 重新解析
    llm_score = get_effective_score(paper)

    # ---------- 标题 tooltip ----------

    summary_for_tooltip = ""

    if (
        llm_summary
        and llm_summary != "大模型总结失败"
        and llm_summary.strip()
    ):
        clean_summary = re.sub(r"<[^>]+>", "", llm_summary)
        clean_summary = (
            clean_summary
            .replace('"', "'")
            .replace("\n", " ")
        )

        if len(clean_summary) > 200:
            summary_for_tooltip = clean_summary[:200] + "..."
        else:
            summary_for_tooltip = clean_summary

    if summary_for_tooltip:
        title_html = (
            '<span class="title-with-tooltip" '
            f'title="{html_escape(summary_for_tooltip)}">'
            f"{title}</span>"
        )
    else:
        title_html = f"<span>{title}</span>"

    # ---------- 资源 ----------

    resource_parts = []

    if pdf_link:
        resource_parts.append(
            '<a class="resource-tag pdf" '
            f'href="{html_escape(pdf_link)}" '
            'target="_blank" rel="noopener">📄 PDF</a>'
        )

        arxiv_number = extract_arxiv_id(pdf_link)

        if not arxiv_number:
            arxiv_number = extract_arxiv_id(abs_link)

        if arxiv_number:
            alpha_link = (
                "https://www.alphaxiv.org/zh/overview/"
                f"{arxiv_number}"
            )

            resource_parts.append(
                '<a class="resource-tag alphaxiv" '
                f'href="{html_escape(alpha_link)}" '
                'target="_blank" rel="noopener">🧠 AlphaXiv</a>'
            )

    if code.strip():
        code_list = [
            c.strip()
            for c in code.split(",")
            if c.strip()
        ]

        for i, c in enumerate(code_list):
            resource_parts.append(
                '<a class="resource-tag code" '
                f'href="{html_escape(c)}" '
                'target="_blank" rel="noopener">'
                f"🔗 Code{i + 1}</a>"
            )

    if comment:
        resource_parts.append(
            '<details class="resource-comment">'
            "<summary>📝 备注</summary>"
            f"<small>{comment}</small>"
            "</details>"
        )

    resource_html = (
        "<br>".join(resource_parts)
        if resource_parts
        else '<span style="color:#6c757d">-</span>'
    )

    # ---------- 星星 ----------

    stars_html = generate_stars_html(llm_score)

    # ---------- LLM 总结 ----------

    if llm_error.strip():
        summary_content = (
            f"{html_escape(llm_summary, preserve_newlines=True)}"
            "<br>"
            '<small style="color:#dc3545">'
            f"⚠️ {html_escape(llm_error)}"
            "</small>"
        )
    elif (
        llm_summary
        and llm_summary != "大模型总结失败"
        and llm_summary.strip()
    ):
        summary_content = html_escape(
            llm_summary,
            preserve_newlines=True,
        )
    else:
        summary_content = "暂无总结"

    if summary_content != "暂无总结":
        summary_html = (
            '<details class="summary-details" '
            'title="点击查看详情">'
            "<summary>📋</summary>"
            f"<small>{summary_content}</small>"
            "</details>"
        )
    else:
        summary_html = (
            '<span style="color:#6c757d;'
            'font-size:0.85rem">-</span>'
        )

    return f"""<tr>
        <td class="title-cell">{title_html}</td>
        <td class="author-cell">{first_author}</td>
        <td class="resource-cell">{resource_html}</td>
        <td class="score-cell" style="text-align:center">{stars_html}</td>
        <td class="summary-cell">{summary_html}</td>
    </tr>"""


def generate_date_section(
    date: str,
    papers: List[Dict],
    is_latest: bool,
    anchor_id: str,
) -> str:
    """生成单个日期区块。"""
    paper_count = len(papers)
    date_display = f"{date}（{paper_count}篇论文）"

    sorted_papers = sorted(
        papers,
        key=get_effective_score,
        reverse=True,
    )

    table_header = """<table>
        <colgroup>
            <col style="width: 25%">
            <col style="width: 15%">
            <col style="width: 10%">
            <col style="width: 10%">
            <col style="width: 40%">
        </colgroup>
        <thead>
            <tr>
                <th>标题</th>
                <th>作者</th>
                <th>资源</th>
                <th>相关性</th>
                <th>总结</th>
            </tr>
        </thead>
        <tbody>"""

    table_rows = "\n".join(
        generate_paper_row(p)
        for p in sorted_papers
    )

    table_footer = "</tbody></table>"

    if is_latest:
        content_style = "display: block;"
        arrow = "▼"
    else:
        content_style = "display: none;"
        arrow = "▶"

    return f'<div class="date-section" id="{anchor_id}">
    <div class="date-header">
        <span>{date_display}</span>
        <span class="arrow">{arrow}</span>
    </div>
    <div class="date-content" style="{content_style}">
        {table_header}
        {table_rows}
        {table_footer}
    </div>
</div>'


def generate_nav_links(
    valid_dates: List[str],
    date_papers: Dict,
) -> str:
    """生成顶部日期导航。"""
    links = []

    for date in valid_dates:
        count = len(date_papers.get(date, []))
        display = f"{date}（{count}篇）"
        anchor_id = f"date-{date.replace('-', '')}"

        links.append(
            f'<a href="#{anchor_id}">{display}</a>'
        )

    return " | ".join(links)


def json_to_html(
    json_path: str,
    output_path: str,
    template_path: str,
) -> bool:
    """读取 JSON + template.html，生成 index.html。"""

    # 1. JSON
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            date_papers = json.load(f)

        if not date_papers:
            logging.error("JSON 文件中无论文数据")
            return False

        logging.info(
            "✓ 已加载 %d 个日期的数据",
            len(date_papers),
        )

    except FileNotFoundError:
        logging.error("JSON 文件不存在：%s", json_path)
        return False

    except json.JSONDecodeError as e:
        logging.error("JSON 解析失败：%s", e)
        return False

    except Exception as e:
        logging.error("读取 JSON 失败：%s", e)
        return False

    # 2. template
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template = f.read()

        logging.info("✓ 已加载模板：%s", template_path)

    except Exception as e:
        logging.error("读取模板失败：%s", e)
        return False

    # 3. 最近 N 个实际有数据的 arXiv 日期
    valid_dates = get_latest_data_dates(
        date_papers,
        DEFAULT_RECENT_DAYS,
    )

    if not valid_dates:
        logging.warning("⚠️ JSON 中没有有效日期数据")

    latest_date = valid_dates[0] if valid_dates else None

    total_papers = sum(
        len(date_papers[d])
        for d in valid_dates
    )

    logging.info(
        "✓ 将生成 %d 篇论文，最近 %d 个 arXiv 批次",
        total_papers,
        len(valid_dates),
    )

    # 4. nav
    nav_links_html = (
        generate_nav_links(valid_dates, date_papers)
        if valid_dates
        else '<span style="color:#6c757d">暂无数据</span>'
    )

    # 5. date sections
    date_sections_html = []

    for date in valid_dates:
        anchor_id = f"date-{date.replace('-', '')}"
        is_latest = (date == latest_date)

        date_sections_html.append(
            generate_date_section(
                date,
                date_papers[date],
                is_latest,
                anchor_id,
            )
        )

    # 6. template replacements
    replacements = {
        "total_papers": str(total_papers),
        "nav_links": nav_links_html,
        "date_sections": "\n".join(date_sections_html),
        "latest_anchor_id": (
            f"date-{latest_date.replace('-', '')}"
            if latest_date
            else ""
        ),
    }

    try:
        html_content = template

        for key, value in replacements.items():
            placeholder = "{" + key + "}"
            html_content = html_content.replace(
                placeholder,
                str(value),
            )

        logging.info("✓ 模板变量替换完成")

    except Exception as e:
        logging.error("模板替换失败：%s", e)
        return False

    # 7. save
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        logging.info("✓ HTML 页面已生成：%s", output_path)
        return True

    except Exception as e:
        logging.error("保存 HTML 失败：%s", e)
        return False


# -------------------------- CLI --------------------------

def main():
    parser = argparse.ArgumentParser(
        description="arXiv 机器人论文汇总 - HTML 生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--json",
        default=DEFAULT_JSON_PATH,
        help=f"JSON 数据文件路径 (默认：{DEFAULT_JSON_PATH})",
    )

    parser.add_argument(
        "--template",
        default=DEFAULT_TEMPLATE_PATH,
        help=f"HTML 模板文件路径 (默认：{DEFAULT_TEMPLATE_PATH})",
    )

    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_PATH,
        help=f"输出 HTML 文件路径 (默认：{DEFAULT_OUTPUT_PATH})",
    )

    args = parser.parse_args()

    logging.info("🚀 开始生成 HTML 页面...")
    logging.info("数据源：%s", args.json)
    logging.info("模板：%s", args.template)
    logging.info("输出：%s", args.output)

    success = json_to_html(
        args.json,
        args.output,
        args.template,
    )

    if success:
        abs_path = os.path.abspath(args.output)
        logging.info(
            "✨ 生成成功：file:///%s",
            abs_path,
        )
        return 0

    logging.error("❌ 生成失败，请检查日志")
    return 1


if __name__ == "__main__":
    sys.exit(main())
