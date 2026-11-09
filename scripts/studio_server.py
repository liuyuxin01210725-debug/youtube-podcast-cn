#!/usr/bin/env python3
"""Loopback-only workbench. Every engine invocation has its own process."""
import argparse, fcntl, hashlib, json, mimetypes, os, re, secrets, shutil, signal, subprocess, sys, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, unquote
ROOT=Path(__file__).resolve().parents[1]
MAX_UPLOAD=2*1024**3
AUDIO_UPLOAD_EXT=('.wav','.mp3','.mp4','.m4a','.mov','.flac','.ogg','.webm','.aac')
TEXT_UPLOAD_EXT=('.txt','.md')
MAX_BOOK_UPLOAD=8*1024*1024

from studio_preview import preview_limit, select_preview_segments
from diarization_cache import assign_studio_voices
from studio_speakers import expected_speakers, use_male_slots, validate_speaker_voices

def as_bool(value):
    return value is True

def lookup_source_metadata(url):
    """English YouTube title and channel. None when oEmbed is unavailable."""
    try:
        import studio_publish_icloud
        return studio_publish_icloud.fetch_youtube_metadata(url, timeout=5)
    except Exception:
        return None

def lookup_chinese_topic(topic, provider):
    """Chinese episode topic via the studio translation API. None if no key."""
    try:
        import studio_publish_icloud
        return studio_publish_icloud.translate_episode_topic(topic, provider=provider or 'deepseek')
    except Exception:
        return None

def _youtube_metadata(sources):
    urls=[s.get('value') for s in sources if isinstance(s, dict) and s.get('kind')=='youtube' and isinstance(s.get('value'), str)]
    found={}
    if not urls:
        return found
    def one(url):
        try:
            return url, lookup_source_metadata(url)
        except Exception:
            return url, None
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        for url, meta in pool.map(one, urls):
            if isinstance(meta, dict):
                found[url]=meta
    return found

def read(p, default=None):
    try: return json.loads(p.read_text())
    except (OSError,ValueError): return default

def write(p,data):
    t=p.with_suffix('.new'); t.write_text(json.dumps(data,ensure_ascii=False,indent=2)); t.replace(p)

