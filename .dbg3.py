# -*- coding: utf-8 -*-
import sys, re
sys.path.insert(0,".")
from pipeline.net import fetch
h=fetch("https://legulegu.com/stockdata/index-basic?indexCode=000300.SH", timeout=20, retries=1, delay=0.3)
# 找 PE 真实值 13.62 附近标记
for val in ("13.62","1.43","2.75"):
    i=h.find(val)
    if i>0:
        print("=== 围绕 %s 的标记 ==="%val)
        print(h[max(0,i-80):i+20].replace("\n"," "))
