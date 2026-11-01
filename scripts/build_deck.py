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

v2.2：新增 build() 可编程入口（sweep_runner 需要按 case 反复生成 deck），
并且支持把 raw 写到子目录（`raw_dir`），这样参数扫描时每个 case 的
raw 不会互相覆盖。
"""
import os
import re
import sys

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
write {raw}tran.raw all
.endc
""",
    "ac": """
.control
ac {args}
write {raw}ac.raw all
.endc
""",
    "dc": """
.control
dc {args}
write {raw}dc.raw all
.endc
""",
}

DEFAULT_ARGS = {"tran": "5u 25m", "ac": "dec 100 10 100meg"}


def clean(raw_text):
    lines = []
    for ln in raw_text.split("\n"):
        s = ln.strip()
        if s.startswith(".tran") or s.startswith(".ac ") or s.startswith(".dc "):
            continue          # 分析命令改由 .control 控制
        if s.startswith("*BeginTrace") or s.startswith("*EndTrace"):
            continue          # LCEDA 偶发的超长注释行
        lines.append(ln.rstrip().replace("Ω", ""))
    return "\n".join(lines)


def build(raw_text, mode, arg=None, raw_dir=None):
    """返回完整 deck 文本。raw_dir 形如 "runs/case_1/"（末尾带斜杠）。"""
    deck = clean(raw_text)
    raw_dir = "" if not raw_dir else str(raw_dir).replace("\\", "/")
    if raw_dir and not raw_dir.endswith("/"):
        raw_dir += "/"

    if mode == "custom":
        if not arg or not os.path.isfile(arg):
            raise ValueError("custom 模式需要控制块正文文件作为第 4 参数")
        with open(arg, encoding="utf-8") as f:
            ctrl = "\n.control\n" + f.read().strip() + "\n.endc\n"
    elif mode in TEMPLATES:
        tpl = TEMPLATES[mode]
        if "{args}" in tpl:
            args = arg or DEFAULT_ARGS.get(mode)
            if not args:
                raise ValueError(f"{mode} 模式需要分析参数（如 V1 0 12 0.1）")
            ctrl = tpl.format(args=args, raw=raw_dir)
        else:
            ctrl = tpl
    else:
        raise ValueError(f"未知模式: {mode}（支持 op/tran/ac/dc/custom，"
                         f"参数扫描请用 config 里的 sweep，由 sweep_runner 驱动）")

    if ".end" not in deck:
        deck += "\n.end\n"
    # 只在“整行 .end”之前插控制块，避免误命中 .ENDS / .ENDL
    m = re.search(r"(?im)^[ \t]*\.end[ \t]*$", deck)
    if m:
        deck = deck[:m.start()] + ctrl.strip("\n") + "\n.end\n" + deck[m.end():]
    else:
        deck = deck + "\n" + ctrl.strip("\n") + "\n.end\n"
    return deck


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    raw_path, out_path, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    arg = sys.argv[4] if len(sys.argv) > 4 else None
    with open(raw_path, encoding="utf-8") as f:
        raw = f.read()
    try:
        deck = build(raw, mode, arg)
    except ValueError as e:
        print(str(e))
        sys.exit(2)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(deck)
    print(f"deck written: {out_path} (mode={mode})")


if __name__ == "__main__":
    main()
