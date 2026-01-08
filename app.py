import os
import re
import json
import time
import threading
import hashlib
from pathlib import Path
from urllib.parse import unquote
from typing import Optional

from flask import Flask, request, jsonify, send_file, abort, Response, render_template_string

# -----------------------------
# CONFIG
# -----------------------------
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}  # add more if needed
DEFAULT_PAGE_SIZE = 12
MAX_PAGE_SIZE = 50

# If you want to lock it to local network only, set:
# HOST = "0.0.0.0" to allow phone access on Wi-Fi
HOST = "0.0.0.0"
PORT = 5179

# -----------------------------
# APP
# -----------------------------
app = Flask(__name__)

INDEX = {
    "built_at": None,
    "root": None,
    "items": [],  # list of dicts: {id, relpath, filename, folder, mtime, size}
    "folders": [],  # sorted unique folder strings
}
INDEX_CACHE_PATH = None

def safe_norm(path: str) -> str:
    # normalize slashes and strip odd chars
    path = path.replace("\\", "/")
    path = re.sub(r"/+", "/", path)
    path = path.lstrip("/")
    return path

def is_within_root(root: Path, target: Path) -> bool:
    try:
        root_res = root.resolve()
        target_res = target.resolve()
        return str(target_res).startswith(str(root_res) + os.sep) or target_res == root_res
    except Exception:
        return False

def compute_id(relpath: str, mtime: float, size: int) -> str:
    h = hashlib.sha1()
    h.update(relpath.encode("utf-8"))
    h.update(str(mtime).encode("utf-8"))
    h.update(str(size).encode("utf-8"))
    return h.hexdigest()[:16]

def build_index(root_dir: Path) -> dict:
    items = []
    folders = set([""])  # empty = "All"

    for p in root_dir.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in VIDEO_EXTS:
            continue

        rel = safe_norm(str(p.relative_to(root_dir)))
        stat = p.stat()
        folder = safe_norm(str(Path(rel).parent))
        if folder == ".":
            folder = ""
        folders.add(folder)

        item = {
            "relpath": rel,
            "filename": p.name,
            "folder": folder,
            "mtime": stat.st_mtime,
            "size": stat.st_size,
        }
        item["id"] = compute_id(item["relpath"], item["mtime"], item["size"])
        items.append(item)

    # newest first (based on mtime)
    items.sort(key=lambda x: x["mtime"], reverse=True)

    return {
        "built_at": time.time(),
        "root": str(root_dir),
        "items": items,
        "folders": sorted(folders, key=lambda s: (s.count("/"), s.lower())),
    }

def save_index(idx: dict, cache_path: Path):
    cache_path.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")

def load_index(cache_path: Path):
    return json.loads(cache_path.read_text(encoding="utf-8"))

def ensure_index(root_dir: Path):
    global INDEX, INDEX_CACHE_PATH

    root_dir = root_dir.resolve()
    INDEX_CACHE_PATH = root_dir / ".reels_index.json"

    # If cache exists, use it; else build
    if INDEX_CACHE_PATH.exists():
        try:
            idx = load_index(INDEX_CACHE_PATH)
            # basic sanity check
            if idx.get("root") == str(root_dir) and isinstance(idx.get("items"), list):
                INDEX = idx
                return
        except Exception:
            pass

    INDEX = build_index(root_dir)
    save_index(INDEX, INDEX_CACHE_PATH)

def refresh_index_if_requested(root_dir: Path):
    if request.args.get("reindex") == "1":
        idx = build_index(root_dir)
        save_index(idx, INDEX_CACHE_PATH)
        global INDEX
        INDEX = idx

def score_match(q: str, filename: str, relpath: str) -> int:
    """
    Simple scoring:
    - direct substring in filename gets highest
    - substring in relpath next
    - token matches
    """
    q = q.lower().strip()
    if not q:
        return 0
    f = filename.lower()
    r = relpath.lower()

    if q == f:
        return 1000
    if q in f:
        return 800 - (len(f) - len(q))
    if q in r:
        return 500 - (len(r) - len(q))

    # token scoring
    tokens = re.split(r"[\s_\-\.]+", q)
    score = 0
    for t in tokens:
        if not t:
            continue
        if t in f:
            score += 120
        elif t in r:
            score += 60
    return score

