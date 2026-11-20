import sys; sys.path.insert(0,'.'); import replay as R
VARS = [("MIN_ADTV",[3e9,5e9,10e9,20e9],"MIN_ADTV (IDR bn)",lambda v:f"{v/1e9:g}"),
        ("MIN_ATR_PCT",[.02,.025,.03,.04,.05],"MIN_ATR_PCT",lambda v:f"{v*100:g}%"),
        ("MAX_ATRS",[.5,.75,1.0,1.5,2.0],"MAX_ATRS_BELOW_HIGH",lambda v:f"{v:g}"),
        ("MAX_DIST",[.03,.05,.08],"MAX_DIST_TO_HIGH",lambda v:f"{v*100:g}%"),
        ("PRICE_MAX",[2000,3000,5000],"PRICE_MAX (IDR)",lambda v:f"{v:,.0f}")]
def configs():
    out=[]
    for key,vals,label,fmt in VARS:
        for v in vals:
            c=dict(R.BASE); c[key]=v; out.append((key,label,v,fmt(v),abs(v-R.BASE[key])<1e-12,c))
    return out
