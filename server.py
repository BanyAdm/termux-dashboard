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

HOME   = os.path.expanduser('~')
SHELL  = os.environ.get('SHELL', '/bin/sh')

app = Flask(__name__, static_folder='.')
app.secret_key = 'txdash-secret-key-change-me'

# one-time tokens: token -> expiry_time
_pty_tokens: dict = {}
_pty_tokens_lock = threading.Lock()

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
# PTY TOKEN  (login-gated, used by WS server)
# ══════════════════════════════════════════════
@app.route('/api/pty/token', methods=['POST'])
@login_required
def pty_token():
    tok = secrets.token_hex(20)
    with _pty_tokens_lock:
        # expire after 30 s if unused
        _pty_tokens[tok] = time.time() + 30
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
            s = os.stat(full)
            isdir = os.path.isdir(full)
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
                  'available_kb':mem.get('MemAvailable',0),'buffers_kb':mem.get('Buffers',0),
                  'cached_kb':mem.get('Cached',0)},
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
    bat=sh('cat /sys/class/power_supply/battery/capacity 2>/dev/null') or \
        sh('cat /sys/class/power_supply/Battery/capacity 2>/dev/null') or 'N/A'
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
# WEBSOCKET PTY SERVER  (port 8081, pure stdlib)
# ══════════════════════════════════════════════
WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

def _ws_accept(key: str) -> str:
    return base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()
    ).decode()

def _ws_recv(sock) -> tuple | None:
    """Receive one WebSocket frame. Returns (opcode, payload) or None on error."""
    try:
        def recv_exact(n):
            buf = b''
            while len(buf) < n:
                chunk = sock.recv(n - len(buf))
                if not chunk: raise ConnectionError
                buf += chunk
            return buf
        h = recv_exact(2)
        opcode = h[0] & 0x0F
        masked  = bool(h[1] & 0x80)
        ln      = h[1] & 0x7F
        if ln == 126: ln = struct.unpack('>H', recv_exact(2))[0]
        elif ln == 127: ln = struct.unpack('>Q', recv_exact(8))[0]
        mask = recv_exact(4) if masked else b'\x00\x00\x00\x00'
        payload = bytearray(recv_exact(ln))
        if masked:
            for i in range(len(payload)):
                payload[i] ^= mask[i % 4]
        return opcode, bytes(payload)
    except:
        return None

def _ws_send(sock, data: bytes, opcode: int = 0x02) -> bool:
    """Send one WebSocket frame."""
    ln = len(data)
    if ln <= 125:   hdr = bytes([0x80 | opcode, ln])
    elif ln <= 65535: hdr = bytes([0x80 | opcode, 126]) + struct.pack('>H', ln)
    else:            hdr = bytes([0x80 | opcode, 127]) + struct.pack('>Q', ln)
    try:
        sock.sendall(hdr + data)
        return True
    except:
        return False


# ── ANSI → HTML converter (server-side, runs in PTY thread) ─────────
_FG = {
    '30':'#475569','31':'#ef4444','32':'#10b981','33':'#f59e0b',
    '34':'#3b82f6','35':'#7c3aed','36':'#00d4ff','37':'#e2e8f0',
    '39': None,
    '90':'#64748b','91':'#fca5a5','92':'#6ee7b7','93':'#fcd34d',
    '94':'#93c5fd','95':'#c4b5fd','96':'#67e8f9','97':'#f8fafc',
}
_BG = {
    '40':'#1e2d40','41':'#ef4444','42':'#10b981','43':'#f59e0b',
    '44':'#3b82f6','45':'#7c3aed','46':'#00d4ff','47':'#e2e8f0',
    '49': None,
}

