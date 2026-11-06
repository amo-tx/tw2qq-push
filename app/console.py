# -*- coding: utf-8 -*-
"""twitter2qq 云控制台 —— 电脑 / 安卓共用的「查看 + 控制」程序。

推送引擎已经全部跑在云端（WorkBuddy 云应用 + QQ 官方机器人），本机不再需要
Flask / NapCat / 代理内核 / 推特抓取 / SQLite，只保留这一个文件：

  · 图形模式（默认）：python console.py
      起一个纯标准库的本地网页，浏览器打开 http://127.0.0.1:5800
      可看状态 / 日志 / 推送记录，可暂停、检查、改间隔、增删博主、回推、
      发群按钮面板，还能一键复制群指令。
  · 命令行模式（Termux 无浏览器时很好用）：
      python console.py status            # 状态快照
      python console.py check             # 立即检查
      python console.py pause | resume
      python console.py interval 60
      python console.py switch translate on
      python console.py feeds | logs 50
      python console.py add @handle | remove 序号 | backfill 序号 24
      python console.py following [关键词]
      python console.py buttons           # 向群发按钮面板并检测权限
      python console.py raw /api/status

配置：同目录 console.json（首次运行自动生成），也可用环境变量
      TW2QQ_CLOUD_URL / TW2QQ_SHARECODE / TW2QQ_PORT / TW2QQ_HOST 覆盖。
依赖：Python 3.8+ 标准库，零第三方包；Windows / Termux / Linux / macOS 通用。
"""
import json
import os
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "console.json"
DEFAULTS = {
    "cloud_url": "https://tw2qq-pusher.app.workbuddy.host",
    "sharecode": "Z3YR5C5DTB",
    "host": "127.0.0.1",
    "port": 5800,
    "refresh_seconds": 30,
}
ENV_KEYS = {
    "cloud_url": "TW2QQ_CLOUD_URL",
    "sharecode": "TW2QQ_SHARECODE",
    "host": "TW2QQ_HOST",
    "port": "TW2QQ_PORT",
    "refresh_seconds": "TW2QQ_REFRESH",
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except Exception as e:
            print("读取 console.json 失败，使用默认配置:", e)
    for key, env in ENV_KEYS.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    try:
        cfg["port"] = int(cfg["port"])
        cfg["refresh_seconds"] = max(5, int(cfg["refresh_seconds"]))
    except (TypeError, ValueError):
        cfg["port"], cfg["refresh_seconds"] = 5800, 30
    if not CONFIG_FILE.exists():
        try:
            CONFIG_FILE.write_text(
                json.dumps(DEFAULTS, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    return cfg


def cloud_call(cfg: dict, path: str, method: str = "GET",
               body=None, timeout: int = None) -> dict:
    """把请求转发到云端实例；sharecode 每次都带上，不需要浏览器管 Cookie。"""
    if not path.startswith("/api/"):
        # Git Bash / MSYS 会把 /api/xxx 改写成 C:/.../api/xxx，提取回真实路径
        m = re.search(r"/api/[A-Za-z0-9_/.\-?=&%]*", str(path))
        if not m:
            raise ValueError("只允许转发 /api/ 路径")
        path = m.group(0)
    url = cfg["cloud_url"].rstrip("/") + path
    if cfg.get("sharecode"):
        url += ("&" if "?" in path else "?") + "sharecode=" + \
               urlparse.quote(str(cfg["sharecode"]), safe="")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") \
        if body is not None else None
    req = urlrequest.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Accept": "application/json",
                 "User-Agent": "tw2qq-console/1.0"})
    if timeout is None:
        timeout = 90 if data else 30
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urlerror.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:220]
        raise RuntimeError(f"云端返回 HTTP {e.code}: {detail}") from None
    except Exception as e:
        raise RuntimeError(f"无法连接云端（{cfg['cloud_url']}）: {e}") from None
    try:
        return json.loads(raw)
    except Exception:
        return {"ok": False, "error": "云端返回的不是 JSON", "raw": raw[:300]}


