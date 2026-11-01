#!/usr/bin/env python3
"""v2.5 P1-C：仿真缓存与增量重跑（按内容哈希）。

cache key = sha256(源工程/IR + 模型文件 + 配置 + 求解器版本 + deck 摘要)

用途：Agent 大批量 sweep / Monte Carlo 时，重复 case 直接复用，不重复跑 ngspice。
**只在输入完全同构时命中**；任何一处变了就直接 miss（宁可重算，不要拿错结果）。
"""
import hashlib
import json
import os
import shutil

FIDELITY = "CONTENT_HASH_CACHE"


def _file_hash(path):
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
        "models": {os.path.basename(p): _file_hash(p) for p in (model_files or [])},
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
        for n in ("metrics.json", "simulation_manifest.json", "result.json"):
            p = os.path.join(d, n)
            if os.path.exists(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        out[n.split(".")[0]] = json.load(f)
                except Exception:
                    pass
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
