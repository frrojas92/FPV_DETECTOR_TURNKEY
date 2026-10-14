import copy, datetime as dt, unittest, xml.etree.ElementTree as ET, sys, time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str((Path(__file__).resolve().parents[1] / "src" / "tak")))
import fpv_tak_sensor as m

CFG={
  'uid':'TEST-SENSOR-331','callsign':'TEST-SENSOR',
  'position':{'mode':'static','lat':-33.5,'lon':-70.6,'movement_threshold_m':5},
  'input':{'state_file':'/dev/shm/no_test_fpv.json','poll_seconds':0.01,'max_rf_age_s':2.5},
  'tak':{'transport':'udp','host':'127.0.0.1','port':6969,
         'heartbeat_s':10,'sensor_stale_s':45,'alert_repeat_s':3,
         'alert_stale_s':12,'volatile_alerts':True,'geochat':False,
         'alert_visual_ring':True,'adversary_uas_marker':True}}
REG={'status':'PERSISTENT_RF','age_s':0.1,'band':'R','channel':8,'frequency_mhz':5917,'relative_db':11,'score':99}

class FakeSender:
    objects=[]
    def __init__(self,cfg,dry_run):
        self.transport='udp';self.dry_run=dry_run;self.sock=None;self.events=[]
        self.objects.append(self)
    def send(self,xml): self.events.append(ET.fromstring(xml))
    def close(self):pass
class StopAfter:
    def __init__(self,steps=2):self.step=0;self.steps=steps
    def is_set(self): return self.step>=self.steps
    def wait(self,s): self.step+=1

class TestOnlyTwoMarkers(unittest.TestCase):
    def simulate(self, steps=2):
        FakeSender.objects=[]
        def state(_):
            return {'running': True,'updated_epoch':time.time(),'receiver_mode':'SCAN',
                    'overall_state':'PERSISTENT_RF','regions':[REG]}
        with patch.object(m,'Sender',FakeSender),patch.object(m,'read_state',state):
            m.run(copy.deepcopy(CFG),dry_run=True,stopping=StopAfter(steps))
        return FakeSender.objects[-1].events
    def test_during_active_rf_only_circle_and_uas(self):
        events=self.simulate()
        active=[e for e in events if e.get('uid').startswith(CFG['uid']+'-RF-')
                and e.get('stale')>e.get('time')]
        self.assertEqual({e.get('uid') for e in active},
                         {CFG['uid']+'-RF-VISUAL-RING', CFG['uid']+'-RF-UAS-HYPOTHESIS'})
        self.assertEqual({e.get('type') for e in active},{m.RING_TYPE,m.HOSTILE_UAS_TYPE})
    def test_legacy_pin_is_only_cleared_never_renewed(self):
        events=self.simulate()
        pin=[e for e in events if e.get('uid')==CFG['uid']+'-RF-ALERT']
        self.assertGreaterEqual(len(pin),1)
        for e in pin:
            self.assertLess(dt.datetime.fromisoformat(e.get('stale').replace('Z','+00:00')),
                            dt.datetime.fromisoformat(e.get('time').replace('Z','+00:00')))
    def test_shutdown_expiry_two_remaining_symbols(self):
        events=self.simulate()
        for uid in (CFG['uid']+'-RF-VISUAL-RING',CFG['uid']+'-RF-UAS-HYPOTHESIS'):
            ev=[e for e in events if e.get('uid')==uid]
            self.assertEqual(len(ev),2)
            self.assertGreater(ev[0].get('stale'),ev[0].get('time'))
            self.assertLess(ev[1].get('stale'),ev[1].get('time'))
    def test_no_geochat_and_sensor_uid_preserved(self):
        events=self.simulate()
        self.assertFalse(any(e.get('type')=='b-t-f' for e in events))
        self.assertTrue(any(e.get('uid')==CFG['uid'] for e in events))
if __name__=='__main__':unittest.main()
