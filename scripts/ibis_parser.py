#!/usr/bin/env python3
"""v2.3 P0-D(1)：IBIS 4.x/5.x 解析器（够用就好，不追求全标准）。

支持的 section:
  [IBIS Ver] [Component] [Manufacturer] [Package] [Pin]
  [Model]  C_comp / C_pkg / R_pkg / L_pkg / Vinh / Vinl / Vmeas / Model_type /
           Polarity / Enable / Vinl / Vinh / [Temperature Range]
  [Pullup] [Pulldown] [POWER Clamp] [GND Clamp]   （V-I 表）
  [Ramp]  dv/dt_r dv/dt_f
  [Rising Waveform] [Falling Waveform]            （R_fixture/V_fixture + 波形点）
  [Diff Pin]（忽略但记录）

解析策略：逐行扫描、用关键字切块、块内按"首列数值"识别数据行。
IBIS 的关键坑：数据行是 **列对齐** 的（不是逗号分隔），以及 `|` 后面是注释。
"""
import os
import re

_KEY = re.compile(r"^\s*\[\s*([A-Za-z0-9_ ]+?)\s*\]\s*(.*)$")
_NUM = re.compile(r"^[\s]*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


class IbisError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _strip_comment(line):
    i = line.find("|")
    return line[:i] if i >= 0 else line


def _num(tok):
    """IBIS 数值带工程后缀（1.2pF / 3.3V / 50ohm / 1nH），复用 parasitics 的解析。"""
    if tok is None:
        return None
    s = str(tok).strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        from parasitics import parse_eng
        return parse_eng(s, "ibis")
    except Exception:
        return None


def _row(line):
    """一行数据 → [float...]；非数值行返回 None。"""
    s = line.strip()
    if not s or not _NUM.match(s):
        return None
    vals = []
    for tok in s.split():
        v = _num(tok)
        if v is None:
            break
        vals.append(v)
    return vals or None


def parse_ibs(path):
    if not os.path.exists(path):
        raise IbisError("IBIS_FILE_MISSING", f"IBIS 文件不存在: {path}")
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if "[" not in text:
        raise IbisError("IBIS_PARSE_ERROR", f"{os.path.basename(path)} 不像 IBIS 文件（无 section 标记）")
    lines = text.splitlines()
    doc = {"file": os.path.basename(path), "path": os.path.abspath(path),
           "ibis_ver": None, "components": {}, "models": {}, "package": {},
           "warnings": [], "sections": [], "diff_pins": [],
           "unsupported": []}
    cur_comp = None
    cur_model = None
    cur_pin = None
    mode = None            # 当前数据块类型
    mode_owner = None
    waveform = None

    def ensure_model(name):
        m = doc["models"].setdefault(name, {
            "name": name, "model_type": None, "c_comp": {}, "c_pkg": None,
            "r_pkg": None, "l_pkg": None, "vinl": None, "vinh": None,
            "vmeas": None, "ramp": {}, "pullup": [], "pulldown": [],
            "power_clamp": [], "gnd_clamp": [], "rising": [], "falling": [],
            "temperature": None, "raw": {},
        })
        return m

    for raw_line in lines:
        line = _strip_comment(raw_line).rstrip()
        if not line.strip():
            continue
        m = _KEY.match(line)
        if m:
            key = re.sub(r"\s+", "", m.group(1)).lower()
            rest = m.group(2).strip()
            doc["sections"].append(key)
            mode, mode_owner = None, None
            if key == "ibisver":
                doc["ibis_ver"] = rest
            elif key.startswith("component"):
                cur_comp = rest
                doc["components"].setdefault(cur_comp, {"name": cur_comp, "pins": {},
                                                        "package": {}, "models_used": [],
                                                        "diff_pins": []})
            elif key == "manufacturer":
                doc["manufacturer"] = rest
            elif key == "package":
                tgt = doc["components"].get(cur_comp) if cur_comp else None
                if tgt is not None:
                    tgt["package"] = doc["package"]
                mode = "package"
                mode_owner = doc["package"]
            elif key == "diffpin":
                mode = "diffpin"
            elif key == "pin":
                # [Pin] 段头；引脚数据在后续行
                mode = "pin"
            elif key == "model":
                cur_model = rest
                ensure_model(cur_model)
                cur_comp_entry = doc["components"].get(cur_comp)
                if cur_comp_entry is not None and cur_model not in cur_comp_entry["models_used"]:
                    cur_comp_entry["models_used"].append(cur_model)
                mode = "model"
                mode_owner = doc["models"][cur_model]
            elif key in ("pullup", "pulldown", "powerclamp", "gndclamp"):
                mode = {"pullup": "pullup", "pulldown": "pulldown",
                        "powerclamp": "power_clamp", "gndclamp": "gnd_clamp"}[key]
                mode_owner = doc["models"].get(cur_model) if cur_model else None
            elif key == "ramp":
                mode = "ramp"
                mode_owner = doc["models"].get(cur_model) if cur_model else None
            elif key == "risingwaveform":
                waveform = {"typ": [], "min": [], "max": [],
                            "r_fixture": None, "v_fixture": None, "v_fixture_min": None,
                            "v_fixture_max": None, "c_fixture": None}
                mode = "rising"
                mode_owner = doc["models"].get(cur_model) if cur_model else None
            elif key == "fallingwaveform":
                waveform = {"typ": [], "min": [], "max": [],
                            "r_fixture": None, "v_fixture": None, "v_fixture_min": None,
                            "v_fixture_max": None, "c_fixture": None}
                mode = "falling"
                mode_owner = doc["models"].get(cur_model) if cur_model else None
            elif key in ("diffpin",):
                pass
            elif key in ("submodel", "aggregator", "seriespinmapping",
                         "seriesswitchgroups", "interpol", "addsubmodel"):
                doc["unsupported"].append(key)
                doc["warnings"].append(f"[{m.group(1)}] 暂不支持（IBIS_FEATURE_UNSUPPORTED）")
            elif key in ("temperaturerange", "voltagetrange"):
                mode = "kv"
            elif key in ("end",):
                cur_model, mode = None, None if rest.lower() == "" else mode
            continue

        # 非关键字行 → 按当前 mode 解析
        if mode == "pin" and cur_comp:
            parts = line.split()
            if len(parts) >= 2 and re.match(r"^\s*\S", line):
                # IBIS: pin_number  signal_name  model_name  [R_pin L_pin C_pin]
                pin_name, sig, mdl = parts[0], parts[1], parts[2] if len(parts) > 2 else None
                keyword = (mdl or "").upper() in ("POWER", "GND", "NC")
                doc["components"][cur_comp]["pins"][pin_name] = {
                    "signal_name": sig,
                    "model": None if keyword else mdl,
                    "keyword": mdl.upper() if keyword else None,
                    "r_pin": _num(parts[3]) if len(parts) > 3 else None,
                    "l_pin": _num(parts[4]) if len(parts) > 4 else None,
                    "c_pin": _num(parts[5]) if len(parts) > 5 else None,
                }
        elif mode == "diffpin" and cur_comp:
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                rec = {"positive": parts[0], "negative": parts[1],
                       "vdiff": _num(parts[2]) if len(parts) > 2 else None,
                       "tdelay_typ": _num(parts[3]) if len(parts) > 3 else None,
                       "tdelay_min": _num(parts[4]) if len(parts) > 4 else None,
                       "tdelay_max": _num(parts[5]) if len(parts) > 5 else None}
            except Exception:
                doc["warnings"].append(f"[Diff Pin] 行无法解析: {line.strip()}")
                continue
            doc["diff_pins"].append(dict(rec, component=cur_comp))
            doc["components"][cur_comp]["diff_pins"].append(rec)
        elif mode == "package" and mode_owner is not None:
            row = _row(line)
            if row:
                mode_owner.setdefault("rows", []).append(
                    {"r": row[0], "l": row[1] if len(row) > 1 else None,
                     "c": row[2] if len(row) > 2 else None})
        elif mode == "model" and mode_owner is not None:
            kv = line.split(None, 1)
            if not kv:
                continue
            k = kv[0].lower()
            v = kv[1].strip() if len(kv) > 1 else ""
            if k == "c_comp":
                vals = [_num(x) for x in v.split()]
                if len(vals) == 1:
                    mode_owner["c_comp"] = {"typ": vals[0], "min": vals[0], "max": vals[0]}
                elif len(vals) >= 3:
                    mode_owner["c_comp"] = {"typ": vals[0], "min": vals[1], "max": vals[2]}
                else:
                    mode_owner["c_comp"] = {"typ": vals[0] if vals else None}
            elif k == "c_pkg":
                mode_owner["c_pkg"] = _num(v)
            elif k == "r_pkg":
                mode_owner["r_pkg"] = _num(v)
            elif k == "l_pkg":
                mode_owner["l_pkg"] = _num(v)
            elif k == "vinl":
                mode_owner["vinl"] = _num(v)
            elif k == "vinh":
                mode_owner["vinh"] = _num(v)
            elif k == "vmeas":
                mode_owner["vmeas"] = _num(v)
            elif k == "model_type":
                mode_owner["model_type"] = v.strip()
            elif k in ("polarity", "enable", "series"):
                mode_owner["raw"][k] = v.strip()
        elif mode in ("pullup", "pulldown", "power_clamp", "gnd_clamp") and mode_owner:
            row = _row(line)
            if row and len(row) >= 2:
                mode_owner[mode].append({"v": row[0], "i_typ": row[1],
                                         "i_min": row[2] if len(row) > 2 else None,
                                         "i_max": row[3] if len(row) > 3 else None})
        elif mode == "ramp" and mode_owner is not None:
            parts = line.split()
            if not parts:
                continue
            k = parts[0].lower()
            if k in ("dv/dt_r", "dv/dt_f"):
                # IBIS 的写法是 "1.2V/0.35n"：dV 与 dt 用斜杠分隔，先拆再解析
                toks = []
                for x in parts[1:]:
                    toks.extend([t for t in x.split("/") if t])
                vals = [_num(x) for x in toks]
                vals = [v for v in vals if v is not None]
                if len(vals) >= 2:
                    mode_owner["ramp"][k] = {"dv": vals[0], "dt": vals[1],
                                             "dv_min": vals[2] if len(vals) > 2 else None,
                                             "dt_min": vals[3] if len(vals) > 3 else None,
                                             "dv_max": vals[4] if len(vals) > 4 else None,
                                             "dt_max": vals[5] if len(vals) > 5 else None}
        elif mode in ("rising", "falling") and waveform is not None:
            low = line.lower()
            if "r_fixture" in low:
                waveform["r_fixture"] = _num(line.split("=")[-1])
            elif "c_fixture" in low:
                waveform["c_fixture"] = _num(line.split("=")[-1])
            elif "v_fixture_min" in low:
                waveform["v_fixture_min"] = _num(line.split("=")[-1])
            elif "v_fixture_max" in low:
                waveform["v_fixture_max"] = _num(line.split("=")[-1])
            elif "v_fixture" in low:
                waveform["v_fixture"] = _num(line.split("=")[-1])
            else:
                row = _row(line)
                if row and len(row) >= 2:
                    waveform["typ"].append((row[0], row[1]))
                    if len(row) >= 4:
                        waveform["min"].append((row[2], row[3]))
                    if len(row) >= 6:
                        waveform["max"].append((row[4], row[5]))
            if doc["models"].get(cur_model) is not None:
                doc["models"][cur_model][mode] = [waveform]

    if not doc["components"]:
        raise IbisError("IBIS_PARSE_ERROR", f"{os.path.basename(path)} 里没有 [Component]")
    if not doc["models"]:
        raise IbisError("IBIS_PARSE_ERROR", f"{os.path.basename(path)} 里没有 [Model]")
    return doc


def summary(doc):
    lines = [f"IBIS {doc.get('ibis_ver') or '?'}  {doc['file']}",
             f"components: {', '.join(doc['components'])}",
             f"models: {len(doc['models'])}"]
    for name, m in list(doc["models"].items())[:6]:
        lines.append(f"  [{name}] type={m.get('model_type')} "
                     f"C_comp={m.get('c_comp')} ramp={list((m.get('ramp') or {}))}")
    return lines


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    for ln in summary(parse_ibs(sys.argv[1])):
        print(ln)
