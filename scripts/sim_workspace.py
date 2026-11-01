#!/usr/bin/env python3
"""v2.3：仿真工作区（脱离 GUI 状态的可复现产物目录）。

目录约定（用户定稿的 v2.3 canonical layout）:

    sim-workspace/
    ├── source/      schematic_ir.json / pcb_geometry.json / stackup.json
    ├── models/      拷贝进来的 .lib/.ibs/.sNp（可选，便于 git 化）
    ├── configs/     实际生效的 sim_config / override / ibis_map
    ├── generated/   deck.cir（以及它的 .include 依赖）
    ├── runs/        case-0001/ …（每个 case 一个目录）
    ├── plots/       *.png / *.csv
    └── simulation_manifest.json

设计原则：**EasyEDA 只做设计数据源**。工作区一旦生成，rerun / compare / sweep /
montecarlo 都可以在不打开 EasyEDA、不切仿真页的情况下完成。
"""
import json
import os
import shutil

SUBDIRS = ("source", "models", "configs", "generated", "runs", "plots")


class Workspace:
    def __init__(self, root, create=True):
        self.root = os.path.abspath(root)
        if create:
            for d in SUBDIRS:
                os.makedirs(os.path.join(self.root, d), exist_ok=True)

    # ---- 路径 ----
    @property
    def source(self):
        return os.path.join(self.root, "source")

    @property
    def models(self):
        return os.path.join(self.root, "models")

    @property
    def configs(self):
        return os.path.join(self.root, "configs")

    @property
    def generated(self):
        return os.path.join(self.root, "generated")

    @property
    def runs(self):
        return os.path.join(self.root, "runs")

    @property
    def plots(self):
        return os.path.join(self.root, "plots")

    @property
    def manifest_path(self):
        return os.path.join(self.root, "simulation_manifest.json")

    def rel(self, path):
        try:
            return os.path.relpath(path, self.root).replace("\\", "/")
        except ValueError:
            return path

    # ---- 写入 ----
    def write_json(self, sub, name, obj):
        p = os.path.join(self.root, sub, name)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        return p

    def copy_in(self, src, sub="models"):
        if not src or not os.path.exists(src):
            return None
        dst = os.path.join(self.root, sub, os.path.basename(src))
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copyfile(src, dst)
        return dst

    def case_dir(self, index, slug=""):
        name = f"case-{index:04d}" + (f"_{slug}" if slug else "")
        d = os.path.join(self.runs, name)
        os.makedirs(d, exist_ok=True)
        return d

    def save_manifest(self, manifest):
        manifest = dict(manifest or {})
        manifest["workspace"] = {
            "root": self.root,
            "layout": list(SUBDIRS),
        }
        with open(self.manifest_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
        return self.manifest_path

    def summary(self):
        counts = {}
        for d in SUBDIRS:
            p = os.path.join(self.root, d)
            counts[d] = len(os.listdir(p)) if os.path.isdir(p) else 0
        return counts


def ensure(root):
    return Workspace(root, create=True)
