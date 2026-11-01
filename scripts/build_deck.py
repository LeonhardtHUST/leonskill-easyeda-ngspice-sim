#!/usr/bin/env python3
"""ngspice deck 生成器：清洗 LCEDA 网表 + 注入 .control 分析脚本。

用法:
  build_deck.py <raw_netlist.cir> <out_deck.cir> <mode> [mode_args 或 custom控制文件]

mode:
  op      静态工作点: op + print all
  tran    瞬态:       tran <args 默认 5u 25m>  + write tran.raw all
  ac      交流扫频:   ac <args 默认 dec 100 10 100meg> + write ac.raw all
  dc      直流扫描:   dc <args 必填, 如 V1 0 12 0.1> + write dc.raw all
  custom  自定义:     第 4 参数为控制块正文文件（不含 .control/.endc）

清洗规则（LCEDA 网表实测坑）:
  - 元件值中的全角/希腊字符 Ω 删除（"43kΩ" -> "43k"）
  - 移除原有 .tran 行（避免与 .control 内的分析重复执行）
"""
import sys
import os

TEMPLATES = {
    "op": """
.control
op
print all
.endc
""",
    "tran": """
.control
tran {args}
write tran.raw all
.endc
""",
    "ac": """
.control
ac {args}
write ac.raw all
.endc
""",
    "dc": """
.control
dc {args}
write dc.raw all
.endc
""",
}


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    raw_path, out_path, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    arg = sys.argv[4] if len(sys.argv) > 4 else None

    raw = open(raw_path, encoding="utf-8").read()
    lines = []
    for ln in raw.split("\n"):
        s = ln.strip()
        if s.startswith(".tran") or s.startswith(".ac ") or s.startswith(".dc "):
            continue  # 分析命令改由 .control 控制
        if s.startswith("*BeginTrace") or s.startswith("*EndTrace"):
            continue  # LCEDA 偶发的超长注释行
        lines.append(ln.rstrip().replace("Ω", ""))
    deck = "\n".join(lines)

    if mode == "custom":
        if not arg or not os.path.isfile(arg):
            print("custom 模式需要控制块正文文件作为第 4 参数")
            sys.exit(2)
        ctrl_body = open(arg, encoding="utf-8").read()
        ctrl = "\n.control\n" + ctrl_body.strip() + "\n.endc\n"
    elif mode in TEMPLATES:
        if "{args}" in TEMPLATES[mode]:
            default = {"tran": "5u 25m", "ac": "dec 100 10 100meg"}.get(mode)
            args = arg or default
            if not args:
                print(f"{mode} 模式需要分析参数（如 V1 0 12 0.1）")
                sys.exit(2)
            ctrl = TEMPLATES[mode].format(args=args)
        else:
            ctrl = TEMPLATES[mode]
    else:
        print(f"未知模式: {mode}")
        sys.exit(2)

    if ".end" not in deck:
        deck += "\n.end\n"
    deck = deck.replace(".end", ctrl + "\n.end", 1)
    open(out_path, "w", encoding="utf-8", newline="\n").write(deck)
    print(f"deck written: {out_path} (mode={mode})")


if __name__ == "__main__":
    main()
