#!/usr/bin/env python3
"""v2.5 P1-C：仿真缓存与增量重跑（按内容哈希）。

cache key = sha256(源工程/IR + 模型文件 + 配置 + 求解器版本 + deck 摘要)

用途：Agent 大批量 sweep / Monte Carlo 时，重复 case 直接复用，不重复跑 ngspice。
**只在输入完全同构时命中**；任何一处变了就直接 miss（宁可重算，不要拿错结果）。
"""
import hashlib
import json
import os
import re
import shutil

FIDELITY = "CONTENT_HASH_CACHE"


def file_hash(path):
    if not path or not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _text_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def build_key(deck_text, config, solver_version=None, model_files=None,
              source_hash=None):
    """返回 (key, parts)。parts 用于诊断"到底哪一项变了"。"""
    parts = {
        "deck": _text_hash(deck_text),
        "config": _text_hash(json.dumps(config or {}, sort_keys=True, default=str)),
        "solver": solver_version,
        "source": source_hash,
        "models": {os.path.basename(p): file_hash(p) for p in (model_files or [])},
    }
    blob = json.dumps(parts, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest(), parts


class SimCache:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def _dir(self, key):
        return os.path.join(self.root, key[:2], key)

    def hit(self, key):
        """命中判据 = 该 key 目录里存在 `cache_meta.json`（store 最后写它，当完成标记）。"""
        return os.path.exists(os.path.join(self._dir(key), "cache_meta.json"))

    def load(self, key):
        d = self._dir(key)
        out = {"key": key, "dir": d, "hit": True}
        for n in ("metrics.json", "simulation_manifest.json", "result.json",
                  "cache_meta.json"):
            p = os.path.join(d, n)
            if os.path.exists(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        out[n.split(".")[0]] = json.load(f)
                except Exception:
                    pass
        return out

    def materialize(self, key, dest):
        """把缓存的 raw/csv/metrics/stdout/stderr 复制回 run 目录。

        v2.5 只恢复了 metrics.json；"cache hit" 与一次正常 run 的产物不等价，
        下游（report.py / 人看波形）会拿到空目录。这里补齐。
        """
        d = self._dir(key)
        if not os.path.isdir(d):
            return []
        os.makedirs(dest, exist_ok=True)
        out = []
        for fn in sorted(os.listdir(d)):
            if fn == "cache_meta.json":
                continue
            src = os.path.join(d, fn)
            if not os.path.isfile(src):
                continue
            shutil.copyfile(src, os.path.join(dest, fn))
            out.append(fn)
        return out

    def store(self, key, files, metrics=None, meta=None):
        d = self._dir(key)
        os.makedirs(d, exist_ok=True)
        for src in files or []:
            if os.path.exists(src):
                shutil.copyfile(src, os.path.join(d, os.path.basename(src)))
        if metrics is not None:
            with open(os.path.join(d, "metrics.json"), "w", encoding="utf-8",
                      newline="\n") as f:
                json.dump(metrics, f, ensure_ascii=False, indent=1)
        # cache_meta.json 一律最后写：它同时是"这条记录已完整落盘"的标记
        with open(os.path.join(d, "cache_meta.json"), "w", encoding="utf-8",
                  newline="\n") as f:
            json.dump(dict(meta or {}, key=key,
                           files=sorted(os.path.basename(s) for s in files or [])),
                      f, ensure_ascii=False, indent=1)
        return d

    def stats(self):
        n = 0
        size = 0
        for base, _dirs, files in os.walk(self.root):
            for f in files:
                n += 1
                try:
                    size += os.path.getsize(os.path.join(base, f))
                except OSError:
                    pass
        return {"entries": n, "bytes": size, "root": self.root}


_INC_KEY_FIELDS = ("schematic", "pcb", "models", "config", "solver")


# --------------------------------------------------------------------------
# 依赖文件收集（v2.5.1）：cache key 必须覆盖 deck 真正引用的外部模型
# --------------------------------------------------------------------------
_INCLUDE_RE = re.compile(
    r'^\s*\.(?:include|inc|lib)\s+"?([^"\s]+)"?', re.IGNORECASE | re.MULTILINE)

_JSON_PATH_KEYS = ("network", "file", "touchstone", "snp", "path", "lib",
                   "subckt", "model", "ibis_file", "ibis")


def deck_includes(deck_text, base_dir):
    """从 deck 文本里抽 `.include/.inc/.lib` 引用的文件（存在才算）。"""
    out = []
    for m in _INCLUDE_RE.finditer(deck_text or ""):
        p = m.group(1)
        cand = p if os.path.isabs(p) else os.path.join(base_dir or ".", p)
        if os.path.exists(cand):
            out.append(os.path.abspath(cand))
    return out


def _json_paths(obj, base):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and str(k).lower() in _JSON_PATH_KEYS:
                cand = v if os.path.isabs(v) else os.path.join(base, v)
                if os.path.exists(cand):
                    out.append(os.path.abspath(cand))
            else:
                out += _json_paths(v, base)
    elif isinstance(obj, list):
        for x in obj:
            out += _json_paths(x, base)
    return out


def collect_deps(deck_text, cfg=None, base_dir=None):
    """收集本次仿真实际依赖的外部文件（去重、只保留存在的）。

    覆盖：deck 里的 `.include/.lib`、model_map、behavioral 库、IBIS 映射与其
    引用的 .ibs、override 里引用的 .sNp、stackup。
    目的：vendor.lib 内容改了但 deck 没变时，cache 必须 miss。
    """
    cfg = cfg or {}
    deps = deck_includes(deck_text, base_dir)
    for key in ("models", "behavioral_models", "ibis", "override", "stackup"):
        p = cfg.get(key)
        if isinstance(p, str) and os.path.exists(p):
            deps.append(os.path.abspath(p))
            if p.lower().endswith(".json"):
                try:
                    with open(p, encoding="utf-8") as f:
                        deps += _json_paths(json.load(f), os.path.dirname(os.path.abspath(p)))
                except (OSError, ValueError):
                    pass
        elif isinstance(p, dict):
            deps += _json_paths(p, base_dir or ".")
    seen, out = set(), []
    for p in deps:
        k = os.path.abspath(p)
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def incremental_plan(prev_keys, cur_keys):
    """比较两轮 key 的构成，给出"哪些 study 可复用 / 哪些必须重跑"。

    prev/cur: {study_name: parts_dict}
    """
    reusable, affected = [], []
    for name, cur in (cur_keys or {}).items():
        prev = (prev_keys or {}).get(name)
        if prev and all(prev.get(k) == cur.get(k) for k in _INC_KEY_FIELDS):
            reusable.append(name)
        else:
            changed = [k for k in _INC_KEY_FIELDS
                       if not prev or prev.get(k) != cur.get(k)]
            affected.append({"study": name, "changed": changed})
    return {"reusable": reusable, "affected": affected,
            "note": "只有全部输入项一致才复用；任何一项变了都重跑"}
