import sys
sys.path.insert(0,".")
from pipeline import funds as F
from pipeline.net import fetch
for frag, code, disp in F.INDEX_MAP:
    try:
        h=fetch(F.LEGULEGU_URL.format(code=code), timeout=20, retries=1, delay=0.2)
        v=F.parse_index_val(h)
        print("%-12s %-12s PE=%-6s PB=%-5s DY=%-5s"%(disp, code, v["pe"], v["pb"], v["dy"]))
    except Exception as e:
        print("%-12s %-12s 失败:%s"%(disp, code, e))
