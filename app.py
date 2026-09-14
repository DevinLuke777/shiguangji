#!/usr/bin/env python3
"""
media-vault Web 展示应用 v4 — 全部重写
列表页：瀑布流卡片（缩略图 + 标题 + 作者/日期同一行）
详情页：标题 + 作者信息条 + 正文 + 视频播放 + 图片网格(lightbox)
"""
import os, re, sqlite3, json, time
from datetime import datetime
from flask import Flask, render_template_string, request, abort, url_for, Response, redirect, jsonify

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE, "media_library.db"))
MEDIA_ROOT = os.environ.get("MEDIA_ROOT", "/media")

app = Flask(__name__)

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")

_avatar_cache = {}

# ─── 粘贴链接自动入库队列 ──────────────────────────────
QUEUE_FILE = os.path.join(MEDIA_ROOT, "_queue.json")

def load_queue():
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []

def save_queue(q):
    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(q, f, ensure_ascii=False, indent=2)
    os.replace(tmp, QUEUE_FILE)

Q_HEAD = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="Cache-Control" content="no-cache,no-store,must-revalidate">
<title>队列 - 拾光集</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--accent:#0071e3;--bg:#f7f7f8;--card:#fff;--text:#18181b;--sub:#71717a;--border:#e8e8ec;--fill:#f0f0f4;--fill2:#a3a3ad;--shadow:0 1px 4px rgba(0,0,0,.06);--danger:#6e6e73;--bar-bg:rgba(255,255,255,.72);--bar-line:rgba(0,0,0,.06)}
@media (prefers-color-scheme:dark){
:root{color-scheme:dark;--accent:#0a84ff;--bg:#050507;--card:#1c1c1e;--text:#f5f5f7;--sub:#86868b;--border:#2c2c2e;--fill:#2c2c2e;--fill2:#636366;--shadow:0 1px 4px rgba(0,0,0,.45);--danger:#48484a;--bar-bg:rgba(10,10,12,.72);--bar-line:rgba(255,255,255,.08)}
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:var(--bg);color:var(--text);padding:20px;max-width:640px;margin:0 auto}
h2{font-size:18px;font-weight:750;letter-spacing:-.02em}
.btn{border:none;border-radius:20px;padding:6px 14px;font-size:13px;color:#fff;cursor:pointer;transition:transform .1s ease}
.btn:active{transform:scale(.96)}
.btn.danger{background:var(--danger)}
.btn.gray{background:var(--sub)}
.meta{color:var(--sub);font-size:14px;margin:14px 0}
.qcard{background:var(--card);border-radius:12px;box-shadow:var(--shadow);overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:10px 12px;border-bottom:1px solid var(--border);vertical-align:top}
tr.hdr th{background:var(--fill);font-size:13px;color:var(--sub);font-weight:600;border-bottom:1px solid var(--border)}
td.empty{text-align:center;color:var(--fill2);padding:26px;border-bottom:none}
.url{font-size:12px;color:var(--fill2);word-break:break-all}
.back-line{margin-top:18px}
.back-line a{color:var(--accent);text-decoration:none;font-size:14px}
.tabbar{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);z-index:90;display:flex;background:var(--bar-bg);backdrop-filter:blur(24px) saturate(180%);-webkit-backdrop-filter:blur(24px) saturate(180%);border:1px solid var(--bar-line);border-radius:26px;box-shadow:var(--shadow-lg);padding:5px;gap:2px}
.tabbar .titem{display:flex;flex-direction:column;align-items:center;gap:2px;font-size:10px;color:var(--sub);text-decoration:none;padding:6px 15px;border-radius:20px;transition:background .15s ease,color .15s ease}
.tabbar .titem .tic{font-size:20px;line-height:1}
.tabbar .titem.on{color:#fff;background:var(--accent)}
body{padding-bottom:88px}
@media (prefers-reduced-motion:reduce){*{transition-duration:.01ms!important}}
</style></head>
<body>
"""

@app.route("/add", methods=["POST"])
def add_link():
    raw = request.form.get("links", "").strip()
    links = [l.strip() for l in re.split(r"[\s,]+", raw) if l.strip().startswith("http")]
    if not links:
        return redirect(url_for("index", msg="no_link"))
    q = load_queue()
    added = 0
    for link in links:
        # 去重：已在队列则不重复加
        if any(it.get("url") == link for it in q):
            continue
        q.append({"id": int(time.time() * 1000) % 1000000,
                  "url": link, "status": "pending", "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  "message": ""})
        added += 1
    if added:
        save_queue(q)
    return redirect(url_for("index", msg="added" if added else "dup"))


# ─── 给 iOS 快捷指令 / 自动化用的极简接口 ──────────────
def _api_reply(payload, raw=""):
    """默认返回**纯文本**（快捷指令通知/朗读直接可用）；带 ?fmt=json 才返回 JSON。"""
    if (request.values.get("fmt") or "").lower() == "json":
        return jsonify(payload)
    return Response(payload["msg"] + "\n", mimetype="text/plain; charset=utf-8")


@app.route("/api/add", methods=["GET", "POST"])
def api_add():
    """任意文本里抓 http(s) 链接丢进队列，返回 JSON。
    用法: GET /api/add?url=<分享文本或链接>  或 POST form url=... / links=...
    兼容分享口令（如「8.88 复制打开抖音… https://v.douyin.com/xxx/」）。"""
    raw = " ".join(x for x in (request.values.get("url"), request.values.get("links"),
                               request.values.get("text")) if x)
    if not raw:
        raw = request.get_data(as_text=True)[:4000]
    links, seen_links = [], set()
    for cand in re.findall(r"https?://[^\s\u4e00-\u9fff,，、；;：:）)】\]]+", raw):
        cand = cand.rstrip(".,;:!?，。；：！？")
        if cand and cand not in seen_links:
            seen_links.add(cand)
            links.append(cand)
    if not links:
        return _api_reply({"ok": False, "added": 0, "dup": 0, "msg": "没找到链接"}, raw)
    q = load_queue()
    added, dup = [], []
    for link in links:
        if any(it.get("url") == link for it in q):
            dup.append(link)
            continue
        q.append({"id": int(time.time() * 1000) % 1000000,
                  "url": link, "status": "pending",
                  "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "message": ""})
        added.append(link)
    if added:
        save_queue(q)
    msg = f"已加入队列 {len(added)} 条" if added else "已在队列中(跳过)"
    if dup and added:
        msg += f"，{len(dup)} 条重复"
    return _api_reply({"ok": True, "added": len(added), "dup": len(dup), "msg": msg}, raw)

@app.route("/queue-clear-failed", methods=["POST"])
def queue_clear_failed():
    """一键删除所有 failed 队列条目"""
    q = [it for it in load_queue() if it.get("status") != "failed"]
    save_queue(q)
    return redirect(url_for("queue_status"))

@app.route("/queue-clear-all", methods=["POST"])
def queue_clear_all():
    """一键清空整个队列"""
    save_queue([])
    return redirect(url_for("queue_status"))


# ─── 失败条目重新排队 ──────────────────────────────────
@app.route("/queue-retry", methods=["POST"])
def queue_retry():
    """把失败条目改回 pending，哨兵几秒内会自动重新处理"""
    qid = request.form.get("id", type=int)
    if qid is not None:
        q = load_queue()
        changed = False
        for it in q:
            if it.get("id") == qid and it.get("status") == "failed":
                it["status"] = "pending"
                it["message"] = "手动重试中"
                it["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                changed = True
        if changed:
            save_queue(q)
    return redirect(url_for("queue_status"))

@app.route("/queue")
def queue_status():
    q = load_queue()
    fail_count = sum(1 for it in q if it.get("status") == "failed")
    try:
        conn = db()
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        conn.close()
    except Exception:
        total = 0
    html = Q_HEAD
    html += f"""<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
<h2>📥 自动入库队列</h2>
<div style="display:flex;gap:8px">
<form method="post" action="/queue-clear-failed" style="display:inline" onsubmit="return confirm('清除所有失败的条目？')"><button class="btn danger">🗑️ 清空失败({fail_count})</button></form>
<form method="post" action="/queue-clear-all" style="display:inline" onsubmit="return confirm('清空整个队列？(含进行中)')"><button class="btn gray">✖️ 清空全部</button></form>
</div>
</div>
<p class="meta">收藏总数: {total} · 队列项: {len(q)} · 失败自动保留1天后清除</p>
<div class="qcard">
<table>
<tr class="hdr"><th style="text-align:left">平台</th><th style="text-align:center">状态</th><th style="text-align:left">标题/链接</th></tr>
"""
    if not q:
        html += "<tr><td colspan='3' class='empty'>队列是空的</td></tr>"
    for it in q[:30]:
        status = it.get("status", "pending")
        emoji = {"pending": "⏳", "processing": "⚙️", "done": "✅", "failed": "❌"}.get(status, "⏳")
        color = {"pending": "#999", "processing": "var(--accent)", "done": "#2f9e5f", "failed": "#ff9f0a"}.get(status, "#999")
        plat = "抖音" if "douyin" in it["url"] or "iesdouyin" in it["url"] else ("小红书" if "xhslink" in it["url"] or "xiaohongshu" in it["url"] else "其他")
        title = it.get("title") or ""
        if title:
            disp = f"{title}<br><span class='url'>{it['url'][:60]}</span>"
        else:
            disp = it["url"][:70]
        msg = f"<br><span style='font-size:12px;color:{color}'>{it.get('message','')}</span>" if it.get("message") else ""
        retry = ""
        if status == "failed":
            retry = (f"<form method='post' action='/queue-retry' style='display:inline'>"
                     f"<input type='hidden' name='id' value='{it['id']}'>"
                     f"<button class='btn' style='background:var(--accent);padding:3px 10px;font-size:12px;margin-left:6px'>🔄 重试</button></form>")
        html += f"<tr><td>{plat}</td><td style='color:{color};text-align:center'>{emoji} {status}</td><td>{disp}{msg}{retry}</td></tr>"
    html += """</table></div>
<nav class="tabbar">
  <a href="/" class="titem"><span class="tic">🏠</span>浏览</a>
  <a href="/queue" class="titem on"><span class="tic">📥</span>队列</a>
  <a href="/stats" class="titem"><span class="tic">📊</span>统计</a>
  <a href="/?mode=manage" class="titem"><span class="tic">🗑️</span>管理</a>
</nav>
<script>/* 还有处理中的条目时每 15 秒自动刷新进度 */
setInterval(function(){var t=document.body.textContent;if(t.indexOf('⏳')>=0||t.indexOf('⚙️')>=0)location.reload();},15000);
</script>
</body></html>"""
    return html

# ─── 列表页 ──────────────────────────────────────────
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache,no-store,must-revalidate">
<title>拾光集</title>
<style>
:root{--accent:#0071e3;--bg:#f7f7f8;--card:#fff;--text:#18181b;--sub:#71717a;--border:#e8e8ec;--fill:#f0f0f4;--fill2:#a3a3ad;--body:#444;--bar-bg:rgba(255,255,255,.72);--bar-line:rgba(0,0,0,.06);--shadow:0 1px 4px rgba(0,0,0,.06);--shadow-lg:0 2px 16px rgba(0,0,0,.12);--danger:#6e6e73;--radius:14px}
@media (prefers-color-scheme:dark){
:root{color-scheme:dark;--accent:#0a84ff;--bg:#050507;--card:#1c1c1e;--text:#f5f5f7;--sub:#86868b;--border:#2c2c2e;--fill:#2c2c2e;--fill2:#636366;--body:#c7c7cc;--bar-bg:rgba(10,10,12,.72);--bar-line:rgba(255,255,255,.08);--shadow:0 1px 4px rgba(0,0,0,.45);--shadow-lg:0 2px 16px rgba(0,0,0,.5);--danger:#48484a}
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:var(--bg);color:var(--text);letter-spacing:0;-webkit-tap-highlight-color:transparent}
@keyframes imgIn{from{opacity:0;transform:scale(.985)}to{opacity:1;transform:scale(1)}}

/* 顶栏 — 毛玻璃，跟随深浅色 */
.topbar{position:sticky;top:0;z-index:100;padding:12px 16px;background:var(--bar-bg);backdrop-filter:blur(24px) saturate(180%);-webkit-backdrop-filter:blur(24px) saturate(180%);border-bottom:1px solid var(--bar-line);transition:box-shadow .25s ease}
.topbar.is-scrolled{box-shadow:var(--shadow-lg)}
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){.topbar{background:var(--card)}}
.topbar h1{font-size:20px;font-weight:750;letter-spacing:-.025em;margin-bottom:10px}
.hrow{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.hrow h1{margin-bottom:0}
.brand{display:flex;align-items:center;gap:8px;text-decoration:none;color:var(--text)}
.logo{width:30px;height:30px;border-radius:7px;object-fit:cover;box-shadow:0 1px 3px rgba(0,0,0,.18);flex-shrink:0;display:block}
.brand-name{font-size:20px;font-weight:750;letter-spacing:-.025em}
.sbtn{display:flex;align-items:center;justify-content:center;width:34px;height:34px;border:1px solid var(--border);border-radius:50%;background:var(--fill);font-size:15px;cursor:pointer;transition:transform .1s ease;flex-shrink:0}
.sbtn:active{transform:scale(.92)}
.scancel{font-size:14px;color:var(--accent);text-decoration:none;white-space:nowrap;padding:6px 10px}
.search-bar{margin-bottom:8px;display:flex}
.search-row{display:flex;gap:8px}
.search-row input{flex:1;padding:9px 14px;border:1px solid var(--border);border-radius:20px;font-size:14px;outline:none;background:var(--fill);color:var(--text);transition:border-color .15s ease,box-shadow .15s ease}
.search-row input::placeholder{color:var(--fill2)}
.search-row input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(10,132,255,.18)}
.filter-row{display:flex;gap:6px;margin-top:8px;overflow-x:auto;-webkit-overflow-scrolling:touch}
.chip{padding:5px 14px;border-radius:20px;font-size:13px;white-space:nowrap;cursor:pointer;border:1px solid var(--border);background:var(--card);color:var(--sub);transition:all .15s ease;user-select:none}
.chip:active{transform:scale(.96)}
.chip.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.chip select{border:none;background:transparent;font-size:13px;outline:none;color:var(--sub)}

/* 排序分段控件（iOS 风格） */
.seg{display:flex;border:1px solid var(--border);border-radius:20px;overflow:hidden;background:var(--card);flex-shrink:0}
.seg-btn{padding:5px 13px;font-size:13px;color:var(--sub);text-decoration:none;white-space:nowrap;transition:background .15s ease,color .15s ease}
.seg-btn.on{background:var(--accent);color:#fff}
.seg .seg-btn + .seg-btn{border-left:1px solid var(--border)}

/* 底部悬浮导航（全尺寸显示，iPad/桌面也用；管理模式隐藏） */
.tabbar{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);z-index:90;display:flex;background:var(--bar-bg);backdrop-filter:blur(24px) saturate(180%);-webkit-backdrop-filter:blur(24px) saturate(180%);border:1px solid var(--bar-line);border-radius:26px;box-shadow:var(--shadow-lg);padding:5px;gap:2px}
.tabbar .titem{display:flex;flex-direction:column;align-items:center;gap:2px;font-size:10px;color:var(--sub);text-decoration:none;padding:6px 15px;border-radius:20px;transition:background .15s ease,color .15s ease}
.tabbar .titem .tic{font-size:20px;line-height:1}
.tabbar .titem.on{color:#fff;background:var(--accent)}
.masonry{padding-bottom:88px}

/* 统计 */
.stat-bar{padding:8px 16px;font-size:12px;color:var(--sub)}

/* 瀑布流 */
.masonry{display:flex;gap:10px;padding:0 10px 20px;align-items:flex-start;max-width:1800px;margin:0 auto}
.col{flex:1;min-width:0;display:flex;flex-direction:column;gap:10px}

/* 平板/桌面：封面限高 + 列数随宽度（列数由 JS 重排，见底部脚本） */
@media(min-width:768px){
  .cover img{max-height:400px}
  .cover .empty{height:160px}
}
@media(min-width:1100px){
  .cover img{max-height:380px}
  .cover .empty{height:150px}
}
@media(min-width:1400px){
  .cover img{max-height:360px}
}

/* 卡片 */
.card{break-inside:avoid;margin-bottom:10px;background:var(--card);border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow);display:block;text-decoration:none;color:inherit;transition:transform .1s ease,box-shadow .2s ease}
.card:active{transform:scale(.97);box-shadow:var(--shadow-lg)}

/* 缩略图 */
.cover{position:relative;width:100%;overflow:hidden;background:var(--fill);min-height:110px}
.cover img{width:100%;display:block;object-fit:cover;animation:imgIn .38s ease-out}
.cover .empty{height:140px;display:flex;align-items:center;justify-content:center;font-size:36px;color:var(--fill2)}
.cover .tag{position:absolute;top:6px;left:6px;background:rgba(0,0,0,.55);color:#fff;font-size:10px;padding:2px 8px;border-radius:12px;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}
.cover .play{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:40px;height:40px;background:rgba(0,0,0,.5);border-radius:50%;display:flex;align-items:center;justify-content:center;color:#fff;font-size:16px;pointer-events:none}

/* 信息行 */
.info{padding:8px 10px;display:flex;align-items:center;gap:4px}
.info .av{width:20px;height:20px;border-radius:50%;flex-shrink:0;background:#eee;object-fit:cover}
.info .av.hide{display:none}
.info .who{font-size:12px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0;text-decoration:none}
.info .when{font-size:10px;color:var(--sub);flex-shrink:0}

.no-result{padding:70px 24px;text-align:center;color:var(--sub);font-size:15px}
.no-result .ic{font-size:42px;display:block;margin-bottom:14px;opacity:.5}
.no-result .tip{font-size:13px;color:var(--fill2);margin-top:8px}

@media (prefers-reduced-motion:reduce){*{animation-duration:.01ms!important;transition-duration:.01ms!important}}
</style>
</head>
<body>
<div class="topbar">
  <div class="hrow">
  <a class="brand" href="/" title="回首页">
    <img class="logo" src="/static/logo.jpg" alt="拾光集">
    <span class="brand-name">拾光集</span>
  </a>
  <button type="button" class="sbtn" id="sOpen" onclick="openSearch()" {% if q %}style="display:none"{% endif %} aria-label="搜索">🔍</button>
</div>
  <form method="post" action="/add" class="search-row" style="margin-bottom:8px">
    <input type="text" name="links" placeholder="粘贴抖音/小红书链接，自动入库（多条用空格或逗号分隔）" {% if request.args.get('msg')=='added' %}style="border-color:#30d158"{% endif %}>
    <button type="submit" style="padding:8px 16px;border:none;border-radius:20px;background:var(--accent);color:#fff;font-size:14px;white-space:nowrap;transition:transform .1s ease">入库</button>
  </form>
  {% if request.args.get('msg') == 'added' %}
  <div style="color:#30d158;font-size:12px;margin:-4px 2px 8px">✅ 已加入队列，处理中请稍候到「📥 队列」查看</div>
  {% elif request.args.get('msg') == 'no_link' %}
  <div style="color:#ff9f0a;font-size:12px;margin:-4px 2px 8px">没有识别到有效链接</div>
  {% endif %}
  <div class="search-bar" id="sBox" style="display:{% if q %}flex{% else %}none{% endif %}">
  <form class="search-row" method="get" style="flex:1;min-width:0">
    <input type="search" name="q" id="sInput" placeholder="搜索标题/作者/内容…" value="{{ q }}" style="-webkit-appearance:none;appearance:none">
    <a class="scancel" href="/">取消</a>
  </form>
</div>
  <div class="filter-row">
    <span class="chip {% if not platform %}active{% endif %}" onclick="location.href='{{ url_no_plat }}'">全部</span>
    {% for p in platforms %}
    <span class="chip {% if platform==p %}active{% endif %}" onclick="location.href='/?platform={{ p|urlencode }}{% if q %}&q={{ q|urlencode }}{% endif %}{% if sort %}&sort={{ sort }}{% endif %}'">{{ '推特' if p == 'X' else p }}</span>
    {% endfor %}
    <span class="seg">
      <a class="seg-btn {% if sort=='created' %}on{% endif %}" href="{{ url_created }}">最新采集</a>
      <a class="seg-btn {% if sort=='posted' %}on{% endif %}" href="{{ url_posted }}">最新发布</a>
    </span>
  </div>
</div>

<div class="stat-bar">共 {{ total }} 条收藏{% if author %} · 作者: {{ author }}{% endif %}</div>

{% if not items %}
<div class="no-result"><span class="ic">🗂️</span>没有找到内容<span class="tip">{% if q %}换个关键词试试{% else %}在顶部粘贴链接，收藏第一条内容吧{% endif %}</span></div>
{% endif %}

{% if mode == 'manage' %}
<form method="post" action="/delete-batch" id="batchForm" onsubmit="return confirm('确定删除选中的收藏？本地文件也会一并删除！')" style="width:100%;margin:0;padding:0">
{% endif %}
<div class="masonry" {% if mode == 'manage' %}style="padding-bottom:88px"{% endif %}>
{% for col in columns %}
<div class="col">
{% for it in col %}
{% if mode == 'manage' %}
<div style="position:relative;width:100%" data-idx="{{ it['idx'] }}">
  <input type="checkbox" name="ids" value="{{ it['id'] }}" style="position:absolute;top:8px;right:8px;z-index:5;width:20px;height:20px;accent-color:var(--accent)">
{% endif %}
<a class="card" href="/item/{{ it['id'] }}" {% if mode != 'manage' %}data-idx="{{ it['idx'] }}"{% endif %}>
  <div class="cover">
    {% if it['cover'] %}
    <img src="{{ url_for('media', relpath=it['cover']) }}" {% if it['idx'] < 12 %}loading="eager" fetchpriority="high"{% else %}loading="lazy"{% endif %}>
    {% else %}
    <div class="empty">{{ '🎬' if it['is_video'] else '🖼️' }}</div>
    {% endif %}
    <span class="tag">{{ '推特' if it['platform'] == 'X' else it['platform'] }}</span>
    {% if it['is_video'] %}<span class="play">▶</span>{% endif %}
  </div>
  <div class="info">
    <img class="av" src="/avatar/{{ it['id'] }}" onerror="this.classList.add('hide')">
    <span class="who">{{ it['title'] or '无标题' }}</span>
    <span class="when">{{ it['created_at'][:10] if it['created_at'] else '' }}</span>
  </div>
</a>
{% if mode == 'manage' %}
</div>
{% endif %}
{% endfor %}
</div>
{% endfor %}
{% if mode == 'manage' %}
</div>
<div class="mgbar" style="position:fixed;bottom:14px;left:50%;transform:translateX(-50%);z-index:95;display:flex;align-items:center;background:var(--bar-bg);backdrop-filter:blur(24px) saturate(180%);-webkit-backdrop-filter:blur(24px) saturate(180%);border:1px solid var(--bar-line);border-radius:26px;box-shadow:var(--shadow-lg);padding:5px;gap:2px">
  <label style="font-size:13px;color:var(--sub);display:flex;align-items:center;gap:5px;padding:6px 10px;user-select:none"><input type="checkbox" id="selAll" style="width:16px;height:16px;accent-color:var(--accent)"> 全选</label>
  <button type="submit" id="delBtn" style="background:var(--danger);color:#fff;border:none;padding:8px 18px;border-radius:18px;font-size:13px;cursor:pointer;transition:transform .1s ease;user-select:none">删除选中</button>
  <a href="/" style="font-size:13px;color:var(--accent);text-decoration:none;padding:8px 14px;border-radius:18px;user-select:none">✕ 退出</a>
</div>
</form>
<script>
document.getElementById('selAll').onchange=function(){var b=this.checked;document.querySelectorAll('input[name=ids]').forEach(function(c){c.checked=b});updDel()};
function updDel(){var n=document.querySelectorAll('input[name=ids]:checked').length;var d=document.getElementById('delBtn');d.textContent=n?('删除选中('+n+')'):'删除选中'}
document.querySelectorAll('input[name=ids]').forEach(function(c){c.addEventListener('change',updDel)});
</script>
{% else %}
</div>
{% endif %}
<script>
/* 响应式列数：按窗口宽度重排卡片（手机2列 / 平板3列 / 大屏4列），保持奇偶交替顺序 */
(function(){
  function relayout(){
    var wrap=document.querySelector('.masonry'); if(!wrap) return;
    var w=window.innerWidth;
    var n = w>=1800 ? 6 : (w>=1400 ? 5 : (w>=1100 ? 4 : (w>=768 ? 3 : 2)));
    if(wrap.dataset.cols===String(n)) return;
    wrap.dataset.cols=n;
    var cards=[].slice.call(wrap.querySelectorAll(':scope > .col > [data-idx]'));
    if(!cards.length) return;
    cards.sort(function(a,b){return (+a.getAttribute('data-idx'))-(+b.getAttribute('data-idx'));});
    var cols=[]; for(var i=0;i<n;i++){var d=document.createElement('div');d.className='col';cols.push(d);}
    cards.forEach(function(c,k){cols[k%n].appendChild(c);});
    wrap.innerHTML=''; cols.forEach(function(c){wrap.appendChild(c);});
  }
  var t;
  window.addEventListener('resize',function(){clearTimeout(t);t=setTimeout(relayout,150);});
  if(document.readyState!=='loading'){relayout();}
  else{document.addEventListener('DOMContentLoaded',relayout);}
})();
</script>
<script>
/* 顶栏搜索展开：平时只有 🔍 按钮 */
function openSearch(){
  var b=document.getElementById('sBox'); if(!b) return;
  b.style.display='flex';
  var o=document.getElementById('sOpen'); if(o) o.style.display='none';
  var i=document.getElementById('sInput'); if(i) i.focus();
}
</script>
<script>
/* 顶栏滚动边界反馈：内容滚过顶栏时加阴影（IntersectionObserver，无滚动监听） */
(function(){
  var tb=document.querySelector('.topbar');
  if(!tb||!('IntersectionObserver' in window)) return;
  var mark=document.createElement('div');
  mark.style.cssText='position:absolute;top:0;left:0;width:1px;height:1px;pointer-events:none';
  document.body.appendChild(mark);
  new IntersectionObserver(function(en){tb.classList.toggle('is-scrolled',!en[0].isIntersecting);},{threshold:0}).observe(mark);
})();
</script>
{% if mode != 'manage' %}
<nav class="tabbar">
  <a href="/" class="titem on"><span class="tic">🏠</span>浏览</a>
  <a href="/queue" class="titem"><span class="tic">📥</span>队列</a>
  <a href="/stats" class="titem"><span class="tic">📊</span>统计</a>
  <a href="/?mode=manage" class="titem"><span class="tic">🗑️</span>管理</a>
</nav>
<script>
/* 下拉刷新（iOS 手势：页面在顶部时下拉 90px 触发） */
(function(){
  var sy=0,on=false;
  document.addEventListener('touchstart',function(e){if(window.scrollY<=0){sy=e.touches[0].clientY;on=true;}},{passive:true});
  document.addEventListener('touchmove',function(e){if(on&&window.scrollY<=0&&e.touches[0].clientY-sy>90){on=false;location.reload();}},{passive:true});
  document.addEventListener('touchend',function(){on=false;},{passive:true});
})();
</script>
{% endif %}
</body>
</html>"""


# ─── 详情页 ──────────────────────────────────────────
DETAIL_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ it['title'] or '详情' }} - 拾光集</title>
<style>
:root{--accent:#0071e3;--bg:#f7f7f8;--card:#fff;--text:#18181b;--sub:#71717a;--border:#e8e8ec;--fill:#f0f0f4;--fill2:#a3a3ad;--body:#444;--shadow:0 2px 10px rgba(0,0,0,.06);--shadow-lg:0 2px 16px rgba(0,0,0,.12);--danger:#6e6e73;--radius:14px}
@media (prefers-color-scheme:dark){
:root{color-scheme:dark;--accent:#0a84ff;--bg:#050507;--card:#1c1c1e;--text:#f5f5f7;--sub:#86868b;--border:#2c2c2e;--fill:#2c2c2e;--fill2:#636366;--body:#c7c7cc;--shadow:0 2px 10px rgba(0,0,0,.5);--shadow-lg:0 2px 16px rgba(0,0,0,.5);--danger:#48484a}
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:var(--bg);color:var(--text)}

.page{max-width:860px;margin:0 auto;padding:16px}
.back{font-size:14px;color:var(--accent);text-decoration:none;display:inline-block;margin-bottom:12px;padding:6px 14px;border:1px solid var(--border);border-radius:20px;background:var(--card);transition:transform .1s ease}
.back:active{transform:scale(.96)}

.card-box{background:var(--card);border-radius:16px;overflow:hidden;box-shadow:var(--shadow)}

/* 标题 */
.title{padding:16px 16px 0;font-size:19px;font-weight:750;line-height:1.4;letter-spacing:-.02em}

/* 作者信息条 — 纯 flex 行 */
.author-bar{padding:10px 16px;display:flex;align-items:center;gap:8px}
.author-bar .av{width:32px;height:32px;border-radius:50%;flex-shrink:0;background:#eee;object-fit:cover}
.author-bar .av.hide{display:none}
.author-bar .name{font-size:14px;color:var(--accent);text-decoration:none;font-weight:500}
.author-bar .plat{font-size:11px;color:var(--sub);background:var(--fill);padding:2px 8px;border-radius:12px}
.author-bar .time{font-size:12px;color:var(--sub);margin-left:auto;white-space:nowrap}

/* 正文 */
.content{padding:0 16px 12px;font-size:14px;line-height:1.7;color:var(--body);white-space:pre-wrap}

/* 媒体预览 */
.media-area{padding:0 16px 16px}
.media-area video{width:100%;max-height:72vh;border-radius:12px;background:#000;margin:0 auto 10px;display:block;box-shadow:var(--shadow)}

.img-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
.img-grid a{display:block;aspect-ratio:1;overflow:hidden;border-radius:12px;background:var(--fill);cursor:pointer;transition:transform .1s ease}
.img-grid a:active{transform:scale(.96)}
.img-grid a img{width:100%;height:100%;object-fit:cover}

.single-img{width:100%;max-height:80vh;object-fit:contain;border-radius:12px;cursor:pointer;background:var(--fill)}

/* Lightbox */
.lb{display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:9999;align-items:center;justify-content:center;flex-direction:column;padding:16px}
.lb.on{display:flex}
.lb img{max-width:92vw;max-height:78vh;border-radius:6px;object-fit:contain}
.lb .x{position:absolute;top:12px;right:20px;font-size:32px;color:#fff;cursor:pointer;line-height:1}
.lb .nav{position:absolute;top:50%;transform:translateY(-50%);font-size:36px;color:rgba(255,255,255,.6);cursor:pointer;padding:12px;user-select:none}
.lb .nav.l{left:8px}
.lb .nav.r{right:8px}
.lb .cnt{color:rgba(255,255,255,.5);font-size:13px;margin-top:8px}

/* 下载条 */
.dl{padding:0 16px 16px;display:flex;flex-wrap:wrap;gap:8px}
.dl a{font-size:12px;color:var(--accent);text-decoration:none;padding:6px 14px;border:1px solid var(--border);border-radius:8px}
.dl a.vid{color:#e74c3c;border-color:#fcc}

.orig{padding:0 16px 16px;font-size:12px;color:var(--sub);word-break:break-all}
.orig a{color:var(--accent);text-decoration:none}
.del-btn{background:var(--danger);color:#fff;border:none;padding:10px 22px;border-radius:12px;font-size:14px;cursor:pointer;transition:transform .1s ease,opacity .15s ease}
.del-btn:active{transform:scale(.96);opacity:.85}
@media (prefers-reduced-motion:reduce){*{animation-duration:.01ms!important;transition-duration:.01ms!important}}
</style>
</head>
<body>
<div class="page">
  <a class="back" href="/">← 返回</a>
  <div class="card-box">
    <div class="title">{{ it['title'] or '无标题' }}</div>
    <div style="padding:4px 16px 0;font-size:11px;color:var(--sub)">ID: <code style="background:var(--fill);padding:1px 6px;border-radius:4px;color:var(--sub)">{{ it['id'] }}</code></div>
    <div class="author-bar">
      <img class="av" src="/avatar/{{ it['id'] }}" onerror="this.classList.add('hide')">
      <a class="name" href="/?author={{ it['author_name']|urlencode }}">{{ it['author_name'] or '未知作者' }}</a>
      <span class="plat">{{ '推特' if it['platform'] == 'X' else it['platform'] }}</span>
      <span class="time" style="display:flex;flex-direction:column;align-items:flex-end;gap:2px;line-height:1.3"><span>🕐 {{ it['post_date'][:19] if it['post_date'] else '' }}</span><span>📥 {{ it['created_at'][:19] if it['created_at'] else '' }}</span></span>
    </div>
    {% if it['content'] %}<div class="content">{{ it['content'] }}</div>{% endif %}

    <div class="media-area">
      {% set ns = namespace(imgs=[], vids=[]) %}
      {% for f in files %}
        {% if f.lower().endswith(('.mp4','.mov','.mkv','.webm')) %}
          {% set ns.vids = ns.vids + [f] %}
        {% elif f.lower().endswith(('.jpg','.jpeg','.png','.webp','.gif')) %}
          {% set ns.imgs = ns.imgs + [f] %}
        {% endif %}
      {% endfor %}
      {% for f in ns.vids %}
      <video controls preload="metadata" src="{{ url_for('media', relpath=f) }}"></video>
      {% endfor %}
      {% if ns.imgs|length == 1 %}
      <img class="single-img" src="{{ url_for('media', relpath=ns.imgs[0]) }}" loading="lazy" onclick="openLb(0)">
      {% elif ns.imgs|length > 1 %}
      <div class="img-grid">
        {% for f in ns.imgs %}
        <a href="javascript:void(0)" onclick="openLb({{ loop.index0 }})"><img src="{{ url_for('media', relpath=f) }}" loading="lazy"></a>
        {% endfor %}
      </div>
      {% endif %}
    </div>

    {% if it['original_url'] %}<div class="orig">🔗 <a href="{{ it['original_url'] }}" target="_blank">{{ it['original_url'] }}</a></div>{% endif %}
    <div style="margin-top:20px;border-top:1px solid var(--border);padding-top:16px">
      <form method="post" action="/delete/{{ it['id'] }}" onsubmit="return confirm('确定删除这条收藏？本地文件也会一并删除！')">
        <button type="submit" class="del-btn">🗑️ 删除这条收藏</button>
      </form>
    </div>
  </div>
</div>

{% if ns.imgs %}
<div class="lb" id="lb" onclick="closeLb(event)">
  <span class="x" onclick="closeLb(event)">&times;</span>
  <span class="nav l" onclick="lbStep(event,-1)">&#10094;</span>
  <img id="lbImg" src="">
  <span class="nav r" onclick="lbStep(event,1)">&#10095;</span>
  <div class="cnt" id="lbCnt"></div>
</div>
<script>
var imgs={{ ns.imgs|map('urlencode')|list|tojson }},idx=0,base="{{ url_for('media', relpath='') }}";
function openLb(i){idx=i;document.getElementById('lb').classList.add('on');show()}
function show(){document.getElementById('lbImg').src=base+imgs[idx];document.getElementById('lbCnt').textContent=(idx+1)+'/'+imgs.length}
function closeLb(e){if(e.target.tagName==='IMG'||e.target.classList.contains('nav'))return;document.getElementById('lb').classList.remove('on')}
function lbStep(e,d){e.stopPropagation();idx=(idx+d+imgs.length)%imgs.length;show()}
document.addEventListener('keydown',function(e){if(!document.getElementById('lb').classList.contains('on'))return;if(e.key==='Escape')document.getElementById('lb').classList.remove('on');if(e.key==='ArrowLeft')lbStep(e,-1);if(e.key==='ArrowRight')lbStep(e,1)});
</script>
{% endif %}
</body>
</html>"""


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.after_request
def no_cache(resp):
    """页面禁缓存（部署后无需强刷）；媒体文件给 10 分钟缓存（刷新秒出，不必重拉缩略图）"""
    p = request.path
    if p.startswith("/media/") or p.startswith("/avatar/") or p.startswith("/static/"):
        resp.headers["Cache-Control"] = "public, max-age=600"
    else:
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

def _natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]

def list_files(rel):
    if not rel:
        return []
    d = os.path.join(MEDIA_ROOT, rel)
    if not os.path.isdir(d):
        return []
    files = []
    for f in sorted(os.listdir(d), key=_natural_key):
        if f.startswith(".") or f == "_thumb.jpg":
            continue
        files.append(os.path.join(rel, f).replace("\\", "/"))
    return files

from urllib.parse import urlencode
@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    plat = request.args.get("platform", "").strip()
    author = request.args.get("author", "").strip()
    sort = request.args.get("sort", "created")
    mode = request.args.get("mode", "")  # manage=批量管理模式
    conn = db()
    where, params = [], []
    if q:
        where.append("(title LIKE ? OR author_name LIKE ? OR content LIKE ?)")
        params += [f"%{q}%"] * 3
    if plat:
        where.append("platform = ?")
        params.append(plat)
    if author:
        where.append("author_name = ?")
        params.append(author)
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    order = "created_at DESC, id DESC" if sort == "created" else "post_date DESC, id DESC"
    items = conn.execute(f"SELECT * FROM items {wsql} ORDER BY {order}", params).fetchall()
    platforms = [r["platform"] for r in conn.execute("SELECT platform, COUNT(*) c FROM items GROUP BY platform ORDER BY c DESC")]
    conn.close()
    card_items = []
    for idx, it in enumerate(items):
        lp = it["local_path"] or ""
        files = list_files(lp)
        cover = None
        is_video = False
        thumb = os.path.join(lp, "_thumb.jpg").replace("\\", "/")
        if os.path.isfile(os.path.join(MEDIA_ROOT, thumb)):
            cover = thumb
        for f in files:
            low = f.lower()
            if low.endswith(IMAGE_EXT) and cover is None:
                cover = f
            if low.endswith(VIDEO_EXT):
                is_video = True
        card_items.append({**dict(it), "cover": cover, "is_video": is_video, "idx": idx})

    base_args = {}
    if q: base_args["q"] = q
    if sort: base_args["sort"] = sort
    url_no_plat = "/?" + urlencode(base_args)
    created_args = dict(base_args)
    created_args["sort"] = "created"
    posted_args = dict(base_args)
    posted_args["sort"] = "posted"

    return render_template_string(INDEX_HTML,
        items=card_items, columns=[card_items[0::2], card_items[1::2]],
        platforms=platforms, q=q, platform=plat,
        total=len(card_items), author=author, sort=sort, mode=mode,
        url_no_plat=url_no_plat,
        url_created="/?" + urlencode(created_args),
        url_posted="/?" + urlencode(posted_args))

@app.route("/stats")
def stats():
    """统计面板：平台分布 / 作者Top / 收藏趋势"""
    conn = db()
    platforms = conn.execute("SELECT platform, COUNT(*) c FROM items GROUP BY platform ORDER BY c DESC").fetchall()
    authors = conn.execute("SELECT author_name, COUNT(*) c FROM items WHERE author_name IS NOT NULL AND author_name != '' GROUP BY author_name ORDER BY c DESC LIMIT 10").fetchall()
    trend = conn.execute("SELECT substr(created_at,1,10) d, COUNT(*) c FROM items GROUP BY d ORDER BY d DESC LIMIT 14").fetchall()
    trend = list(reversed(trend))  # 旧→新
    total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    conn.close()
    max_p = max((r["c"] for r in platforms), default=1)
    max_a = max((r["c"] for r in authors), default=1)
    return render_template_string(STATS_HTML, platforms=platforms, authors=authors,
                                  trend=trend, total=total, max_p=max_p, max_a=max_a)

STATS_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>统计 - 拾光集</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--accent:#0071e3;--bg:#f7f7f8;--card:#fff;--text:#18181b;--sub:#71717a;--border:#e8e8ec;--fill:#f0f0f4;--fill2:#a3a3ad;--shadow:0 1px 4px rgba(0,0,0,.06);--bar-bg:rgba(255,255,255,.72);--bar-line:rgba(0,0,0,.06)}
@media (prefers-color-scheme:dark){
:root{color-scheme:dark;--accent:#0a84ff;--bg:#050507;--card:#1c1c1e;--text:#f5f5f7;--sub:#86868b;--border:#2c2c2e;--fill:#2c2c2e;--fill2:#636366;--shadow:0 1px 4px rgba(0,0,0,.45);--bar-bg:rgba(10,10,12,.72);--bar-line:rgba(255,255,255,.08)}
}
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:var(--bg);color:var(--text);padding:20px;max-width:760px;margin:0 auto}
h1{font-size:20px;margin-bottom:20px;letter-spacing:-.02em}
.back{display:inline-block;margin-bottom:16px;color:var(--accent);text-decoration:none;font-size:14px;padding:6px 14px;border:1px solid var(--border);border-radius:20px;background:var(--card);transition:transform .1s ease}
.back:active{transform:scale(.96)}
.card{background:var(--card);border-radius:12px;padding:18px 20px;margin-bottom:16px;box-shadow:var(--shadow)}
.card h2{font-size:15px;margin-bottom:14px;color:var(--sub)}
.row{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.row .label{width:90px;font-size:13px;color:var(--sub);flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .label a{color:var(--accent)}
.bar{height:22px;background:linear-gradient(90deg,var(--accent),#8b8ef0);border-radius:4px;min-width:2px;transition:width .4s}
.row .num{font-size:12px;color:var(--fill2);flex-shrink:0}
.total{font-size:14px;color:var(--sub);margin-bottom:16px}
.trend-grid{display:flex;align-items:flex-end;gap:4px;height:120px;padding-top:10px}
.trend-col{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;height:100%;justify-content:flex-end}
.trend-bar{width:70%;background:linear-gradient(180deg,var(--accent),#8b8ef0);border-radius:3px 3px 0 0;min-height:2px}
.trend-date{font-size:10px;color:var(--fill2);writing-mode:vertical-rl;transform:rotate(180deg);max-height:34px;overflow:hidden}
.tabbar{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);z-index:90;display:flex;background:var(--bar-bg);backdrop-filter:blur(24px) saturate(180%);-webkit-backdrop-filter:blur(24px) saturate(180%);border:1px solid var(--bar-line);border-radius:26px;box-shadow:var(--shadow-lg);padding:5px;gap:2px}
.tabbar .titem{display:flex;flex-direction:column;align-items:center;gap:2px;font-size:10px;color:var(--sub);text-decoration:none;padding:6px 15px;border-radius:20px;transition:background .15s ease,color .15s ease}
.tabbar .titem .tic{font-size:20px;line-height:1}
.tabbar .titem.on{color:#fff;background:var(--accent)}
body{padding-bottom:88px;max-width:760px}
@media (prefers-reduced-motion:reduce){*{animation-duration:.01ms!important;transition-duration:.01ms!important}}
</style>
</head>
<body>
<a class="back" href="/">← 返回列表</a>
<h1>📊 拾光集统计</h1>
<div class="total">共 {{ total }} 条收藏</div>

<div class="card">
  <h2>🌐 平台分布</h2>
  {% for p in platforms %}
  <div class="row">
    <span class="label">{{ p['platform'] }}</span>
    <div class="bar" style="width:{{ (p['c'] / max_p * 100)|int }}%"></div>
    <span class="num">{{ p['c'] }}</span>
  </div>
  {% endfor %}
</div>

<div class="card">
  <h2>👤 作者 Top 10</h2>
  {% for a in authors %}
  <div class="row">
    <span class="label"><a href="/?author={{ a['author_name']|urlencode }}" style="color:var(--accent);text-decoration:none">{{ a['author_name'] }}</a></span>
    <div class="bar" style="width:{{ (a['c'] / max_a * 100)|int }}%"></div>
    <span class="num">{{ a['c'] }}</span>
  </div>
  {% endfor %}
</div>

<div class="card">
  <h2>📈 收藏趋势（近 {{ trend|length }} 天）</h2>
  {% if trend %}
  <div class="trend-grid">
    {% for t in trend %}
    <div class="trend-col" title="{{ t['d'] }}: {{ t['c'] }} 条">
      <div class="trend-bar" style="height:{{ (t['c'] / trend|map(attribute='c')|max * 100)|int }}%"></div>
      <span class="trend-date">{{ t['d'][5:] }}</span>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div style="color:var(--fill2);font-size:13px">暂无数据</div>
  {% endif %}
</div>
<nav class="tabbar">
  <a href="/" class="titem"><span class="tic">🏠</span>浏览</a>
  <a href="/queue" class="titem"><span class="tic">📥</span>队列</a>
  <a href="/stats" class="titem on"><span class="tic">📊</span>统计</a>
  <a href="/?mode=manage" class="titem"><span class="tic">🗑️</span>管理</a>
</nav>
</body>
</html>"""

@app.route("/item/<int:item_id>")
def detail(item_id):
    conn = db()
    it = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    if not it:
        abort(404)
    files = list_files(it["local_path"])
    return render_template_string(DETAIL_HTML, it=dict(it), files=files)
@app.route("/avatar/<int:item_id>")
def avatar(item_id):
    """作者头像代理：优先读本地预下载文件，其次服务端拉取外链，带内存缓存"""
    local_avatar = os.path.join(BASE, "avatars", f"{item_id}.jpg")
    if os.path.isfile(local_avatar):
        from flask import send_file
        return send_file(local_avatar)
    conn = db()
    it = conn.execute("SELECT author_avatar FROM items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    if not it or not it["author_avatar"]:
        abort(404)
    url = it["author_avatar"]
    cached = _avatar_cache.get(url)
    if cached:
        return Response(cached, content_type="image/jpeg")
    import urllib.request
    try:
        referer = "https://weibo.com/"
        if "xhscdn" in url:
            referer = "https://www.xiaohongshu.com/"
        elif "douyinpic" in url or "douyin" in url:
            referer = "https://www.douyin.com/"
        elif "twimg" in url:
            referer = "https://x.com/"
        elif "tiktokcdn" in url:
            referer = "https://www.tiktok.com/"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148 Safari/604.1", "Referer": referer})
        data = urllib.request.urlopen(req, timeout=10).read()
        if len(data) > 2000000:
            abort(404)
        _avatar_cache[url] = data
        return Response(data, content_type="image/jpeg")
    except Exception:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=10).read()
            if len(data) > 2000000:
                abort(404)
            _avatar_cache[url] = data
            return Response(data, content_type="image/jpeg")
        except Exception:
            abort(404)

@app.route("/delete/<int:item_id>", methods=["POST"])
def delete_item(item_id):
    """删除单条收藏：数据库记录 + 媒体目录文件"""
    conn = db()
    it = conn.execute("SELECT local_path FROM items WHERE id=?", (item_id,)).fetchone()
    conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    if it and it["local_path"]:
        import shutil
        d = os.path.join(MEDIA_ROOT, it["local_path"])
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    return redirect(url_for("index"))

@app.route("/delete-batch", methods=["POST"])
def delete_batch():
    """批量删除：勾选的 ids 一起删（记录 + 文件）"""
    ids = request.form.getlist("ids")
    import shutil
    conn = db()
    for i in ids:
        it = conn.execute("SELECT local_path FROM items WHERE id=?", (i,)).fetchone()
        if it:
            conn.execute("DELETE FROM items WHERE id=?", (i,))
            if it["local_path"]:
                d = os.path.join(MEDIA_ROOT, it["local_path"])
                if os.path.isdir(d):
                    shutil.rmtree(d, ignore_errors=True)
    conn.commit()
    conn.close()
    return redirect(url_for("index"))

@app.route("/media/<path:relpath>")
def media(relpath):
    full = os.path.realpath(os.path.join(MEDIA_ROOT, relpath))
    root = os.path.realpath(MEDIA_ROOT)
    if not full.startswith(root):
        abort(403)
    if not os.path.isfile(full):
        abort(404)
    from flask import send_file
    return send_file(full)

def ensure_db():
    """启动时自动建库（容器首次挂载空目录时）"""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        title TEXT,
        content TEXT,
        author_name TEXT,
        author_avatar TEXT,
        post_date TEXT,
        original_url TEXT,
        local_path TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(original_url)
    )""")
    conn.commit()
    conn.close()

if __name__ == "__main__":
    ensure_db()
    app.run(host="0.0.0.0", port=8090)