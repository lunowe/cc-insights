import json,glob,os,collections,statistics
from datetime import datetime,timezone

def ts(s):
    try: return datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()
    except: return None

sessions=collections.defaultdict(lambda:{"t":[],"cwd":None,"models":set(),"side":0})
gaps=[]
for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
    for l in open(f,errors="ignore"):
        try: d=json.loads(l)
        except: continue
        t=ts(d.get("timestamp") or "")
        if not t: continue
        sid=d.get("sessionId") or os.path.basename(f)
        S=sessions[sid]; S["t"].append(t)
        S["cwd"]=S["cwd"] or d.get("cwd")
        if d.get("isSidechain"): S["side"]+=1
        m=(d.get("message") or {}).get("model")
        if m and m!="<synthetic>": S["models"].add(m)

for S in sessions.values():
    T=sorted(S["t"])
    gaps.extend(T[i+1]-T[i] for i in range(len(T)-1))

print(f"sessions: {len(sessions)}   events: {sum(len(s['t']) for s in sessions.values()):,}")
g=sorted(gaps)
q=lambda p: g[int(len(g)*p)]
print("inter-event gap percentiles (s): p50=%.1f p90=%.1f p95=%.1f p99=%.1f p99.9=%.0f max=%.0f"%(q(.5),q(.9),q(.95),q(.99),q(.999),g[-1]))
for thr in (60,120,300,900,1800):
    print(f"  gaps > {thr:>4}s: {sum(1 for x in g if x>thr):>6}  ({100*sum(1 for x in g if x>thr)/len(g):.2f}%)")

# span vs active time with 5-min idle cap
IDLE=300
tot_span=tot_act=0
for S in sessions.values():
    T=sorted(S["t"])
    if len(T)<2: continue
    tot_span+=T[-1]-T[0]
    tot_act+=sum(min(T[i+1]-T[i],IDLE) for i in range(len(T)-1))
print(f"\nsum of session wall-spans : {tot_span/3600:8.1f} h")
print(f"sum of 'active' time      : {tot_act/3600:8.1f} h   ({100*tot_act/tot_span:.0f}% of span)")
print(f"date range: {datetime.fromtimestamp(min(min(s['t']) for s in sessions.values())):%Y-%m-%d} .. {datetime.fromtimestamp(max(max(s['t']) for s in sessions.values())):%Y-%m-%d}")
