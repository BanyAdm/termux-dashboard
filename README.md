# Termux Dashboard

A clean, modern web dashboard for Termux — accessible from any browser on your local network.

## Features
- 🔐 **Login screen** with password protection
- 📁 **File Explorer** — browse, open, edit, delete, create folders
- 💻 **System Specs** — device model, CPU, RAM, storage, Android version
- 📊 **Live Monitor** — CPU %, memory, disk, battery, network, process list (auto-refreshes)
- 🖥 **Terminal** — full shell access with history (↑↓ arrows), quick command buttons
- 👤 **User** — whoami, groups, PATH, env vars, installed packages

## Setup

### 1. Install requirements (Termux)
```bash
pkg update && pkg install python
pip install flask
```

### 2. Copy files
Place both files in the same folder, e.g. `~/dashboard/`:
```
~/dashboard/
  server.py
  index.html
```

### 3. Change your password
Open `server.py` and find line:
```python
PASSWORD = "termux2024"
```
Change `termux2024` to your chosen password.

### 4. Run the server
```bash
cd ~/dashboard
python3 server.py
```

### 5. Access the dashboard
- From Termux device: `http://localhost:8080`
- From other devices on same WiFi: `http://<your-phone-ip>:8080`
  - Find your IP: run `ip addr` or `ifconfig` in Termux

## Tips
- To keep it running in background: `nohup python3 server.py &`
- To stop background server: `pkill -f server.py`
- The server auto-starts at the home directory in the terminal panel
- File double-click opens/enters directories; single-click selects for deletion

## Security Notes
- Only run on trusted networks (home WiFi)
- Change the default password before first use
- The terminal has full shell access — keep the password secret
