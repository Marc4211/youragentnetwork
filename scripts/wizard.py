#!/usr/bin/env python3
"""
Standalone browser onboarding wizard for Agent Network.

Stdlib only (no pip installs) so the bootstrap one-liner can launch it on any
box with Python 3. It runs BEFORE our stack exists: it collects config, tests
the existing OpenClaw, writes infra/rocketchat/.env, then runs scripts/install.sh
and streams progress. When the install finishes it hands off to the admin console
and stops.

Run from the repo root (the bootstrap does this for you):
    python3 scripts/wizard.py
Then open the printed URL in your browser.
"""
import http.server
import json
import os
import pathlib
import re
import socketserver
import subprocess
import threading
import urllib.request
import webbrowser

REPO = pathlib.Path(__file__).resolve().parent.parent
RC_DIR = REPO / "infra" / "rocketchat"
ENV_FILE = RC_DIR / ".env"
INSTALL_SH = REPO / "scripts" / "install.sh"
LOG_FILE = pathlib.Path("/tmp/agentnetwork-install.log")
PORT = int(os.environ.get("WIZARD_PORT", "8787"))
# Bind address. Defaults to loopback (reach via SSH tunnel). Set to the box's
# LAN/Tailscale IP to reach the wizard directly from another device on that network.
HOST = os.environ.get("WIZARD_HOST", "127.0.0.1")

STATE = {"running": False, "done": False, "ok": False}

REQUIRED = [
    "INSTANCE_NAME", "ADMIN_EMAIL", "ADMIN_PASS", "INGRESS_PROFILE",
    "OPENCLAW_GATEWAY_URL", "OPENCLAW_GATEWAY_TOKEN", "OPENCLAW_DATA_DIR",
]
# Container name is only used by the docker-restart reload fallback; the default
# hotreload path doesn't need it.
OPTIONAL = ["BRAND_LOGO_URL", "OPENCLAW_CONTAINER_NAME"]
DEFAULTS = {"ADMIN_USERNAME": "admin", "BOT_USERNAME": "lois"}


def test_openclaw(cfg: dict) -> list[dict]:
    """Mirror install.sh's OpenClaw preflight, for live feedback in the wizard.

    Two checks that matter for the default (hot-reload) path on any install
    (Docker or native): the data dir holds openclaw.json, and the gateway is
    reachable on its port. (No docker-network or container check: the glue
    reaches OpenClaw via host.docker.internal, and hot-reload needs no restart.)
    """
    checks = []
    data = cfg.get("OPENCLAW_DATA_DIR", "")
    checks.append({
        "name": "Data dir contains openclaw.json",
        "ok": bool(data) and (pathlib.Path(data) / "openclaw.json").is_file(),
    })
    port_match = re.search(r":(\d+)", cfg.get("OPENCLAW_GATEWAY_URL", ""))
    gok = False
    if port_match:
        try:
            with urllib.request.urlopen(
                f"http://localhost:{port_match.group(1)}/healthz", timeout=5
            ) as r:
                gok = r.status == 200
        except Exception:
            gok = False
    checks.append({"name": "Gateway reachable (/healthz)", "ok": gok})
    return checks


def write_env(cfg: dict) -> None:
    RC_DIR.mkdir(parents=True, exist_ok=True)
    with open(ENV_FILE, "w") as f:
        for k in REQUIRED + OPTIONAL + list(DEFAULTS):
            f.write(f"{k}={cfg.get(k, DEFAULTS.get(k, ''))}\n")


def run_install() -> None:
    STATE.update(running=True, done=False, ok=False)
    with open(LOG_FILE, "w") as logf:
        proc = subprocess.Popen(
            ["bash", str(INSTALL_SH)], stdout=logf, stderr=subprocess.STDOUT, cwd=str(REPO)
        )
        rc = proc.wait()
    STATE.update(running=False, done=True, ok=(rc == 0))


