# -*- coding: utf-8 -*-
import sys, re
sys.path.insert(0,".")
from pipeline.net import fetch

def ctx(code):
    h=fetch("https://legulegu.com/stockdata/index-basic?indexCode=%s"%code, timeout=20, retries=1, delay=0.3)
    title=re.search(r"<title>(.*?)</title>", h)
    out=[title.group(1) if title else "?"]
    for m in re.finditer(r".{0,8}市盈率.{0,50}", h):
        out.append(m.group(0).replace("\n"," "))
        if len(out)>=3: break
    return out

print("=== 000300.SH 正常页标记 ===")
for c in ctx("000300.SH"): print("  ",c)

print("\n=== 试替代代码（title 是否仍为 null）===")
for label,code in [("中证红利.XSHG","000922.XSHG"),("中证红利.无后缀","000922"),
                   ("红利低波.CSI","h30269.CSI"),("红利低波100.CSI","930955.CSI"),
                   ("中证A500.XSHG","000510.XSHG"),("中证A50.XSHG","000950.XSHG"),
                   ("MSCI.SH","718711.SH")]:
    try:
        print("  %-16s -> %s"%(label, ctx(code)[0]))
    except Exception as e:
        print("  %-16s -> 失败:%s"%(label,e))
