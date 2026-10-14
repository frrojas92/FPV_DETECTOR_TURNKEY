#!/usr/bin/env python3
"""Tests a TAK mTLS connection without transmitting markers or GeoChat."""
import json
from pathlib import Path
import sys
from fpv_tak_sensor import Sender,validate

cfgpath=Path('/home/cetel/detector_drone/fpv_tak_config.json')
if len(sys.argv)>1: cfgpath=Path(sys.argv[1])
try:
    cfg=validate(json.loads(cfgpath.read_text(encoding='utf8')))
    if cfg['tak'].get('transport')!='tls': raise ValueError('No se configuró transporte TLS')
    sender=Sender(cfg,False)
    try:
        sender._connect()
        sock=sender.sock
        print('TLS version:',sock.version())
        print('Cipher:',sock.cipher()[0])
        cert=sock.getpeercert()
        print('Certificado servidor válido hasta:',cert.get('notAfter'))
        print('Handshake mTLS completado; NO se transmitieron eventos CoT.')
    finally: sender.close()
except (OSError,ValueError,KeyError) as e:
    print('FALLO handshake TLS:',e,file=sys.stderr);sys.exit(2)
