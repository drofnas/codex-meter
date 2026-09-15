"""Pure-Python synthetic calendar fixtures shared by host and device tools."""
import copy
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
DAY=86400
WEEK=7*DAY
LABELS=("M","T","W","Th","F","Sa","Su")
def reset_text(epoch,zone):
    value=datetime.fromtimestamp(epoch,zone).strftime('%Y-%m-%d %H:%M %z')
    return value[:-2]+':'+value[-2:]
def calendar_bounds(end,zone):
    start=end-WEEK;result=[]
    while start<end:
        tomorrow=datetime.fromtimestamp(start,zone).date()+timedelta(days=1)
        stop=min(end,int(datetime.combine(tomorrow,time(),zone).timestamp()))
        if stop<=start or len(result)>=9:raise ValueError('invalid calendar period')
        result.append((start,stop));start=stop
    return result

def calendarize(value):
    v=copy.deepcopy(value);v['version']=2
    old=v['days'];v['days']=[]
    if v['observed_at'] is None:return v
    z=ZoneInfo(v['timezone']);v['reset_local']=reset_text(v['reset_at'],z)
    now=max(v['as_of'],v['updated_at'],v['observed_at'])
    for start,end in calendar_bounds(v['reset_at'],z):
        d=dict(start_at=start,end_at=end,label=LABELS[datetime.fromtimestamp(start,z).weekday()],used_delta_pp=None,coverage='unknown')
        if start>now:d.update(used_delta_pp=0,coverage='future')
        elif v['cycle']['state']!='ambiguous':
            for o in old:
                if o['start_at']==start and o.get('end_at',start+DAY)==end and o['coverage']!='future':
                    d.update(used_delta_pp=o['used_delta_pp'],coverage=o['coverage']);break
        v['days'].append(d)
    return v

def scenario(normal,reset,zone='America/Los_Angeles'):
    v=copy.deepcopy(normal);v['timezone']=zone
    end=int(datetime.fromisoformat(reset).replace(tzinfo=ZoneInfo(zone)).timestamp())
    v['reset_at']=end;v['cycle'].update(start_at=end-WEEK,end_at=end)
    v['observed_at']=v['updated_at']=v['as_of']=end-WEEK+3600
    v['days']=[];v=calendarize(v)
    v['status'],v['reason'],v['age_seconds']=('ok',None,0)
    v['days'][0].update(used_delta_pp=3,coverage='partial')
    return v
