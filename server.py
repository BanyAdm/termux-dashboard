#!/usr/bin/env python3
"""
Termux Web Dashboard
  - Flask REST API + static files: port 8080
  - WebSocket PTY terminal:        port 8081
Run:   python3 server.py
Deps:  pip install flask
"""

from flask import Flask, request, jsonify, send_from_directory, session
import os, subprocess, shutil, platform, time, signal, threading
import pty, select, hashlib, base64, struct, socket, secrets
from functools import wraps

# ─── CHANGE YOUR PASSWORD HERE ─────────────────────────────────────
PASSWORD = "termux2024"
# ───────────────────────────────────────────────────────────────────

HOME  = os.path.expanduser('~')
SHELL = os.environ.get('SHELL', '/bin/sh')

app = Flask(__name__, static_folder='.')
app.secret_key = 'txdash-secret-key-change-me'

# ── one-time WS tokens ──────────────────────────────────────────────
_pty_tokens: dict = {}          # token -> {expiry, session_id, keep_alive}
_pty_tokens_lock = threading.Lock()

# ── persistent sessions ─────────────────────────────────────────────
# session_id -> {pid, fd, name, created, keep_alive, connected, lock}
_sessions: dict = {}
_sessions_lock = threading.Lock()

# ══════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

def sh(cmd):
    try:
        return subprocess.check_output(
            cmd, shell=True, stderr=subprocess.DEVNULL, cwd=HOME
        ).decode('utf-8', errors='replace').strip()
    except:
        return ''

