#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
arXiv 机器人领域论文汇总 - HTML 生成器
功能：读取 JSON 数据，根据 template.html 生成带 Zotero 一键添加的 index.html
用法：python generate_html.py [--json PATH] [--template PATH] [--output PATH]
"""

import json
import re
import sys
import argparse
import logging
import os  # ✅ 补充：main() 中使用了 os.path.abspath
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
DEFAULT_RECENT_DAYS = 5  # 显示最近几天的数据

# -------------------------- 工具函数 --------------------------

def html_escape(text: str) -> str:
    """转义HTML特殊字符，防止XSS"""
    if not text:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
            .replace("\n", "<br>"))


def extract_arxiv_id(url: str) -> str:
    """从arXiv链接中提取论文ID，如 2403.12345"""
    if not url:
        return ""
    patterns = [
        r'arxiv\.org/abs/([\w\.]+)',
        r'arxiv\.org/pdf/([\w\.]+)',
        r'arxiv\.org/html/([\w\.]+)',
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
    """获取最近N天的日期列表，格式 YYYY-MM-DD"""
    dates = []
    for i in range(limit):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        dates.append(date)
    return dates


def generate_zotero_link(pdf_url: str, abs_url: str = "") -> str:
    """生成 Zotero 一键保存链接"""
    target_url = abs_url if abs_url else pdf_url
    if not target_url:
        return "#"
    return target_url


def generate_stars_html(score: int) -> str:
    """生成星星评分HTML，score范围1-5"""
    if not isinstance(score, int) or not (1 <= score <= 5):
        return '<span class="stars">-</span>'
    filled = "★" * score
    empty = "☆" * (5 - score)
    return f'<span class="stars" title="相关性评分: {score}/5">{filled}{empty}</span>'


def generate_zotero_button_html(pdf_url: str, abs_url: str = "", title: str = "") -> str:
    """生成 Zotero 一键添加按钮的HTML"""
    target_url = abs_url if abs_url else pdf_url
    if not target_url:
        return '<span style="color:#6c757d">-</span>'
    
    display_title = title[:30] + "..." if len(title) > 30 else (title or "论文")
    return f'''<a class="zotero-btn" 
           href="{html_escape(target_url)}" 
           target="_blank" 
           title="点击后，如已安装Zotero Connector将自动弹出保存对话框">
           📚 添加
       </a>'''


def generate_paper_row(paper: Dict) -> str:
    """生成单篇论文的表格行HTML（备注+代码+PDF合并为一列）"""
    # 基础字段
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
    
    # 🔹【合并列】资源列：包含 PDF + Code + Comment
    resource_parts = []
    
    # PDF 标签
    if pdf_link:
        resource_parts.append(
            f'<a href="{html_escape(pdf_link)}" target="_blank" '
            f'style="display:inline-block;padding:3px 8px;background:#007bff15;color:#007bff;'
            f'text-decoration:none;border-radius:4px;font-size:0.8rem;margin:2px 0">'
            f'📄 PDF</a>'
        )
    
    # Code 标签（支持多个）
    if code and code.strip():
        code_list = [c.strip() for c in code.split(",") if c.strip()]
        for i, c in enumerate(code_list):
            resource_parts.append(
                f'<a href="{html_escape(c)}" target="_blank" '
                f'style="display:inline-block;padding:3px 8px;background:#28a74515;color:#28a745;'
                f'text-decoration:none;border-radius:4px;font-size:0.8rem;margin:2px 0">'
                f'🔗 Code{i+1}</a>'
            )
    
    # Comment 折叠详情
    if comment and comment != "":
        resource_parts.append(
            f'<details style="display:inline-block;margin:2px 0">'
            f'<summary style="cursor:pointer;padding:3px 8px;background:#6c757d15;color:#6c757d;'
            f'border-radius:4px;font-size:0.8rem;list-style:none">📝 备注</summary>'
            f'<small style="display:block;margin-top:5px;padding:8px;background:#f8f9fa;'
            f'border-radius:4px;color:#6c757d;font-size:0.75rem;line-height:1.4">{comment}</small>'
            f'</details>'
        )
    
    resource_html = "<br>".join(resource_parts) if resource_parts else '<span style="color:#6c757d">-</span>'
    
    # 相关性评分：星星展示
    stars_html = generate_stars_html(llm_score)
    
    # Zotero按钮列
    zotero_html = generate_zotero_button_html(pdf_link, abs_link, paper.get("title", ""))
    
    # LLM总结列：折叠展示
    if llm_error and llm_error.strip():
        summary_content = f'{html_escape(llm_summary)}<br><small style="color:#dc3545">⚠️ {html_escape(llm_error)}</small>'
    elif llm_summary and llm_summary != "大模型总结失败" and llm_summary.strip():
        summary_content = html_escape(llm_summary)
    else:
        summary_content = "暂无总结"
    
    if summary_content and summary_content != "暂无总结":
        summary_html = f'<details><summary style="cursor:pointer;color:#007bff;font-size:0.85rem">📋 查看</summary>' \
                      f'<small style="display:block;margin-top:5px;padding:8px;background:rgba(0,123,255,0.05);' \
                      f'border-radius:4px;color:var(--text-secondary);font-size:0.8rem;line-height:1.4">' \
                      f'{summary_content}</small></details>'
    else:
        summary_html = '<span style="color:#6c757d;font-size:0.85rem">暂无总结</span>'
    
    # 🔹【修改】表格行：6列 → 5列（合并资源列）
    return f'''<tr>
        <td><strong>{title}</strong></td>
        <td>{first_author}</td>
        <td>{resource_html}</td>
        <td style="text-align:center">{stars_html}</td>
        <td style="text-align:center">{zotero_html}</td>
        <td>{summary_html}</td>
    </tr>'''


def generate_date_section(date: str, papers: List[Dict], is_latest: bool, anchor_id: str) -> str:
    """生成单个日期区块的HTML"""
    paper_count = len(papers)
    date_display = f"{date}（{paper_count}篇论文）"
    
    sorted_papers = sorted(papers, key=lambda x: x.get("llm_score", 0) or 0, reverse=True)
    
    # 🔹【修改】表格头：6列 → 5列
    table_header = '''<table>
        <thead>
            <tr>
                <th>标题</th>
                <th>作者</th>
                <th>资源</th>
                <th style="text-align:center">相关性</th>
                <th style="text-align:center">Zotero</th>
                <th>LLM总结</th>
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
    """生成顶部日期导航链接HTML"""
    links = []
    for date in valid_dates:
        count = len(date_papers.get(date, []))
        display = f"{date}（{count}篇）"
        anchor_id = f"date-{date.replace('-', '')}"
        links.append(f'<a href="#{anchor_id}">{display}</a>')
    return " | ".join(links)


def json_to_html(json_path: str, output_path: str, template_path: str) -> bool:
    """主函数：读取JSON，填充模板，生成HTML"""
    
    # 1. 读取JSON数据
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            date_papers = json.load(f)
        if not date_papers:
            logging.error("JSON文件中无论文数据")
            return False
        logging.info(f"✓ 已加载 {len(date_papers)} 个日期的数据")
    except FileNotFoundError:
        logging.error(f"JSON文件不存在: {json_path}")
        return False
    except json.JSONDecodeError as e:
        logging.error(f"JSON解析失败: {e}")
        return False
    except Exception as e:
        logging.error(f"读取JSON失败: {e}")
        return False
    
    # 2. 读取HTML模板
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template = f.read()
        logging.info(f"✓ 已加载模板: {template_path}")
    except Exception as e:
        logging.error(f"读取模板失败: {e}")
        return False
    
    # 3. 筛选最近几天有数据的日期
    recent_dates = get_recent_dates(DEFAULT_RECENT_DAYS)
    valid_dates = [d for d in recent_dates if d in date_papers and date_papers[d]]
    
    if not valid_dates:
        logging.warning(f"⚠️ 最近 {DEFAULT_RECENT_DAYS} 天内无有效论文数据")
        valid_dates = []
    
    latest_date = valid_dates[0] if valid_dates else None
    total_papers = sum(len(date_papers[d]) for d in valid_dates)
    
    logging.info(f"✓ 将生成包含 {total_papers} 篇论文的HTML页面（最近{len(valid_dates)}天）")
    
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
    
    # 🔧 【关键修复】使用 replace() 替代 format()，避免CSS花括号冲突
    try:
        html_content = template
        for key, value in replacements.items():
            placeholder = "{" + key + "}"  # 构造如 {total_papers}
            html_content = html_content.replace(placeholder, str(value))
        
        # 可选：验证是否所有占位符都被替换
        for key in replacements.keys():
            if "{" + key + "}" in html_content:
                logging.warning(f"⚠️ 占位符 {{{key}}} 可能未被正确替换")
        
        logging.info("✓ 模板变量替换完成")
    except Exception as e:
        logging.error(f"模板替换失败: {e}")
        return False
    
    # 7. 保存HTML文件
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        logging.info(f"✓ HTML页面已生成: {output_path}")
        return True
    except Exception as e:
        logging.error(f"保存HTML失败: {e}")
        return False


# -------------------------- 命令行入口 --------------------------
def main():
    parser = argparse.ArgumentParser(
        description="arXiv机器人论文汇总 - 生成带Zotero功能的HTML页面",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:
  # 使用默认配置
  python generate_html.py
  
  # 指定自定义路径
  python generate_html.py --json my_data.json --template my_template.html --output result.html
  
  # 仅显示最近3天数据
  python generate_html.py --days 3

Zotero使用说明:
  1. 安装 Zotero: https://www.zotero.org/  
  2. 安装浏览器 Connector: https://www.zotero.org/download/connectors  
  3. 在生成的页面中点击「📚 添加」按钮，将自动弹出保存对话框
        """
    )
    
    parser.add_argument("--json", default=DEFAULT_JSON_PATH,
                        help=f"JSON数据文件路径 (默认: {DEFAULT_JSON_PATH})")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE_PATH,
                        help=f"HTML模板文件路径 (默认: {DEFAULT_TEMPLATE_PATH})")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH,
                        help=f"输出HTML文件路径 (默认: {DEFAULT_OUTPUT_PATH})")
    
    args = parser.parse_args()

    logging.info("🚀 开始生成 HTML 页面...")
    logging.info(f"   数据源: {args.json}")
    logging.info(f"   模板: {args.template}")
    logging.info(f"   输出: {args.output}")
    logging.info(f"   时间范围: 最近 {DEFAULT_RECENT_DAYS} 天")
    
    success = json_to_html(args.json, args.output, args.template)
    
    if success:
        abs_path = os.path.abspath(args.output)
        logging.info(f"✨ 生成成功！请用浏览器打开查看: file:///{abs_path}")
        return 0
    else:
        logging.error("❌ 生成失败，请检查日志")
        return 1


if __name__ == "__main__":
    sys.exit(main())