def result_urls() -> dict:
    def get(k):
        if not ENV_FILE.exists():
            return ""
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith(k + "="):
                return line.split("=", 1)[1]
        return ""
    root = get("ROOT_URL") or "http://localhost:3000"
    return {
        "chat": root,
        "admin": root.replace(":3000", ":8000") + "/admin",
        "rcadmin": root + "/admin",
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/log":
            log = LOG_FILE.read_text() if LOG_FILE.exists() else ""
            body = {"log": log, "running": STATE["running"], "done": STATE["done"], "ok": STATE["ok"]}
            if STATE["done"] and STATE["ok"]:
                body["urls"] = result_urls()
            return self._send(200, json.dumps(body))
        return self._send(404, "{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            cfg = json.loads(self.rfile.read(length) or "{}")
        except Exception:
            return self._send(400, json.dumps({"error": "bad json"}))
        if self.path == "/api/test-openclaw":
            return self._send(200, json.dumps({"checks": test_openclaw(cfg)}))
        if self.path == "/api/install":
            missing = [k for k in REQUIRED if not cfg.get(k)]
            if missing:
                return self._send(400, json.dumps({"error": "missing: " + ", ".join(missing)}))
            if STATE["running"]:
                return self._send(409, json.dumps({"error": "install already running"}))
            write_env(cfg)
            threading.Thread(target=run_install, daemon=True).start()
            return self._send(200, json.dumps({"started": True}))
        return self._send(404, "{}")


PAGE = """<!DOCTYPE html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Agent Network - setup</title>
<style>
:root{--jet:#30343f;--ghost:#fafaff;--peri:#e4d9ff;--twi:#273469;--space:#1e2749;--bd:#ddd9f0;--muted:#8b8fa8;--hover:#f0eeff;--warnB:#f0c060;--warnBg:#fffbeb;--warnT:#6b4c00}
*{box-sizing:border-box}
body{margin:0;font-family:'Inter',system-ui,-apple-system,sans-serif;background:var(--ghost);color:var(--jet);min-height:100vh}
.topbar{display:flex;align-items:center;border-bottom:1px solid var(--bd);background:#fff;padding:12px 24px}
.wm b{font-weight:700;font-size:15px;letter-spacing:-.03em}.wm i{color:#2563eb;font-weight:700;font-style:normal}.wm s{font-weight:400;font-size:15px;text-decoration:none}
.layout{display:flex;min-height:calc(100vh - 46px)}
.side{width:208px;flex:none;border-right:1px solid var(--bd);background:#fff;padding:32px 16px}
.side .hd{font-size:10px;font-weight:500;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);margin:0 0 16px;padding:0 8px}
.nav{display:flex;flex-direction:column;gap:2px}
.navbtn{width:100%;display:flex;align-items:center;gap:10px;padding:8px;border:0;background:transparent;border-radius:6px;font-size:14px;color:var(--muted);cursor:pointer;text-align:left}
.navbtn.active{background:var(--peri);color:var(--space)}
.navbtn .num{width:18px;height:18px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;background:var(--bd);color:var(--muted);flex:none}
.navbtn.active .num,.navbtn.done .num{background:var(--twi);color:#fff}
.main{flex:1;display:flex;flex-direction:column;min-width:0}
.cols{flex:1;display:flex;min-width:0}
.form{flex:1;padding:32px 40px;min-width:0}
.help{width:256px;flex:none;border-left:1px solid var(--bd);padding:32px 24px}
h1{font-size:22px;font-weight:600;margin:0 0 4px}
.sub{font-size:14px;color:var(--muted);margin:0 0 28px}
.seclabel{font-size:10px;font-weight:500;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--bd);padding-bottom:8px;margin:24px 0 16px}
.seclabel:first-child{margin-top:0}
.fld{display:block;font-size:12px;margin:0 0 6px}.fld .req{color:#c0392b;margin-right:2px}
input.t,input.m{width:100%;border:1px solid var(--bd);border-radius:6px;padding:9px 12px;font-size:14px;outline:none;background:#fff;color:var(--jet)}
input.m{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:12px;color:var(--space)}
input.t:focus,input.m:focus{border-color:var(--twi);box-shadow:0 0 0 3px #e4d9ff66}
.hint{font-size:12px;color:var(--muted);margin:6px 0 0}
.grid2{display:flex;gap:16px}.grid2>div{flex:1}
.warn{display:flex;gap:10px;border:1px solid var(--warnB);background:var(--warnBg);border-radius:8px;padding:12px 14px;font-size:12px;color:var(--warnT);line-height:1.5;margin-top:16px}
.radio{display:flex;gap:12px;align-items:flex-start;border:1px solid var(--bd);border-radius:8px;padding:14px 16px;cursor:pointer;margin-bottom:8px;background:#fff;opacity:.55}
.radio.sel{border-color:var(--twi);background:#e4d9ff33;opacity:1}
.radio .rb{width:16px;height:16px;border-radius:50%;border:2px solid var(--bd);flex:none;margin-top:2px}
.radio.sel .rb{border-color:var(--twi);background:var(--twi);box-shadow:inset 0 0 0 3px #fff}
.radio .lab{font-size:14px}.radio .desc{font-size:12px;color:var(--muted);margin-top:2px}
.helpcard{border:1px solid var(--bd);background:var(--hover);border-radius:8px;padding:16px;margin-bottom:16px}
.helpcard .ht{font-size:12px;color:var(--space);margin:0 0 8px;font-weight:500}
.helpcard p{font-size:12px;color:#5a5f7a;line-height:1.6;margin:0}
.mono{background:#e4d9ff66;border-radius:3px;padding:1px 4px;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:11px}
.checks{margin-top:14px}.checks .row{display:flex;align-items:center;gap:8px;font-size:13px;padding:4px 0}
.ok{color:#15803d}.bad{color:#b91c1c}.soft{color:#92660a}
.summary{font-size:13px;margin-top:8px;padding:8px 12px;border-radius:8px}
.footer{border-top:1px solid var(--bd);background:#fff;padding:16px 40px;display:flex;align-items:center;justify-content:space-between}
.btn{padding:9px 20px;border-radius:6px;font-size:14px;border:1px solid var(--bd);background:#fff;color:var(--jet);cursor:pointer}
.btn.primary{background:var(--twi);color:#fff;border-color:var(--twi)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.ghost{background:none;border:0;color:var(--muted);cursor:pointer;font-size:14px}.ghost:disabled{opacity:.3;cursor:not-allowed}
.log{white-space:pre-wrap;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:12px;background:#f7f8fa;border:1px solid var(--bd);border-radius:6px;padding:10px;margin-top:16px;max-height:240px;overflow:auto}
.done{background:#e1f5ee;border-radius:8px;padding:14px 16px;margin-top:16px;font-size:13px;line-height:1.7}.done a{color:var(--twi)}
.step{display:none}.step.active{display:block}
@media(max-width:760px){.side,.help{display:none}.form{padding:24px 18px}.footer{padding:16px 18px}.grid2{flex-direction:column;gap:0}}
</style></head><body>
<div class=topbar><div class=wm><b>YourAgent</b><i>.</i><s>Network</s></div></div>
<div class=layout>
  <aside class=side><p class=hd>Setup</p><div class=nav id=nav></div></aside>
  <main class=main>
    <div class=cols>
      <div class=form>
        <h1 id=h1></h1><p class=sub id=sub></p>

        <div class=step data-step=branding>
          <p class=seclabel>Identity</p>
          <label class=fld><span class=req>*</span>Instance name</label>
          <input class=t id=INSTANCE_NAME value="Agent Network" placeholder="e.g. Production Agents">
          <p class=hint>Shown on the join page and in each agent's identity.</p>
        </div>

        <div class=step data-step=admin>
          <p class=seclabel>Credentials</p>
          <div class=grid2>
            <div><label class=fld><span class=req>*</span>Email</label><input class=t id=ADMIN_EMAIL type=email placeholder="you@example.com"></div>
            <div><label class=fld><span class=req>*</span>Password</label><input class=t id=ADMIN_PASS type=password placeholder="min 14 chars"></div>
          </div>
          <div class=warn><span>&#9888;</span><span>Store your password securely. There is no email-based password reset on a self-hosted instance.</span></div>
        </div>

        <div class=step data-step=access>
          <p class=seclabel>Network exposure</p>
          <div id=accesscards></div>
        </div>

        <div class=step data-step=openclaw>
          <p class=seclabel>How is OpenClaw running?</p>
          <p class=hint style="margin:-8px 0 12px">We install alongside the OpenClaw already on this machine (local or a VM &mdash; doesn't matter). Is it running in Docker, or as a native (npm) process?</p>
          <div id=ocruncards></div>
          <p class=seclabel>Gateway</p>
          <label class=fld><span class=req>*</span>Gateway URL</label>
          <input class=m id=OPENCLAW_GATEWAY_URL value="http://host.docker.internal:18789">
          <label class=fld style=margin-top:16px><span class=req>*</span>Gateway token</label>
          <input class=m id=OPENCLAW_GATEWAY_TOKEN type=password placeholder="From your OpenClaw .env (OPENCLAW_GATEWAY_TOKEN)">
          <p class=seclabel>Storage</p>
          <label class=fld>Data dir</label>
          <input class=m id=OPENCLAW_DATA_DIR placeholder="~/.openclaw">
          <p class=hint>OpenClaw's data folder on this machine (contains <span class=mono>openclaw.json</span>). We mount it so new agents can be created &mdash; e.g. <span class=mono>/root/.openclaw</span> or <span class=mono>/Users/you/.openclaw</span>.</p>
          <div id=containerfield style=display:none>
            <label class=fld style=margin-top:16px>Container name <span style=color:#8b8fa8>(optional)</span></label>
            <input class=m id=OPENCLAW_CONTAINER_NAME value="openclaw-openclaw-gateway-1">
            <p class=hint>Only used if you switch to the docker-restart reload fallback. The default applies new agents via hot-reload, so this is usually not needed.</p>
          </div>
          <div class=checks id=checks></div>
          <div id=summary></div>
        </div>

        <div id=log class=log style=display:none></div>
        <div id=done class=done style=display:none></div>
      </div>
      <aside class=help id=help></aside>
    </div>
    <div class=footer>
      <button class=ghost id=prev>&#8592; Previous</button>
      <div style="display:flex;gap:10px">
        <button class=btn id=test style=display:none>Test connection</button>
        <button class="btn primary" id=next>Continue</button>
      </div>
    </div>
  </main>
</div>
<script>
var STEPS=[{id:'branding',label:'Instance name',h1:'Name your instance',sub:"This name shows on the join page and in each agent's identity."},
{id:'admin',label:'Admin account',h1:'Admin account',sub:'This account will have full admin access to the Agent Network.'},
{id:'access',label:'Chat access',h1:'Chat access',sub:'Choose how your team will reach the chat interface.'},
{id:'openclaw',label:'OpenClaw config',h1:'OpenClaw config',sub:'Point this Agent Network at the OpenClaw you already run.'}];
var HELP={branding:{t:'About the instance name',b:'The instance name shows on the join page and in each agent\\'s identity. To change it later, update INSTANCE_NAME in your config and restart.'},
admin:{t:'Admin security',b:'This is your Rocket.Chat admin account; it bypasses role restrictions. Use a strong, unique password and store it safely \\u2014 there is no email-based password reset on a self-hosted instance.'},
access:{t:'How teammates reach the chat',b:'Loopback is SSH-tunnel only; LAN exposes the chat on your network; Tailscale keeps it on a private mesh \\u2014 nothing public, no firewall changes. This is a setup-time choice: changing it later means re-running setup (a brief restart), so pick what fits how your team connects.'},
openclaw:{t:'No LLM key needed',b:'This service proxies requests to the OpenClaw you already run. Your LLM keys stay in OpenClaw; this service never holds or touches them.'}};
var OC_HELP={t:'Finding your gateway details',b:'The glue reaches OpenClaw at <span class=mono>host.docker.internal:18789</span> \\u2014 works whether OpenClaw runs in Docker (with its port published) or natively (npm). The token is in OpenClaw\\'s <span class=mono>.env</span> (<span class=mono>OPENCLAW_GATEWAY_TOKEN</span>).'};
var ACCESS=[{v:'loopback',l:'Loopback',d:'SSH tunnel only (nothing exposed)'},{v:'lan',l:'LAN',d:'Reachable on your network'},{v:'tailscale',l:'Tailscale',d:'Private mesh'}];
var OCRUN=[{v:'docker',l:'In Docker',d:'OpenClaw runs as a container (its docker-compose).'},{v:'native',l:'Natively (npm)',d:'OpenClaw installed via npm; no container.'}];
var idx=0, done=new Set(), access='loopback', ocrun='docker', installing=false, installed=false;

function renderAccess(){
  document.getElementById('accesscards').innerHTML=ACCESS.map(function(o){
    var sel=access===o.v?' sel':'';
    return '<div class="radio'+sel+'" data-v="'+o.v+'"><div class=rb></div><div><div class=lab>'+o.l+'</div><div class=desc>'+o.d+'</div></div></div>';
  }).join('');
  Array.prototype.forEach.call(document.querySelectorAll('#accesscards .radio'),function(el){
    el.onclick=function(){access=el.getAttribute('data-v');renderAccess();};
  });
}
function renderOcRun(){
  document.getElementById('ocruncards').innerHTML=OCRUN.map(function(o){
    var sel=ocrun===o.v?' sel':'';
    return '<div class="radio'+sel+'" data-v="'+o.v+'"><div class=rb></div><div><div class=lab>'+o.l+'</div><div class=desc>'+o.d+'</div></div></div>';
  }).join('');
  Array.prototype.forEach.call(document.querySelectorAll('#ocruncards .radio'),function(el){
    el.onclick=function(){ocrun=el.getAttribute('data-v');renderOcRun();};
  });
  document.getElementById('containerfield').style.display = ocrun==='docker' ? '' : 'none';
}
function render(){
  var step=STEPS[idx];
  document.getElementById('nav').innerHTML=STEPS.map(function(s,i){
    var cls=i===idx?' active':(done.has(s.id)?' done':'');
    var mark=done.has(s.id)?'&#10003;':(i+1);
    return '<button class="navbtn'+cls+'" data-i="'+i+'"><span class=num>'+mark+'</span>'+s.label+'</button>';
  }).join('');
  Array.prototype.forEach.call(document.querySelectorAll('.navbtn'),function(b){b.onclick=function(){idx=+b.getAttribute('data-i');render();};});
  document.getElementById('h1').textContent=step.h1;
  document.getElementById('sub').textContent=step.sub;
  Array.prototype.forEach.call(document.querySelectorAll('.step'),function(el){el.classList.toggle('active',el.getAttribute('data-step')===step.id);});
  var h='<div class=helpcard><p class=ht>'+HELP[step.id].t+'</p><p>'+HELP[step.id].b+'</p></div>';
  if(step.id==='openclaw') h+='<div class=helpcard><p class=ht>'+OC_HELP.t+'</p><p>'+OC_HELP.b+'</p></div>';
  document.getElementById('help').innerHTML=h;
  document.getElementById('prev').disabled=idx===0;
  var isOC=step.id==='openclaw';
  document.getElementById('test').style.display=isOC?'':'none';
  var next=document.getElementById('next');
  next.textContent=isOC?(installed?'Installed':(installing?'Installing\\u2026':'Install')):'Continue';
  next.disabled=isOC&&(installing||installed);
}
function cfg(){
  return {INSTANCE_NAME:val('INSTANCE_NAME'),ADMIN_EMAIL:val('ADMIN_EMAIL'),ADMIN_PASS:val('ADMIN_PASS'),
  INGRESS_PROFILE:access,OPENCLAW_GATEWAY_URL:val('OPENCLAW_GATEWAY_URL'),OPENCLAW_GATEWAY_TOKEN:val('OPENCLAW_GATEWAY_TOKEN'),
  OPENCLAW_DATA_DIR:val('OPENCLAW_DATA_DIR'),OPENCLAW_CONTAINER_NAME:val('OPENCLAW_CONTAINER_NAME')};
}
function val(id){return document.getElementById(id).value;}
document.getElementById('prev').onclick=function(){if(idx>0){idx--;render();}};
document.getElementById('next').onclick=function(){
  if(STEPS[idx].id==='openclaw'){install();return;}
  done.add(STEPS[idx].id); if(idx<STEPS.length-1) idx++; render();
};
document.getElementById('test').onclick=function(){
  var t=this; t.disabled=true; t.textContent='Testing\\u2026';
  document.getElementById('summary').innerHTML='';
  fetch('/api/test-openclaw',{method:'POST',body:JSON.stringify(cfg())}).then(function(r){return r.json()}).then(function(d){
    var hard=0,fail=0;
    document.getElementById('checks').innerHTML=d.checks.map(function(c){
      var cls=c.ok?'ok':(c.soft?'soft':'bad');var mark=c.ok?'&#10003;':(c.soft?'!':'&#10007;');
      if(!c.soft){hard++; if(!c.ok)fail++;}
      return '<div class="row '+cls+'"><span>'+mark+'</span><span>'+c.name+'</span></div>';
    }).join('');
    var s=document.getElementById('summary');
    if(fail===0){s.className='summary ok';s.style.background='#f0fdf4';s.textContent='All required checks passed \\u2014 you can install.';}
    else{s.className='summary bad';s.style.background='#fef2f2';s.textContent=fail+' check'+(fail>1?'s':'')+' failed. Fix the values above and test again.';}
    t.disabled=false; t.textContent='Test connection';
  }).catch(function(){t.disabled=false;t.textContent='Test connection';});
};
function install(){
  installing=true; render();
  document.getElementById('log').style.display='block';
  fetch('/api/install',{method:'POST',body:JSON.stringify(cfg())}).then(function(r){return r.json()}).then(function(d){
    if(d.error){document.getElementById('log').textContent='Error: '+d.error;installing=false;render();return;}
    poll();
  });
}
function poll(){
  fetch('/api/log').then(function(r){return r.json()}).then(function(d){
    var l=document.getElementById('log');l.textContent=d.log;l.scrollTop=l.scrollHeight;
    if(d.done){
      installing=false;
      if(d.ok&&d.urls){
        installed=true;
        document.getElementById('done').style.display='block';
        document.getElementById('done').innerHTML='<strong>Agent Network is installed.</strong><br>Chat: <a href="'+d.urls.chat+'">'+d.urls.chat+'</a><br>Admin console (invites, people, health): <a href="'+d.urls.admin+'">'+d.urls.admin+'</a><br>Manage chat users or change the interface: sign in to the chat as admin, then open Administration (<a href="'+d.urls.rcadmin+'">'+d.urls.rcadmin+'</a>).';
      }
      render(); return;
    }
    setTimeout(poll,1500);
  });
}
renderAccess(); renderOcRun(); render();
</script></body></html>"""


def main():
    # ThreadingHTTPServer sets allow_reuse_address, so a restart doesn't hit
    # "Address already in use" while the old socket is in TIME_WAIT.
    with http.server.ThreadingHTTPServer((HOST, PORT), Handler) as httpd:
        shown = "localhost" if HOST in ("127.0.0.1", "0.0.0.0") else HOST
        url = f"http://{shown}:{PORT}"
        print("\n  Agent Network setup wizard is running.")
        print(f"  Open: {url}")
        if HOST == "127.0.0.1":
            print("  (Remote box? From your laptop: "
                  f"ssh -L {PORT}:127.0.0.1:{PORT} <user>@<host>  then open {url})\n")
        if HOST == "127.0.0.1":
            try:
                webbrowser.open(url)
            except Exception:
                pass
        httpd.serve_forever()


if __name__ == "__main__":
    main()