class _ANSIState:
    __slots__ = ('fg','bg','bold','italic','ul','in_esc','esc_buf','in_osc')
    def __init__(self):
        self.fg=None; self.bg=None
        self.bold=False; self.italic=False; self.ul=False
        self.in_esc=False; self.esc_buf=''
        self.in_osc=False
    def reset(self):
        self.fg=None; self.bg=None
        self.bold=False; self.italic=False; self.ul=False
    def apply_sgr(self, params):
        i=0
        while i<len(params):
            p=params[i]
            if p in ('','0'):      self.reset()
            elif p=='1':           self.bold=True
            elif p=='22':          self.bold=False
            elif p=='3':           self.italic=True
            elif p=='23':          self.italic=False
            elif p=='4':           self.ul=True
            elif p=='24':          self.ul=False
            elif p in _FG:         self.fg=_FG[p]
            elif p in _BG:         self.bg=_BG[p]
            elif p in ('38','48'):
                if i+1<len(params) and params[i+1]=='5':   i+=2
                elif i+1<len(params) and params[i+1]=='2': i+=4
            i+=1
    def open_span(self):
        styles=[]
        if self.fg:  styles.append(f'color:{self.fg}')
        if self.bg:  styles.append(f'background:{self.bg}')
        if self.bold:   styles.append('font-weight:700')
        if self.italic: styles.append('font-style:italic')
        if self.ul:     styles.append('text-decoration:underline')
        if not styles: return ''
        return f'<span style="{";".join(styles)}">'
    def close_span(self):
        return '</span>'
    @property
    def has_style(self):
        return bool(self.fg or self.bg or self.bold or self.italic or self.ul)

# One persistent state per PTY session (reset on reconnect)
# We store it on the thread-local via the fd key
_pty_ansi: dict = {}   # fd -> _ANSIState

def _ansi_to_html(data: bytes, fd: int) -> str:
    """Convert raw PTY bytes to HTML. Handles ANSI colors, strips control seqs."""
    state = _pty_ansi.get(fd)
    if state is None:
        state = _ANSIState()
        _pty_ansi[fd] = state

    raw = data.decode('utf-8', errors='replace')
    html = []
    span_open = False
    text_buf = []

    def flush_text():
        nonlocal span_open
        if not text_buf: return
        txt = ''.join(text_buf)
        text_buf.clear()
        if state.has_style:
            sp = state.open_span()
            html.append(sp + txt + state.close_span())
            span_open = False
        else:
            html.append(txt)

    i = 0
    while i < len(raw):
        ch = raw[i]

        # Inside OSC — skip until ST or BEL
        if state.in_osc:
            if ch == '\x07' or (ch == '\\' and i>0 and raw[i-1]=='\x1b'):
                state.in_osc = False
            i += 1; continue

        # Inside CSI escape sequence
        if state.in_esc:
            state.esc_buf += ch
            if '\x40' <= ch <= '\x7e':   # final byte
                seq = state.esc_buf
                state.in_esc = False; state.esc_buf = ''
                cmd = seq[-1]
                params = seq[:-1].split(';') if seq[:-1] else ['']
                if cmd == 'm':
                    flush_text()
                    state.apply_sgr(params)
                # all other CSI commands (cursor move, clear, etc.) ignored
            i += 1; continue

        # ESC byte
        if ch == '\x1b':
            nxt = raw[i+1] if i+1<len(raw) else ''
            if nxt == '[':
                state.in_esc = True; state.esc_buf = ''; i += 2; continue
            if nxt == ']':
                state.in_osc = True; i += 2; continue
            if nxt in ('(', ')', '#', '%'): i += 3; continue
            i += 2; continue

        # Strip all other control characters except \t \n
        if ch < ' ' and ch not in ('\t', '\n'):
            # \r — if next char is \n, skip it; otherwise clear line
            if ch == '\r':
                if i+1<len(raw) and raw[i+1]=='\n':
                    i += 1; continue   # \r\n → just \n
                else:
                    # bare \r — treated as line feed for display
                    flush_text(); html.append('<br>'); i += 1; continue
            i += 1; continue

        # Newline
        if ch == '\n':
            flush_text(); html.append('<br>'); i += 1; continue

        # Normal printable character — HTML-escape it
        if ch == '&': text_buf.append('&amp;')
        elif ch == '<': text_buf.append('&lt;')
        elif ch == '>': text_buf.append('&gt;')
        else: text_buf.append(ch)
        i += 1

    flush_text()
    return ''.join(html)

