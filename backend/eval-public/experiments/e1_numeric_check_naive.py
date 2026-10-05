import re, sys
from docket.eval.schema import load_gold_set, load_records
from docket.eval.scoring import score_run, strip_citations, Verdict
gold=load_gold_set("eval-public/gold-extended.yaml"); qs=gold.by_id()
recs=load_records(sys.argv[1])
NUM=re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?%?")
def nums(t):
    out=set()
    for m in NUM.findall(t):
        v=m.rstrip('%').replace(',','')
        if v.endswith('.'): v=v[:-1]
        out.add(v)
    return out
def norm_chunk(t): return nums(t)
rows=[]
for r in recs:
    q=qs[r.question_id]; sc=score_run(q,r)
    if r.abstained or sc.abstained: continue
    ans=strip_citations(r.answer)
    # drop citation-label hashes and years in file names crudely
    ans_n=nums(ans)-nums(q.question)
    cited={c.chunk_id for c in r.citations}
    ctx=" ".join(c.text for c in r.retrieved if c.chunk_id in cited)
    ctx_all=" ".join(c.text for c in r.retrieved)
    missing_cited=sorted(n for n in ans_n if n not in norm_chunk(ctx))
    missing_all=sorted(n for n in ans_n if n not in norm_chunk(ctx_all))
    rows.append((r.question_id,q.type.value,sc.verdict.value,missing_cited,missing_all))
for label,sel in (("PASS",lambda v:v=="pass"),("FAIL",lambda v:v=="fail"),("NEEDS_JUDGE",lambda v:v=="needs_judge")):
    sub=[x for x in rows if sel(x[2])]
    fl_c=[x for x in sub if x[3]]; fl_a=[x for x in sub if x[4]]
    print(f"{label}: answered={len(sub)} flagged(cited)={len(fl_c)} flagged(any retrieved)={len(fl_a)}")
    for x in fl_c: print("   ",x[0],x[1],"cited-missing",x[3],"| retrieved-missing",x[4])
