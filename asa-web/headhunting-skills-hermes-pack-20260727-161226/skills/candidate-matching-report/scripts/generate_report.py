#!/usr/bin/env python3
"""
候选人匹配分析报告生成器
用法:
  python3 generate_report.py \\
    --resume /path/to/resume.docx \\
    --jd /path/to/jd.txt \\
    --output /path/to/output.docx \\
    --candidate "张祥翔" \\
    --company "苏科思" \\
    --position "光学产品经理"

或者管道输入 JD 文本:
  echo "JD内容..." | python3 generate_report.py --resume resume.docx --output report.docx
"""

import argparse
import sys
import json
from datetime import date
from docx import Document as DocxReader


def extract_resume(filepath: str) -> str:
    """从 .docx 简历中提取文本"""
    try:
        doc = DocxReader(filepath)
        return '\n'.join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as e:
        return f"[ERROR: 简历提取失败 - {e}]"


def read_jd(filepath: str = None) -> str:
    """读取 JD：文件路径或 stdin"""
    if filepath:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    elif not sys.stdin.isatty():
        return sys.stdin.read()
    else:
        return ""


def main():
    parser = argparse.ArgumentParser(description='候选人匹配分析 - 简历和JD提取')
    parser.add_argument('--resume', required=True, help='简历 .docx 文件路径')
    parser.add_argument('--jd', default=None, help='JD 文本文件路径（可选，也可管道输入）')
    parser.add_argument('--output', default=None, help='输出路径（默认桌面）')
    parser.add_argument('--candidate', default='候选人', help='候选人姓名')
    parser.add_argument('--company', default='企业', help='目标公司')
    parser.add_argument('--position', default='岗位', help='目标岗位')
    args = parser.parse_args()

    resume_text = extract_resume(args.resume)
    jd_text = read_jd(args.jd)

    result = {
        "candidate": args.candidate,
        "company": args.company,
        "position": args.position,
        "resume_text": resume_text,
        "jd_text": jd_text,
        "date": date.today().isoformat(),
    }

    if args.output:
        result["output_path"] = args.output

    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
