"""Project contractual source tests; checks without Pi/GPIO."""
import json,sys,unittest
from pathlib import Path
R=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(R/'src/tak'))
import fpv_tak_sensor as tak
class TestReleaseContract(unittest.TestCase):
    def test_volatile_sensor_and_markers(self):
        cfg=json.loads((R/'src/tak/config.example.json').read_text())
        self.assertEqual(cfg['tak']['alert_stale_s'],12)
        self.assertEqual(cfg['tak']['alert_repeat_s'],3)
        self.assertTrue(cfg['tak']['volatile_alerts'])
        self.assertFalse(cfg['tak']['geochat'])
        self.assertEqual(cfg['position']['mode'],'auto')
        self.assertFalse(cfg['position']['fallback_static'])
        tak.validate(cfg)
    def test_legacy_is_interactive_tui(self):
        t=(R/'src/tak/fpv_tak_tui.py').read_text()
        i=(R/'src/tak/cargar_softcert.py').read_text()
        self.assertIn("cmd.append('--legacy')",t)
        self.assertIn("ap.add_argument('--legacy'",i)
    def test_final_sources_not_applied_runtime_patches(self):
        s=(R/'src/tft/display_tft_v19e_fps30.py').read_text()
        self.assertIn('last_cache_poll',s)
        self.assertIn('time.sleep(0.002 if mode_video else 0.015)',s)
        self.assertEqual((R/'src/detector/spi_hz.txt').read_text().strip(),'24000000')
        launcher=(R/'src/detector/iniciar_detector_drone_v19e_consolidado.sh').read_text()
        self.assertIn('--video-sample-rate 10000000',launcher)
    def test_cot_hypothesis_is_not_geolocation(self):
        cfg=json.loads((R/'src/tak/config.example.json').read_text())
        loc=tak.valid_location(-33.5,-70.6)
        import xml.etree.ElementTree as ET
        uas=ET.fromstring(tak.adversary_uas_cot(cfg,loc,{'score':99},False))
        self.assertEqual(uas.get('uid'),cfg['uid']+'-RF-UAS-HYPOTHESIS')
        self.assertEqual(uas.find('detail/_fpv_rf_hypothesis').get('position_status'),'NOT_GEOLOCATED')
if __name__=='__main__':unittest.main()
