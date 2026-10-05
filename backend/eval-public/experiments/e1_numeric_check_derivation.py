import re, sys, itertools
from docket.eval.schema import load_gold_set, load_records
from docket.eval.scoring import score_run, strip_citations
gold=load_gold_set(sys.argv[2] if len(sys.argv)>2 else "eval-public/gold-extended.yaml"); qs=gold.by_id()
recs=load_records(sys.argv[1])
NUM=re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?%?")
def nums(t):
    o=set()
    for m in NUM.findall(t):
        v=m.rstrip('%').replace(',','').rstrip('.')
        try:o.add(float(v))
        except:pass
    return o
def close(x,y): return abs(x-y)<=max(0.006,abs(y)*0.0006)
def derivable(x,pool):
    pool=list(pool)[:60]
    for a,b in itertools.permutations(pool,2):
        cands=[a-b,a+b]
        if b: cands+=[a/b, a/b*100,(a-b)/b*100,(a/b-1)*100]
        for c in cands:
            for k in (1,1000,1e-3):
                if close(x,c*k): return True
    for a in pool:
        for k in (1000,1e-3,1e-6,1e6):
            if close(x,a*k): return True
    return False
def ignorable(x): return (x==int(x) and (x<=31 or 2000<=x<=2100))
tot={}
for r in recs:
    q=qs[r.question_id]; sc=score_run(q,r)
    if sc.abstained: continue
    ans=nums(strip_citations(r.answer))-nums(q.question)
    cited={c.chunk_id for c in r.citations}
    pool=set().union(*[nums(c.text) for c in r.retrieved if c.chunk_id in cited] or [set()])
    bad=[x for x in ans if not ignorable(x) and not any(close(x,p) for p in pool) and not derivable(x,pool)]
    k=sc.verdict.value; t=tot.setdefault(k,[0,0]); t[0]+=1; t[1]+=bool(bad)
    if bad: print(k,r.question_id,bad)
print(tot)
