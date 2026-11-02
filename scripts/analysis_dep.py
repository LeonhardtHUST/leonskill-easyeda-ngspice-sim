#!/usr/bin/env python3
"""离线入口的依赖层：定位并调用 `leonskill-easyeda-project-analysis`。

本 skill **不再自己解析 .epro2**（ZIP 容器 / .epru 记录流 / ticket 归约都在依赖里）。
这里只做三件事：

  1. 找到依赖 skill 的 scripts/analyze.py
  2. 用子进程跑它，拿它的公开产物（schematic_*.json / pcb_geometry.json）
  3. 找不到或跑失败时给出**可操作**的错误，不静默降级

定位顺序（先命中先用）：
  $EASYEDA_PROJECT_ANALYSIS_DIR/scripts/analyze.py
  ~/.workbuddy/skills/leonskill-easyeda-project-analysis/scripts/analyze.py
  <本仓库父目录>/leonskill-easyeda-project-analysis/scripts/analyze.py
  ~/leonskill-easyeda-project-analysis/scripts/analyze.py

用法:
  python analysis_dep.py --locate
  python analysis_dep.py <工程.epro2> <产物目录> [页名]
"""
import glob
import json
import os
import subprocess
import sys

SKILL_NAME = "leonskill-easyeda-project-analysis"
ENV_VAR = "EASYEDA_PROJECT_ANALYSIS_DIR"


class DependencyError(RuntimeError):
    """依赖缺失或调用失败。code 用作稳定判据（DEPENDENCY_MISSING / ANALYSIS_FAILED）。"""

    def __init__(self, code, message, detail=""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail


def _candidates():
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)                       # .../easyeda-ngspice-sim
    parent = os.path.dirname(repo_root)                     # .../GitHub
    seen, out = set(), []
    raw = [
        os.environ.get(ENV_VAR),
        os.path.join(os.path.expanduser("~"), ".workbuddy", "skills", SKILL_NAME),
        os.path.join(parent, SKILL_NAME),
        os.path.join(os.path.expanduser("~"), SKILL_NAME),
    ]
    for path in raw:
        if not path:
            continue
        norm = os.path.normpath(path)
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def locate(required=True):
    """返回依赖 skill 根目录；找不到时 required=True 抛 DEPENDENCY_MISSING。"""
    tried = []
    for root in _candidates():
        analyze = os.path.join(root, "scripts", "analyze.py")
        tried.append(analyze)
        if os.path.isfile(analyze):
            return root
    if not required:
        return None
    msg = (f"离线入口需要 {SKILL_NAME}（负责解析 .epro2），但没有找到它的 "
           f"scripts/analyze.py。\n"
           f"  安装: 把该 skill 放到 ~/.workbuddy/skills/{SKILL_NAME}/，"
           f"或设置环境变量 {ENV_VAR} 指向它的仓库根目录。\n"
           f"  已尝试:\n    " + "\n    ".join(tried))
    raise DependencyError("DEPENDENCY_MISSING", msg)


def analyze_path(root=None):
    root = root or locate()
    return os.path.join(root, "scripts", "analyze.py")


def run_analyze(epro2_path, out_dir, page=None, root=None, timeout=1800):
    """子进程跑依赖的 analyze.py，返回 (out_dir, 标准输出)。

    依赖自己会保证产物目录结构；这里只负责调用与错误翻译。
    """
    analyze = analyze_path(root)
    if not os.path.isfile(epro2_path):
        raise DependencyError("INPUT_NOT_FOUND", f"工程文件不存在: {epro2_path}")
    os.makedirs(out_dir, exist_ok=True)
    cmd = [sys.executable, analyze, epro2_path, "--out", out_dir]
    if page:
        cmd += ["--page", page]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise DependencyError(
            "ANALYSIS_FAILED",
            f"解析超时（{timeout}s）: {os.path.basename(epro2_path)}",
            detail=" ".join(cmd))
    if proc.returncode != 0:
        detail = (proc.stdout or "") + (proc.stderr or "")
        raise DependencyError(
            "ANALYSIS_FAILED",
            f"依赖解析失败（exit {proc.returncode}）: {os.path.basename(epro2_path)}",
            detail=detail.strip()[-2000:])
    return out_dir, (proc.stdout or "") + (proc.stderr or "")


