import json
import sys
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str((Path(__file__).resolve().parents[1] / "src" / "tak")))
import fpv_tak_sensor as m


CFG = {
    'uid': 'FPV-RF-TEST-PERSISTENT', 'callsign': 'FPV-RF-TEST',
    'position': {'mode': 'auto', 'gpsd_host': '127.0.0.1', 'gpsd_port': 2947,
                 'gpsd_max_age_s': 8, 'movement_threshold_m': 5,
                 'fallback_static': False},
    'input': {'state_file': '/dev/shm/not_a_real_file.json', 'poll_seconds': 0.01, 'max_rf_age_s': 2.5},
    'tak': {'transport': 'udp', 'host': '127.0.0.1', 'port': 6969, 'heartbeat_s': 10,
            'sensor_stale_s': 45, 'alert_repeat_s': 15, 'alert_stale_s': 35,
            'geochat': True, 'chat_cooldown_s': 30, 'alert_visual_ring': True,
            'adversary_uas_marker': True}
}
REGION = {'status': 'PERSISTENT_RF', 'age_s': 0.1, 'band': 'R', 'channel': 8,
          'frequency_mhz': 5917, 'score': 99, 'relative_db': 12}
LOC1 = m.valid_location(-33.5000, -70.6000, how='m-g')
LOC2 = m.valid_location(-33.5010, -70.6000, how='m-g')

class FakeStop:
    def __init__(self, steps):
        self.steps = steps
        self.at = 0
    def is_set(self):
        return self.at >= self.steps
    def wait(self, _seconds):
        self.at += 1

class FakeGps:
    samples = []
    def __init__(self, *args):
        self.idx = 0
        self.sock = object()
    def read(self):
        v = self.samples[min(self.idx, len(self.samples) - 1)]
        self.idx += 1
        return v
    def close(self):
        pass

class FakeSender:
    instances = []
    def __init__(self, cfg, dry_run):
        self.cfg = cfg
        self.dry_run = dry_run
        self.transport = 'udp'
        self.sock = None
        self.events = []
        self.instances.append(self)
    def send(self, packet):
        self.events.append(ET.fromstring(packet))
    def close(self):
        pass

class TestV32(unittest.TestCase):
    def test_movement_distance_and_same_uid(self):
        self.assertFalse(m.location_changed(LOC1, LOC1))
        self.assertTrue(m.location_changed(LOC2, LOC1))
        a = ET.fromstring(m.sensor_cot(CFG, LOC1, True, 'SCAN', REGION))
        b = ET.fromstring(m.sensor_cot(CFG, LOC2, True, 'SCAN', REGION))
        self.assertEqual(a.get('uid'), b.get('uid'))
        self.assertNotEqual(a.find('point').get('lat'), b.find('point').get('lat'))
    def test_expiry_payloads(self):
        s = ET.fromstring(m.sensor_cot(CFG, LOC1, False, 'OFFLINE', REGION, expire=True))
        self.assertEqual(s.get('uid'), CFG['uid'])
        self.assertEqual(s.find('detail/status').get('readiness'), 'false')
        self.assertEqual(s.find('detail/_fpv_rf_sensor').get('state'), 'OFFLINE')
        self.assertEqual(s.find('point').get('lon'), '-70.6000000')
        self.assertLess((m.dt.datetime.fromisoformat(s.get('stale').replace('Z','+00:00'))-
                         m.dt.datetime.fromisoformat(s.get('time').replace('Z','+00:00'))).total_seconds(), 1.1)
    def test_gpsd_freshness_and_explicit_loss(self):
        # Build a local fake gpsd TCP server and validate TPV receiver behaviour.
        import socket, threading
        srv = socket.socket(); srv.bind(('127.0.0.1',0)); srv.listen(1)
        port = srv.getsockname()[1]
        ready = threading.Event()
        def serve():
            c, _ = srv.accept()
            c.settimeout(3)
            c.recv(512)  # WATCH
            c.sendall(b'{"class":"TPV","mode":3,"lat":-33.5,"lon":-70.6}\n')
            ready.set()
            time.sleep(.2)
            c.sendall(b'{"class":"TPV","mode":1}\n')
            time.sleep(.1)
            c.close(); srv.close()
        t=threading.Thread(target=serve,daemon=True); t.start()
        g=m.GpsdReceiver('127.0.0.1',port,max_age=8)
        found=False
        for _ in range(100):
            if g.read():
                found=True; break
            time.sleep(.005)
        self.assertTrue(found)
        ready.wait(1)
        time.sleep(.26)
        self.assertIsNone(g.read())
        g.close(); t.join(timeout=2)
    def run_scenario(self, gps_samples, steps):
        FakeGps.samples=gps_samples
        FakeSender.instances=[]
        with patch.object(m, 'GpsdReceiver', FakeGps), patch.object(m, 'Sender', FakeSender), \
             patch.object(m, 'read_state', lambda path: {'running':True,'updated_epoch':time.time(),
                    'receiver_mode':'SCAN','overall_state':'PERSISTENT_RF','regions':[REGION]}):
            m.run(CFG, dry_run=True, stopping=FakeStop(steps))
        return FakeSender.instances[-1].events
    def test_movement_refreshes_sensor_and_alert_markers(self):
        events=self.run_scenario([LOC1, LOC2, LOC2], 3)
        sensors=[e for e in events if e.get('uid') == CFG['uid']]
        self.assertGreaterEqual(len(sensors),2)
        self.assertEqual({e.find('point').get('lat') for e in sensors}, {'-33.5000000','-33.5010000'})
        adversaries=[e for e in events if e.get('uid') == CFG['uid']+'-RF-UAS-HYPOTHESIS']
        self.assertGreaterEqual(len(adversaries),2)
        self.assertEqual(len(set(e.get('uid') for e in adversaries)),1)
    def test_gnss_loss_expires_without_false_position(self):
        events=self.run_scenario([LOC1,None,None], 3)
        sensors=[e for e in events if e.get('uid') == CFG['uid']]
        self.assertEqual(len(sensors),2)
        self.assertEqual(sensors[-1].find('detail/_fpv_rf_sensor').get('state'),'OFFLINE')
        self.assertFalse(any(e.find('point').get('lat') == '0.0000000' for e in events))
    def test_clean_shutdown_expires_all_active_markers(self):
        events=self.run_scenario([LOC1,LOC1], 2)
        last=[e for e in events if e.get('uid') == CFG['uid']]
        self.assertEqual(last[-1].find('detail/_fpv_rf_sensor').get('state'),'OFFLINE')
        for suffix in ('-RF-ALERT','-RF-VISUAL-RING','-RF-UAS-HYPOTHESIS'):
            seq=[e for e in events if e.get('uid') == CFG['uid']+suffix]
            self.assertEqual(len(seq),2)
            exp=seq[-1]
            delta=(m.dt.datetime.fromisoformat(exp.get('stale').replace('Z','+00:00'))-
                   m.dt.datetime.fromisoformat(exp.get('time').replace('Z','+00:00'))).total_seconds()
            self.assertLessEqual(delta,1.1)
    def test_default_gps_auto_does_not_use_static_implicitly(self):
        c=json.loads(json.dumps(CFG))
        c['position'].update(lat=-33.5,lon=-70.6, fallback_static=False)
        self.assertEqual(m.validate(c)['position']['mode'],'auto')
        self.assertIsNone(m.valid_location(None,None))

if __name__ == '__main__': unittest.main()
