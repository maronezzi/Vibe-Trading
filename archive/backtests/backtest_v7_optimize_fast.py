"""
backtest_v7_optimize_fast.py — Reduced Parameter Sweep
"""
import sys, csv, io, subprocess, os
from itertools import product
import numpy as np, pandas as pd

WINE_PYTHON = os.path.expanduser("~/.wine/drive_c/Python311/python.exe")
FETCH_SCRIPT = os.path.join(os.path.dirname(__file__), "mt5_fetch.py")
CLOSE_HOUR, CLOSE_MINUTE = 16, 45

CONTRACT_SPECS = {
    "WIN$": {"mult": 0.20, "margin": 5000, "slip_r": 1.0},
    "WDO$": {"mult": 10.0, "margin": 3000, "slip_r": 5.0},
}
COMMISSION = 2.5
ATR_PERIOD = 14
MAX_CT = 1

def fetch(symbol, tf, n_bars):
    cmd = ["wine", WINE_PYTHON, FETCH_SCRIPT, "rates", symbol, tf, str(n_bars)]
    env = {**os.environ, "WINEDEBUG": "-all"}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    if r.returncode != 0 or not r.stdout.strip():
        return pd.DataFrame()
    reader = csv.reader(io.StringIO(r.stdout.strip()))
    headers = next(reader)
    rows = [x for x in reader if x]
    if not rows: return pd.DataFrame()
    df = pd.DataFrame(rows, columns=headers)
    for c in ["open", "high", "low", "close", "tick_volume", "real_volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["time"] = pd.to_datetime(df["time"].astype(int), unit="s")
    df = df.set_index("time")
    df["hour"] = df.index.hour; df["minute"] = df.index.minute; df["date"] = df.index.date
    return df[["open", "high", "low", "close", "tick_volume", "real_volume", "hour", "minute", "date"]].dropna(subset=["close"])

def calc_atr(df, p=14):
    h,l,c = df["high"],df["low"],df["close"].shift(1)
    tr = pd.concat([h-l,(h-c).abs(),(l-c).abs()],axis=1).max(axis=1)
    return tr.rolling(p).mean()
def calc_vwap(df, p=20):
    t=(df["high"]+df["low"]+df["close"])/3; v=df["tick_volume"].replace(0,1)
    return (t*v).rolling(p).sum()/v.rolling(p).sum()
def calc_rsi(df, p=14):
    d=df["close"].diff(); g=d.where(d>0,0).rolling(p).mean(); l=(-d.where(d<0,0)).rolling(p).mean()
    l=l.replace(0,1e-10)
    return 100-(100/(1+g/l))
def calc_adx(df, p=14):
    h,l,c=df["high"],df["low"],df["close"]
    pd_=h.diff(); md=-l.diff()
    pd_=pd_.where((pd_>md)&(pd_>0),0); md=md.where((md>pd_)&(md>0),0)
    tr=pd.concat([h-l,(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1)
    a=tr.rolling(p).mean(); a=a.replace(0,1e-10); pdi=100*(pd_.rolling(p).mean()/a); mdi=100*(md.rolling(p).mean()/a)
    di_sum=(pdi+mdi).replace(0,1e-10); dx=100*((pdi-mdi).abs()/di_sum); return dx.rolling(p).mean(), pdi, mdi
def calc_ema(df, p): return df["close"].ewm(span=p, adjust=False).mean()

def _stats(equity, trade_log, daily_pnl_dict, cash, capital, n_trades, n_wins, n_long, n_short, n_sl, n_trail, n_close, gw, gl):
    tr = (cash-capital)/capital*100; nd = max(len(daily_pnl_dict),1)
    dv = list(daily_pnl_dict.values())
    sharpe = np.mean(dv)/np.std(dv)*np.sqrt(252) if len(dv)>1 and np.std(dv)>0 else 0
    ea = np.array(equity) if equity else np.array([capital])
    rm = np.maximum.accumulate(ea); dd = (rm-ea)/rm*100; mdd = float(np.max(dd)) if len(dd)>0 else 0
    pf = gw/gl if gl>0 else (999 if gw>0 else 0)
    wr = (n_wins/n_trades*100) if n_trades else 0
    wp = [t["pnl"] for t in trade_log if t["pnl"]>0]
    lp = [t["pnl"] for t in trade_log if t["pnl"]<=0]
    aw = np.mean(wp) if wp else 0; al = abs(np.mean(lp)) if lp else 1
    po = aw/al if al>0 else 0; ad = sum(t["pnl"] for t in trade_log)/nd
    return {"ok":True,"trades":n_trades,"wins":n_wins,"wr":wr,"long":n_long,"short":n_short,
            "ret":tr,"sharpe":sharpe,"max_dd":mdd,"pf":pf,"avg_daily":ad,"avg_win":aw,"avg_loss":al,"payoff":po}

def bt_wdo(df, p, cap=100000):
    sp = CONTRACT_SPECS["WDO$"]; m,mg,sl_r = sp["mult"],sp["margin"],sp["slip_r"]
    atr=calc_atr(df,ATR_PERIOD); vw=calc_vwap(df,p["vwap_period"])
    ef=calc_ema(df,p["ema_fast"]); es=calc_ema(df,p["ema_slow"]); rsi=calc_rsi(df,14)
    cash=cap; pos=0;ep=0;ed=None;ea=0;bst=0;slp=0;tro=False;bars=0;lts=None;dtc=0;cd=None
    tl=[];gw=0;gl=0;nw=0;nl=0;ns=0;nt=0;dpd={}

    def _c(pr,rsn):
        nonlocal cash,pos,ep,ed,bst,slp,tro,ea,bars,nw,nl,ns,nt,gw,gl
        if pos==0:return
        sc=sl_r*MAX_CT;co=COMMISSION*MAX_CT
        if pos==1:pn=(pr-ep)*m*MAX_CT-sc-co;nl+=1
        else:pn=(ep-pr)*m*MAX_CT-sc-co;ns+=1
        cash+=mg*MAX_CT+pn;nt+=1
        if rsn=="SL":pass
        if pn>0:nw+=1;gw+=pn
        else:gl+=abs(pn)
        tl.append({"pnl":pn,"reason":rsn,"bars":bars})
        d=ed.date() if hasattr(ed,'date') else ed
        if d not in dpd:dpd[d]=0.0
        dpd[d]+=pn
        pos=0;ep=0;bst=0;slp=0;tro=False;bars=0

    def _o(d,pr,dt,ca):
        nonlocal cash,pos,ep,ed,bst,slp,tro,ea,bars,dtc,lts
        if pos!=0:return False
        rs=int(ca*p["sl_atr_mult"]);rs=max(rs,200);rs=((rs+4)//5)*5
        co=sl_r*MAX_CT+COMMISSION*MAX_CT
        if cash>=mg*MAX_CT+co:
            cash-=mg*MAX_CT+co;pos=1 if d=="BUY" else -1
            ep=pr;ed=dt;ea=ca;bst=pr;tro=False;bars=0
            slp=pr-rs if pos==1 else pr+rs;dtc+=1;lts=dt;return True
        return False

    for i,(dt,row) in enumerate(df.iterrows()):
        pr=float(row["close"]);hi=float(row["high"]);lo=float(row["low"])
        h=int(row["hour"]);mi=int(row["minute"])
        ca=float(atr.iloc[i]) if i>0 and not pd.isna(atr.iloc[i]) else 0
        cv=float(vw.iloc[i]) if not pd.isna(vw.iloc[i]) else 0
        cr=float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
        cef=float(ef.iloc[i]) if not pd.isna(ef.iloc[i]) else 0
        ces=float(es.iloc[i]) if not pd.isna(es.iloc[i]) else 0
        _cd=dt.date() if hasattr(dt,'date') else dt
        if cd!=_cd:cd=_cd;dtc=0
        cm=h*60+mi;safe=not((9*60+5<=cm<=9*60+20)or(16*60+30<=cm<=16*60+45))
        if not safe:
            if pos==1:tl_eq=cash+(pr-ep)*m*MAX_CT+mg*MAX_CT
            elif pos==-1:tl_eq=cash+(ep-pr)*m*MAX_CT+mg*MAX_CT
            else:tl_eq=cash
            continue
        if pos==1:eq=cash+(pr-ep)*m*MAX_CT+mg*MAX_CT
        elif pos==-1:eq=cash+(ep-pr)*m*MAX_CT+mg*MAX_CT
        else:eq=cash

        if pos==0:
            if ca>0 and cv>0 and dtc<p["max_daily"]:
                if cef>0 and ces>0:
                    sp_=abs(cef-ces)/pr if pr>0 else 0
                    if sp_<p.get("trend_min_spread",0.001):continue
                ap=ca/pr if pr>0 else 0
                if ap<0.0015:bm=1.0005;sm=0.9995
                elif ap<0.003:bm=1.0015;sm=0.9985
                else:bm=p["buy_thresh"];sm=p["sell_thresh"]
                d=None
                if pr>cv*bm:d="BUY"
                elif pr<cv*sm:d="SELL"
                if d:
                    if cef>0 and ces>0:
                        if d=="BUY" and cef<ces:continue
                        if d=="SELL" and cef>ces:continue
                    if d=="BUY" and cr>70:continue
                    if d=="SELL" and cr<30:continue
                    if lts and (dt-lts).total_seconds()<p["cooldown"]:continue
                    _o(d,pr,dt,ca)
            continue
        bars+=1
        if pos==1:bst=max(bst,hi)
        elif pos==-1:bst=min(bst,lo) if bst>0 else lo
        pp=(bst-ep) if pos==1 else (ep-bst)
        if not tro and ea>0 and pp>=p["trail_act"]*ea:tro=True
        if tro and ea>0:
            td=p["trail_dist"]*ea
            if pos==1:
                ns_=bst-td
                if ns_>slp:slp=ns_
            else:
                ns_=bst+td
                if ns_<slp:slp=ns_
        if slp>0:
            if pos==1 and lo<=slp:_c(slp,"SL");continue
            elif pos==-1 and hi>=slp:_c(slp,"SL");continue
        if h>CLOSE_HOUR or (h==CLOSE_HOUR and mi>=CLOSE_MINUTE):
            _c(pr,"1645");continue
    if pos!=0:_c(float(df["close"].iloc[-1]),"FORCE")
    return _stats([],tl,dpd,cash,cap,nt,nw,nl,ns,0,0,0,gw,gl)

def bt_win(df, p, cap=100000):
    sp = CONTRACT_SPECS["WIN$"]; m,mg,sl_r = sp["mult"],sp["margin"],sp["slip_r"]
    atr=calc_atr(df,ATR_PERIOD); ef=calc_ema(df,p["ema_fast"]); es=calc_ema(df,p["ema_slow"])
    adx,pdi,mdi = calc_adx(df,p["adx_period"]); rsi=calc_rsi(df,p["rsi_period"])
    cash=cap; pos=0;ep=0;ed=None;ea=0;bst=0;slp=0;tro=False;bars=0;lts=None;dtc=0;cd=None
    tl=[];gw=0;gl=0;nw=0;nl=0;ns=0;nt=0;dpd={}

    def _c(pr,rsn):
        nonlocal cash,pos,ep,ed,bst,slp,tro,ea,bars,nw,nl,ns,nt,gw,gl
        if pos==0:return
        sc=sl_r*MAX_CT;co=COMMISSION*MAX_CT
        if pos==1:pn=(pr-ep)*m*MAX_CT-sc-co;nl+=1
        else:pn=(ep-pr)*m*MAX_CT-sc-co;ns+=1
        cash+=mg*MAX_CT+pn;nt+=1
        if pn>0:nw+=1;gw+=pn
        else:gl+=abs(pn)
        tl.append({"pnl":pn,"reason":rsn,"bars":bars})
        d=ed.date() if hasattr(ed,'date') else ed
        if d not in dpd:dpd[d]=0.0
        dpd[d]+=pn
        pos=0;ep=0;bst=0;slp=0;tro=False;bars=0

    def _o(d,pr,dt,ca):
        nonlocal cash,pos,ep,ed,bst,slp,tro,ea,bars,dtc,lts
        if pos!=0:return False
        rs=int(ca*p["sl_atr_mult"]);rs=max(rs,200);rs=((rs+4)//5)*5
        co=sl_r*MAX_CT+COMMISSION*MAX_CT
        if cash>=mg*MAX_CT+co:
            cash-=mg*MAX_CT+co;pos=1 if d=="BUY" else -1
            ep=pr;ed=dt;ea=ca;bst=pr;tro=False;bars=0
            slp=pr-rs if pos==1 else pr+rs;dtc+=1;lts=dt;return True
        return False

    for i,(dt,row) in enumerate(df.iterrows()):
        pr=float(row["close"]);hi=float(row["high"]);lo=float(row["low"])
        h=int(row["hour"]);mi=int(row["minute"])
        ca=float(atr.iloc[i]) if i>0 and not pd.isna(atr.iloc[i]) else 0
        cef=float(ef.iloc[i]) if not pd.isna(ef.iloc[i]) else 0
        ces=float(es.iloc[i]) if not pd.isna(es.iloc[i]) else 0
        ca_=(float(adx.iloc[i]) if not pd.isna(adx.iloc[i]) else 0)
        cpd=float(pdi.iloc[i]) if not pd.isna(pdi.iloc[i]) else 0
        cmd_=float(mdi.iloc[i]) if not pd.isna(mdi.iloc[i]) else 0
        cr=float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
        pef=float(ef.iloc[i-1]) if i>0 and not pd.isna(ef.iloc[i-1]) else cef
        pes=float(es.iloc[i-1]) if i>0 and not pd.isna(es.iloc[i-1]) else ces
        _cd=dt.date() if hasattr(dt,'date') else dt
        if cd!=_cd:cd=_cd;dtc=0
        cm=h*60+mi;safe=not((9*60+5<=cm<=9*60+20)or(16*60+30<=cm<=16*60+45))
        if not safe:
            if pos==1:tl_eq=cash+(pr-ep)*m*MAX_CT+mg*MAX_CT
            elif pos==-1:tl_eq=cash+(ep-pr)*m*MAX_CT+mg*MAX_CT
            else:tl_eq=cash
            continue
        if pos==1:eq=cash+(pr-ep)*m*MAX_CT+mg*MAX_CT
        elif pos==-1:eq=cash+(ep-pr)*m*MAX_CT+mg*MAX_CT
        else:eq=cash

        if pos==0:
            if ca>0 and cef>0 and ces>0 and ca_>0:
                if ca_<p["adx_threshold"]:continue
                if dtc>=p["max_daily"]:continue
                d=None
                if pef<=pes and cef>ces:d="BUY"
                elif pef>=pes and cef<ces:d="SELL"
                if not d:continue
                if d=="BUY" and cr>p["rsi_ob"]:continue
                if d=="SELL" and cr<p["rsi_os"]:continue
                if d=="BUY" and cpd<cmd_:continue
                if d=="SELL" and cmd_<cpd:continue
                if lts and (dt-lts).total_seconds()<p["cooldown"]:continue
                _o(d,pr,dt,ca)
            continue
        bars+=1
        if pos==1:bst=max(bst,hi)
        elif pos==-1:bst=min(bst,lo) if bst>0 else lo
        pp=(bst-ep) if pos==1 else (ep-bst)
        if not tro and ea>0 and pp>=p["trail_act"]*ea:tro=True
        if tro and ea>0:
            td=p["trail_dist"]*ea
            if pos==1:
                ns_=bst-td
                if ns_>slp:slp=ns_
            else:
                ns_=bst+td
                if ns_<slp:slp=ns_
        if slp>0:
            if pos==1 and lo<=slp:_c(slp,"SL");continue
            elif pos==-1 and hi>=slp:_c(slp,"SL");continue
        if h>CLOSE_HOUR or (h==CLOSE_HOUR and mi>=CLOSE_MINUTE):
            _c(pr,"1645");continue
    if pos!=0:_c(float(df["close"].iloc[-1]),"FORCE")
    return _stats([],tl,dpd,cash,cap,nt,nw,nl,ns,0,0,0,gw,gl)

def run():
    print("="*100)
    print("  FAST PARAMETER SWEEP — Iteration 1")
    print("="*100)

    wdo_m5 = fetch("WDO$", "M5", 500)
    wdo_m15 = fetch("WDO$", "M15", 500)
    win_m5 = fetch("WIN$", "M5", 500)
    win_m15 = fetch("WIN$", "M15", 500)

    # WDO sweep (reduced)
    print("\n--- WDO$ SWEEP ---")
    wdo_base = {"ema_fast": 9, "ema_slow": 21, "trend_min_spread": 0.001}
    wdo_list = []
    for vp in [15, 20, 30]:
        for bt in [1.001, 1.003, 1.005]:
            for sl in [0.8, 1.0, 1.2]:
                for ta in [1.0, 1.5]:
                    for td in [0.2, 0.3]:
                        for cd in [300, 600]:
                            wdo_list.append({**wdo_base, "vwap_period": vp, "buy_thresh": bt,
                                "sell_thresh": round(2.0-bt, 3), "sl_atr_mult": sl,
                                "trail_act": ta, "trail_dist": td, "cooldown": cd, "max_daily": 8})
    print(f"  {len(wdo_list)} WDO combos")
    wdo_res = []
    for idx, p in enumerate(wdo_list):
        for sdf, stf, sn in [(wdo_m5,"M5","WDO$ M5"),(wdo_m15,"M15","WDO$ M15")]:
            if sdf.empty: continue
            r = bt_wdo(sdf, p)
            if r["ok"] and r["trades"]>=3:
                sc = r["ret"]*0.3 + min(r["pf"],20)*0.3 + r["wr"]*0.2 + min(r["sharpe"],50)*0.2
                wdo_res.append({"sym":sn,"p":p,"r":r,"sc":sc})
    wdo_res.sort(key=lambda x: x["sc"], reverse=True)
    print(f"\n  TOP 5 WDO:")
    for i, wr in enumerate(wdo_res[:5]):
        r=wr["r"];p=wr["p"]
        print(f"  #{i+1} [{wr['sym']}] Sc={wr['sc']:.1f} Ret={r['ret']:+.2f}% WR={r['wr']:.0f}% PF={r['pf']:.2f} Sh={r['sharpe']:.1f} DD={r['max_dd']:.2f}% T={r['trades']}")
        print(f"       VP={p['vwap_period']} Buy={p['buy_thresh']} Sell={p['sell_thresh']} SL={p['sl_atr_mult']}x Tr={p['trail_act']}/{p['trail_dist']} CD={p['cooldown']}s")

    # WIN sweep (reduced)
    print("\n--- WIN$ SWEEP ---")
    win_list = []
    for ef in [8, 9, 12]:
        for es in [21, 26]:
            if ef>=es: continue
            for adx_th in [15, 20, 25]:
                for sl in [1.0, 1.5]:
                    for ta in [1.0, 1.5]:
                        for td in [0.2, 0.3]:
                            for cd in [600, 900]:
                                win_list.append({"ema_fast":ef,"ema_slow":es,"adx_period":14,
                                    "adx_threshold":adx_th,"rsi_period":14,"rsi_ob":70,"rsi_os":30,
                                    "sl_atr_mult":sl,"trail_act":ta,"trail_dist":td,"cooldown":cd,"max_daily":6})
    print(f"  {len(win_list)} WIN combos")
    win_res = []
    for idx, p in enumerate(win_list):
        for sdf, stf, sn in [(win_m5,"M5","WIN$ M5"),(win_m15,"M15","WIN$ M15")]:
            if sdf.empty: continue
            r = bt_win(sdf, p)
            if r["ok"] and r["trades"]>=3:
                sc = r["ret"]*0.3 + min(r["pf"],20)*0.3 + r["wr"]*0.2 + min(r["sharpe"],50)*0.2
                win_res.append({"sym":sn,"p":p,"r":r,"sc":sc})
    win_res.sort(key=lambda x: x["sc"], reverse=True)
    print(f"\n  TOP 5 WIN:")
    for i, wr in enumerate(win_res[:5]):
        r=wr["r"];p=wr["p"]
        print(f"  #{i+1} [{wr['sym']}] Sc={wr['sc']:.1f} Ret={r['ret']:+.2f}% WR={r['wr']:.0f}% PF={r['pf']:.2f} Sh={r['sharpe']:.1f} DD={r['max_dd']:.2f}% T={r['trades']}")
        print(f"       EMA={p['ema_fast']}/{p['ema_slow']} ADX>{p['adx_threshold']} SL={p['sl_atr_mult']}x Tr={p['trail_act']}/{p['trail_dist']} CD={p['cooldown']}s")

    print("\n" + "="*100)
    print("  OPTIMAL PARAMETERS")
    print("="*100)
    if wdo_res:
        bw=wdo_res[0];bp=bw["p"];br=bw["r"]
        print(f"\n  WDO$ BEST ({bw['sym']}): Ret={br['ret']:+.2f}% WR={br['wr']:.0f}% PF={br['pf']:.2f} Sharpe={br['sharpe']:.1f}")
        print(f"    VWAP={bp['vwap_period']} Buy={bp['buy_thresh']} Sell={bp['sell_thresh']} SL={bp['sl_atr_mult']}x Trail={bp['trail_act']}/{bp['trail_dist']} CD={bp['cooldown']}s")
    if win_res:
        bw=win_res[0];bp=bw["p"];br=bw["r"]
        print(f"\n  WIN$ BEST ({bw['sym']}): Ret={br['ret']:+.2f}% WR={br['wr']:.0f}% PF={br['pf']:.2f} Sharpe={br['sharpe']:.1f}")
        print(f"    EMA={bp['ema_fast']}/{bp['ema_slow']} ADX>{bp['adx_threshold']} SL={bp['sl_atr_mult']}x Trail={bp['trail_act']}/{bp['trail_dist']} CD={bp['cooldown']}s")
    print("\n" + "="*100 + "\n")

if __name__ == "__main__":
    run()
