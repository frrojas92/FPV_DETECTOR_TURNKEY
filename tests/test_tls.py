import json
import subprocess
import socket
import ssl
import tempfile
from pathlib import Path
import threading
import unittest
import sys
sys.path.insert(0, str((Path(__file__).resolve().parents[1] / "src" / "tak")))
from fpv_tak_sensor import Sender, validate

def openssl(*args):
    subprocess.run(('openssl',*map(str,args)),check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

class TestTLS(unittest.TestCase):
    def test_verified_client_server_and_cot(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            ca=p/'ca.crt'; cak=p/'ca.key'
            openssl('req','-x509','-newkey','rsa:2048','-nodes','-days','2',
                    '-subj','/CN=FPV Test CA','-addext','basicConstraints=critical,CA:TRUE',
                    '-addext','keyUsage=critical,keyCertSign,cRLSign',
                    '-keyout',cak,'-out',ca)
            for typ in ('server','client'):
                openssl('req','-newkey','rsa:2048','-nodes','-subj',f'/CN={typ}',
                        '-keyout',p/f'{typ}.key','-out',p/f'{typ}.csr')
                ext=p/f'{typ}.ext'
                ext.write_text(('subjectAltName=DNS:localhost\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature,keyEncipherment\n' if typ=='server' else 'extendedKeyUsage=clientAuth\nkeyUsage=digitalSignature\n'))
                openssl('x509','-req','-in',p/f'{typ}.csr','-CA',ca,'-CAkey',cak,
                        '-CAcreateserial','-out',p/f'{typ}.crt','-days','2','-extfile',ext)
            cfg={'uid':'FPV-RF','callsign':'FPV-1','position':{'mode':'disabled'},
                 'input':{'state_file':'/dev/shm/fpv_status.json'},
                 'tak':{'host':'127.0.0.1','port':0,'transport':'tls',
                        'tls':{'ca_file':str(ca),'cert_file':str(p/'client.crt'),
                               'key_file':str(p/'client.key'),'server_name':'localhost'}}}
            serverctx=ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            serverctx.load_cert_chain(str(p/'server.crt'),str(p/'server.key'))
            serverctx.load_verify_locations(cafile=str(ca))
            serverctx.verify_mode=ssl.CERT_REQUIRED
            listener=socket.socket()
            listener.bind(('127.0.0.1',0)); listener.listen(1); listener.settimeout(5)
            cfg['tak']['port']=listener.getsockname()[1]
            delivered=[]
            def run_server():
                try:
                    sock,_=listener.accept()
                    with serverctx.wrap_socket(sock,server_side=True) as peer:
                        data=b''
                        while not data.endswith(b'\n'):
                            data+=peer.recv(4096)
                        delivered.append(data)
                except Exception as e:
                    delivered.append(str(e))
                finally: listener.close()
            t=threading.Thread(target=run_server,daemon=True);t.start()
            sender=Sender(validate(cfg),False)
            sender.send(b'<event version="2.0"></event>')
            sender.close(); t.join(5)
            self.assertEqual(delivered,[b'<event version="2.0"></event>\n'])

if __name__=='__main__': unittest.main()
