import random, statistics as st
random.seed(0)
def ece(conf, y, bins=10):
    n=len(conf); tot=0
    for b in range(bins):
        idx=[i for i,c in enumerate(conf) if min(int(c*bins),bins-1)==b]
        if idx: tot+=len(idx)/n*abs(sum(y[i] for i in idx)/len(idx)-sum(conf[i] for i in idx)/len(idx))
    return tot
def sim(n, shift, trials=2000, hi=True):
    out=[]
    for _ in range(trials):
        # confidences skewed high like a precision-first system
        conf=[min(0.999,max(0.5,random.betavariate(8,1)*0.5+0.5)) if hi else random.random() for _ in range(n)]
        y=[1 if random.random()<min(1,max(0,c-shift)) else 0 for c in conf]
        out.append(ece(conf,y))
    out.sort(); return st.mean(out), out[int(.05*trials)], out[int(.95*trials)]
for n in (100,400,1000):
    for shift in (0,0.03,0.05):
        m,lo,hi=sim(n,shift)
        print(f"n={n} true_miscal={shift:.2f} ECE mean={m:.3f} 5-95%=[{lo:.3f},{hi:.3f}]")