def filter_items(q: str, folder: str, starts_with: Optional[str] = None):
  q = (q or "").strip()
  folder = safe_norm(folder or "")
  items = INDEX["items"]

  if folder:
    items = [it for it in items if it["folder"] == folder or it["folder"].startswith(folder + "/")]

  if starts_with:
    sw = starts_with.strip().lower()
    if sw:
      items = [it for it in items if it["filename"].lower().startswith(sw)]

  if not q:
    return items

  scored = []
  for it in items:
    s = score_match(q, it["filename"], it["relpath"])
    if s > 0:
      scored.append((s, it))
  scored.sort(key=lambda x: x[0], reverse=True)
  return [it for _, it in scored]

def make_page(items, offset: int, limit: int):
    chunk = items[offset: offset + limit]
    return {
        "total": len(items),
        "offset": offset,
        "limit": limit,
        "items": [
            {
                "id": it["id"],
                "filename": it["filename"],
                "folder": it["folder"],
                "relpath": it["relpath"],
                "url": f"/v/{it['id']}",
                "mtime": it["mtime"],
                "size": it["size"],
            }
            for it in chunk
        ],
    }

def get_item_by_id(item_id: str):
    for it in INDEX["items"]:
        if it["id"] == item_id:
            return it
    return None

HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover" />
  <title>Local Reels Feed</title>
  <style>
    :root {
      --bg: #0b0b0c;
      --panel: rgba(20,20,22,0.85);
      --text: #f4f4f6;
      --muted: rgba(244,244,246,0.65);
      --line: rgba(244,244,246,0.12);
      --chip: rgba(244,244,246,0.10);
      --chip2: rgba(244,244,246,0.06);
      --shadow: 0 10px 40px rgba(0,0,0,0.45);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
      background: var(--bg);
      color: var(--text);
      overflow: hidden; /* we scroll in the feed container */
    }

    /* Top bar */
    .topbar {
      position: fixed;
      left: 0; right: 0; top: 0;
      padding: 12px 12px 10px;
      background: linear-gradient(to bottom, rgba(0,0,0,0.75), rgba(0,0,0,0.15), rgba(0,0,0,0));
      z-index: 50;
      pointer-events: none;
    }
    .controls {
      pointer-events: auto;
      max-width: 900px;
      margin: 0 auto;
      display: flex;
      gap: 10px;
      align-items: stretch;
    }
    .searchWrap {
      flex: 1;
      position: relative;
    }
    input[type="search"] {
      width: 100%;
      border: 1px solid var(--line);
      background: rgba(15,15,18,0.65);
      color: var(--text);
      padding: 12px 12px;
      border-radius: 14px;
      outline: none;
      font-size: 16px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
    }
    .select {
      width: 180px;
      border: 1px solid var(--line);
      background: rgba(15,15,18,0.65);
      color: var(--text);
      padding: 12px 12px;
      border-radius: 14px;
      outline: none;
      font-size: 14px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
    }
    .hint {
      margin-top: 6px;
      font-size: 12px;
      color: var(--muted);
      pointer-events: none;
    }

    /* Typeahead */
    .suggest {
      position: absolute;
      left: 0; right: 0;
      top: 52px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      overflow: hidden;
      box-shadow: var(--shadow);
      backdrop-filter: blur(12px);
      display: none;
      max-height: 44vh;
      overflow-y: auto;
    }
    .suggest.show { display: block; }
    .sItem {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      cursor: pointer;
    }
    .sItem:hover { background: rgba(255,255,255,0.06); }
    .sTitle { font-size: 13px; color: var(--text); }
    .sMeta { font-size: 12px; color: var(--muted); margin-top: 3px; }

    /* Feed */
    .feed {
      position: fixed;
      top: 0; bottom: 0; right: 0;
      left: 64px; /* Space for sidebar */
      padding-top: 78px; /* space for topbar */
      overflow-y: auto;
      scroll-snap-type: y mandatory;
      -webkit-overflow-scrolling: touch;
    }
    .card {
      scroll-snap-align: start;
      height: calc(100vh - 78px);
      max-height: calc(100vh - 78px);
      display: grid;
      place-items: center;
      position: relative;
      border-bottom: 1px solid rgba(255,255,255,0.04);
    }
    video {
      width: 100%;
      height: 100%;
      object-fit: contain;
      background: #000;
    }

    /* Overlay */
    .overlay {
      position: absolute;
      left: 12px;
      right: 12px;
      bottom: 12px;
      display: flex;
      justify-content: space-between;
      gap: 10px;
      pointer-events: none;
    }
    .meta {
      pointer-events: auto;
      background: rgba(15,15,18,0.55);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 10px 12px;
      max-width: 74%;
      backdrop-filter: blur(10px);
    }
    .filename { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .folder { font-size: 12px; color: var(--muted); margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .actions {
      pointer-events: auto;
      display: flex;
      flex-direction: column;
      gap: 10px;
      align-items: flex-end;
    }
    .btn {
      border: 1px solid var(--line);
      background: rgba(15,15,18,0.55);
      color: var(--text);
      padding: 10px 12px;
      border-radius: 16px;
      font-size: 12px;
      cursor: pointer;
      backdrop-filter: blur(10px);
      user-select: none;
    }
    .btn:hover { background: rgba(255,255,255,0.06); }

    /* Loading */
    .loading {
      padding: 24px;
      color: var(--muted);
      text-align: center;
      font-size: 13px;
    }
    .toast {
      position: fixed;
      left: 50%;
      transform: translateX(-50%);
      bottom: 18px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      background: rgba(15,15,18,0.85);
      color: var(--text);
      border-radius: 14px;
      display: none;
      z-index: 60;
      backdrop-filter: blur(10px);
      box-shadow: var(--shadow);
      font-size: 13px;
    }
    .toast.show { display: block; }

    /* Alpha sidebar */
    .alphaBar {
      position: fixed;
      left: 12px;
      top: 50%;
      transform: translateY(-50%);
      width: 40px;
      max-height: 80vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 2px;
      z-index: 55;
      pointer-events: auto;
      padding: 8px 0;
      background: rgba(20,20,22,0.4);
      backdrop-filter: blur(8px);
      border-radius: 24px;
      border: 1px solid var(--line);
      overflow-y: auto;
      scrollbar-width: none;
    }
    .alphaBar::-webkit-scrollbar { display: none; }

    .alphaItem {
      width: 28px;
      height: 24px;
      flex-shrink: 0;
      display: grid;
      place-items: center;
      background: transparent;
      color: var(--muted);
      border-radius: 6px;
      font-size: 11px;
      cursor: pointer;
      user-select: none;
      transition: all 0.2s;
    }
    .alphaItem:hover { background: rgba(255,255,255,0.1); color: var(--text); }
    .alphaItem.active { background: var(--text); color: var(--bg); font-weight: bold; box-shadow: 0 2px 8px rgba(255,255,255,0.2); }

    /* UI Hidden State */
    body.hideUI .topbar,
    body.hideUI .alphaBar,
    body.hideUI .overlay,
    body.hideUI #shutdown { display: none; }
    body.hideUI #toggleUi {
      left: auto;
      right: 12px;
      bottom: 12px;
    }
    body.hideUI .feed {
      left: 0;
      padding-top: 0;
    }
    body.hideUI .card {
      height: 100vh;
      max-height: 100vh;
    }
  </style>
</head>
<body>
  <div class="topbar">
    <div class="controls">
      <div class="searchWrap">
        <input id="q" type="search" placeholder="Search filename… (e.g. 'gym', 'ramadan', '2023-08')" autocomplete="off" />
        <div id="suggest" class="suggest"></div>
        <div class="hint" id="hint"></div>
      </div>
      <select id="folder" class="select">
        <option value="">All folders</option>
      </select>
    </div>
  </div>

  <div id="feed" class="feed"></div>
  <div id="toast" class="toast"></div>
  <div id="shutdown" class="btn" style="position: fixed; bottom: 12px; left: 70px; background: rgba(220,30,30,0.9); color: white;">Shutdown</div>
  <div id="toggleUi" class="btn" style="position: fixed; bottom: 12px; left: 170px; background: rgba(48,178,96,0.9); color: #0b0b0c; font-weight: 600;">Hide UI</div>
  <div id="alphaBar" class="alphaBar"></div>

<script>
const feedEl = document.getElementById("feed");
const qEl = document.getElementById("q");
const folderEl = document.getElementById("folder");
const suggestEl = document.getElementById("suggest");
const hintEl = document.getElementById("hint");
const toastEl = document.getElementById("toast");
const alphaBarEl = document.getElementById("alphaBar");
const toggleUiBtn = document.getElementById("toggleUi");

let offset = 0;
let total = 0;
let loading = false;
let pageSize = 10;
let currentQuery = "";
let currentFolder = "";
let observer = null;
let currentLetter = "";
let uiHidden = false;

function toast(msg) {
  toastEl.textContent = msg;
  toastEl.classList.add("show");
  setTimeout(() => toastEl.classList.remove("show"), 1200);
}

function debounce(fn, ms) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  }
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error("HTTP " + res.status);
  return await res.json();
}

