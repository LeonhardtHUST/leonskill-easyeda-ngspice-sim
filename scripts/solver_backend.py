#!/usr/bin/env python3
"""v2.4 P1-C：求解器抽象（接口形状参考 Qucs-S 的"前端 + 可换后端"，代码自写）。

    probe()        → 可执行文件在不在、版本、能力
    prepare(deck)  → 落地 deck（后端可能要改方言）
    run(deck, dir) → 子进程 + 超时 + 日志
    parse(raw)     → 统一波形结构
    capabilities() → 支持哪些分析 / 有哪些已知缺口

默认且唯一的生产后端是 `NgspiceBackend`。`XyceBackend` 是 **EXPERIMENTAL**：
它缺少 XSPICE 的 `sp` 分析/RF 端口，所以 PDN/sparam 类分析不可用；
只在"大规模瞬态 / 海量 Monte Carlo"场景作为可选加速，不阻塞发布。
"""
import os
import shutil
import subprocess

DEFAULT_NGSPICE = os.path.expanduser(
    "~/.workbuddy/binaries/ngspice/ngspice-47/Spice64/bin/ngspice_con.exe")


class SolverError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class SolverBackend:
    name = "abstract"
    status = "EXPERIMENTAL"

    def probe(self):
        raise NotImplementedError

    def capabilities(self):
        raise NotImplementedError

    def prepare(self, deck_text):
        return deck_text

    def run(self, deck_path, workdir, timeout=600):
        raise NotImplementedError

    def parse(self, raw_path):
        import parse_raw
        return parse_raw.parse_all(raw_path)


class NgspiceBackend(SolverBackend):
    name = "ngspice"
    status = "QUALIFIED"          # 见 VALIDATION_STATUS.md 的定义

    def __init__(self, binary=None):
        self.binary = binary or os.environ.get("NGSPICE_BIN") or DEFAULT_NGSPICE

    def probe(self):
        ok = os.path.exists(self.binary)
        ver = None
        if ok:
            try:
                p = subprocess.run([self.binary, "-v"], capture_output=True, timeout=20)
                txt = (p.stdout or b"").decode("utf-8", "replace")
                import re
                m = re.search(r"ngspice-(\d+[\w.\-]*)", txt)
                ver = f"ngspice-{m.group(1)}" if m else None
            except Exception:
                pass
        return {"name": self.name, "available": ok, "version": ver,
                "binary": self.binary, "status": self.status}

    def capabilities(self):
        return {"analyses": ["op", "tran", "ac", "dc", "sp", "sweep", "noise"],
                "rf_ports": True, "sparam_elem": False,
                "xspice": True,
                "notes": ["`S` 元件不可用（model type mismatch）→ 用 vectorfit/lumped adapter",
                          "`.sp` 只支持 dec/oct 扫描"]}

    def run(self, deck_path, workdir, timeout=600):
        try:
            p = subprocess.run([self.binary, "-b", os.path.basename(deck_path)],
                               cwd=workdir, capture_output=True, timeout=timeout)
        except FileNotFoundError as e:
            raise SolverError("SOLVER_NOT_FOUND", f"{self.binary}: {e}") from e
        except subprocess.TimeoutExpired:
            return -9, "", f"ngspice 超时（>{timeout}s）"
        return (p.returncode,
                (p.stdout or b"").decode("utf-8", "replace"),
                (p.stderr or b"").decode("utf-8", "replace"))


class XyceBackend(SolverBackend):
    name = "xyce"
    status = "EXPERIMENTAL"

    def __init__(self, binary=None):
        self.binary = binary or os.environ.get("XYCE_BIN") or shutil.which("Xyce")

    def probe(self):
        ok = bool(self.binary) and os.path.exists(self.binary)
        return {"name": self.name, "available": ok, "binary": self.binary,
                "status": self.status}

    def capabilities(self):
        return {"analyses": ["op", "tran", "ac", "dc", "step"],
                "rf_ports": False, "sparam_elem": False, "xspice": False,
                "notes": ["无 sp 分析/RF 端口 → PDN/sparam 不可用",
                          "方言与 ngspice 有差异，复杂 .control 块不保证兼容",
                          "仅实验：大规模瞬态 / 海量 Monte Carlo 的加速选项"]}

    def prepare(self, deck_text):
        # 已知差异：Xyce 不接受 `.endc` 后的裸结束标记写法差异；这里只做保守清理
        return deck_text

    def run(self, deck_path, workdir, timeout=600):
        if not self.binary:
            raise SolverError("SOLVER_NOT_FOUND",
                              "Xyce 未安装（设 XYCE_BIN 或加入 PATH）；这是实验后端")
        p = subprocess.run([self.binary, "-r", "xyce.raw", os.path.basename(deck_path)],
                           cwd=workdir, capture_output=True, timeout=timeout)
        return (p.returncode,
                (p.stdout or b"").decode("utf-8", "replace"),
                (p.stderr or b"").decode("utf-8", "replace"))


BACKENDS = {"ngspice": NgspiceBackend, "xyce": XyceBackend}


def get(name=None, **kw):
    """取后端；默认 ngspice。未知名字直接报错（不默默回退）。"""
    key = (name or "ngspice").lower()
    if key not in BACKENDS:
        raise SolverError("SOLVER_UNKNOWN",
                          f"未知求解器 {name!r}（支持 {list(BACKENDS)}）")
    return BACKENDS[key](**kw)


def survey():
    """所有后端的能力快照（manifest / 文档用）。"""
    out = []
    for k, cls in BACKENDS.items():
        b = cls()
        try:
            p = b.probe()
        except Exception as e:
            p = {"name": k, "available": False, "error": str(e), "status": b.status}
        p["capabilities"] = b.capabilities()
        out.append(p)
    return out