# ---------------------------------------------------------------------------
# 命令行模式
# ---------------------------------------------------------------------------
CLI_HELP = """用法：
  python console.py [status|check|pause|resume|interval N|switch KEY on|off]
  python console.py feeds|logs [N]|add @handle|remove 序号|backfill 序号 [小时]
  python console.py following [关键词] | buttons | raw /api/路径
  python console.py media @handle [noprobe]   # 诊断视频/GIF 抓取与 CDN 可达性
  python console.py media_test [@handle] [send]  # 单条 GIF 端到端自检（默认不群发）
  python console.py relay [probe|setup|status|sendtest|selftest]  # QQ 小号转发器
  python console.py [serve] [--port N] [--host IP] [--no-browser]
不带参数 = 启动图形控制台。KEY = translate|send_images|send_videos|at_all
转发器也可独立运行：python relay.py [probe|setup|status|sendtest|selftest]"""


def _cli(cfg: dict, argv: list) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(CLI_HELP)
        return 0
    cmd, rest = argv[0].lower(), argv[1:]

    def post(action, **kw):
        return cloud_call(cfg, "/api/control", "POST", {"action": action, **kw})

    def get(path):
        return cloud_call(cfg, path)

    try:
        if cmd in ("raw",):
            if not rest:
                print("需要路径，例：raw /api/status")
                return 2
            result = get(rest[0]) if len(rest) == 1 else \
                cloud_call(cfg, rest[0], "POST", json.loads(rest[1]))
        elif cmd in ("status", "health", "snapshot", "状态"):
            result = get("/api/console/snapshot")
        elif cmd in ("check", "检查"):
            result = post("check")
        elif cmd in ("pause", "暂停"):
            result = post("pause")
        elif cmd in ("resume", "恢复"):
            result = post("resume")
        elif cmd == "interval" and rest:
            result = post("interval", value=int(rest[0]))
        elif cmd == "switch" and len(rest) >= 2:
            result = post("switch", key=rest[0],
                          value=rest[1].lower() in ("1", "on", "开", "true", "yes"))
        elif cmd in ("feeds", "博主"):
            result = get("/api/feeds")
        elif cmd in ("logs", "日志"):
            result = post("logs", n=int(rest[0]) if rest else 50)
        elif cmd in ("add", "关注") and rest:
            result = post("add", handle=rest[0].lstrip("@"))
        elif cmd in ("remove", "移除") and rest:
            arg = rest[0].lstrip("@")
            result = post("remove", **({"index": int(arg)} if arg.isdigit()
                                       else {"key": arg}))
        elif cmd in ("backfill", "回推") and rest:
            arg = rest[0].lstrip("@")
            kw = {"index": int(arg)} if arg.isdigit() else {"key": arg}
            if len(rest) > 1:
                kw["hours"] = int(rest[1])
            result = post("backfill", **kw)
        elif cmd in ("following", "关注列表"):
            result = post("following")
            if result.get("ok") and rest:
                kw = " ".join(rest).lower()
                result["users"] = [u for u in result["users"]
                                   if kw in (u.get("handle") or "").lower()
                                   or kw in (u.get("name") or "").lower()]
                result["filtered"] = len(result["users"])
        elif cmd in ("buttons", "按钮"):
            result = post("buttons_test")
        elif cmd == "self_test":
            result = post("self_test")
        elif cmd in ("media", "媒体", "media_diag") and rest:
            from urllib.parse import quote as _quote
            probe = "0" if len(rest) > 1 and rest[1].lower() in ("0", "no", "false", "noprobe") else "1"
            result = get(f"/api/x/raw?handle={_quote(rest[0].lstrip('@'))}&probe={probe}")
        elif cmd in ("media_test", "媒体自检", "gif_test"):
            kw = {}
            send_flags = ("send", "发", "1", "true", "yes")
            for arg in rest:
                if arg.lower() in send_flags:
                    kw["send"] = True
                else:
                    kw["handle"] = arg.lstrip("@")
            result = post("media_test", **kw)
        else:
            print("未知命令:", " ".join(argv))
            print(CLI_HELP)
            return 2
    except Exception as e:
        print("操作失败:", e)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", True) else 1