function buildCard(item) {
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.id = item.id;

  const v = document.createElement("video");
  v.src = item.url;
  v.playsInline = true;
  v.loop = true;
  v.muted = false;
  v.controls = false;
  v.preload = "metadata";

  // tap to mute/unmute
  v.addEventListener("click", () => {
    v.muted = !v.muted;
    toast(v.muted ? "Muted" : "Unmuted");
  });

  const overlay = document.createElement("div");
  overlay.className = "overlay";

  const meta = document.createElement("div");
  meta.className = "meta";

  const filename = document.createElement("div");
  filename.className = "filename";
  filename.textContent = item.filename;
  meta.appendChild(filename);

  const folder = document.createElement("div");
  folder.className = "folder";
  folder.textContent = item.folder ? item.folder : "(root)";
  meta.appendChild(folder);

  const actions = document.createElement("div");
  actions.className = "actions";

  const openBtn = document.createElement("div");
  openBtn.className = "btn";
  openBtn.textContent = "Open file";
  openBtn.addEventListener("click", () => {
    window.open("/file/" + encodeURIComponent(item.relpath), "_blank");
  });

  actions.appendChild(openBtn);

  overlay.appendChild(meta);
  overlay.appendChild(actions);

  card.appendChild(v);
  card.appendChild(overlay);

  return card;
}