def _kill_pid(pid):
    """Kill a process group thoroughly."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except:
            pass
        try:
            os.kill(pid, sig)
        except:
            pass
    # Reap zombie
    try:
        os.waitpid(pid, os.WNOHANG)
    except:
        pass

# ══════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════
@app.route('/api/login', methods=['POST'])
def login():
    if (request.json or {}).get('password') == PASSWORD:
        session['logged_in'] = True
        return jsonify({'success': True})
    return jsonify({'error': 'Wrong password'}), 403

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})

@app.route('/api/check')
def check_auth():
    return jsonify({'logged_in': bool(session.get('logged_in'))})

# ══════════════════════════════════════════════
# PTY SESSIONS API
# ══════════════════════════════════════════════
@app.route('/api/sessions', methods=['GET'])
@login_required
def list_sessions():
    with _sessions_lock:
        result = []
        for sid, s in list(_sessions.items()):
            # Check if process still alive
            alive = True
            try:
                os.kill(s['pid'], 0)
            except:
                alive = False
            if not alive:
                # Reap dead sessions
                try: os.close(s['fd'])
                except: pass
                del _sessions[sid]
                continue
            result.append({
                'id': sid,
                'name': s['name'],
                'created': s['created'],
                'keep_alive': s['keep_alive'],
                'connected': s['connected'],
                'pid': s['pid'],
            })
    return jsonify({'sessions': result})

@app.route('/api/sessions/create', methods=['POST'])
@login_required
def create_session():
    data = request.json or {}
    name = data.get('name', 'Session')
    keep_alive = bool(data.get('keep_alive', False))

    # Spawn the shell in a PTY
    pid, fd = pty.fork()
    if pid == 0:
        env = {**os.environ, 'TERM': 'xterm-256color',
               'HOME': HOME, 'COLORTERM': 'truecolor', 'FORCE_COLOR': '1'}
        try:    os.execvpe(SHELL, [SHELL, '-l'], env)
        except: os.execvpe('/bin/sh', ['/bin/sh'], env)
        os._exit(1)

    sid = secrets.token_hex(8)
    with _sessions_lock:
        _sessions[sid] = {
            'pid': pid, 'fd': fd,
            'name': name,
            'created': time.time(),
            'keep_alive': keep_alive,
            'connected': False,
            'lock': threading.Lock(),
        }

    return jsonify({'session_id': sid})

@app.route('/api/sessions/<sid>/kill', methods=['POST'])
@login_required
def kill_session(sid):
    with _sessions_lock:
        s = _sessions.pop(sid, None)
    if not s:
        return jsonify({'error': 'Session not found'}), 404
    _kill_pid(s['pid'])
    try: os.close(s['fd'])
    except: pass
    return jsonify({'success': True})

@app.route('/api/sessions/<sid>/rename', methods=['POST'])
@login_required
def rename_session(sid):
    name = (request.json or {}).get('name', '')
    with _sessions_lock:
        if sid not in _sessions:
            return jsonify({'error': 'Session not found'}), 404
        _sessions[sid]['name'] = name
    return jsonify({'success': True})

@app.route('/api/sessions/<sid>/keep_alive', methods=['POST'])
@login_required
def set_keep_alive(sid):
    keep = bool((request.json or {}).get('keep_alive', False))
    with _sessions_lock:
        if sid not in _sessions:
            return jsonify({'error': 'Session not found'}), 404
        _sessions[sid]['keep_alive'] = keep
    return jsonify({'success': True})

@app.route('/api/pty/token', methods=['POST'])
@login_required
def pty_token():
    """Get a one-time WS token to connect to an existing session."""
    data = request.json or {}
    sid = data.get('session_id', '')
    with _sessions_lock:
        if sid not in _sessions:
            return jsonify({'error': 'Session not found'}), 404
    tok = secrets.token_hex(20)
    with _pty_tokens_lock:
        _pty_tokens[tok] = {'expiry': time.time() + 30, 'session_id': sid}
    def expire():
        time.sleep(31)
        with _pty_tokens_lock:
            _pty_tokens.pop(tok, None)
    threading.Thread(target=expire, daemon=True).start()
    return jsonify({'token': tok, 'ws_port': 8081})

# ══════════════════════════════════════════════
# FILES
# ══════════════════════════════════════════════
@app.route('/api/files')
@login_required
def list_files():
    path = os.path.realpath(os.path.expanduser(request.args.get('path','') or HOME))
    parent = os.path.dirname(path) if path != '/' else None
    try:
        names = sorted(os.listdir(path))
    except PermissionError:
        return jsonify({'error':'Permission denied','path':path,'parent':parent,'entries':[]})
    except Exception as e:
        return jsonify({'error':str(e),'path':path,'parent':parent,'entries':[]})
    entries = []
    for name in names:
        full = os.path.join(path, name)
        try:
            s = os.stat(full); isdir = os.path.isdir(full)
            entries.append({'name':name,'path':full,'is_dir':isdir,
                            'size':0 if isdir else s.st_size,
                            'modified':s.st_mtime,'hidden':name.startswith('.')})
        except:
            entries.append({'name':name,'path':full,'is_dir':False,
                            'size':0,'modified':0,'hidden':name.startswith('.')})
    return jsonify({'path':path,'parent':parent,'entries':entries})

@app.route('/api/file/read')
@login_required
def read_file():
    path = request.args.get('path','')
    try:
        if os.path.getsize(path) > 524288:
            return jsonify({'error':'File too large (>512 KB)'})
        with open(path,'r',encoding='utf-8',errors='replace') as f:
            return jsonify({'content':f.read(),'path':path})
    except Exception as e:
        return jsonify({'error':str(e)}), 500

@app.route('/api/file/write', methods=['POST'])
@login_required
def write_file():
    d = request.json or {}
    try:
        with open(d['path'],'w',encoding='utf-8') as f: f.write(d['content'])
        return jsonify({'success':True})
    except Exception as e:
        return jsonify({'error':str(e)}), 500

@app.route('/api/file/new', methods=['POST'])
@login_required
def new_file():
    path = (request.json or {}).get('path','')
    try:
        if os.path.exists(path):
            return jsonify({'error':'Already exists'}), 400
        with open(path,'w') as f: f.write('')
        return jsonify({'success':True})
    except Exception as e:
        return jsonify({'error':str(e)}), 500

@app.route('/api/file/delete', methods=['POST'])
@login_required
def delete_file():
    path = (request.json or {}).get('path','')
    try:
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        return jsonify({'success':True})
    except Exception as e:
        return jsonify({'error':str(e)}), 500

@app.route('/api/file/mkdir', methods=['POST'])
@login_required
def make_dir():
    path = (request.json or {}).get('path','')
    try:
        os.makedirs(path, exist_ok=True)
        return jsonify({'success':True})
    except Exception as e:
        return jsonify({'error':str(e)}), 500

@app.route('/api/file/rename', methods=['POST'])
@login_required
def rename_file():
    d = request.json or {}
    try:
        os.rename(d['src'], d['dst'])
        return jsonify({'success':True})
    except Exception as e:
        return jsonify({'error':str(e)}), 500

# ══════════════════════════════════════════════
# SPECS
# ══════════════════════════════════════════════
@app.route('/api/specs')
@login_required
def get_specs():
    cpu = sh("grep 'model name' /proc/cpuinfo | head -1 | cut -d: -f2").strip() or \
          sh("grep 'Hardware' /proc/cpuinfo | head -1 | cut -d: -f2").strip() or platform.machine()
    mem = {}
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                p = line.split(':')
                if len(p)==2: mem[p[0].strip()] = int(p[1].strip().split()[0])
    except: pass
    disk = shutil.disk_usage(HOME)
    up = 0
    try:
        with open('/proc/uptime') as f: up = float(f.read().split()[0])
    except: pass
    d,h,m = int(up//86400), int(up%86400//3600), int(up%3600//60)
    return jsonify({
        'cpu':{'model':cpu,'cores':sh('nproc') or sh("grep -c '^processor' /proc/cpuinfo"),'arch':platform.machine()},
        'memory':{'total_kb':mem.get('MemTotal',0),'free_kb':mem.get('MemFree',0),
                  'available_kb':mem.get('MemAvailable',0),'buffers_kb':mem.get('Buffers',0),'cached_kb':mem.get('Cached',0)},
        'storage':{'total':disk.total,'used':disk.used,'free':disk.free},
        'system':{'android_version':sh('getprop ro.build.version.release'),
                  'android_sdk':sh('getprop ro.build.version.sdk'),
                  'device_model':sh('getprop ro.product.model'),
                  'kernel':sh('uname -r'),'python':platform.python_version(),
                  'platform':platform.system(),'hostname':platform.node(),
                  'uptime':f"{d}d {h}h {m}m" if d else f"{h}h {m}m"},
    })

# ══════════════════════════════════════════════
# MONITOR
# ══════════════════════════════════════════════
def _procs():
    procs, mtotal = [], 0
    try:
        with open('/proc/meminfo') as f:
            for l in f:
                if l.startswith('MemTotal'): mtotal = int(l.split()[1]); break
    except: pass
    for pid in os.listdir('/proc'):
        if not pid.isdigit(): continue
        try:
            with open(f'/proc/{pid}/comm') as f: name = f.read().strip()
            st = {}
            with open(f'/proc/{pid}/status') as f:
                for l in f:
                    k,_,v = l.partition(':'); st[k.strip()] = v.strip()
            with open(f'/proc/{pid}/stat') as f: sv = f.read().split()
            rss = int(st.get('VmRSS','0 kB').split()[0])
            uid = st.get('Uid','0').split()[0]
            try:
                import pwd; user = pwd.getpwuid(int(uid)).pw_name
            except: user = uid
            procs.append({'pid':int(pid),'name':name[:28],
                          'state':st.get('State','?').split()[0],
                          'user':user,'mem_kb':rss,
                          'mem_pct':round(100*rss/mtotal,1) if mtotal else 0,
                          'cpu_time':int(sv[13])+int(sv[14]),
                          'threads':st.get('Threads','1')})
        except: continue
    procs.sort(key=lambda x: x['mem_kb'], reverse=True)
    return procs

@app.route('/api/monitor')
@login_required
def get_monitor():
    try:
        with open('/proc/stat') as f: l=f.readline()
        f1=list(map(int,l.split()[1:8])); time.sleep(0.35)
        with open('/proc/stat') as f: l=f.readline()
        f2=list(map(int,l.split()[1:8]))
        dt=sum(f2)-sum(f1)
        cpu=round(100*(1-(f2[3]-f1[3])/dt),1) if dt else 0
    except: cpu=0
    mem={}
    try:
        with open('/proc/meminfo') as f:
            for l in f:
                p=l.split(':')
                if len(p)==2: mem[p[0].strip()]=int(p[1].strip().split()[0])
    except: pass
    tm=mem.get('MemTotal',1); um=tm-mem.get('MemAvailable',0)
    disk=shutil.disk_usage(HOME)
    rx=tx=0
    try:
        with open('/proc/net/dev') as f:
            for l in f.readlines()[2:]:
                p=l.split()
                if len(p)>9 and not p[0].startswith('lo'): rx+=int(p[1]); tx+=int(p[9])
    except: pass
    bat=sh("termux-battery-status | jq ''.percentage'") or \
        sh("termux-battery-status | jq '.percentage'") or 'N/A'
    procs=_procs()
    return jsonify({'cpu_pct':cpu,'mem_pct':round(100*um/tm,1),'mem_used_kb':um,'mem_total_kb':tm,
                    'disk_pct':round(100*disk.used/disk.total,1),'disk_used':disk.used,'disk_total':disk.total,
                    'processes':procs[:80],'proc_count':len(procs),
                    'net_rx_bytes':rx,'net_tx_bytes':tx,'battery':bat,'timestamp':time.time()})

@app.route('/api/kill', methods=['POST'])
@login_required
def kill_proc():
    d=request.json or {}
    try: pid=int(d.get('pid',0))
    except: return jsonify({'error':'Bad PID'}),400
    sigs={'TERM':signal.SIGTERM,'KILL':signal.SIGKILL,'HUP':signal.SIGHUP}
    try:
        os.kill(pid, sigs.get(d.get('signal','TERM'), signal.SIGTERM))
        return jsonify({'success':True})
    except ProcessLookupError: return jsonify({'error':'Not found'}),404
    except PermissionError:    return jsonify({'error':'Permission denied'}),403
    except Exception as e:     return jsonify({'error':str(e)}),500

# ══════════════════════════════════════════════
# USER
# ══════════════════════════════════════════════
@app.route('/api/user')
@login_required
def get_user():
    path=os.environ.get('PATH','')
    raw=sh('pkg list-installed 2>/dev/null | head -60')
    pkgs=[p.split('/')[0] for p in raw.split('\n') if p][:50]
    ssh=[]
    try: ssh=os.listdir(os.path.join(HOME,'.ssh'))
    except: pass
    return jsonify({
        'whoami':sh('whoami') or os.environ.get('USER','user'),
        'home':HOME,'groups':sh('groups'),
        'shell':os.environ.get('SHELL','/bin/sh'),
        'path_dirs':[p for p in path.split(':') if p],
        'packages_count':sh('pkg list-installed 2>/dev/null | wc -l') or '?',
        'env_vars':[{'key':k,'value':v} for k,v in sorted(os.environ.items())][:60],
        'ssh_keys':ssh,'installed_packages':pkgs,
        'termux_version':sh('cat $PREFIX/etc/termux-version 2>/dev/null'),
        'prefix':os.environ.get('PREFIX','/data/data/com.termux/files/usr'),
    })

# ══════════════════════════════════════════════
# STATIC
# ══════════════════════════════════════════════
@app.route('/')
def index(): return send_from_directory('.','index.html')

@app.route('/<path:p>')
def static_f(p): return send_from_directory('.',p)

# ══════════════════════════════════════════════
# WEBSOCKET PTY SERVER (port 8081)
# ══════════════════════════════════════════════
WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

def _ws_accept(key):
    return base64.b64encode(hashlib.sha1((key+WS_GUID).encode()).digest()).decode()

def _ws_recv(sock):
    try:
        def rx(n):
            buf=b''
            while len(buf)<n:
                c=sock.recv(n-len(buf))
                if not c: raise ConnectionError
                buf+=c
            return buf
        h=rx(2); opcode=h[0]&0x0F; masked=bool(h[1]&0x80); ln=h[1]&0x7F
        if ln==126: ln=struct.unpack('>H',rx(2))[0]
        elif ln==127: ln=struct.unpack('>Q',rx(8))[0]
        mask=rx(4) if masked else b'\x00'*4
        payload=bytearray(rx(ln))
        if masked:
            for i in range(len(payload)): payload[i]^=mask[i%4]
        return opcode,bytes(payload)
    except: return None

def _ws_send(sock, data, opcode=0x02):
    ln=len(data)
    if ln<=125:   hdr=bytes([0x80|opcode,ln])
    elif ln<=65535: hdr=bytes([0x80|opcode,126])+struct.pack('>H',ln)
    else:          hdr=bytes([0x80|opcode,127])+struct.pack('>Q',ln)
    try: sock.sendall(hdr+data); return True
    except: return False

def _handle_ws_client(conn):
    try:
        raw=b''
        while b'\r\n\r\n' not in raw:
            c=conn.recv(4096)
            if not c: return
            raw+=c
        head=raw.split(b'\r\n\r\n')[0].decode('utf-8',errors='replace')
        lines=head.split('\r\n')
        hdrs={}
        for l in lines[1:]:
            if ':' in l:
                k,_,v=l.partition(':'); hdrs[k.strip().lower()]=v.strip()
        req=lines[0] if lines else ''
        qs=req.split(' ')[1] if ' ' in req else ''
        token=''
        for part in qs.split('?')[-1].split('&'):
            if part.startswith('token='): token=part[6:]; break

        with _pty_tokens_lock:
            tok_data=_pty_tokens.pop(token,None)
        if not tok_data or time.time()>tok_data['expiry']:
            conn.sendall(b'HTTP/1.1 403 Forbidden\r\n\r\n'); return

        sid=tok_data['session_id']
        with _sessions_lock:
            s=_sessions.get(sid)
        if not s:
            conn.sendall(b'HTTP/1.1 404 Not Found\r\n\r\n'); return

        # WS handshake
        ws_key=hdrs.get('sec-websocket-key','')
        conn.sendall((
            'HTTP/1.1 101 Switching Protocols\r\n'
            'Upgrade: websocket\r\nConnection: Upgrade\r\n'
            f'Sec-WebSocket-Accept: {_ws_accept(ws_key)}\r\n\r\n'
        ).encode())

        fd=s['fd']
        s['connected']=True
        conn.setblocking(False)

        try:
            while True:
                try:
                    r,_,_=select.select([fd,conn.fileno()],[],[],0.05)
                except (ValueError,OSError):
                    break
                # PTY → WS
                if fd in r:
                    try:
                        data=os.read(fd,4096)
                        if not data: break
                        while True:
                            r2,_,_=select.select([fd],[],[],0.02)
                            if fd not in r2: break
                            more=os.read(fd,4096)
                            if not more: break
                            data+=more
                        if not _ws_send(conn,data,0x02): break
                    except OSError: break
                # WS → PTY
                if conn.fileno() in r:
                    frame=_ws_recv(conn)
                    if frame is None: break
                    op,payload=frame
                    if op==0x08: break
                    if op==0x09: _ws_send(conn,payload,0x0A)
                    elif op in (0x01,0x02):
                        if payload[:1]==b'\x01':
                            try:
                                rows,cols=map(int,payload[1:].split(b','))
                                import fcntl,termios
                                fcntl.ioctl(fd,termios.TIOCSWINSZ,struct.pack('HHHH',rows,cols,0,0))
                            except: pass
                        else:
                            try: os.write(fd,payload)
                            except OSError: break
        finally:
            s['connected']=False
            # If not keep_alive, kill the session entirely
            with _sessions_lock:
                sess=_sessions.get(sid)
            if sess and not sess['keep_alive']:
                with _sessions_lock:
                    _sessions.pop(sid,None)
                _kill_pid(s['pid'])
                try: os.close(fd)
                except: pass
    finally:
        try: conn.close()
        except: pass

def _run_ws_server():
    srv=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    srv.bind(('0.0.0.0',8081))
    srv.listen(32)
    while True:
        try:
            c,_=srv.accept()
            threading.Thread(target=_handle_ws_client,args=(c,),daemon=True).start()
        except: pass

# ══════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════
if __name__=='__main__':
    threading.Thread(target=_run_ws_server,daemon=True).start()
    print('\n'+'='*52)
    print('  Termux Dashboard')
    print('='*52)
    print(f'  Dashboard: http://localhost:8080')
    print(f'  PTY WS:    ws://localhost:8081')
    print(f'  Password:  {PASSWORD}')
    print('='*52+'\n')
    app.run(host='0.0.0.0',port=8080,debug=False,threaded=True)
