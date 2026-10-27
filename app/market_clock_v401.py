#!/usr/bin/env python3
import datetime as dt
from zoneinfo import ZoneInfo
NY=ZoneInfo("America/New_York")
H26={(1,1),(1,19),(2,16),(4,3),(5,25),(6,19),(7,3),(9,7),(11,26),(12,25)};E26={(11,27),(12,24)}
def state(now=None):
 n=(now or dt.datetime.now(dt.timezone.utc)).astimezone(NY)
 if n.weekday()>=5 or (n.month,n.day) in H26:return {"session":"CLOSED","execution":False,"refresh_seconds":300}
 m=n.hour*60+n.minute;c=780 if (n.month,n.day) in E26 else 960
 if 240<=m<570:return {"session":"PRE_MARKET","execution":False,"refresh_seconds":60}
 if 570<=m<c:return {"session":"REGULAR","execution":True,"refresh_seconds":60}
 if c<=m<1200:return {"session":"AFTER_HOURS","execution":False,"refresh_seconds":60}
 return {"session":"CLOSED","execution":False,"refresh_seconds":300}