function setupObserver() {
  if (observer) observer.disconnect();

  observer = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const v = e.target.querySelector("video");
      if (!v) continue;

      if (e.isIntersecting && e.intersectionRatio > 0.65) {
        // pause all others
        document.querySelectorAll(".card video").forEach(x => {
          if (x !== v) x.pause();
        });
        v.play().catch(() => {});
        // light prefetch: next 2 videos
        prefetchNext(e.target);
      } else {
        v.pause();
      }
    }
  }, { threshold: [0.25, 0.65, 0.9] });

  document.querySelectorAll(".card").forEach(card => observer.observe(card));
}

function prefetchNext(cardEl) {
  const cards = Array.from(document.querySelectorAll(".card"));
  const i = cards.indexOf(cardEl);
  for (let k = 1; k <= 2; k++) {
    const next = cards[i + k];
    if (!next) continue;
    const v = next.querySelector("video");
    if (!v) continue;
    // force the browser to touch the resource
    if (!v.dataset.prefetched) {
      v.dataset.prefetched = "1";
      fetch(v.src, { method: "GET" }).catch(() => {});
    }
  }
}

async function loadFolders() {
  const data = await fetchJSON("/api/folders");
  for (const f of data.folders) {
    const opt = document.createElement("option");
    opt.value = f;
    opt.textContent = f ? f : "(root)";
    folderEl.appendChild(opt);
  }
}

async function loadPage(reset=false) {
  if (loading) return;
  loading = true;

  if (reset) {
    offset = 0;
    total = 0;
    feedEl.innerHTML = "";
  }

  const q = encodeURIComponent(currentQuery);
  const fol = encodeURIComponent(currentFolder);
  const sw = encodeURIComponent(currentLetter.toLowerCase());
  const url = `/api/feed?q=${q}&folder=${fol}&starts_with=${sw}&offset=${offset}&limit=${pageSize}`;

  try {
    const data = await fetchJSON(url);
    total = data.total;

    for (const item of data.items) {
      feedEl.appendChild(buildCard(item));
    }

    offset += data.items.length;
    hintEl.textContent = `${total} match(es) • showing ${Math.min(offset, total)}/${total}`;

    setupObserver();

    // autoplay first visible
    const first = document.querySelector(".card");
    if (first && reset) {
      first.scrollIntoView({ behavior: "instant", block: "start" });
      // Auto-play the first video
      const firstVideo = first.querySelector("video");
      if (firstVideo) {
        firstVideo.play().catch(() => {});
      }
    }

  } catch (e) {
    console.error(e);
    toast("Load failed");
  } finally {
    loading = false;
  }
}

