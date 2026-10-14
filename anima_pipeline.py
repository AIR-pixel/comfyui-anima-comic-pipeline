#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
anima_pipeline - 漫画产出流水线（本地，纯标准库，零依赖）

两步，一个进程：
  1) 任务书：内置生成器产出任务书 md——下载后交给任意 AI（ChatGPT / Claude /
     本地模型都行），按任务书里的输出协议拿回分镜 md
  2) ComfyUI 生产：分镜 md -> Anima 文生图工作流 -> 结果/第N章/

引擎自管理：ComfyUI 没在跑就自动拉起、用完自动关（--keep-comfy 常驻）。
自动拉起只对 ComfyUI-aki-v3 布局开箱即用；其他安装请先手动启动 ComfyUI，
或用 --comfy-python / --comfy-main 指定。你手动开着的实例会被直接复用，
退出流水线时也不会关。

用法：
  python anima_pipeline.py                      # 默认端口 8790
  python anima_pipeline.py --port 9000
  python anima_pipeline.py --comfy-url http://192.168.1.10:8188
然后浏览器打开 http://127.0.0.1:8790
"""
import argparse
import copy
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
TASKBOOK_DIR = STATIC / "taskbook"
OUT_IMG = BASE / "结果"
OUT_IMG.mkdir(exist_ok=True)

COMFY = "http://127.0.0.1:8188"     # --comfy-url 可覆盖（支持远程 ComfyUI）

# ComfyUI 引擎自管理：没在跑就由流水线以后台子进程拉起，用完自动关
# （--keep-comfy 可常驻）。以下默认路径是 ComfyUI-aki-v3 布局，仅在没有
# ComfyUI 在跑时才会用到；其他安装用 --comfy-python / --comfy-main 指定。
COMFY_PYTHON = Path(r"C:\Program Files\ComfyUI-aki-v3\python\python.exe")
COMFY_MAIN = Path(r"C:\Program Files\ComfyUI-aki-v3\ComfyUI\main.py")
ENGINE_LOG = BASE / "logs" / "comfy_engine.log"
ENGINE_LOG.parent.mkdir(exist_ok=True)
# ComfyUI 子进程的 PYTHONPATH 锚点：不继承宿主（IDE/代理 shim）的 PYTHONPATH，
# 避免无关目录混入 ComfyUI 的 import 搜索路径。用途见 _spawn_comfy()。
ENGINE_ENV = BASE / "engine_env"
ENGINE_ENV.mkdir(exist_ok=True)

_COMFY_PROC = [None]        # 我们拉起的子进程句柄
_COMFY_OWNED = [False]      # 当前 8188 上的实例是否由本流水线拉起（决定能不能杀）
_COMFY_LOCK = threading.Lock()
KEEP_COMFY = [False]        # --keep-comfy：任务完成后不关引擎

_tpl = json.loads((BASE / "workflow_template.json").read_text(encoding="utf-8"))
TEMPLATE = _tpl["template"]
TPL_META = _tpl.get("meta", {})
TPL_DEFAULTS = TPL_META.get("_defaults", {})
TPL_MODELS = TPL_META.get("_models", {})
SAVE_NODE = next(nid for nid, n in TEMPLATE.items() if n["class_type"] == "SaveImage")

MD_BEGIN = "<!-- PIPELINE-PANELS-BEGIN -->"
MD_END = "<!-- PIPELINE-PANELS-END -->"

# 动态加载链的节点 id 段（避开模板自带的 348-439）
N_UNET, N_CLIP, N_VAE, N_AURA = "870", "880", "881", "890"


# ---------- HTTP 工具 ----------

# 全部目标都是本机/LAN 地址，禁用系统代理（否则每次连接被代理拖数秒）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _req(url, data=None, timeout=60, headers=None):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    body = json.dumps(data).encode("utf-8") if data is not None else None
    return urllib.request.Request(url, data=body, headers=h)


def get_json(url, timeout=15):
    with _OPENER.open(_req(url, None, timeout), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post_json(url, data, timeout=60):
    with _OPENER.open(_req(url, data, timeout), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- 服务状态与显存 ----------

def check_comfy():
    """严格探测：HTTP 通不够，必须返回带 devices 的 ComfyUI JSON
    （系统代理会对本机请求返回 502 HTML，只看连通性会被骗）"""
    try:
        d = get_json(COMFY + "/system_stats", timeout=4)
        return isinstance(d, dict) and "devices" in d
    except Exception:
        return False


def comfy_vram():
    try:
        d = get_json(COMFY + "/system_stats", timeout=5)
        for dev in d.get("devices", []):
            if dev.get("type") == "cuda":
                return {"total": dev.get("vram_total"), "free": dev.get("vram_free"),
                        "name": dev.get("name", "")}
    except Exception:
        pass
    return None


def comfy_free(unload_models=True, free_memory=True):
    try:
        post_json(COMFY + "/free", {"unload_models": unload_models, "free_memory": free_memory},
                  timeout=30)
        return True
    except Exception:
        return False


# ---------- ComfyUI 引擎自管理 ----------

def _kill_tree(pid):
    """杀进程树（ComfyUI 拉起时会带子进程）；Windows 用 taskkill"""
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       creationflags=0x08000000, timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def comfy_engine_info():
    """给 /api/status 用的引擎状态"""
    running = check_comfy()
    proc = _COMFY_PROC[0]
    return {"running": running,
            "owned": bool(_COMFY_OWNED[0]) and running,
            "pid": (proc.pid if proc and proc.poll() is None else None),
            "keep": KEEP_COMFY[0],
            "log": str(ENGINE_LOG)}


def _spawn_comfy():
    """后台拉起 ComfyUI-aki-v3（无窗口、stdin=DEVNULL），返回 Popen 句柄。

    两个关键环境变量：
    - PYTHONPATH=ENGINE_ENV：不继承宿主（IDE/代理 shim）的 import 搜索路径；
    - COMFYTV_TESTING=1：ComfyTV 官方测试模式开关，跳过其 import 阶段的
      run_startup_backup()。否则每次无头启动都会写一份 db-backup 快照并
      prune 最旧快照；旧快照 >50 文件时触发 safe-delete 批量删除确认，
      无头进程等不到输入直接退出（日志尾部 SAFE_DELETE_BULK_CONFIRM_REQUIRED）。
      该模式下 ComfyTV 的 comfy_entrypoint 抛 RuntimeError，ComfyUI 捕获后
      仅跳过该节点（nodes.py comfy_entrypoint 的 except 分支），服务与其他
      节点不受影响；Anima 工作流不用 ComfyTV 节点。手动启动 ComfyUI 时无此
      变量，ComfyTV 全功能加载。
    """
    with ENGINE_LOG.open("a", encoding="utf-8") as lf:
        lf.write("\n===== %s 流水线拉起 =====\n" % datetime.now())
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ENGINE_ENV)
    env["COMFYTV_TESTING"] = "1"
    return subprocess.Popen(
        [str(COMFY_PYTHON), str(COMFY_MAIN),
         "--listen", "127.0.0.1", "--port", "8188",
         "--disable-auto-launch", "--offline"],
        cwd=str(COMFY_MAIN.parent),
        env=env,
        stdin=subprocess.DEVNULL,      # 阻塞管道会被 ComfyTV 判为交互模式
        stdout=open(ENGINE_LOG, "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        creationflags=0x08000000)      # CREATE_NO_WINDOW


def comfy_ensure_running(log=None, timeout=240):
    """确保 8188 上有 ComfyUI 在服务：已在跑（不管谁起的）直接用；
    没在跑就后台拉起 ComfyUI-aki-v3，轮询 /system_stats 等就绪。
    返回 (ok, owned, err)"""
    if check_comfy():
        return True, bool(_COMFY_OWNED[0]), None
    if not COMFY_PYTHON.exists() or not COMFY_MAIN.exists():
        return False, False, ("找不到 ComfyUI：%s 或 %s 不存在"
                              % (COMFY_PYTHON, COMFY_MAIN))
    with _COMFY_LOCK:
        if check_comfy():          # 并发双点生产时另一个线程可能已拉起
            return True, bool(_COMFY_OWNED[0]), None
        proc = _COMFY_PROC[0]
        if proc is None or proc.poll() is not None:
            if log is not None:
                log.append("ComfyUI 未运行，正在后台启动（约 40–60 秒）…")
            try:
                proc = _spawn_comfy()
            except Exception as e:
                return False, False, "启动 ComfyUI 失败：%s" % e
            _COMFY_PROC[0] = proc
            _COMFY_OWNED[0] = True
    t0 = time.time()
    exited_at = None
    while time.time() - t0 < timeout:
        if proc.poll() is not None and exited_at is None:
            exited_at = time.time()
        if exited_at is not None and time.time() - exited_at > 15:
            # 退出后宽限 15s 再判失败：留时间给"主进程退出、服务由派生
            # 子进程接管"的情况；超时则读日志尾部报错
            try:
                tail = ENGINE_LOG.read_text(encoding="utf-8", errors="ignore")[-600:]
            except Exception:
                tail = ""
            _COMFY_PROC[0] = None
            _COMFY_OWNED[0] = False
            return False, False, ("ComfyUI 启动即退出（详见 logs\\comfy_engine.log）：%s"
                                  % tail[-300:])
        if check_comfy():
            if log is not None:
                log.append("ComfyUI 已就绪（%.0fs）" % (time.time() - t0))
            return True, True, None
        time.sleep(1.5)
    return False, False, "ComfyUI 启动超时（%ds），详见 logs\\comfy_engine.log" % timeout


def comfy_stop_owned():
    """只杀本流水线拉起的实例；用户手动开的绝不碰。
    返回 (stopped, note)"""
    proc = _COMFY_PROC[0]
    if not _COMFY_OWNED[0] or proc is None or proc.poll() is not None:
        _COMFY_PROC[0] = None
        _COMFY_OWNED[0] = False
        return False, "引擎不是流水线拉起的，未做操作"
    pid = proc.pid
    _kill_tree(pid)
    try:
        proc.wait(timeout=15)
    except Exception:
        pass
    _COMFY_PROC[0] = None
    _COMFY_OWNED[0] = False
    return True, "已关闭流水线拉起的 ComfyUI（PID %d）" % pid


def maybe_stop_owned_comfy(log=None):
    """生产全部结束后调用：非 keep 模式且没有其他生产任务在跑时关掉引擎"""
    if KEEP_COMFY[0]:
        return False, "keep 模式，引擎保持常驻"
    with JOBS_LOCK:
        busy = any(j["kind"] == "produce" and j["state"] == "running" for j in JOBS.values())
    if busy:
        return False, "还有其他生产任务，引擎保留"
    stopped, note = comfy_stop_owned()
    if stopped and log is not None:
        log.append(note)
    return stopped, note


# ---------- 模型配置（ComfyUI 文件枚举） ----------

_OBJ_CACHE = {}


def object_info(cls):
    if cls not in _OBJ_CACHE:
        u = COMFY + "/object_info/" + urllib.parse.quote(cls)
        _OBJ_CACHE[cls] = get_json(u, timeout=30).get(cls, {})
    return _OBJ_CACHE[cls]


def _enum_of(cls, field):
    try:
        spec = object_info(cls)["input"]["required"].get(field)
        if isinstance(spec, list) and spec and isinstance(spec[0], list):
            return [str(x) for x in spec[0]]
    except Exception:
        pass
    return []


def model_options():
    return {
        "unets": _enum_of("UNETLoader", "unet_name"),
        "weight_dtypes": _enum_of("UNETLoader", "weight_dtype"),
        "clips": _enum_of("CLIPLoader", "clip_name"),
        "clip_types": _enum_of("CLIPLoader", "type"),
        "vaes": _enum_of("VAELoader", "vae_name"),
        "loras": _enum_of("LoraLoaderModelOnly", "lora_name"),
    }


def default_model_cfg():
    loras = [{"name": l.get("name"), "strength": l.get("strength", 1.0), "enabled": True}
             for l in TPL_MODELS.get("loras", [])]
    return {
        "unet": TPL_MODELS.get("unet", {}).get("name", ""),
        "weight_dtype": TPL_MODELS.get("unet", {}).get("weight_dtype", "default"),
        "clip": TPL_MODELS.get("clip", {}).get("name", ""),
        "clip_type": TPL_MODELS.get("clip", {}).get("type", "stable_diffusion"),
        "vae": TPL_MODELS.get("vae", {}).get("name", ""),
        "loras": loras,
        "shift": TPL_MODELS.get("shift", 3),
        "sampling": TPL_MODELS.get("sampling", "flow"),
    }


def sanitize_model_cfg(cfg):
    """校验/补全前端传来的模型配置；无效则回退模板默认"""
    d = default_model_cfg()
    if not isinstance(cfg, dict):
        return d
    for k in ("unet", "clip", "vae"):
        v = cfg.get(k)
        if isinstance(v, str) and v.strip():
            d[k] = v.strip()
    for k in ("weight_dtype", "clip_type"):
        v = cfg.get(k)
        if isinstance(v, str) and v.strip():
            d[k] = v.strip()
    loras = []
    for l in (cfg.get("loras") or []):
        if not isinstance(l, dict):
            continue
        name = (l.get("name") or "").strip()
        if not name:
            continue
        try:
            strength = float(l.get("strength", 1.0))
        except Exception:
            strength = 1.0
        loras.append({"name": name, "strength": strength, "enabled": bool(l.get("enabled", True))})
    d["loras"] = loras
    try:
        d["shift"] = float(cfg.get("shift", d["shift"]))
    except Exception:
        pass
    return d


# ---------- 分镜 md 解析（生产环节读取） ----------

def normalize_panels(obj):
    warnings = []
    panels = []
    default_seed = int(TPL_DEFAULTS.get("seed", 0))
    for i, p in enumerate(obj.get("panels", []), 1):
        prompt = str(p.get("prompt") or "").strip()
        if not prompt:
            warnings.append("第 %s 格缺少 prompt，已跳过" % p.get("id", i))
            continue
        seed = p.get("seed")
        try:
            seed = int(seed)
        except Exception:
            seed = default_seed + i
            warnings.append("第 %s 格缺少 seed，已用默认种子+%d 代替（连续性可能受损）" % (p.get("id", i), i))
        panels.append({
            "seq": i,
            "id": p.get("id"),
            "seed": seed,
            "prompt": prompt,
            "caption": str(p.get("caption") or ""),
            "negative": (str(p["negative"]).strip() if p.get("negative") else None),
            "width": p.get("width"), "height": p.get("height"),
            "steps": p.get("steps"), "cfg": p.get("cfg"),
        })
    return {
        "chapter": obj.get("chapter"),
        "panels": panels,
        "self_check": obj.get("self_check", ""),
        "running_summary": obj.get("running_summary", ""),
    }, warnings


def parse_md_panels(text):
    """从 md 提取 panels：优先读 PIPELINE 标记之间的块，否则取第一个含 panels 的 json 块"""
    best = None
    i = text.find(MD_BEGIN)
    j = text.find(MD_END)
    if 0 <= i < j:
        seg = text[i + len(MD_BEGIN):j]
        for m in re.finditer(r"```(?:json)?\s*\n(.*?)```", seg, flags=re.S):
            frag = m.group(1).strip()
            if '"panels"' not in frag:
                continue
            try:
                obj = json.loads(frag)
            except Exception:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("panels"), list):
                best = obj
                break
    if best is None:
        for m in re.finditer(r"```(?:json)?\s*\n(.*?)```", text, flags=re.S):
            frag = m.group(1).strip()
            if '"panels"' not in frag:
                continue
            try:
                obj = json.loads(frag)
            except Exception:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("panels"), list):
                best = obj
                break
    if best is None:
        raise ValueError("md 中找不到含 panels 的 JSON 块（应放在 ```json 代码块内）")
    return normalize_panels(best)


# ---------- ComfyUI 环节 ----------

def build_prompt(panel, chapter, model_cfg=None):
    """按模型配置动态构建加载链 + 模板固定的提示词/采样/保存节点"""
    cfg = sanitize_model_cfg(model_cfg)
    p = {}
    p[N_UNET] = {"class_type": "UNETLoader",
                 "inputs": {"unet_name": cfg["unet"], "weight_dtype": cfg["weight_dtype"]}}
    prev = N_UNET
    for idx, l in enumerate(cfg["loras"]):
        if not l.get("enabled"):
            continue
        nid = "9%02d" % idx
        p[nid] = {"class_type": "LoraLoaderModelOnly",
                  "inputs": {"model": [prev, 0], "lora_name": l["name"],
                             "strength_model": l["strength"]}}
        prev = nid
    p[N_AURA] = {"class_type": "ModelSamplingAuraFlow",
                 "inputs": {"model": [prev, 0], "shift": cfg["shift"], "sampling": cfg["sampling"]}}
    p[N_CLIP] = {"class_type": "CLIPLoader",
                 "inputs": {"clip_name": cfg["clip"], "type": cfg["clip_type"], "device": "default"}}
    p[N_VAE] = {"class_type": "VAELoader", "inputs": {"vae_name": cfg["vae"]}}
    for nid, node in TEMPLATE.items():
        if node["class_type"] in ("UNETLoader", "CLIPLoader", "VAELoader",
                                  "LoraLoaderModelOnly", "ModelSamplingAuraFlow"):
            continue
        p[nid] = copy.deepcopy(node)
    p["357"]["inputs"]["clip"] = [N_CLIP, 0]
    p["358"]["inputs"]["clip"] = [N_CLIP, 0]
    p["364"]["inputs"]["vae"] = [N_VAE, 0]
    p["362"]["inputs"]["model"] = [N_AURA, 0]
    p["353"]["inputs"]["prompt"] = panel["prompt"]
    if panel.get("negative"):
        p["358"]["inputs"]["text"] = panel["negative"]
    ks = p["362"]["inputs"]
    ks["seed"] = int(panel["seed"]) % (2 ** 64)
    if panel.get("steps"):
        ks["steps"] = int(panel["steps"])
    if panel.get("cfg") is not None:
        ks["cfg"] = float(panel["cfg"])
    lat = p["361"]["inputs"]
    if panel.get("width"):
        lat["width"] = int(panel["width"])
    if panel.get("height"):
        lat["height"] = int(panel["height"])
    p[SAVE_NODE]["inputs"]["filename_prefix"] = "anima-t2i/ch%02d_p%02d" % (int(chapter), panel["seq"])
    return p


def comfy_submit(prompt_dict):
    d = post_json(COMFY + "/prompt", {"prompt": prompt_dict, "client_id": "anima_pipeline"}, timeout=60)
    if d.get("node_errors"):
        raise RuntimeError("ComfyUI 节点错误：" + json.dumps(d["node_errors"], ensure_ascii=False)[:400])
    return d["prompt_id"]


def comfy_history(pid):
    try:
        h = get_json(COMFY + "/history/" + urllib.parse.quote(pid), timeout=10)
    except Exception:
        return None
    return h.get(pid)


def comfy_queue():
    try:
        return get_json(COMFY + "/queue", timeout=8)
    except Exception:
        return {"queue_running": [], "queue_pending": []}


def fetch_image(filename, subfolder, ftype="output"):
    u = COMFY + "/view?" + urllib.parse.urlencode(
        {"filename": filename, "subfolder": subfolder or "", "type": ftype})
    with _OPENER.open(u, timeout=30) as r:
        return r.read(), r.headers.get("Content-Type", "image/png")


def copy_result(filename, subfolder, ftype, dest_dir, new_name):
    """通过 HTTP 拉取 ComfyUI 的输出图存到本地：不依赖 ComfyUI 安装路径，
    远程 ComfyUI 同样适用"""
    if ftype and ftype != "output":
        return None
    try:
        data, _ = fetch_image(filename, subfolder, ftype)
    except Exception:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dst = dest_dir / new_name
    dst.write_bytes(data)
    return dst


# ---------- 任务管理 ----------

JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_SEQ = [0]


def new_job(kind, chapter=1, name=""):
    with JOBS_LOCK:
        JOB_SEQ[0] += 1
        jid = "%s-%d" % (kind, JOB_SEQ[0])
        JOBS[jid] = {"id": jid, "kind": kind, "chapter": chapter, "name": name,
                     "state": "running", "started": time.time(), "error": None,
                     "result": None, "panels": [], "warnings": [], "steps": []}
    return jid


def job_set(jid, **kw):
    with JOBS_LOCK:
        JOBS[jid].update(kw)


def job_log(jid, msg):
    with JOBS_LOCK:
        JOBS[jid].setdefault("steps", []).append(msg)


def job_panel_set(jid, seq, **kw):
    with JOBS_LOCK:
        for t in JOBS[jid]["panels"]:
            if t["seq"] == seq:
                t.update(kw)


def jobs_snapshot():
    with JOBS_LOCK:
        return copy.deepcopy(list(JOBS.values()))


def run_produce_job(jid, md_text, out_name, model_cfg=None, auto_free=True):
    try:
        if auto_free:
            job_log(jid, "清理 ComfyUI 显存…")
            comfy_free(unload_models=True, free_memory=True)
        ok, owned, err = comfy_ensure_running(log=JOBS[jid]["steps"])
        if not ok:
            raise RuntimeError(err or "ComfyUI 启动失败")
        if not owned:
            job_log(jid, "检测到已在运行的 ComfyUI（你手动开的），直接使用，不会动它")
        parsed, warnings = parse_md_panels(md_text)
        panels = parsed["panels"]
        if not panels:
            raise ValueError("md 解析成功但没有可用面板（prompt 为空？）")
        chapter = int(parsed["chapter"]) if str(parsed.get("chapter") or "").isdigit() else JOBS[jid]["chapter"]
        folder = out_name or ("第%d章" % chapter)
        cfg = sanitize_model_cfg(model_cfg)
        job_set(jid, chapter=chapter, name=folder, warnings=warnings, model_cfg=cfg,
                panels=[{"seq": p["seq"], "id": p["id"], "seed": p["seed"],
                         "prompt": p["prompt"][:110], "caption": p["caption"],
                         "status": "queued", "prompt_id": None, "file": None,
                         "subfolder": "", "url": None, "copied": None, "error": None}
                        for p in panels])
        dest_dir = OUT_IMG / folder
        manifest = []
        for p in panels:
            try:
                pid = comfy_submit(build_prompt(p, chapter, cfg))
                job_panel_set(jid, p["seq"], prompt_id=pid)
                manifest.append({"seq": p["seq"], "id": p["id"], "seed": p["seed"],
                                 "prompt": p["prompt"], "caption": p["caption"],
                                 "prompt_id": pid})
            except Exception as e:
                job_panel_set(jid, p["seq"], status="failed", error=str(e)[:300])
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "manifest.json").write_text(
            json.dumps({"chapter": chapter, "model_cfg": cfg, "panels": manifest},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
        job_set(jid, manifest=str(dest_dir / "manifest.json"), dest=str(dest_dir))
    except Exception as e:
        job_set(jid, state="error", error="%s: %s" % (type(e).__name__, e))
        maybe_stop_owned_comfy(log=JOBS[jid]["steps"])


def poller():
    while True:
        try:
            time.sleep(2)
            q = comfy_queue()
            running_ids = {x[1] for x in q.get("queue_running", [])}
            pending_ids = {x[1] for x in q.get("queue_pending", [])}
            for jid, job in list(JOBS.items()):
                if job["state"] != "running":
                    continue
                if not job["panels"]:
                    continue
                unfinished = False
                for t in job["panels"]:
                    if t["status"] in ("done", "failed"):
                        continue
                    pid = t.get("prompt_id")
                    if not pid:
                        unfinished = True
                        continue
                    h = comfy_history(pid)
                    if h is not None and (h.get("outputs") or h.get("status")):
                        st = h.get("status", {}) or {}
                        st_str = st.get("status_str", "")
                        if h.get("outputs"):
                            imgs = []
                            for _nid, out in h["outputs"].items():
                                for im in out.get("images", []):
                                    imgs.append(im)
                            if imgs:
                                im = imgs[0]
                                copied = None
                                try:
                                    copied = copy_result(im.get("filename"), im.get("subfolder"),
                                                         im.get("type", "output"),
                                                         OUT_IMG / (job.get("name") or ""),
                                                         "%02d_id%s.png" % (t["seq"], t["id"]))
                                except Exception as e:
                                    t["error"] = "复制失败: %s" % e
                                job_panel_set(jid, t["seq"], status="done",
                                              file=im.get("filename"),
                                              subfolder=im.get("subfolder", ""),
                                              url="/img?f=%s&sf=%s" % (
                                                  urllib.parse.quote(im.get("filename", "")),
                                                  urllib.parse.quote(im.get("subfolder", ""))),
                                              copied=str(copied) if copied else None)
                            else:
                                job_panel_set(jid, t["seq"], status="done",
                                              error=t["error"] or "已完成但无图像输出")
                            continue
                        if st_str == "error" or st.get("completed") is False:
                            reason = "ComfyUI 执行失败"
                            for m in (st.get("messages") or []):
                                if m and m[0] == "execution_interrupted":
                                    reason = "已被中断（ComfyUI 手动停止或队列被清空）"
                                elif m and m[0] == "execution_error":
                                    reason = "执行错误：" + json.dumps(
                                        m[1].get("exception_message", ""), ensure_ascii=False)[:200]
                            job_panel_set(jid, t["seq"], status="failed", error=reason)
                            continue
                    if pid in running_ids:
                        job_panel_set(jid, t["seq"], status="running", misses=0)
                    elif pid in pending_ids:
                        job_panel_set(jid, t["seq"], status="queued", misses=0)
                    else:
                        misses = (t.get("misses") or 0) + 1
                        job_panel_set(jid, t["seq"], misses=misses)
                        if misses > 30:
                            job_panel_set(jid, t["seq"], status="failed",
                                          error="任务丢失（ComfyUI 重启过？）")
                            continue
                    unfinished = True
                if not unfinished:
                    job_set(jid, state="done")
                    maybe_stop_owned_comfy(log=job["steps"])
        except Exception:
            pass


# ---------- HTTP 服务 ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, data, ctype="application/json"):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        elif isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n).decode("utf-8") if n else ""

    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            path = u.path
            if path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html")
            elif path.startswith("/taskbook"):
                sub = path[len("/taskbook"):].lstrip("/") or "index.html"
                f = (TASKBOOK_DIR / sub).resolve()
                if not str(f).startswith(str(TASKBOOK_DIR.resolve())) or not f.is_file():
                    return self._send(404, {"error": "not found"})
                ctype = "text/html" if f.suffix == ".html" else "application/octet-stream"
                self._send(200, f.read_bytes(), ctype)
            elif path == "/api/status":
                self._send(200, {"comfy": check_comfy(),
                                 "comfy_url": COMFY,
                                 "vram": comfy_vram(),
                                 "engine": comfy_engine_info()})
            elif path == "/api/model-options":
                self._send(200, {"options": model_options(),
                                 "defaults": default_model_cfg()})
            elif path == "/api/jobs":
                self._send(200, {"jobs": jobs_snapshot()})
            elif path == "/img":
                fn, sf = q.get("f", [""])[0], q.get("sf", [""])[0]
                try:
                    data, ctype = fetch_image(fn, sf)
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception as e:
                    self._send(502, {"error": str(e)})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def do_POST(self):
        try:
            u = urllib.parse.urlparse(self.path)
            body = json.loads(self._read_body() or "{}")
            if u.path == "/api/produce/start":
                text = (body.get("text") or "").strip()
                if not text:
                    return self._send(400, {"error": "md 内容为空"})
                out_name = (body.get("out_name") or "").strip()
                jid = new_job("produce", body.get("chapter", 1) or 1, out_name)
                threading.Thread(target=run_produce_job,
                                 args=(jid, text, out_name, body.get("model_cfg"),
                                       body.get("auto_free", True)),
                                 daemon=True).start()
                self._send(200, {"job": jid})
            elif u.path == "/api/free":
                comfy_free(unload_models=True, free_memory=True)
                time.sleep(1)
                self._send(200, {"ok": True, "steps": ["ComfyUI 已释放"],
                                 "vram": comfy_vram()})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})


def main():
    global COMFY, COMFY_PYTHON, COMFY_MAIN
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--comfy-url", default=COMFY,
                    help="ComfyUI 地址（默认 http://127.0.0.1:8188，可指向远程）")
    ap.add_argument("--comfy-python", default=str(COMFY_PYTHON),
                    help="自动拉起 ComfyUI 用的 python.exe（默认 ComfyUI-aki-v3 布局）")
    ap.add_argument("--comfy-main", default=str(COMFY_MAIN),
                    help="自动拉起 ComfyUI 用的 main.py")
    ap.add_argument("--keep-comfy", action="store_true",
                    help="生产结束后不关闭流水线拉起的 ComfyUI（默认自动关）")
    args = ap.parse_args()
    COMFY = args.comfy_url
    COMFY_PYTHON = Path(args.comfy_python)
    COMFY_MAIN = Path(args.comfy_main)
    KEEP_COMFY[0] = bool(args.keep_comfy)
    if not TASKBOOK_DIR.exists():
        print("警告：static/taskbook/ 不存在，任务书标签页将为空")
    threading.Thread(target=poller, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("anima_pipeline 已启动: http://127.0.0.1:%d" % args.port)
    print("结果目录: %s" % OUT_IMG)
    print("ComfyUI: %s（%s）" % (COMFY,
          "常驻不关" if KEEP_COMFY[0] else "未运行时自动拉起，生产结束自动关；你手动开的绝不碰"))
    print("停止: Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopped, note = comfy_stop_owned()
        print(note)
        print("已退出")


if __name__ == "__main__":
    main()