# ---------------------------------------------------------------------------
# 图形模式：纯标准库 HTTP 服务 + 单页控制台
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>推特QQ推送 · 云控制台</title>
<style>
:root{--bg:#f5f7fb;--card:#fff;--line:#e6eaf2;--text:#1f2733;--dim:#71809a;
--blue:#2563eb;--blue-soft:#eaf1ff;--green:#0f9d58;--amber:#d97706;--red:#dc2626}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding:18px 16px 48px}
header{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:16px}
h1{font-size:19px;margin:0 6px 0 0}.sub{color:var(--dim);font-size:12px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:99px;
background:#e8f5ee;color:var(--green);font-size:12px;font-weight:600}
.pill.bad{background:#fdeaea;color:var(--red)}
.spacer{flex:1}
button{font:inherit;cursor:pointer;border:1px solid var(--line);background:#fff;color:var(--text);
border-radius:9px;padding:7px 12px;transition:.15s}
button:hover{border-color:#b9c6df;background:#f8faff}
button.primary{background:var(--blue);border-color:var(--blue);color:#fff}
button.primary:hover{background:#1d4fd7}
button.warn{color:var(--red)}button.small{padding:4px 9px;font-size:12px}
button:disabled{opacity:.5;cursor:default}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;
box-shadow:0 2px 10px rgba(25,40,80,.035);margin-bottom:14px}
.card h2{font-size:14px;margin:0 0 12px;display:flex;align-items:center;gap:8px}
.card h2 .tag{font-weight:400;color:var(--dim);font-size:12px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:9px}
.kv div{background:#f8faff;border-radius:9px;padding:8px 10px;min-width:0}
.kv .k{color:var(--dim);font-size:11px}.kv .v{font-weight:600;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px}
input,select{font:inherit;border:1px solid var(--line);border-radius:9px;padding:7px 10px;
background:#fff;color:var(--text);min-width:0}
input[type=number]{width:100px}.hint{color:var(--dim);font-size:12px}
.switch{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);
border-radius:9px;padding:6px 10px;background:#fff;cursor:pointer;font-size:13px}
.switch .dot{width:9px;height:9px;border-radius:50%;background:#c3ccdc}
.switch.on .dot{background:var(--green)}.switch.off .dot{background:#c3ccdc}
.list{display:flex;flex-direction:column;gap:7px;max-height:300px;overflow:auto}
.item{display:flex;align-items:center;gap:8px;padding:8px 10px;background:#f8faff;
border-radius:9px;font-size:13px}
.item .name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item .meta{color:var(--dim);font-size:11px}
pre{background:#0f172a;color:#d7e3f7;border-radius:9px;padding:11px;overflow:auto;
max-height:230px;font:12px/1.55 Consolas,Monaco,monospace;margin:0}
.chips{display:flex;flex-wrap:wrap;gap:7px}
.chip{border:1px solid #cddcf7;background:var(--blue-soft);color:#1d4fd7;
border-radius:99px;padding:5px 11px;font-size:12px}
.chip:hover{background:#dbe7ff}
.banner{display:none;padding:10px 13px;border-radius:10px;background:#fdeaea;
color:var(--red);margin-bottom:14px;font-size:13px}
.toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);background:#1f2733;
color:#fff;padding:9px 16px;border-radius:10px;font-size:13px;opacity:0;
pointer-events:none;transition:.25s;z-index:9;max-width:90vw}
.toast.show{opacity:1}
.group-title{color:var(--dim);font-size:12px;margin:10px 0 4px}
a{color:var(--blue);text-decoration:none}
@media(max-width:600px){.wrap{padding:12px 10px 40px}h1{font-size:17px}
.card{padding:13px}.kv{grid-template-columns:repeat(2,1fr)}}
</style></head><body><div class="wrap">
<header>
  <h1>🐦 推特QQ推送</h1><span class="sub">云控制台 · 查看与控制</span>
  <span id="pill" class="pill">连接中…</span><span class="spacer"></span>
  <button onclick="load()">🔄 刷新</button>
  <a href="__CLOUD__/?sharecode=__SHARE__" target="_blank"><button>打开云端页面</button></a>
</header>
<div id="banner" class="banner"></div>

<div class="card"><h2>运行状态 <span class="tag" id="upd"></span></h2>
  <div class="kv" id="kv"></div>
  <div class="row">
    <button class="primary" onclick="act('check')">立即检查</button>
    <button onclick="act('pause')">暂停推送</button>
    <button onclick="act('resume')">恢复推送</button>
    <button onclick="act('buttons_test')">群按钮自检</button>
    <button onclick="selfTest()">运行自检</button>
  </div>
  <div class="hint" style="margin-top:8px">「群按钮自检」会向默认群发一条带按钮的面板消息，
  用于确认 QQ 平台是否给本机器人开了消息按钮权限；没开也会自动降级为纯文本。</div>
</div>

<div class="grid">
  <div class="card"><h2>抓取与开关</h2>
    <div class="row">
      <span>抓取间隔</span>
      <input id="interval" type="number" min="30" max="3600" step="10">
      <span>秒</span>
      <button class="small" onclick="saveInterval()">保存</button>
    </div>
    <div class="row" id="switches"></div>
    <div class="hint">开关修改立即热生效，无需重启云端。</div>
  </div>

  <div class="card"><h2>添加博主</h2>
    <div class="row">
      <input id="handle" placeholder="@handle，例：meicyououenbu" style="flex:1;min-width:160px">
      <button class="primary" onclick="addFeed()">添加并回推</button>
    </div>
    <div class="hint" style="margin-top:8px">只加进推送列表，不会在 X 上取关 / 关注；
    自动绑定云端默认群。</div>
    <div class="row"><button class="small" onclick="loadFollowing()">载入我的 X 关注列表</button>
      <input id="fsearch" placeholder="筛选 handle / 昵称" oninput="renderFollowing()" style="flex:1;min-width:120px"></div>
    <div class="list" id="following" style="max-height:180px"><span class="hint">未载入</span></div>
  </div>
</div>

<div class="card"><h2>当前推送的博主 <span class="tag" id="feedCount"></span></h2>
  <div class="list" id="feeds"></div>
</div>

<div class="grid">
  <div class="card"><h2>最近推送 <span class="tag" id="total"></span></h2>
    <div class="list" id="history"></div></div>
  <div class="card"><h2>实时日志</h2><pre id="logs">载入中…</pre></div>
</div>

<div class="card"><h2>群指令速查 <span class="tag">点一下复制，粘贴到群里 @机器人 发送</span></h2>
  <div id="cmds"></div>
  <div class="hint" style="margin-top:10px">更推荐：在群里 @机器人 发「按钮」或「菜单」，
  点消息下方的按钮即可把正确指令自动填进输入框。</div>
</div>
</div><div id="toast" class="toast"></div>
<script>
let S=null, USERS=[], REFRESH=__REFRESH__;
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(msg){const t=$('toast');t.textContent=msg;t.classList.add('show');
  clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove('show'),2600);}
async function bridge(path,body){
  const opt=body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{};
  const r=await fetch('/api/bridge?path='+encodeURIComponent(path),opt);
  return await r.json();
}
async function act(action,extra){
  const r=await bridge('/api/control',Object.assign({action:action},extra||{}));
  toast(r.ok?(r.message||'完成'):('失败：'+(r.error||'未知错误')));
  if(r.ok)setTimeout(load,900);
  return r;
}
function kv(k,v,color){return '<div><div class="k">'+esc(k)+'</div><div class="v"'+
  (color?' style="color:'+color+'"':'')+'>'+esc(v)+'</div></div>';}
function yn(v){return v?'✅ 正常':'❌ 异常';}
function render(s){
  S=s;const h=s.health||{},st=s.status||{},cfg=s.config||{},w=h.watchdog||{};
  const ok=h.ok;$('pill').className='pill'+(ok?'':' bad');
  $('pill').textContent=ok?'● 云端在线':'● 云端异常';
  $('upd').textContent='更新于 '+(s.time||'--');
  $('banner').style.display='none';
  let items=[];
  items.push(kv('服务状态',ok?'运行中':'需检查',ok?'#0f9d58':'#dc2626'));
  items.push(kv('自动推送',h.paused?'已暂停':(h.polling?'运行中':'已停止'),
                 h.paused?'#d97706':null));
  items.push(kv('QQ 通道',yn(h.bot_ok)));
  items.push(kv('X 登录态',cfg.x_configured?'✅ 已配置':'❌ 未配置'));
  const cd=s.cloud_db||{};
  items.push(kv('云数据库',cd.enabled?
    ('✅ '+(cd.cloud_seen_rows!=null?cd.cloud_seen_rows+' 条记录':'已连接')):
    (h.cloud_ok?'✅ 已连接':'⚠ 未启用'),cd.enabled?'#0f9d58':null));
  items.push(kv('代理内核',yn(h.proxy_ok)));
  items.push(kv('守护模式',w.mode||'--'));
  items.push(kv('上次检查',h.last_check||'--'));
  items.push(kv('运行轮次',w.loops!=null?w.loops+' 轮':'--'));
  items.push(kv('实例运行',w.uptime_seconds!=null?
    Math.floor(w.uptime_seconds/3600)+' 小时 '+Math.floor(w.uptime_seconds%3600/60)+' 分':'--'));
  const at=h.at_commands||{},btn=h.buttons||{};
  items.push(kv('@ 指令',at.enabled?(at.events+' 次 / 回复 '+at.replies):'未挂载'));
  items.push(kv('群消息按钮',btn.supported?('✅ '+btn.mode):
    (btn.mode==='unsupported'?'⚠ 平台未开通':'未测试')));
  $('kv').innerHTML=items.join('');
  $('interval').value=cfg.poll_seconds||120;
  const sw=[['translate','自动翻译'],['send_images','推文配图'],
            ['send_videos','视频 / GIF'],['buttons','群消息按钮']];
  $('switches').innerHTML=sw.map(([k,label])=>{
    const on=!!cfg[k];return '<button class="switch '+(on?'on':'off')+
    '" onclick="toggle(\''+k+'\','+(!on)+')"><span class="dot"></span>'+label+
    ' · '+(on?'开':'关')+'</button>';}).join('');
  const feeds=cfg.feeds||[];$('feedCount').textContent=feeds.length+' 位';
  $('feeds').innerHTML=feeds.length?feeds.map((f,i)=>
    '<div class="item"><span class="name"><b>'+(i+1)+'.</b> @'+esc(f.key)+
    ' <span class="meta">'+esc(f.name)+'</span></span>'+
    '<span class="meta">'+(f.groups||[]).length+' 群</span>'+
    '<button class="small" onclick="act(\'backfill\',{key:\''+esc(f.key)+'\'})">回推</button>'+
    '<button class="small warn" onclick="removeFeed(\''+esc(f.key)+'\')">移除</button></div>').join('')
    :'<span class="hint">暂无博主</span>';
  const hist=s.history||[];const stats=s.stats||{};
  $('total').textContent='累计 '+(stats.total!=null?stats.total:'--')+' 条';
  $('history').innerHTML=hist.length?hist.map(x=>{
    const t=x.text||x.title||x.summary||x.link||JSON.stringify(x);
    return '<div class="item"><span class="name">'+esc(String(t).slice(0,90))+
      '</span><span class="meta">'+esc(x.time||x.created_at||x.ts||'')+'</span></div>';}).join('')
    :'<span class="hint">暂无记录</span>';
  $('logs').textContent=(s.logs||[]).join('\n')||'暂无日志';
  const cat=s.commands||[];
  $('cmds').innerHTML=cat.map(g=>'<div class="group-title">'+esc(g.group)+
    '</div><div class="chips">'+g.items.map(it=>'<button class="chip" onclick="copyCmd(\''+
    esc(it.cmd).replace(/'/g,"\\'")+'\')" title="'+esc(it.desc)+'">'+esc(it.label)+'</button>').join('')+
    '</div>').join('');
}
function toggle(k,v){if(k==='buttons')return act('buttons',{value:v});return act('switch',{key:k,value:v});}
function saveInterval(){const n=parseInt($('interval').value,10);
  if(!(n>=30&&n<=3600))return toast('间隔需 30~3600 秒');act('interval',{value:n});}
function addFeed(){const v=$('handle').value.trim().replace(/^@/,'');
  if(!v)return toast('请输入 handle');act('add',{handle:v});$('handle').value='';}
function removeFeed(k){if(confirm('移除 @'+k+' ？（仅停止推送，不会取关 X）'))
  act('remove',{key:k});}
async function loadFollowing(){toast('正在从 X 拉取关注列表…');
  const r=await bridge('/api/control',{action:'following'});
  if(!r.ok)return toast('失败：'+(r.error||'未知错误'));
  USERS=r.users||[];renderFollowing();toast('已载入 '+USERS.length+' 位');}
function renderFollowing(){const kw=($('fsearch').value||'').toLowerCase();
  const have=new Set(((S&&S.config&&S.config.feeds)||[]).map(f=>f.key.toLowerCase()));
  const list=USERS.filter(u=>!kw||(u.handle||'').toLowerCase().includes(kw)||
    (u.name||'').toLowerCase().includes(kw)).slice(0,40);
  $('following').innerHTML=list.length?list.map(u=>'<div class="item"><span class="name">@'+
    esc(u.handle)+' <span class="meta">'+esc(u.name||'')+'</span></span>'+
    (have.has((u.handle||'').toLowerCase())?'<span class="meta">已推送</span>':
     '<button class="small" onclick="act(\'add\',{handle:\''+esc(u.handle)+'\'})">加入推送</button>')+
    '</div>').join(''):'<span class="hint">'+(USERS.length?'无匹配':'点击上方按钮载入')+'</span>';}
function copyCmd(cmd){const done=()=>toast('已复制「'+cmd+'」，到群里 @机器人 粘贴发送');
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(cmd).then(done,()=>fallbackCopy(cmd,done));
  }else fallbackCopy(cmd,done);}
function fallbackCopy(t,done){const ta=document.createElement('textarea');ta.value=t;
  document.body.appendChild(ta);ta.select();try{document.execCommand('copy');done();}
  catch(e){toast('复制失败，请手动输入：'+t);}document.body.removeChild(ta);}
async function selfTest(){toast('自检中…');const r=await bridge('/api/control',{action:'self_test'});
  if(r.ok)$('logs').textContent=r.text;else toast('自检失败：'+(r.error||''));}
async function load(){try{const s=await bridge('/api/console/snapshot');
  if(s.error)throw new Error(s.error);
  try{s.cloud_db=await bridge('/api/cloud/status');}catch(_){s.cloud_db=null;}
  render(s);}catch(e){
  $('banner').style.display='block';
  $('banner').textContent='无法连接云端：'+e.message+'（检查 console.json 里的 cloud_url / sharecode）';
  $('pill').className='pill bad';$('pill').textContent='● 离线';}}
load();setInterval(load,REFRESH*1000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    cfg = {}
    server_version = "tw2qq-console/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("TW2QQ_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, code: int, data: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        u = urlparse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            html = (INDEX_HTML
                    .replace("__REFRESH__", str(self.cfg["refresh_seconds"]))
                    .replace("__CLOUD__", self.cfg["cloud_url"].rstrip("/"))
                    .replace("__SHARE__", urlparse.quote(str(self.cfg.get("sharecode") or ""))))
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if u.path == "/api/config":
            self._json({"ok": True, "cloud_url": self.cfg["cloud_url"],
                        "sharecode_set": bool(self.cfg.get("sharecode")),
                        "refresh_seconds": self.cfg["refresh_seconds"]})
            return
        if u.path == "/api/bridge":
            path = urlparse.parse_qs(u.query).get("path", [""])[0]
            try:
                self._json(cloud_call(self.cfg, path, "GET"))
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 502)
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        u = urlparse.urlparse(self.path)
        if u.path != "/api/bridge":
            self._json({"ok": False, "error": "not found"}, 404)
            return
        path = urlparse.parse_qs(u.query).get("path", [""])[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            self._json(cloud_call(self.cfg, path, "POST", body))
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 502)


def serve(cfg: dict, open_browser: bool = True):
    Handler.cfg = cfg
    srv = ThreadingHTTPServer((cfg["host"], cfg["port"]), Handler)
    url = f"http://{cfg['host']}:{cfg['port']}"
    print("=" * 58)
    print("  twitter2qq 云控制台（查看 + 控制）")
    print("  本机地址:", url)
    print("  云端实例:", cfg["cloud_url"])
    print("  配置文件:", CONFIG_FILE)
    print("  关闭本窗口即退出；推送本身在云端继续运行，不受影响")
    print("=" * 58)
    termux = "com.termux" in str(os.environ.get("PREFIX", ""))
    if open_browser and not termux:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出（云端推送不受影响）")
    finally:
        srv.server_close()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 小号转发器：仍需常驻在本地，但通过统一入口调用，端侧保持一个程序
    if argv and argv[0].lower() in ("relay", "转发"):
        import relay
        return int(relay.main(argv[1:]) or 0)
    flags = {"--no-browser": False, "--serve": False}
    for f in list(flags):
        if f in argv:
            flags[f] = True
            argv.remove(f)
    for opt, key in (("--port", "port"), ("--host", "host"),
                     ("--cloud", "cloud_url"), ("--sharecode", "sharecode")):
        if opt in argv:
            i = argv.index(opt)
            if i + 1 < len(argv):
                os.environ[ENV_KEYS[key]] = argv[i + 1]
                del argv[i:i + 2]
    cfg = load_config()
    if not argv or flags["--serve"]:
        serve(cfg, open_browser=not flags["--no-browser"])
        return 0
    return _cli(cfg, argv)


if __name__ == "__main__":
    raise SystemExit(main())