const updateSuggest = debounce(async () => {
  const q = (qEl.value || "").trim();
  if (!q) {
    suggestEl.classList.remove("show");
    suggestEl.innerHTML = "";
    return;
  }

  try {
    const fol = encodeURIComponent(currentFolder);
    const sw = encodeURIComponent(currentLetter.toLowerCase());
    const data = await fetchJSON(`/api/suggest?q=${encodeURIComponent(q)}&folder=${fol}&starts_with=${sw}&limit=8`);
    suggestEl.innerHTML = "";

    if (!data.items.length) {
      suggestEl.classList.remove("show");
      return;
    }

    for (const item of data.items) {
      const div = document.createElement("div");
      div.className = "sItem";

      const t = document.createElement("div");
      t.className = "sTitle";
      t.textContent = item.filename;

      const m = document.createElement("div");
      m.className = "sMeta";
      m.textContent = item.folder ? item.folder : "(root)";

      div.appendChild(t);
      div.appendChild(m);

      div.addEventListener("click", async () => {
        // set query to that filename token-ish and load
        qEl.value = item.filename;
        currentQuery = item.filename;
        suggestEl.classList.remove("show");
        await loadPage(true);
      });

      suggestEl.appendChild(div);
    }

    suggestEl.classList.add("show");
  } catch (e) {
    console.error(e);
  }
}, 120);

qEl.addEventListener("input", () => updateSuggest());

qEl.addEventListener("keydown", async (e) => {
  if (e.key === "Enter") {
    currentQuery = (qEl.value || "").trim();
    suggestEl.classList.remove("show");
    await loadPage(true);
  } else if (e.key === "Escape") {
    suggestEl.classList.remove("show");
  }
});

folderEl.addEventListener("change", async () => {
  currentFolder = folderEl.value;
  await loadPage(true);
});

feedEl.addEventListener("scroll", () => {
  // close suggestions while scrolling
  suggestEl.classList.remove("show");

  // near bottom -> load more
  const nearBottom = feedEl.scrollTop + feedEl.clientHeight > feedEl.scrollHeight - (feedEl.clientHeight * 2);
  if (nearBottom && offset < total) {
    loadPage(false);
  }
});

document.getElementById("shutdown").addEventListener("click", async () => {
  if (confirm("Are you sure you want to shut down the server?")) {
    try {
      const res = await fetch("/shutdown", { method: "POST" });
      if (res.ok) {
        alert("Server is shutting down...");
      } else {
        alert("Failed to shut down the server.");
      }
    } catch (e) {
      console.error(e);
      alert("An error occurred while shutting down the server.");
    }
  }
});

toggleUiBtn.addEventListener("click", () => {
  uiHidden = !uiHidden;
  document.body.classList.toggle("hideUI", uiHidden);
  toggleUiBtn.textContent = uiHidden ? "Show UI" : "Hide UI";
  toast(uiHidden ? "UI Hidden" : "UI Visible");
});

(async function init() {
  await loadFolders();
  // Start with 'A' selected by default (alphabetically first)
  currentLetter = "A";
  const firstAlpha = alphaBarEl.querySelector('.alphaItem[title="Starts with A"]');
  if (firstAlpha) firstAlpha.classList.add("active");
  await loadPage(true);
})();

