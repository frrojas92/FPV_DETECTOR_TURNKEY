import datetime as dt
import json
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET
sys.path.insert(0,str((Path(__file__).resolve().parents[1] / "src" / "tak")))
import fpv_tak_sensor as s
import fpv_tak_tui as ui

CFG={
  'uid':'TEST-SENSOR-123','callsign':'TEST-01',
  'position':{'mode':'static','lat':-33.5,'lon':-70.6,'movement_threshold_m':5},
  'input':{'state_file':'/dev/shm/fpv_status.json'},
  'tak':{'transport':'udp','host':'127.0.0.1','port':6969,
         'alert_repeat_s':3,'alert_stale_s':12,'sensor_stale_s':45,
         'heartbeat_s':10,'volatile_alerts':True,'geochat':False,
         'alert_visual_ring':True,'adversary_uas_marker':True}}
LOC=s.valid_location(-33.5,-70.6)
REG={'band':'R','channel':8,'score':99,'frequency_mhz':5917.0,'relative_db':16}

class SenderMock:
  transport='udp'
  dry_run=True
  sock=None
  def __init__(self):self.items=[]
  def send(self,p):self.items.append(ET.fromstring(p))

class TestV33(unittest.TestCase):
  def test_volatility_values(self):
    self.assertEqual(s.validate(json.loads(json.dumps(CFG)))['tak']['alert_stale_s'],12)
  def test_no_persistent_chat_in_volatile(self):
    cfg=json.loads(json.dumps(CFG));cfg['tak']['geochat']=True
    with self.assertRaises(ValueError):s.validate(cfg)
  def test_reject_long_alert_ttl_in_volatile(self):
    cfg=json.loads(json.dumps(CFG));cfg['tak']['alert_stale_s']=40
    with self.assertRaises(ValueError):s.validate(cfg)
  def test_alerts_ttl(self):
    for fun in (s.spot_cot,s.warning_ring_cot,s.adversary_uas_cot):
      ev=ET.fromstring(fun(CFG,LOC,REG))
      t=dt.datetime.fromisoformat(ev.attrib['time'].replace('Z','+00:00'))
      stale=dt.datetime.fromisoformat(ev.attrib['stale'].replace('Z','+00:00'))
      self.assertGreaterEqual((stale-t).total_seconds(),11)
      self.assertLessEqual((stale-t).total_seconds(),13)
  def test_expire_all_markers_even_if_active_false(self):
    snd=SenderMock()
    s.expire_markers(snd,CFG,LOC,REG,active=False,expire_sensor=True,fast=True)
    self.assertEqual(len(snd.items),4)
    self.assertEqual(len({e.attrib['uid'] for e in snd.items}),4)
    for e in snd.items:
      a=dt.datetime.fromisoformat(e.attrib['time'].replace('Z','+00:00'))
      b=dt.datetime.fromisoformat(e.attrib['stale'].replace('Z','+00:00'))
      self.assertLess((b-a).total_seconds(),0)
  def test_name_updates_and_uid_fixed(self):
    c=json.loads(json.dumps(CFG));c['callsign']='NEW-UNIT'
    self.assertEqual(ET.fromstring(s.sensor_cot(c,LOC,False,'SCAN',None)).attrib['uid'],CFG['uid'])

if __name__=='__main__': unittest.main()
