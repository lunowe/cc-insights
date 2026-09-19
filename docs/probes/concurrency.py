import json,glob,os,collections
from datetime import datetime
IDLE=300
sess=collections.defaultdict(list); side=collections.Counter(); cwds={}
for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
    for l in open(f,errors="ignore"):
        try: d=json.loads(l)
        except: continue
        s=d.get("timestamp")
        if not s: continue
        try: t=datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()
        except: continue
        sid=d.get("sessionId") or f
        sess[sid].append((t,bool(d.get("isSidechain"))))
        cwds.setdefault(sid,d.get("cwd"))
        side[bool(d.get("isSidechain"))]+=1

# active intervals per session
iv=[]
for sid,ev in sess.items():
    T=sorted(t for t,_ in ev)
    if len(T)<2: continue
    st=T[0]; prev=T[0]
    for t in T[1:]:
        if t-prev>IDLE:
            iv.append((st,prev+1,sid)); st=t
        prev=t
    iv.append((st,prev+1,sid))
print(f"active intervals: {len(iv)}  (from {len(sess)} sessions)")
print(f"sidechain(subagent) events: {side[True]:,} / {side[True]+side[False]:,}")

# sweep for concurrency
pts=sorted([(a,1,s) for a,b,s in iv]+[(b,-1,s) for a,b,s in iv])
cur=0; last=None; time_at=collections.Counter(); peak=0; peak_when=None
for t,delta,s in pts:
    if last is not None and cur>0: time_at[cur]+=t-last
    cur+=delta; last=t
    if cur>peak: peak,peak_when=cur,t
tot=sum(time_at.values())
print("\nwall-clock time by # of concurrent agent sessions:")
for n in sorted(time_at):
    print(f"  {n} concurrent: {time_at[n]/3600:7.1f} h  ({100*time_at[n]/tot:5.1f}%)")
print(f"\ntotal wall-clock with >=1 active : {tot/3600:.1f} h")
print(f"sum of active session-hours      : {sum(b-a for a,b,_ in iv)/3600:.1f} h")
print(f"parallelism multiplier           : {sum(b-a for a,b,_ in iv)/tot:.2f}x")
print(f"peak concurrency                 : {peak} at {datetime.fromtimestamp(peak_when):%Y-%m-%d %H:%M}")
print(f"wall-clock in parallel (>=2)     : {sum(v for k,v in time_at.items() if k>=2)/3600:.1f} h ({100*sum(v for k,v in time_at.items() if k>=2)/tot:.0f}%)")