// Build A-Z, numbers, and symbols sidebar
(function buildAlphaBar(){
  const lettersNumbersAndSymbols = ["All"].concat(
    Array.from({length:26}, (_,i)=>String.fromCharCode(65+i))
    .concat(Array.from({length:10}, (_,i)=>i.toString()))
    .concat(["#", "@", "&", "$"])
  );
  lettersNumbersAndSymbols.forEach(ch => {
    const div = document.createElement("div");
    div.className = "alphaItem";
    div.textContent = ch === "All" ? "•" : ch;
    div.title = ch === "All" ? "All" : `Starts with ${ch}`;
    div.addEventListener("click", async () => {
      currentLetter = ch === "All" ? "" : ch;
      // update active state
      alphaBarEl.querySelectorAll(".alphaItem").forEach(el => el.classList.remove("active"));
      div.classList.add("active");
      await loadPage(true);
    });
    alphaBarEl.appendChild(div);
  });
})();
</script>
</body>
</html>
"""

@app.route("/")
def home():
    root = Path(app.config["ROOT_DIR"])
    refresh_index_if_requested(root)
    return render_template_string(HTML)

@app.route("/api/folders")
def api_folders():
    return jsonify({"folders": INDEX["folders"]})

@app.route("/api/feed")
def api_feed():
  q = request.args.get("q", "")
  folder = request.args.get("folder", "")
  starts_with = (request.args.get("starts_with", "") or "").strip() or None
  try:
      offset = int(request.args.get("offset", "0"))
      limit = int(request.args.get("limit", str(DEFAULT_PAGE_SIZE)))
  except ValueError:
      return jsonify({"error": "bad offset/limit"}), 400

  if offset < 0:
      offset = 0
  limit = max(1, min(limit, MAX_PAGE_SIZE))

  items = filter_items(q, folder, starts_with)
  return jsonify(make_page(items, offset, limit))

@app.route("/api/suggest")
def api_suggest():
  q = request.args.get("q", "").strip()
  folder = request.args.get("folder", "")
  starts_with = (request.args.get("starts_with", "") or "").strip() or None
  try:
      limit = int(request.args.get("limit", "8"))
  except ValueError:
      limit = 8
  limit = max(1, min(limit, 20))

  items = filter_items(q, folder, starts_with)
    # "recommendations as we search" = top matches for current partial query
  top = items[:limit]
  return jsonify({
      "items": [
          {"id": it["id"], "filename": it["filename"], "folder": it["folder"], "relpath": it["relpath"]}
          for it in top
      ]
  })

@app.route("/v/<item_id>")
def stream_video(item_id: str):
    root = Path(app.config["ROOT_DIR"]).resolve()
    it = get_item_by_id(item_id)
    if not it:
        abort(404)

    target = (root / it["relpath"]).resolve()
    if not is_within_root(root, target) or not target.exists():
        abort(404)

    # send_file supports range requests via conditional in Werkzeug in many setups,
    # but for best streaming behavior we implement basic Range support:
    range_header = request.headers.get("Range", None)
    if not range_header:
        return send_file(str(target), conditional=True)

    size = target.stat().st_size
    m = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if not m:
        return send_file(str(target), conditional=True)

    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    end = min(end, size - 1)
    if start > end:
        abort(416)

    length = end - start + 1
    with open(target, "rb") as f:
        f.seek(start)
        data = f.read(length)

    resp = Response(data, 206, mimetype="video/mp4", direct_passthrough=True)
    resp.headers.add("Content-Range", f"bytes {start}-{end}/{size}")
    resp.headers.add("Accept-Ranges", "bytes")
    resp.headers.add("Content-Length", str(length))
    return resp

@app.route("/file/<path:relpath>")
def open_file(relpath: str):
    root = Path(app.config["ROOT_DIR"]).resolve()
    relpath = safe_norm(unquote(relpath))
    target = (root / relpath).resolve()
    if not is_within_root(root, target) or not target.exists() or not target.is_file():
        abort(404)
    return send_file(str(target), conditional=True)

# Add a route to terminate the server
@app.route("/shutdown", methods=["POST"])
def shutdown():
  environ = request.environ

  def _shutdown(env):
    # Slight delay to allow response to be sent before exiting
    time.sleep(0.2)
    func = env.get("werkzeug.server.shutdown")
    if callable(func):
      try:
        func()
        return
      except Exception:
        pass
    # Fallback: forcefully exit the process (works across servers)
    os._exit(0)

  threading.Thread(target=_shutdown, args=(environ,), daemon=True).start()
  return jsonify({"message": "Server shutting down..."})

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="Root folder containing reels (subfolders included)")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    root_dir = Path(args.root).expanduser().resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        raise SystemExit(f"Root folder does not exist or is not a directory: {root_dir}")

    app.config["ROOT_DIR"] = str(root_dir)
    ensure_index(root_dir)

    # Tip: you can refresh index by visiting http://IP:PORT/?reindex=1
    app.run(host=args.host, port=args.port, debug=False, threaded=True)

if __name__ == "__main__":
    main()