def _handle_ws_client(sock: socket.socket):
    """Perform WS handshake, validate token, then bridge PTY <-> WebSocket."""
    try:
        # --- read HTTP upgrade request ---
        raw = b''
        while b'\r\n\r\n' not in raw:
            chunk = sock.recv(4096)
            if not chunk: return
            raw += chunk
        head = raw.split(b'\r\n\r\n')[0].decode('utf-8', errors='replace')
        lines = head.split('\r\n')
        hdrs = {}
        for l in lines[1:]:
            if ':' in l:
                k, _, v = l.partition(':')
                hdrs[k.strip().lower()] = v.strip()
        # parse query string for token
        req_line = lines[0] if lines else ''
        qs = req_line.split(' ')[1] if ' ' in req_line else ''
        token = ''
        for part in qs.split('?')[-1].split('&'):
            if part.startswith('token='): token = part[6:]; break

        # validate token
        with _pty_tokens_lock:
            exp = _pty_tokens.pop(token, None)
        if not exp or time.time() > exp:
            sock.sendall(b'HTTP/1.1 403 Forbidden\r\n\r\n')
            return

        # --- WebSocket handshake ---
        ws_key = hdrs.get('sec-websocket-key','')
        resp = (
            'HTTP/1.1 101 Switching Protocols\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Accept: {_ws_accept(ws_key)}\r\n'
            '\r\n'
        )
        sock.sendall(resp.encode())

        # --- spawn PTY shell ---
        pid, fd = pty.fork()
        if pid == 0:
            env = {**os.environ, 'TERM':'xterm-256color',
                   'HOME':HOME, 'COLORTERM':'truecolor', 'FORCE_COLOR':'1'}
            try:    os.execvpe(SHELL, [SHELL, '-l'], env)
            except: os.execvpe('/bin/sh',['/bin/sh'], env)
            os._exit(1)

        sock.setblocking(False)

        try:
            while True:
                try:
                    r, _, _ = select.select([fd, sock.fileno()], [], [], 0.05)
                except (ValueError, OSError):
                    break

                # PTY output → WebSocket (as HTML)
                if fd in r:
                    try:
                        data = os.read(fd, 4096)
                        if not data: break
                        html = _ansi_to_html(data, fd)
                        if html:
                            payload = html.encode('utf-8')
                            if not _ws_send(sock, payload, 0x01): break  # text frame
                    except OSError:
                        break

                # WebSocket input → PTY
                if sock.fileno() in r:
                    frame = _ws_recv(sock)
                    if frame is None: break
                    opcode, payload = frame
                    if opcode == 0x08: break          # close
                    if opcode == 0x09:                 # ping → pong
                        _ws_send(sock, payload, 0x0A)
                    elif opcode in (0x01, 0x02):
                        # resize message: b'\x01ROWS,COLS'
                        if payload[:1] == b'\x01':
                            try:
                                rows, cols = map(int, payload[1:].split(b','))
                                import fcntl, termios
                                fcntl.ioctl(fd, termios.TIOCSWINSZ,
                                            struct.pack('HHHH', rows, cols, 0, 0))
                            except: pass
                        else:
                            try: os.write(fd, payload)
                            except OSError: break
        finally:
            try: os.kill(pid, signal.SIGTERM)
            except: pass
            try: os.close(fd)
            except: pass
            _pty_ansi.pop(fd, None)
    finally:
        try: sock.close()
        except: pass

def _run_ws_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', 8081))
    srv.listen(16)
    while True:
        try:
            conn, _ = srv.accept()
            t = threading.Thread(target=_handle_ws_client, args=(conn,), daemon=True)
            t.start()
        except: pass

# ══════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════
if __name__ == '__main__':
    # start WS server in background
    ws_thread = threading.Thread(target=_run_ws_server, daemon=True)
    ws_thread.start()

    print('\n' + '='*52)
    print('  Termux Dashboard')
    print('='*52)
    print(f'  Dashboard: http://localhost:8080')
    print(f'  PTY WS:    ws://localhost:8081')
    print(f'  Password:  {PASSWORD}')
    print('  Deps:      pip install flask')
    print('='*52 + '\n')

    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)