class Studio:
    def __init__(self, root, engine=None, recover=True):
        self.root=Path(root).expanduser().resolve(); self.root.mkdir(parents=True,exist_ok=True)
        self.token=secrets.token_urlsafe(32); self.lock=threading.RLock(); self.process=None; self.active=None
        self.engine=engine or [str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/studio_engine.py')]
        self.stop=False; self.lock_fd=None
        if not recover:
            return
        for p in self.root.iterdir():
            if p.is_dir() and (p/'state.json').exists():
                s=read(p/'state.json',{})
                if s.get('status') in ('running','queued','cancelling'):
                    s.update(status='interrupted',message='工作台已重新启动；可继续任务。'); write(p/'state.json',s)
    def job(self, ident):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',ident): raise ValueError('无效任务')
        p=self.root/ident
        if not p.is_dir() or p.is_symlink(): raise ValueError('任务不存在')
        return p
    def revision(self,p,preview=False):
        doc=read(p/'document.json',{}); config=read(p/'config.json',{})
        segments=doc.get('segments',[])
        selected=select_preview_segments(segments, preview_limit(segments)) if preview else segments
        ids={s.get('speaker_id') for s in selected}; backend=config.get('tts','edge')
        voices={sid:doc.get('speakers',{}).get(sid) for sid in ids}
        refs={}
        if backend=='clone':
            for sid in ids:
                ref=doc.get('references',{}).get(sid,{})
                f=(p/ref.get('path','')).resolve()
                digest=hashlib.sha256(f.read_bytes()).hexdigest() if f.is_relative_to(p.resolve()) and f.is_file() else None
                refs[sid]={'text':ref.get('text'),'sha256':digest}
        identity={'segments':[{k:s.get(k) for k in ('id','chinese','speaker_id')} for s in selected], 'voices':voices,'references':refs,'backend':backend,'clone_model':config.get('clone_model')}
        if 'expected_speakers' in config: identity['expected_speakers']=config['expected_speakers']
        return hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    def migrate_assets(self,p):
        s=read(p/'state.json',{}); assets=s.setdefault('asset_revisions',{})
        changed=False
        for name in ('preview.mp3','output.mp3'):
            if (p/name).is_file() and name not in assets:
                assets[name]=None if s.get('stale') else self.revision(p,name=='preview.mp3'); changed=True
        if changed: write(p/'state.json',s)
    def info(self,p):
        self.migrate_assets(p)
        s=read(p/'state.json',{})
        asset_stale={n:s.get('asset_revisions',{}).get(n)!=self.revision(p,n=='preview.mp3') for n in ('preview.mp3','output.mp3') if (p/n).is_file()}
        progress=dict(read(p/'progress.json',{}) or {})
        # Surface durable subtitle-fallback reason even if progress.json was rewritten mid-stage.
        if not progress.get('subtitle_fallback_reason'):
            fallback=read(p/'debug-subtitle-fallback.json') or {}
            reason=fallback.get('reason') if isinstance(fallback,dict) else None
            if reason: progress['subtitle_fallback_reason']=reason
        return dict(id=p.name,title=s.get('title','合成英文测试对话' if p.name=='demo-dialogue' else p.name),status=s.get('status','complete' if (p/'output.mp3').exists() else 'interrupted'),action=s.get('action'),message=s.get('message'),progress=progress,document=read(p/'document.json'),config=read(p/'config.json',{}),files={n:(p/n).exists() for n in ('preview.mp3','output.mp3')},stale=any(asset_stale.values()),asset_stale=asset_stale,accept_unreviewed=bool(s.get('accept_unreviewed')),updated_at=s.get('updated_at',0))
    def list(self):
        with self.lock: return sorted([self.info(p) for p in self.root.iterdir() if p.is_dir() and not p.is_symlink() and (p/'config.json').exists()],key=lambda j:j['updated_at'],reverse=True)
    def create(self, sources, options):
        ids=[]
        if not isinstance(sources,list) or not 1<=len(sources)<=50: raise ValueError('每次可添加 1 至 50 个来源')
        if options.get('action','prepare') not in ('prepare','run'): raise ValueError('操作无效')
        if len({s.get('value') for s in sources})!=len(sources): raise ValueError('请移除重复来源')
        if options.get('provider','deepseek') not in ('deepseek','qwen') or options.get('tts','edge') not in ('clone','edge') or options.get('style','conversation') not in ('conversation','documentary'): raise ValueError('设置无效')
        content_type=options.get('content_type', 'conversation')
        if content_type not in ('conversation', 'monologue', 'book'): raise ValueError('内容类型无效')
        if content_type=='book':
            if len(sources)!=1: raise ValueError('书听一次只收一份你自己的文本')
            if options.get('tts','edge')!='edge': raise ValueError('书听只用一位朗读者的系统音色')
            if options.get('book_language','zh') not in ('zh','en'): raise ValueError('书的语言无效')
        counts=[]
        for source in sources:
            if not isinstance(source, dict): raise ValueError('不支持的来源')
            counts.append(expected_speakers({**options, **{k: source[k] for k in ('expected_speakers',) if k in source}}))
            if content_type=='book' and source.get('kind')!='upload':
                raise ValueError('书听只接受你放入的 txt 或 md，不会从网上抓书')
            if source.get('kind')=='youtube':
                u=urlparse(source.get('value') or '')
                if u.scheme!='https' or u.hostname not in ('youtube.com','www.youtube.com','m.youtube.com','youtu.be'): raise ValueError('请输入有效的 HTTPS YouTube 链接')
            elif source.get('kind')=='upload':
                name=source.get('value')
                if not isinstance(name, str) or not re.fullmatch(r'[a-f0-9]{32}\.[a-z0-9]{1,8}',name): raise ValueError('上传文件无效')
                upload=self.root/'.uploads'/name
                if not upload.is_file(): raise ValueError('上传文件不存在')
                suffix=Path(name).suffix.lower()
                if content_type=='book':
                    if suffix not in TEXT_UPLOAD_EXT: raise ValueError('书听只接受 txt 或 md')
                    if upload.stat().st_size>MAX_BOOK_UPLOAD: raise ValueError('书稿请小于 8 MB')
                elif suffix in TEXT_UPLOAD_EXT:
                    raise ValueError('文本请用内容类型「书」')
                elif suffix not in AUDIO_UPLOAD_EXT:
                    raise ValueError('请选择常见音频或视频文件')
            elif source.get('kind')=='web':
                from studio_urls import is_public_https
                if not isinstance(source.get('value'), str) or not is_public_https(source['value']): raise ValueError('请输入公开的 HTTPS 链接')
            else: raise ValueError('不支持的来源')
        fetched=_youtube_metadata(sources)
        provider=options.get('provider','deepseek')
        with self.lock:
            for source, count in zip(sources, counts):
                ident=uuid.uuid4().hex; p=self.root/ident; p.mkdir()
                import studio_publish_icloud
                if source['kind']=='upload':
                    dest=p/('source'+Path(source['value']).suffix); shutil.move(str(self.root/'.uploads'/source['value']),dest)
                    if content_type=='book':
                        src={'kind':'text','value':str(dest)}
                        label=Path(str(source.get('title') or dest.name)).stem or dest.stem
                    else:
                        src={'kind':'local','value':str(dest)}
                        label=source.get('title') or dest.name
                elif source['kind']=='web':
                    from studio_urls import canonical_url, web_label
                    canonical=canonical_url(source['value'])
                    if not canonical: raise ValueError('请输入公开的 HTTPS 链接')
                    if studio_publish_icloud.youtube_id_from_url(canonical):
                        src={'kind':'youtube','value':canonical}
                        label=studio_publish_icloud.youtube_id_from_url(canonical) or ident
                    else:
                        src={'kind':'web','value':canonical}
                        label=web_label(canonical)
                        if studio_publish_icloud.looks_like_url_title(label): label=ident
                else:
                    src=source
                    label=studio_publish_icloud.youtube_id_from_url(source['value']) or ident
                config={'source':src,'provider':provider,'tts':options.get('tts','edge'),'style':options.get('style','conversation')}
                config.update(content_type=content_type, expected_speakers=count)
                if content_type=='book':
                    config['diarization']='skipped'
                    config['book_language']=options.get('book_language','zh')
                else:
                    if 'prefer_male_slots' in options:
                        config['prefer_male_slots']=options.get('prefer_male_slots') is True and count<=3
                    else:
                        config['prefer_male_slots']=count<=3
                if src['kind']=='youtube': config['diarization_threshold']=0.9
                if config['provider'] not in ('deepseek','qwen') or config['tts'] not in ('clone','edge') or config['style'] not in ('conversation','documentary'): raise ValueError('设置无效')
                write(p/'config.json',config); write(p/'state.json',{'title':label,'status':'queued','action':options.get('action','prepare'),'updated_at':time.time()})
                if src['kind']=='youtube':
                    meta=fetched.get(source['value']) or fetched.get(src.get('value')) or {}
                    given=source.get('title')
                    if not isinstance(given, str) or studio_publish_icloud.looks_like_url_title(given):
                        given=None
                    try:
                        studio_publish_icloud.remember_source_title(
                            p,
                            source_title=meta.get('source_title') or given,
                            video_id=meta.get('video_id') or studio_publish_icloud.youtube_id_from_url(src.get('value') or source['value']),
                            channel=meta.get('channel'),
                            webpage_url=meta.get('webpage_url') or src.get('value') or source['value'],
                            thumbnail_url=meta.get('thumbnail_url'),
                            provider=provider,
                            translator=lambda topic, prov=provider: lookup_chinese_topic(topic, prov),
                        )
                    except Exception:
                        pass
                ids.append(ident)
        return ids
    def voice(self,ident,backend):
        if backend not in ('clone','edge'): raise ValueError('声音模式无效')
        with self.lock:
            p=self.job(ident); self.migrate_assets(p); s=read(p/'state.json',{})
            if s.get('status') in ('running','queued','cancelling'): raise ValueError('请等待任务停止后切换声音')
            config=read(p/'config.json',{})
            if config.get('content_type')=='book' and backend!='edge': raise ValueError('书听只用一位朗读者的系统音色')
            config['tts']=backend; write(p/'config.json',config)
    def set_expected_speakers(self, ident, count):
        with self.lock:
            p=self.job(ident); state=read(p/'state.json',{})
            if state.get('status') in ('running','queued','cancelling'): raise ValueError('请等待任务停止后编辑')
            config=read(p/'config.json',{}); config['expected_speakers']=count
            expected_speakers(config)
            self.migrate_assets(p)
            write(p/'config.json',config)
            doc=read(p/'document.json')
            if doc:
                doc['voices_confirmed']=False; write(p/'document.json',doc)
            state['stale']=True; state['updated_at']=time.time(); write(p/'state.json',state)
    def action(self,ident,action,preview=False,accept_unreviewed=False):
        with self.lock:
            p=self.job(ident); s=read(p/'state.json',{})
            if action=='cancel':
                if s.get('status')=='queued': s['status']='cancelled'
                elif self.active==ident:
                    (p/'cancel').touch(); s['status']='cancelling'
                else: s['status']='cancelled'
            elif action=='delete':
                if self.active==ident or s.get('status')=='queued': raise ValueError('请先取消任务，等待停止后再删除')
                shutil.rmtree(p); return
            else:
                if s.get('status') in ('running','queued','cancelling'): raise ValueError('任务已在队列中')
                if action not in ('prepare','render','run'): raise ValueError('操作无效')
                if action=='render' and not (p/'document.json').exists(): raise ValueError('请先准备译稿')
                s.update(status='queued',action=action,preview=bool(preview),accept_unreviewed=as_bool(accept_unreviewed) if action in ('render','run') else False,message=None)
            s['updated_at']=time.time(); write(p/'state.json',s)
    def _editable(self, ident):
        p=self.job(ident); state=read(p/'state.json',{})
        if state.get('status') in ('running','queued','cancelling'): raise ValueError('请等待任务停止后编辑')
        doc=read(p/'document.json')
        if not isinstance(doc, dict) or not isinstance(doc.get('segments'), list) or not isinstance(doc.get('speakers'), dict):
            raise ValueError('请先准备译稿')
        return p, state, doc
    def confirm_speakers(self, ident):
        """Clear every 待核对 flag without requiring a per-segment edit."""
        with self.lock:
            p, state, doc = self._editable(ident)
            for segment in doc['segments']:
                if isinstance(segment, dict):
                    segment['ambiguous']=False
            write(p/'document.json', doc)
            state['updated_at']=time.time(); write(p/'state.json', state)
    def reset_voice_slots(self, ident):
        """Reapply the male-first studio default in first-appearance order."""
        with self.lock:
            p, state, doc = self._editable(ident)
            slots=assign_studio_voices([segment.get('speaker_id') for segment in doc['segments'] if isinstance(segment, dict) and not segment.get('is_ad')], male_only=use_male_slots(read(p/'config.json',{})))
            for speaker, slot in slots.items():
                record=doc['speakers'].get(speaker)
                if isinstance(record, dict):
                    record['voice_slot']=slot
            doc['voices_confirmed']=False
            write(p/'document.json', doc)
            state['stale']=True; state['updated_at']=time.time(); write(p/'state.json', state)
    def confirm_voices(self, ident):
        with self.lock:
            p, state, doc = self._editable(ident)
            validate_speaker_voices(read(p/'config.json',{}), doc)
            doc['voices_confirmed']=True
            write(p/'document.json', doc)
            state['updated_at']=time.time(); write(p/'state.json', state)
    def engine_args(self, p, state):
        cmd=self.engine+['--job-dir',str(p),'--config',str(p/'config.json'),'--action',state.get('action','run')]
        if state.get('preview'):
            doc=read(p/'document.json',{}) or {}
            segments=doc.get('segments') if isinstance(doc, dict) else []
            cmd+=['--preview', str(preview_limit(segments or []))]
        if state.get('accept_unreviewed'):
            cmd.append('--accept-unreviewed')
        return cmd
    def private_listen_needs_retry(self, p, state):
        if state.get('action')=='prepare' or state.get('preview'): return False
        audio=p/'output.mp3'
        if not audio.is_file() or audio.is_symlink(): return False
        return not (read(p/'private-listen.json') or {}).get('ok')
    def retry_private_listen(self, p):
        # Engine stdout is discarded. Retry here so the workbench window shows the result.
        try:
            import studio_publish_icloud
            try:
                studio_publish_icloud.ensure_publishable_title(p)
            except Exception:
                pass
            result=studio_publish_icloud.publish_job(p)
        except Exception as error:
            print(f'私人听：iCloud 发布未成功（任务成品仍保留）：{type(error).__name__}: {error}', flush=True)
            return
        print(f"私人听：iCloud 资料库已更新 {result.get('episode_dir','')}", flush=True)
        try:
            import studio_push_iphone
            studio_push_iphone.push_after_publish(result, p)
        except Exception:
            pass
    def worker(self):
        while not self.stop:
            with self.lock:
                jobs=[p for p in self.root.iterdir() if p.is_dir() and read(p/'state.json',{}).get('status')=='queued']
                jobs.sort(key=lambda p:read(p/'state.json',{}).get('updated_at',0))
                p=jobs[0] if jobs else None
                if p:
                    self.migrate_assets(p)
                    s=read(p/'state.json'); s['status']='running'; write(p/'state.json',s); self.active=p.name
                    (p/'cancel').unlink(missing_ok=True)
                    cmd=self.engine_args(p, s)
                    try: self.process=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,pass_fds=(self.lock_fd,) if self.lock_fd is not None else ())
                    except OSError: self.process=None
            if p:
                code=self.process.wait() if self.process else 1
                retry_private=False
                with self.lock:
                    s=read(p/'state.json',{}); s['status']='cancelled' if code==130 else ('complete' if code==0 else 'failed')
                    if code==0 and s.get('action')!='prepare':
                        name='preview.mp3' if s.get('preview') else 'output.mp3'
                        if (p/name).is_file(): s.setdefault('asset_revisions',{})[name]=self.revision(p,s.get('preview',False))
                    s['updated_at']=time.time(); write(p/'state.json',s)
                    retry_private=code==0 and self.private_listen_needs_retry(p, s)
                    if code==0 and not retry_private and not s.get('preview') and s.get('action')!='prepare' and (p/'output.mp3').is_file():
                        note=read(p/'private-listen.json') or {}
                        if note.get('ok'): print(f"私人听：iCloud 资料库已更新 {note.get('episode_dir','')}", flush=True)
                        else: print(f"私人听：iCloud 发布未成功（任务成品仍保留）：{note.get('error') or '引擎未留下发布记录'}", flush=True)
                    self.active=None; self.process=None
                if retry_private: self.retry_private_listen(p)
            else: time.sleep(.25)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def send(self,data,status=200,ctype='application/json'):
        b=json.dumps(data,ensure_ascii=False).encode() if ctype=='application/json' else data
        self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(b))); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(b)
    def guard(self,mutate=False):
        host=self.headers.get('Host','')
        if host not in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'): raise ValueError('拒绝非本机请求')
        origin=self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'): raise ValueError('请求来源无效')
        if mutate and self.headers.get('X-CSRF-Token')!=self.server.studio.token: raise ValueError('请刷新页面后重试')
    def do_GET(self):
        try:
            self.guard(); path=urlparse(self.path).path; studio=self.server.studio
            if path=='/api/session': return self.send({'token':studio.token})
            if path=='/api/jobs': return self.send(studio.list())
            m=re.fullmatch(r'/api/jobs/([\w-]+)/files/(preview.mp3|output.mp3|transcript.txt|reference-\w+)',path)
            if m:
                p=studio.job(m[1]); name=m[2]
                if name=='transcript.txt':
                    d=read(p/'document.json',{}); return self.send('\n\n'.join(f"{s.get('speaker_id','')}\n{s.get('text','')}\n{s.get('chinese','')}" for s in d.get('segments',[])).encode(),ctype='text/plain; charset=utf-8')
                if name.startswith('reference-'):
                    ref=read(p/'document.json',{}).get('references',{}).get(name[10:],{}); f=(p/ref.get('path','')).resolve()
                    if not f.is_relative_to(p.resolve()) or not f.is_file(): raise ValueError('参考音频不存在')
                else: f=p/name
                if not f.resolve().is_relative_to(p.resolve()) or not f.is_file(): raise ValueError('文件不存在')
                self.send_response(200); self.send_header('Content-Type',mimetypes.guess_type(f.name)[0] or 'application/octet-stream'); self.send_header('Content-Length',str(f.stat().st_size)); self.end_headers()
                with f.open('rb') as stream: shutil.copyfileobj(stream,self.wfile,1024*1024)
                return
            assets={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
            if path in assets:
                f=ROOT/'studio_static'/assets[path]; return self.send(f.read_bytes(),ctype=mimetypes.guess_type(f.name)[0]+'; charset=utf-8')
            self.send({'error':'不存在'},404)
        except (ValueError,OSError) as e: self.send({'error':str(e)},400)
    def do_POST(self):
        try:
            self.guard(True); studio=self.server.studio; path=urlparse(self.path).path
            length=int(self.headers.get('Content-Length','0'))
            if path=='/api/uploads':
                ext=Path(unquote(self.headers.get('X-Filename',''))).suffix.lower()
                if ext not in AUDIO_UPLOAD_EXT+TEXT_UPLOAD_EXT: raise ValueError('请选择常见音频、视频，或书稿 txt / md')
                if not 0<length<=MAX_UPLOAD: raise ValueError('文件大小须在 2 GB 以内')
                folder=studio.root/'.uploads'; folder.mkdir(exist_ok=True); ident=uuid.uuid4().hex+ext; target=folder/ident
                try:
                    with target.open('wb') as out:
                        remaining=length
                        while remaining:
                            chunk=self.rfile.read(min(1024*1024,remaining))
                            if not chunk: raise ValueError('上传中断')
                            out.write(chunk); remaining-=len(chunk)
                except Exception:
                    target.unlink(missing_ok=True); raise
                return self.send({'id':ident})
            if not 0<length<=2*1024*1024: raise ValueError('请求大小无效')
            data=json.loads(self.rfile.read(length))
            if path=='/api/jobs': return self.send({'ids':studio.create(data.get('sources',[]),data.get('options',{}))})
            m=re.fullmatch(r'/api/jobs/([\w-]+)/(action|document|voice|expected-speakers|confirm-speakers|voice-slots|confirm-voices)',path)
            if not m: raise ValueError('操作不存在')
            if m[2]=='action': studio.action(m[1],data['action'],data.get('preview',False),data.get('accept_unreviewed',False))
            elif m[2]=='voice': studio.voice(m[1],data['tts'])
            elif m[2]=='expected-speakers': studio.set_expected_speakers(m[1],data['expected_speakers'])
            elif m[2]=='confirm-speakers': studio.confirm_speakers(m[1])
            elif m[2]=='voice-slots':
                if not isinstance(data, dict) or data.get('scheme')!='male_first': raise ValueError('音色方案无效')
                studio.reset_voice_slots(m[1])
            elif m[2]=='confirm-voices': studio.confirm_voices(m[1])
            else:
                with studio.lock:
                    p=studio.job(m[1]); studio.migrate_assets(p); state=read(p/'state.json',{})
                    if state.get('status') in ('running','queued','cancelling'): raise ValueError('请等待任务停止后编辑')
                    doc=read(p/'document.json',{}); updates=data.get('segments',[])
                    if len(updates)!=len(doc.get('segments',[])): raise ValueError('译稿段落不完整')
                    for original,new in zip(doc['segments'],updates):
                        if original['id']!=new['id'] or new.get('speaker_id') not in doc['speakers'] or not isinstance(new.get('chinese'),str) or len(new['chinese'])>20000: raise ValueError('译稿数据无效')
                        # A text edit must not sign off every 待核对 row. Only the
                        # row whose speaker actually changed is confirmed here.
                        confirmed = original.get('speaker_id') != new.get('speaker_id')
                        if confirmed: doc['voices_confirmed']=False
                        original.update(chinese=new['chinese'],speaker_id=new['speaker_id'],ambiguous=False if confirmed else bool(original.get('ambiguous')))
                    for ident,slot in data.get('voices',{}).items():
                        if ident not in doc['speakers'] or slot not in ('male_1','male_2','male_3','female_1','female_2'): raise ValueError('音色无效')
                        if doc['speakers'][ident].get('voice_slot')!=slot: doc['voices_confirmed']=False
                        doc['speakers'][ident]['voice_slot']=slot
                    write(p/'document.json',doc); state['stale']=True; write(p/'state.json',state)
            self.send({'ok':True})
        except (ValueError,KeyError,TypeError,AttributeError,OSError) as e: self.send({'error':str(e)},400)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--port',type=int,default=8765); ap.add_argument('--jobs',default='~/Desktop/音频/中文播客工作台/jobs'); args=ap.parse_args()
    owned_root=Path(args.jobs).expanduser().resolve(); owned_root.mkdir(parents=True,exist_ok=True)
    lease=(owned_root/'.server.lock').open('a+')
    try: fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('同一任务目录仍有工作台或引擎运行，请等待停止后再打开。')
    studio=Studio(args.jobs); studio.lock_fd=lease.fileno()
    for p in owned_root.iterdir():
        if p.is_dir() and read(p/'state.json',{}).get('status')=='interrupted':
            shutil.rmtree(p/'temporary',ignore_errors=True)
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler); server.studio=studio
    threading.Thread(target=studio.worker,daemon=True).start(); print(f'中文播客工作台：http://127.0.0.1:{args.port}',flush=True)
    def stop(*_):
        studio.stop=True
        if studio.active: (studio.job(studio.active)/'cancel').touch()
        if studio.process:
            studio.process.send_signal(signal.SIGTERM)
            try: studio.process.wait(timeout=30)
            except subprocess.TimeoutExpired: pass
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    try: server.serve_forever()
    except KeyboardInterrupt:
        studio.stop=True
        if studio.active: (studio.job(studio.active)/'cancel').touch()
        if studio.process:
            studio.process.send_signal(signal.SIGTERM)
            try: studio.process.wait(timeout=30)
            except subprocess.TimeoutExpired: pass
    finally: server.server_close()
if __name__=='__main__': main()