def list_schematics(out_dir):
    """产物目录里的 schematic_*.json，按文件名排序。"""
    return sorted(glob.glob(os.path.join(out_dir, "schematic_*.json")))


def pick_schematic(out_dir, page=None):
    """按页名/uuid 选一份 schematic；未指定时取第一份。

    依赖对同名页会加 _2/_3 后缀，所以不能只靠文件名猜页，必须读 page 字段。
    """
    files = list_schematics(out_dir)
    if not files:
        raise DependencyError(
            "ANALYSIS_FAILED",
            f"依赖没有产出 schematic_*.json（目录: {out_dir}）",
            detail="通常是工程里没有原理图页，或解析中途失败；看上面的 ANALYZE 输出。")
    pages = []
    for path in files:
        with open(path, encoding="utf-8") as f:
            ir = json.load(f)
        pages.append((path, ir.get("page") or {}))
    if page:
        for path, meta in pages:
            if page in (meta.get("uuid"), meta.get("name")):
                return path
        names = ", ".join(f"{m.get('name') or '?'}" for _p, m in pages)
        raise DependencyError(
            "PAGE_NOT_FOUND",
            f"工程里没有页 {page!r}；可用页: {names}")
    return pages[0][0]


def pcb_geometry_path(out_dir):
    path = os.path.join(out_dir, "pcb_geometry.json")
    if not os.path.isfile(path):
        raise DependencyError(
            "ANALYSIS_FAILED",
            f"依赖没有产出 pcb_geometry.json（目录: {out_dir}）",
            detail="工程里可能没有 PCB 文档（只有原理图的项目无法做几何提参）。")
    return path


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 2
    if argv[0] == "--locate":
        root = locate(required=False)
        if root:
            print(root)
            return 0
        print(f"{SKILL_NAME} 未找到；已尝试:")
        for path in _candidates():
            print("  " + path)
        return 1
    ir_out = None
    if "--ir" in argv:
        i = argv.index("--ir")
        if i + 1 >= len(argv):
            print("--ir 需要一个输出路径", file=sys.stderr)
            return 2
        ir_out = argv[i + 1]
        del argv[i:i + 2]
    if len(argv) < 2:
        print("usage: analysis_dep.py <project.epro2> <out_dir> [page] [--ir ir.json]")
        return 2
    epro2, out = argv[0], argv[1]
    page = argv[2] if len(argv) > 2 else None
    try:
        _out, log = run_analyze(epro2, out, page=page)
    except DependencyError as e:
        print(str(e), file=sys.stderr)
        if e.detail:
            print(e.detail, file=sys.stderr)
        return 2
    if log.strip():
        print(log.strip())
    schematic = pick_schematic(out, page)
    print("schematic:", schematic)
    if os.path.isfile(os.path.join(out, "pcb_geometry.json")):
        print("pcb_geometry:", pcb_geometry_path(out))
    if ir_out:
        import ir_from_analysis as conv
        ir = conv.convert(conv.load_schematic(schematic),
                          pages=conv.pages_index(out))
        from ir_common import save_ir, validate
        save_ir(ir, ir_out)
        print(f"IR written: {ir_out}")
        print(f"  project: {ir.get('project')}  page: "
              f"{(ir.get('page') or {}).get('name')} "
              f"({(ir.get('page') or {}).get('docType')})")
        print(f"  components: {len(ir['components'])}  wires: {len(ir['wires'])}")
        for w in validate(ir):
            print("  WARN:", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
