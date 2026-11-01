#!/usr/bin/env python3
"""解析 ngspice rawfile（write 命令产出的二进制/ascii raw 格式）为 JSON 或 CSV。

用法:
  parse_raw.py <file.raw>                  # 输出 JSON 到 stdout
  parse_raw.py <file.raw> --csv <out.csv>  # 输出 CSV
  parse_raw.py <file.raw> --list           # 只列出各 plot 的变量名

支持多 plot 顺序解析；complex 类型（AC 分析）的变量值输出为 [re, im]。
"""
import sys
import json


def _is_int(tok):
    try:
        int(tok)
        return "." not in tok and "e" not in tok.lower().replace("e+", "").replace("e-", "")
    except ValueError:
        return False


def parse_ascii(data):
    """解析 ASCII rawfile（ngspice write 默认格式）。

    Values 块结构：每个数据点占多行——首行为 "点序号 第一个数值"，续行各含一个数值。
    """
    text = data.decode("ascii", "replace")
    plots = []
    idx = [i for i in range(len(text)) if text.startswith("Plotname:", i)]
    if not idx:
        idx = [0]
    idx.append(len(text))
    for b in range(len(idx) - 1):
        blk = text[idx[b]:idx[b + 1]]
        if "Values:" not in blk:
            continue
        plot = {"title": "", "plotname": "", "flags": "real", "n_vars": 0,
                "n_points": 0, "vars": [], "points": []}
        section = None
        need = 0
        rows = []
        cur = []

        def consume(tokens):
            for t in tokens:
                try:
                    cur.append(float(t))
                except ValueError:
                    pass

        for ln in blk.splitlines():
            s = ln.strip()
            if not s:
                continue
            if s.startswith("Title:"):
                plot["title"] = s[6:].strip(); section = None
            elif s.startswith("Plotname:"):
                plot["plotname"] = s[9:].strip(); section = None
            elif s.startswith("Flags:"):
                plot["flags"] = s[6:].strip(); section = None
            elif s.startswith("No. Variables:"):
                plot["n_vars"] = int(s.split(":", 1)[1]); section = None
            elif s.startswith("No. Points:"):
                plot["n_points"] = int(s.split(":", 1)[1]); section = None
            elif s == "Variables:":
                section = "vars"
            elif s == "Values:":
                section = "values"
                per = 2 if "complex" in plot["flags"] else 1
                need = plot["n_vars"] * per
            elif section == "vars":
                parts = s.split()
                if len(parts) >= 2 and _is_int(parts[0]):
                    plot["vars"].append(parts[1])
            elif section == "values":
                toks = s.replace(",", " ").split()  # complex 值写作 "re,im"，逗号拆开
                if toks and _is_int(toks[0]):
                    if cur and len(cur) == need:
                        rows.append(cur)
                    cur = []
                    consume(toks[1:])
                else:
                    consume(toks)
        if cur and len(cur) == need:
            rows.append(cur)
        if not plot["vars"] or not rows:
            continue
        per = 2 if "complex" in plot["flags"] else 1
        if per == 2:
            pts = []
            for r in rows:
                pts.append([[r[j], r[j + 1]] for j in range(0, len(r), 2)])
            plot["points"] = pts
        else:
            plot["points"] = rows
        plot["n_points"] = len(plot["points"])
        plots.append(plot)
    return plots


def parse_all(path):
    with open(path, "rb") as f:
        data = f.read()
    if b"Binary:" not in data:
        return parse_ascii(data)
    plots = []
    pos = 0
    while pos < len(data):
        b_off = data.find(b"Binary:", pos)
        if b_off < 0:
            break
        header = data[pos:b_off].decode("ascii", "replace")
        b_start = b_off + len(b"Binary:\n")
        plot = {"title": "", "plotname": "", "flags": "real", "n_vars": 0,
                "n_points": 0, "vars": []}
        in_vars = False
        for ln in header.splitlines():
            ln = ln.strip()
            if ln.startswith("Title:"):
                plot["title"] = ln[6:].strip(); in_vars = False
            elif ln.startswith("Plotname:"):
                plot["plotname"] = ln[9:].strip(); in_vars = False
            elif ln.startswith("Flags:"):
                plot["flags"] = ln[6:].strip(); in_vars = False
            elif ln.startswith("No. Variables:"):
                plot["n_vars"] = int(ln.split(":", 1)[1]); in_vars = False
            elif ln.startswith("No. Points:"):
                plot["n_points"] = int(ln.split(":", 1)[1]); in_vars = False
            elif ln == "Variables:":
                in_vars = True
            elif in_vars and ln:
                parts = ln.split()
                if len(parts) >= 2:
                    plot["vars"].append(parts[1])
        if plot["n_vars"] == 0 or plot["n_points"] == 0 or not plot["vars"]:
            break
        per = 2 if "complex" in plot["flags"] else 1
        size = plot["n_points"] * plot["n_vars"] * per * 8
        blob = data[b_start:b_start + size]
        vals = []
        for i in range(0, len(blob) - 7, 8):
            vals.append(struct_unpack(blob[i:i + 8]))
        n_v = plot["n_vars"]
        pts = []
        k = 0
        for _ in range(plot["n_points"]):
            row = []
            for _ in range(n_v):
                if per == 2:
                    row.append([vals[k], vals[k + 1]]); k += 2
                else:
                    row.append(vals[k]); k += 1
            pts.append(row)
        plot["points"] = pts
        plots.append(plot)
        pos = b_start + size
        while pos < len(data) and data[pos:pos + 1] in (b"\n", b"\r"):
            pos += 1
        if data[pos:pos + 6] != b"Title:":
            break
    return plots


def struct_unpack(b8):
    import struct
    return struct.unpack("<d", b8)[0]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    path = sys.argv[1]
    mode = "json"
    csv_out = None
    if "--csv" in sys.argv:
        mode = "csv"
        csv_out = sys.argv[sys.argv.index("--csv") + 1]
    elif "--list" in sys.argv:
        mode = "list"

    plots = parse_all(path)
    if not plots:
        print("no plots parsed", file=sys.stderr)
        sys.exit(5)

    if mode == "list":
        for p in plots:
            print(f"[{p['plotname']}] {p['n_points']} pts, vars: {p['vars']}")
        return

    if mode == "csv":
        with open(csv_out, "w", encoding="utf-8", newline="\n") as f:
            for p in plots:
                f.write("# " + p["plotname"] + "\n")
                f.write(",".join(p["vars"]) + "\n")
                for row in p["points"]:
                    cells = []
                    for v in row:
                        if isinstance(v, list):
                            cells.append(f"{v[0]:.6g},{v[1]:.6g}")
                        else:
                            cells.append(f"{v:.6g}")
                    f.write(",".join(cells) + "\n")
        print(f"csv written: {csv_out}")
    else:
        out = [{"plotname": p["plotname"], "vars": p["vars"],
                "n_points": p["n_points"], "points": p["points"]} for p in plots]
        json.dump(out, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
