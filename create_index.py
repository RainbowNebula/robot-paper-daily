#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
arXiv 机器人领域论文汇总 - HTML 生成器
优化版：标题无虚线，总结列更大，只用 icon
"""

import json
import re
import sys
import argparse
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import quote

# -------------------------- 日志配置 --------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)

# -------------------------- 默认配置 --------------------------
DEFAULT_JSON_PATH = "arxiv_cs_ro_papers_final.json"
DEFAULT_TEMPLATE_PATH = "template.html"
DEFAULT_OUTPUT_PATH = "index.html"
DEFAULT_RECENT_DAYS = 5

# -------------------------- 工具函数 --------------------------

def html_escape(text: str, preserve_newlines: bool = False) -> str:
    """转义 HTML 特殊字符"""
    if not text:
        return ""
    text = str(text)
    text = (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&#39;"))
    if not preserve_newlines:
        text = text.replace("\n", " ")
    return text


def extract_arxiv_id(url: str) -> str:
    """从 arXiv 链接中提取论文 ID"""
    if not url:
        return ""
    patterns = [
        r'arxiv\.org/abs/([\w\.]+)',
        r'arxiv\.org/pdf/([\w\.]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            return match.group(1).replace(".pdf", "")
    return ""


def get_first_author(authors_str: str) -> str:
    """提取第一作者姓名"""
    if not authors_str:
        return "未知作者"
    first = authors_str.split(",")[0].strip()
    if "et al" in authors_str.lower():
        return first
    if ";" in authors_str:
        first = authors_str.split(";")[0].strip().split(",")[0].strip()
    return first if first else "未知作者"


def get_recent_dates(limit: int = 5) -> List[str]:
    """获取最近 N 天的日期列表"""
    dates = []
    for i in range(limit):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        dates.append(date)
    return dates


def generate_stars_html(score: int) -> str:
    """生成星星评分 HTML"""
    if not isinstance(score, int) or not (1 <= score <= 5):
        return '<span class="stars">-</span>'
    filled = "★" * score
    empty = "☆" * (5 - score)
    return f'<span class="stars" title="相关性评分：{score}/5">{filled}{empty}</span>'


# 🔧【修改】生成论文行：标题无虚线，总结列只用 icon
def generate_paper_row(paper: Dict) -> str:
    """生成单篇论文的表格行 HTML"""
    title = html_escape(paper.get("title", "未知标题"))
    authors = paper.get("authors", "未知作者")
    first_author = html_escape(get_first_author(authors))
    comment = html_escape(paper.get("comment", ""))
    pdf_link = paper.get("pdf_link", "")
    code = paper.get("code", "")
    abs_link = paper.get("arxiv_abs_link", "")
    llm_summary = paper.get("llm_summary", "")
    llm_score = paper.get("llm_score", 0)
    llm_error = paper.get("llm_error", "")
    
    # 🔹 准备悬浮总结（用于 title 属性）
    summary_for_tooltip = ""
    if llm_summary and llm_summary != "大模型总结失败" and llm_summary.strip():
        clean_summary = re.sub(r'<[^>]+>', '', llm_summary)
        clean_summary = clean_summary.replace('"', "'").replace('\n', ' ')
        summary_for_tooltip = clean_summary[:200] + "..." if len(clean_summary) > 200 else clean_summary
    
    # 🔹 标题 HTML（无虚线，正常显示）
    if summary_for_tooltip:
        title_html = f'<span class="title-with-tooltip" title="{html_escape(summary_for_tooltip)}">{title}</span>'
    else:
        title_html = f'<span>{title}</span>'
    
    # 🔹 资源列：PDF + Code + Comment（带文字）
    resource_parts = []
    
    if pdf_link:
        resource_parts.append(
            f'<a class="resource-tag pdf" href="{html_escape(pdf_link)}" target="_blank" rel="noopener">📄 PDF</a>'
        )
        arxiv_number = pdf_link.split("/")[-1]
        alpha_link = f"https://www.alphaxiv.org/zh/overview/{arxiv_number}" # 2603.24844
        resource_parts.append(
            f'<a class="resource-tag alphaxiv" href="{html_escape(alpha_link)}" target="_blank" rel="noopener">🧠 AlphaXiv</a>'
        )
    
    if code and code.strip():
        code_list = [c.strip() for c in code.split(",") if c.strip()]
        for i, c in enumerate(code_list):
            resource_parts.append(
                f'<a class="resource-tag code" href="{html_escape(c)}" target="_blank" rel="noopener">🔗 Code{i+1}</a>'
            )
    
    if comment and comment != "":
        resource_parts.append(
            f'<details class="resource-comment">'
            f'<summary>📝 备注</summary>'
            f'<small>{comment}</small>'
            f'</details>'
        )
    
    resource_html = "<br>".join(resource_parts) if resource_parts else '<span style="color:#6c757d">-</span>'
    
    # 🔹 评分
    stars_html = generate_stars_html(llm_score)
    
    # 🔹 LLM 总结列（只用 icon📋）
    if llm_error and llm_error.strip():
        summary_content = f'{html_escape(llm_summary)}<br><small style="color:#dc3545">⚠️ {html_escape(llm_error)}</small>'
    elif llm_summary and llm_summary != "大模型总结失败" and llm_summary.strip():
        summary_content = html_escape(llm_summary, preserve_newlines=True)
    else:
        summary_content = "暂无总结"
    
    if summary_content and summary_content != "暂无总结":
        # 🔹 只用 icon，无文字
        summary_html = f'<details class="summary-details" title="点击查看详情"><summary>📋</summary><small>{summary_content}</small></details>'
    else:
        summary_html = '<span style="color:#6c757d;font-size:0.85rem">-</span>'
    
    # 🔹 表格行：5 列
    return f'''<tr>
        <td class="title-cell">{title_html}</td>
        <td class="author-cell">{first_author}</td>
        <td class="resource-cell">{resource_html}</td>
        <td class="score-cell" style="text-align:center">{stars_html}</td>
        <td class="summary-cell">{summary_html}</td>
    </tr>'''


def generate_date_section(date: str, papers: List[Dict], is_latest: bool, anchor_id: str) -> str:
    """生成单个日期区块的 HTML"""
    paper_count = len(papers)
    date_display = f"{date}（{paper_count}篇论文）"
    
    sorted_papers = sorted(papers, key=lambda x: x.get("llm_score", 0) or 0, reverse=True)
    
    # 🔹 表格头：5 列
    table_header = '''<table>
        <thead>
            <tr>
                <th>标题</th>
                <th>作者</th>
                <th>资源</th>
                <th style="text-align:center">相关性</th>
                <th>总结</th>
            </tr>
        </thead>
        <tbody>'''
    
    table_rows = "\n".join(generate_paper_row(p) for p in sorted_papers)
    table_footer = "</tbody></table>"
    
    if is_latest:
        content_style = "display: block;"
        arrow = "▼"
    else:
        content_style = "display: none;"
        arrow = "▶"
    
    return f'''<div class="date-section" id="{anchor_id}">
    <div class="date-header">
        <span>{date_display}</span>
        <span class="arrow">{arrow}</span>
    </div>
    <div class="date-content" style="{content_style}">
        {table_header}
        {table_rows}
        {table_footer}
    </div>
</div>'''


def generate_nav_links(valid_dates: List[str], date_papers: Dict) -> str:
    """生成顶部日期导航链接 HTML"""
    links = []
    for date in valid_dates:
        count = len(date_papers.get(date, []))
        display = f"{date}（{count}篇）"
        anchor_id = f"date-{date.replace('-', '')}"
        links.append(f'<a href="#{anchor_id}">{display}</a>')
    return " | ".join(links)


def json_to_html(json_path: str, output_path: str, template_path: str) -> bool:
    """主函数：读取 JSON，填充模板，生成 HTML"""
    
    # 1. 读取 JSON 数据
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            date_papers = json.load(f)
        if not date_papers:
            logging.error("JSON 文件中无论文数据")
            return False
        logging.info(f"✓ 已加载 {len(date_papers)} 个日期的数据")
    except FileNotFoundError:
        logging.error(f"JSON 文件不存在：{json_path}")
        return False
    except json.JSONDecodeError as e:
        logging.error(f"JSON 解析失败：{e}")
        return False
    except Exception as e:
        logging.error(f"读取 JSON 失败：{e}")
        return False
    
    # 2. 读取 HTML 模板
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template = f.read()
        logging.info(f"✓ 已加载模板：{template_path}")
    except Exception as e:
        logging.error(f"读取模板失败：{e}")
        return False
    
    # 3. 筛选最近几天有数据的日期
    recent_dates = get_recent_dates(DEFAULT_RECENT_DAYS)
    valid_dates = [d for d in recent_dates if d in date_papers and date_papers[d]]
    
    if not valid_dates:
        logging.warning(f"⚠️ 最近 {DEFAULT_RECENT_DAYS} 天内无有效论文数据")
        valid_dates = []
    
    latest_date = valid_dates[0] if valid_dates else None
    total_papers = sum(len(date_papers[d]) for d in valid_dates)
    
    logging.info(f"✓ 将生成包含 {total_papers} 篇论文的 HTML 页面（最近{len(valid_dates)}天）")
    
    # 4. 生成日期导航
    nav_links_html = generate_nav_links(valid_dates, date_papers) if valid_dates else "<span style='color:#6c757d'>暂无数据</span>"
    
    # 5. 生成各日期区块
    date_sections_html = []
    for date in valid_dates:
        anchor_id = f"date-{date.replace('-', '')}"
        is_latest = (date == latest_date)
        section_html = generate_date_section(date, date_papers[date], is_latest, anchor_id)
        date_sections_html.append(section_html)
    
    # 6. 准备替换字典
    replacements = {
        "total_papers": str(total_papers),
        "nav_links": nav_links_html,
        "date_sections": "\n".join(date_sections_html),
        "latest_anchor_id": f"date-{latest_date.replace('-', '')}" if latest_date else ""
    }
    
    # 使用 replace() 避免 CSS 花括号冲突
    try:
        html_content = template
        for key, value in replacements.items():
            placeholder = "{" + key + "}"
            html_content = html_content.replace(placeholder, str(value))
        
        logging.info("✓ 模板变量替换完成")
    except Exception as e:
        logging.error(f"模板替换失败：{e}")
        return False
    
    # 7. 保存 HTML 文件
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        logging.info(f"✓ HTML 页面已生成：{output_path}")
        return True
    except Exception as e:
        logging.error(f"保存 HTML 失败：{e}")
        return False


# -------------------------- 命令行入口 --------------------------
def main():
    parser = argparse.ArgumentParser(
        description="arXiv 机器人论文汇总 - HTML 生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument("--json", default=DEFAULT_JSON_PATH,
                        help=f"JSON 数据文件路径 (默认：{DEFAULT_JSON_PATH})")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE_PATH,
                        help=f"HTML 模板文件路径 (默认：{DEFAULT_TEMPLATE_PATH})")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH,
                        help=f"输出 HTML 文件路径 (默认：{DEFAULT_OUTPUT_PATH})")
    
    args = parser.parse_args()

    logging.info("🚀 开始生成 HTML 页面...")
    logging.info(f"   数据源：{args.json}")
    logging.info(f"   模板：{args.template}")
    logging.info(f"   输出：{args.output}")
    
    success = json_to_html(args.json, args.output, args.template)
    
    if success:
        abs_path = os.path.abspath(args.output)
        logging.info(f"✨ 生成成功！请用浏览器打开：file:///{abs_path}")
        return 0
    else:
        logging.error("❌ 生成失败，请检查日志")
        return 1


if __name__ == "__main__":
    sys.exit(main())
