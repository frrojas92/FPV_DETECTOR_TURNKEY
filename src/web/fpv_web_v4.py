#!/usr/bin/env python3
"""FPV WEB TFT V4: frame-driven browser replica of the *existing* V19-E PIL/TFT UI.

Uses the actual display_tft_v19e.render() and handle_tap() functions.
Only reads state/video from /dev/shm; sends supported JSON IPC to the
already-running V19-E detector. Never connects to SPI, GPIO, or HackRF.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import uuid
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from PIL import ImageDraw

PROJECT_DEFAULT = Path('/home/cetel/detector_drone')

HTML = r'''<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark"><title>FPV DETECTOR · TFT V19-E</title>
<style>
:root{color-scheme:dark;font-family:system-ui,DejaVu Sans,sans-serif;background:#080f17;color:#e8eff2}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:12px}
main{width:min(100%,700px);display:grid;justify-items:center;gap:10px}
header{width:100%;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.brand{font-size:15px;letter-spacing:.09em;font-weight:800}.muted{font-size:12px;color:#91a9b5}
.light{display:inline-block;width:8px;height:8px;border-radius:50%;background:#e9b454;vertical-align:middle;margin-right:6px}
.tft-shell{background:#15232d;border:3px solid #344c57;padding:10px;border-radius:16px;box-shadow:0 10px 30px #0008;max-width:100%}
#tft{display:block;width:min(80vw,480px);height:auto;aspect-ratio:3/4;image-rendering:auto;cursor:pointer;touch-action:manipulation;border:1px solid #384f5b;user-select:none;-webkit-user-drag:none}
.controls{display:flex;gap:7px;flex-wrap:wrap;justify-content:center;align-items:center}
button,select{border:1px solid #476875;background:#1e4351;color:#e3f1f1;padding:8px 11px;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer}
button.active{background:#126c73;border-color:#4ddecc}button:focus-visible,#tft:focus-visible{outline:3px solid #6fdccf;outline-offset:3px}
.info{font-size:12px;color:#a5b8c2;max-width:520px;text-align:center;line-height:1.4}#feedback{font-size:12px;color:#ffd38e;min-height:17px;text-align:center}
@media(max-width:420px){.tft-shell{padding:6px;border-radius:12px}#tft{width:min(90vw,480px)}body{padding:6px}}
</style></head><body><main>
<header><div><div class="brand">◉ FPV DETECTOR</div><div class="muted">Raspberry Pi 5 · HackRF One · V19-E RAMTEST</div></div><div class="muted"><span class="light" id="light"></span><span id="connection">Conectando...</span></div></header>
<div class="tft-shell"><img id="tft" src="/screen.png" width="240" height="320" alt="TFT del detector FPV" draggable="false" tabindex="0"></div>
<div class="controls"><button type="button" id="smooth" class="active">Fluido · MJPEG</button><button type="button" id="sharp">Nítido · PNG</button>
<label class="muted" for="fps">Refresco</label><select id="fps" title="Límite máximo de fotogramas transmitidos, solo fotogramas nuevos"><option value="4">4 FPS</option><option value="6">6 FPS</option><option value="8">8 FPS</option><option value="11">11 FPS (TFT)</option><option value="15">15 FPS</option><option value="20">20 FPS</option><option value="25">25 FPS</option><option value="30" selected>30 FPS (NTSC)</option></select>
<button type="button" id="refresh">Reconectar</button><button type="button" id="fullscreen">Pantalla completa</button></div>
<div id="feedback" role="status" aria-live="polite"></div>
<div class="info">Mismos controles táctiles y estado SCAN/VIDEO de V19-E. Fluido envía solo los cuadros nuevos (hasta 30 FPS con NTSC) y actualiza el vídeo sin recalcular todos los textos; Nítido conserva los píxeles originales mediante PNG sin pérdidas. No inicia otro HackRF. Acceso local sin contraseña.</div>
</main>
<script>
(()=>{
 const tft=document.getElementById('tft'),fb=document.getElementById('feedback');
 const label=document.getElementById('connection'),light=document.getElementById('light');
 const smooth=document.getElementById('smooth'),sharp=document.getElementById('sharp');
 const fpsSelect=document.getElementById('fps');
 let mode='mjpeg',running=true,pngBusy=false,epoch=0;
 const sleep=ms=>new Promise(r=>setTimeout(r,ms));
 const setStatus=(ok,msg)=>{label.textContent=msg;light.style.background=ok?'#5dcd97':'#fa7474';};
 const closeImage=()=>{if(tft.dataset.url){URL.revokeObjectURL(tft.dataset.url);delete tft.dataset.url;}};
 function reconnect(){
   ++epoch;closeImage();
   if(!running||document.hidden){tft.removeAttribute('src');return;}
   if(mode==='mjpeg'){
     tft.src='/stream.mjpg?fps='+fpsSelect.value+'&t='+Date.now();
   }else{tft.removeAttribute('src');pngLoop(epoch);}
 }
 async function pngFrame(){
   if(pngBusy)return;
   pngBusy=true;
   try{
     const response=await fetch('/screen.png?t='+Date.now(),{cache:'no-store'});
     if(!response.ok)throw Error('HTTP '+response.status);
     const blob=await response.blob();
     const objectURL=URL.createObjectURL(blob);
     try{await new Promise((resolve,reject)=>{const im=new Image();im.onload=resolve;im.onerror=reject;im.src=objectURL;});}
     catch(e){URL.revokeObjectURL(objectURL);throw e;}
     if(mode==='png'&&running&&!document.hidden){const old=tft.dataset.url;tft.src=objectURL;tft.dataset.url=objectURL;if(old)URL.revokeObjectURL(old);}
     else URL.revokeObjectURL(objectURL);
   }finally{pngBusy=false;}
 }
 async function pngLoop(version){
   while(running&&mode==='png'&&epoch===version&&!document.hidden){
     const start=performance.now();
     try{await pngFrame();}catch(e){setStatus(false,'Sin conexión web');await sleep(900);}
     const delay=Math.max(0,1000/Number(fpsSelect.value)-(performance.now()-start));
     if(delay)await sleep(delay);
   }
 }
 async function health(){
   if(document.hidden)return;
   try{const r=await fetch('/api/health',{cache:'no-store'});if(!r.ok)throw Error();const o=await r.json();
     const rate=Number(o.web_fps||0).toFixed(1);
     setStatus(true,(o.receiver_mode==='VIDEO'?'VIDEO':'SCAN')+' · WEB '+rate+' FPS'+' / max '+o.target_fps_max);}
   catch(e){setStatus(false,'Sin conexión web');}
 }
 function setMode(next){mode=next;smooth.classList.toggle('active',mode==='mjpeg');sharp.classList.toggle('active',mode==='png');reconnect();}
 smooth.onclick=()=>setMode('mjpeg');sharp.onclick=()=>setMode('png');fpsSelect.onchange=reconnect;
 document.getElementById('refresh').onclick=reconnect;
 document.getElementById('fullscreen').onclick=()=>{if(document.fullscreenElement)document.exitFullscreen();else document.querySelector('.tft-shell').requestFullscreen?.();};
 tft.addEventListener('error',()=>{if(mode==='mjpeg'&&running&&!document.hidden)setStatus(false,'Sin transmisión');});
 tft.addEventListener('click',async ev=>{
   const b=tft.getBoundingClientRect();
   const x=Math.max(0,Math.min(239,Math.floor((ev.clientX-b.left)*240/b.width)));
   const y=Math.max(0,Math.min(319,Math.floor((ev.clientY-b.top)*320/b.height)));
   try{const res=await fetch('/api/tap',{method:'POST',headers:{'Content-Type':'application/json','X-FPV-UI':'tft'},body:JSON.stringify({x,y})});
     const ans=await res.json();if(!res.ok)throw Error(ans.error||('HTTP '+res.status));fb.textContent=ans.notice||'';
     if(mode==='png'){await pngFrame();}}
   catch(e){fb.textContent='No se pudo enviar: '+e.message;}
 });
 tft.addEventListener('keydown',ev=>{if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();fb.textContent='Toca la pantalla para seleccionar un control.';}});
 document.addEventListener('visibilitychange',()=>{reconnect();if(!document.hidden)health();});
 window.addEventListener('pagehide',()=>{running=false;++epoch;closeImage();tft.removeAttribute('src');});
 // Wait until the first lossless PNG is visible before opening the continuous stream.
 window.addEventListener('load',()=>{reconnect();health();setInterval(health,2500);},{once:true});
})();
</script></body></html>''' 
@dataclass
class Session:
    page: str = 'home'
    band: str = 'R'
    page_index: int = 0
    selected: dict | None = None
    last_video: bool = False
    cache: object | None = None
    last_access: float = field(default_factory=time.monotonic)
    last_tap: float = 0.0
    notice: str = ''
    notice_until: float = 0.0
    frame_times: deque = field(default_factory=lambda: deque(maxlen=25))
    web_times: deque = field(default_factory=lambda: deque(maxlen=30))
    lock: threading.RLock = field(default_factory=threading.RLock)
    streamed_image: object | None = None
    streamed_key: object | None = None
    streamed_frame_size: object | None = None
    streamed_at: float = -1.0
    overlay_at: float = -1.0
    force_render: bool = True


class Backend:
    def __init__(self, args):
        self.args = args
        sys.path.insert(0, str(args.project))
        try:
            import display_tft_v19e as tft
        except Exception as exc:
            raise RuntimeError('No se puede importar el renderizador TFT V19-E: '+str(exc)) from exc
        for method in ('render', 'handle_tap', 'read_status', 'valid_regions', 'VideoFrameCache'):
            if not hasattr(tft, method):
                raise RuntimeError('La TFT instalada no incluye '+method)
        self.tft = tft
        self.sessions: dict[str, Session] = {}
        self.guard = threading.Lock()
        self.render_slots = threading.BoundedSemaphore(3)
        self.stream_slots = threading.BoundedSemaphore(3)
        self.command_lock = threading.Lock()
        self.last_command = 0.0
        self.html = HTML.encode('utf-8')

    def session(self, ident: str | None = None):
        now = time.monotonic()
        with self.guard:
            if ident and ident in self.sessions:
                s = self.sessions[ident]
                s.last_access = now
                return ident,s,False
            # Prevent unbounded memory use.
            for k, old in list(self.sessions.items()):
                if now-old.last_access > 3600:
                    del self.sessions[k]
            if len(self.sessions) >= 32:
                oldest=min(self.sessions, key=lambda k:self.sessions[k].last_access)
                del self.sessions[oldest]
            ident=secrets.token_urlsafe(24)
            s=Session(cache=self.tft.VideoFrameCache(self.args.raw_frame_file,self.args.video_frame_file))
            self.sessions[ident]=s
            return ident,s,True

    def state(self):
        if self.args.demo:
            return self.tft.demo_data()
        return self.tft.read_status(self.args.state_file,2.5)

    def sync(self,s,st):
        video=st.get('receiver_mode')=='VIDEO'
        if video!=s.last_video:
            if video:
                s.page='video'
            elif s.page=='video':
                s.page='signals';s.selected=None;s.page_index=0
            if s.cache is not None:s.cache.reset()
            s.frame_times.clear()
            s.last_video=video
            s.streamed_image=None
            s.streamed_key=None
            s.streamed_frame_size=None
            s.streamed_at=-1.0
            s.overlay_at=-1.0
            s.force_render=True

    def render_image(self,s):
        with s.lock:
            st=self.state();self.sync(s,st)
            now=time.monotonic()
            message=s.notice if now<s.notice_until else ''
            if s.page=='signals' and s.page_index*3 >= len(self.tft.valid_regions(st)):
                s.page_index=0
            frame_img=None;age=None;source=None;fps=None
            if st.get('receiver_mode')=='VIDEO':
                color_only=(st.get('video') or {}).get('backend')=='fpvdec'
                if s.cache.update(3.0,color_only=color_only):
                    s.frame_times.append(now)
                frame_img=s.cache.valid_image(3.0)
                age=s.cache.age()
                source=s.cache.source
                times=s.frame_times
                fps=(len(times)-1)/max(0.001,times[-1]-times[0]) if len(times)>1 else 0.0
            img=self.tft.render(st,s.page,s.band,message,s.page_index,s.selected,
                                frame_path=self.args.video_frame_file,video_fps=fps,
                                frame_image=frame_img,frame_age=age,frame_source=source)
            return img

    def render(self,s):
        image = self.render_image(s)
        out=io.BytesIO()
        image.save(out,format='PNG',compress_level=1)
        return out.getvalue()

    def web_fps(self,s):
        now=time.monotonic()
        times=[v for v in s.web_times if now-v <= 3.0]
        return round((len(times)-1)/max(0.001,times[-1]-times[0]),1) if len(times)>1 else 0.0

    def render_jpeg(self,s):
        image=self.render_image(s)
        out=io.BytesIO()
        image.save(out,format='JPEG',quality=self.args.quality,subsampling=0,optimize=False)
        return out.getvalue()

    def render_jpeg_fresh(self,s):
        """Render only new source video or timed UI overlays.

        VIDEO: mirrors TFT optimization: cached background and fast video-region
        blit. SCAN: redraw about once per 0.30 s, as in the actual TFT.
        Never buffers source frames: VideoFrameCache holds only the latest one.
        Returns None when no new image should be sent to the browser.
        """
        with s.lock:
            now=time.monotonic()
            st=self.state()
            self.sync(s,st)
            video=st.get('receiver_mode')=='VIDEO'
            message=s.notice if now<s.notice_until else ''
            if not video:
                if not s.force_render and now-s.streamed_at<0.30:
                    return None
                if s.page=='signals' and s.page_index*3>=len(self.tft.valid_regions(st)):
                    s.page_index=0
                img=self.tft.render(st,s.page,s.band,message,s.page_index,s.selected)
                s.streamed_image=img
                s.streamed_key=None
                s.overlay_at=now
            else:
                color_only=(st.get('video') or {}).get('backend')=='fpvdec'
                if s.cache.update(3.0,color_only=color_only):
                    s.frame_times.append(now)
                key=s.cache.key
                frame=s.cache.valid_image(3.0)
                fps=((len(s.frame_times)-1)/max(.001,s.frame_times[-1]-s.frame_times[0])
                     if len(s.frame_times)>1 else 0.0)
                new_video=key is not None and key!=s.streamed_key and frame is not None
                overlay_due=now-s.overlay_at>=1.0
                if not (s.force_render or new_video or overlay_due):
                    return None
                # Only the video area changes for successive frames, matching
                # Display.video_frame() in display_tft_v19e.py.
                if (new_video and not overlay_due and not s.force_render
                        and s.streamed_image is not None):
                    img=s.streamed_image
                    if frame.size != s.streamed_frame_size:
                        # Reset the video rectangle only on aspect/size change.
                        ImageDraw.Draw(img).rectangle((1,68,238,249),fill=(7,13,18))
                    img.paste(frame,(120-frame.width//2,159-frame.height//2))
                else:
                    img=self.tft.render(
                        st,s.page,s.band,message,s.page_index,s.selected,
                        frame_path=self.args.video_frame_file,video_fps=fps,
                        frame_image=frame,frame_age=s.cache.age(),
                        frame_source=s.cache.source)
                    s.overlay_at=now
                s.streamed_image=img
                s.streamed_key=key
                s.streamed_frame_size=frame.size if frame is not None else None
            s.streamed_at=now
            s.force_render=False
            out=io.BytesIO()
            img.save(out,format='JPEG',quality=self.args.quality,subsampling=0,optimize=False)
            return out.getvalue()

    def atomic_json(self,path,payload):
        # UNIQUE temp path: TFT uses the fixed '*.tmp' name; don't interfere.
        path.parent.mkdir(exist_ok=True,parents=True)
        tmp=path.with_name(path.name+'.web.'+uuid.uuid4().hex+'.tmp')
        try:
            with tmp.open('x',encoding='utf-8') as f:
                json.dump(payload,f,separators=(',',':'),allow_nan=False)
                f.flush()
            os.replace(tmp,path)
        finally:
            tmp.unlink(missing_ok=True)

    def command(self,cmd,region=None):
        if self.args.demo:
            return 'SIMULACIÓN: sin comando al detector'
        if cmd not in ('START_VIDEO','STOP_VIDEO','TOGGLE_AUDIO'):
            raise ValueError('Comando no permitido')
        now=time.time()
        with self.command_lock:
            # Detector polls this single-slot IPC every 100 ms. Avoid bursts.
            if time.monotonic()-self.last_command < 0.45:
                raise ValueError('Espera 0,5 s entre comandos')
            try:
                mtime=self.args.command_file.stat().st_mtime
                existing=json.loads(self.args.command_file.read_text(encoding='utf-8'))
                if (existing.get('schema')==1 and
                    0 <= now-mtime < 0.30 and
                    0 <= now-float(existing.get('epoch',0)) < 0.30):
                    raise ValueError('Hay una orden reciente; espera 0,3 s')
            except (FileNotFoundError, json.JSONDecodeError, UnicodeError, TypeError):
                pass
            data={'schema':1,'command':cmd,'epoch':now,'id':uuid.uuid4().hex}
            if cmd=='START_VIDEO':
                if not isinstance(region,dict) or type(region.get('frequency_mhz')) is not int:
                    raise ValueError('Primero selecciona una región RF')
                freq=region['frequency_mhz']
                if freq not in {f for freqs in self.tft.BANDS.values() for f in freqs}:
                    raise ValueError('Frecuencia fuera de las bandas FPV disponibles')
                data['frequency_mhz']=freq
                selection={'schema':1,'mode':'SCAN','video_ready':False,
                           'note':'Operator-selected frequency; no RF validation on VIDEO entry',
                           'requested_epoch':now,'frequency_mhz':freq,
                           'band':str(region['band']),'channel':int(region['channel']),
                           'score':int(region.get('score',0)),
                           'members':list(region.get('members',[]))}
                self.atomic_json(self.args.selection_file,selection)
            self.atomic_json(self.args.command_file,data)
            self.last_command=time.monotonic()
            return 'ORDEN ENVIADA: '+cmd

    def tap(self,s,x,y):
        with s.lock:
            now=time.monotonic()
            if now-s.last_tap < 0.18:
                raise ValueError('Pulsaciones demasiado rápidas')
            s.last_tap=now
            st=self.state();self.sync(s,st)
            connected=st.get('overall_state') not in ('NO_DATA','STOPPED','DISCONNECTED')
            page,band,idx,selected,cmd=self.tft.handle_tap(
                x,y,s.page,s.band,connected,st,s.page_index,s.selected)
            s.page,s.band,s.page_index,s.selected=page,band,idx,selected
            s.force_render=True
            notice=''
            if cmd:
                try:
                    notice=self.command(cmd,selected)
                except (OSError,ValueError) as exc:
                    notice=str(exc)
            s.notice=notice;s.notice_until=time.monotonic()+2
            return {'page':page,'band':band,'action':cmd,'notice':notice}


class Server(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    def __init__(self,address,backend):
        self.backend=backend
        super().__init__(address,Handler)


class Handler(BaseHTTPRequestHandler):
    server_version='FPV-Web-TFT/4.0'
    sys_version=''

    def log_message(self,fmt,*args):
        # Do not log passwords, cookies, or querystrings.
        try:
            print('[FPV WEB] '+fmt % args,flush=True)
        except Exception:
            pass

    def send_bytes(self,status,data,mime,extra=None):
        self.send_response(status)
        self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store, max-age=0')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if extra:
            for k,v in extra.items():self.send_header(k,v)
        self.end_headers()
        try:self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError):pass

    def json(self,status,obj,extra=None):
        self.send_bytes(status,json.dumps(obj,ensure_ascii=False,separators=(',',':')).encode('utf-8'),'application/json; charset=utf-8',extra)

    def get_session(self):
        import http.cookies
        cookies=http.cookies.SimpleCookie()
        try:cookies.load(self.headers.get('Cookie',''))
        except http.cookies.CookieError:pass
        ident=cookies['fpv_sid'].value if 'fpv_sid' in cookies else None
        return self.server.backend.session(ident)

    def same_origin(self):
        origin=self.headers.get('Origin')
        if not origin:return False
        parsed=urllib.parse.urlsplit(origin)
        return parsed.scheme in ('http','https') and parsed.netloc==self.headers.get('Host')

    def do_GET(self):
        path=urllib.parse.urlsplit(self.path).path
        if path not in ('/','/screen.png','/stream.mjpg','/api/health'):
            self.send_error(404);return
        ident,s,new=self.get_session()
        extra={'Set-Cookie':f'fpv_sid={ident}; HttpOnly; SameSite=Strict; Path=/'} if new else None
        if path=='/':
            self.send_bytes(200,self.server.backend.html,'text/html; charset=utf-8',extra)
        elif path=='/stream.mjpg':
            self.stream_video(s,extra)
        elif path=='/api/health':
            st=self.server.backend.state()
            self.json(200,{'detector':st.get('overall_state','NO_DATA'),
                          'receiver_mode':st.get('receiver_mode','UNKNOWN'),
                          'page':s.page,'client_sessions':len(self.server.backend.sessions),
                          'target_fps_max':self.server.backend.args.fps,
                          'web_fps':self.server.backend.web_fps(s)},extra)
        else:
            b=self.server.backend
            if not b.render_slots.acquire(timeout=0.7):
                self.json(429,{'error':'Servidor ocupado'},extra);return
            try:
                data=b.render(s)
                self.send_bytes(200,data,'image/png',extra)
                s.web_times.append(time.monotonic())
            except Exception as exc:
                print('ERROR render web:',repr(exc),file=sys.stderr,flush=True)
                self.json(500,{'error':'Error renderizando TFT'},extra)
            finally:b.render_slots.release()

    def stream_video(self,s,extra=None):
        b=self.server.backend
        if not b.stream_slots.acquire(blocking=False):
            self.json(503,{'error':'Máximo de 3 clientes de vídeo; prueba PNG'});return
        # Multipart continuous JPEG, one TCP request, no browser polling delay.
        q=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        try:
            fps=max(1.0,min(b.args.fps,float(q.get('fps',[str(b.args.fps)])[0])))
            if not (fps==fps):raise ValueError()
        except (ValueError,TypeError,OverflowError):fps=b.args.fps
        period=1.0/fps
        boundary=b'fpvframe'
        self.close_connection=True
        try:
            self.send_response(200)
            self.send_header('Content-Type','multipart/x-mixed-replace; boundary=fpvframe')
            self.send_header('Cache-Control','no-store, no-cache, must-revalidate')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('X-Frame-Options','DENY')
            self.send_header('X-Accel-Buffering','no')
            if extra:
                for key,val in extra.items():self.send_header(key,val)
            self.end_headers()
            try:self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            except OSError:pass
            try:self.connection.settimeout(3.0)
            except OSError:pass
            next_due=0.0
            while True:
                started=time.monotonic()
                if started<next_due:
                    time.sleep(min(0.025,next_due-started))
                    continue
                if not b.render_slots.acquire(timeout=0.08):
                    time.sleep(0.02)
                    continue
                try:
                    frame=b.render_jpeg_fresh(s)
                finally:
                    b.render_slots.release()
                if frame is None:
                    time.sleep(0.008 if s.last_video else 0.040)
                    continue
                packet=(b'--'+boundary+b'\r\nContent-Type: image/jpeg\r\nContent-Length: '
                        +str(len(frame)).encode('ascii')+b'\r\n\r\n')
                self.wfile.write(packet)
                self.wfile.write(frame)
                self.wfile.write(b'\r\n')
                self.wfile.flush()
                s.web_times.append(time.monotonic())
                # Start-based period; no backlog and no forced extra sleep.
                next_due=started+period
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,TimeoutError,OSError):
            pass
        except Exception as exc:
            print('ERROR streaming web:',repr(exc),file=sys.stderr,flush=True)
        finally:
            b.stream_slots.release()

    def do_POST(self):
        path=urllib.parse.urlsplit(self.path).path
        if path!='/api/tap':self.send_error(404);return
        if not self.same_origin():self.json(403,{'error':'Origen no autorizado'});return
        if not self.headers.get('Content-Type','').lower().startswith('application/json'):
            self.json(415,{'error':'Se requiere JSON'});return
        if self.headers.get('X-FPV-UI')!='tft':self.json(403,{'error':'Cabecera de control faltante'});return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<2 or length>128:raise ValueError('Solicitud demasiado grande')
            obj=json.loads(self.rfile.read(length))
            x,y=obj['x'],obj['y']
            if type(x) is not int or type(y) is not int or not (0<=x<240 and 0<=y<320):
                raise ValueError('Coordenadas fuera de la TFT')
        except (ValueError,KeyError,TypeError,json.JSONDecodeError):
            self.json(400,{'error':'Coordenadas invalidas'});return
        _,s,new=self.get_session()
        try:
            response=self.server.backend.tap(s,x,y)
            self.json(200,response)
        except (ValueError,OSError) as exc:
            self.json(409,{'error':str(exc)})
        except Exception as exc:
            print('ERROR tap web:',repr(exc),file=sys.stderr,flush=True)
            self.json(500,{'error':'Error interno al manejar la pulsacion'})


def main():
    p=argparse.ArgumentParser(description='Replica web TFT V19-E sin tocar HackRF/SPI')
    p.add_argument('--project',type=Path,default=PROJECT_DEFAULT)
    p.add_argument('--bind',default='127.0.0.1')
    p.add_argument('--port',type=int,default=8765)
    p.add_argument('--fps',type=float,default=30.0,help='Límite FPS web, 1 a 30; por defecto 30 (solo fotogramas nuevos)')
    p.add_argument('--quality',type=int,default=95,help='Calidad JPEG 70 a 98')
    p.add_argument('--state-file',type=Path,default=Path('/dev/shm/fpv_status.json'))
    p.add_argument('--command-file',type=Path,default=None)
    p.add_argument('--selection-file',type=Path,default=None)
    p.add_argument('--raw-frame-file',type=Path,default=None)
    p.add_argument('--video-frame-file',type=Path,default=None)
    p.add_argument('--demo',action='store_true',help='Datos sinteticos; los botones no controlan hardware')
    args=p.parse_args()
    args.project=args.project.resolve()
    if not (1 <= args.fps <= 30):p.error('--fps debe estar entre 1 y 30')
    if not (70 <= args.quality <= 98):p.error('--quality debe estar entre 70 y 98')
    args.command_file=args.command_file or (args.project/'fpv_touch_command.json')
    args.selection_file=args.selection_file or (args.project/'fpv_video_selection.json')
    args.video_frame_file=args.video_frame_file or (args.project/'fpv_video_frame.png')
    backend=Backend(args)
    httpd=Server((args.bind,args.port),backend)
    print(f'FPV Web TFT V4 sin auth, MJPEG {args.fps}fps Q{args.quality} | listening {args.bind}:{httpd.server_port} | project={args.project} | demo={args.demo}',flush=True)
    try:httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:pass
    finally:httpd.server_close()

if __name__=='__main__':main()
