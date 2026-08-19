# -*- coding: utf-8 -*-
import sys, os, re
sys.path.insert(0,".")
from pipeline.net import fetch
for code in ("000922.SH","930955.SH","h30269.SH","000510.SH","000950.SH","718711.CSI"):
    try:
        h=fetch("https://legulegu.com/stockdata/index-basic?indexCode=%s"%code, timeout=20, retries=1, delay=0.3)
        # 找 市盈率 上下文
        ctx=[]
        for m in re.finditer(r".{0,15}市盈率.{0,40}", h):
            ctx.append(m.group(0).replace("\n"," "))
        print("### %s len=%d  市盈率ctx=%s"%(code,len(h),ctx[:3]))
        # 是否含 暂无/404/不存在
        for kw in ("暂无","不存在","没有","未找到","error"):
            if kw in h: print("   含'%s'"%kw)
    except Exception as e:
        print("### %s 失败:%s"%(code,e))
