#!/usr/bin/env python3
"""v2.5 P0-A：验证等级的可机器校验实现。

问题（v2.4 云端审核）：`VALIDATION_STATUS.md` 里 `CORRELATED` 被用在了"与原始 CAD 数据
一致"和"与 ngspice 复算一致"上，而文档自己定义 `CORRELATED` = 与**实测数据**一致。
这是元数据 bug：证据等级被高估。

修法不是改文案，而是：
  1. 把每个功能的等级与证据写成 `validation_registry.json`；
  2. 用这里的规则**校验**等级是否被证据支持（跑在 pytest 里）；
  3. `VALIDATION_STATUS.md` 由 `render_markdown()` 生成，**禁止手改表格**。

等级定义（唯一权威）：

  IMPLEMENTED   代码可运行。无测试要求。
  TESTED        ≥1 条 unit_test 证据（断言的是我们自己定义的行为）。
  CROSS_CHECKED ≥1 条独立信源证据：analytic（解析式）/ cross_tool（另一工具或原始 CAD 数据）/
                golden_corpus（闭式真值）。
  CORRELATED    ≥1 条 measurement 证据，且必须是 instrument 实测 + OUT_OF_SAMPLE。
                合成/拟合内数据一律不算。
  QUALIFIED     TESTED + CROSS_CHECKED + CORRELATED 同时满足，且 regression=true。
  EXPERIMENTAL  接口存在、结果不可用于决策。任何证据都不能把它"升格"。

`QUALIFIED` 因此几乎只可能出现在"有实测闭环"的功能上 —— 这正是它应有的门槛。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
REGISTRY = os.path.join(SKILL_DIR, "validation_registry.json")

LEVELS = ("IMPLEMENTED", "TESTED", "CROSS_CHECKED", "CORRELATED", "QUALIFIED",
          "EXPERIMENTAL")

# 证据类型 → 它能支撑的最高等级
EVIDENCE_POWER = {
    "unit_test": "TESTED",
    "analytic": "CROSS_CHECKED",
    "cross_tool": "CROSS_CHECKED",
    "golden_corpus": "CROSS_CHECKED",
    "measurement": "CORRELATED",       # 还需 instrument + OUT_OF_SAMPLE
    "field_solver": "CORRELATED",      # 全波提取也算"独立物理信源"
}

LEVEL_ORDER = {lvl: i for i, lvl in enumerate(LEVELS)}


class RegistryError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def load(path=None):
    with open(path or REGISTRY, encoding="utf-8") as f:
        return json.load(f)


def _measurement_ok(ev):
    """实测证据的硬门槛：仪器来源 + 样本外。"""
    if str(ev.get("source", "")).lower() not in ("instrument", "vna", "oscilloscope",
                                                 "lab"):
        return False, "measurement evidence must come from an instrument (source=instrument)"
    if str(ev.get("sample", "")).upper() != "OUT_OF_SAMPLE":
        return False, "measurement evidence must be OUT_OF_SAMPLE (not used for fitting)"
    if not ev.get("instrument"):
        return False, "measurement evidence must name the instrument"
    if not ev.get("file") and not ev.get("record"):
        return False, "measurement evidence must reference a data file or record id"
    return True, None


def check_entry(entry):
    """返回 (ok, problems[])。"""
    name = entry.get("feature") or "?"
    status = str(entry.get("status") or "").upper()
    problems = []
    if status not in LEVELS:
        return False, [f"{name}: 未知等级 {status!r}（允许 {LEVELS}）"]
    evs = entry.get("evidence") or []
    kinds = {str(e.get("type", "")).lower() for e in evs}
    for e in evs:
        if str(e.get("type", "")).lower() not in EVIDENCE_POWER:
            problems.append(f"{name}: 未知证据类型 {e.get('type')!r}")
    # 每个证据必须有可追溯的说明
    for e in evs:
        if not (e.get("what") or e.get("detail") or e.get("ref")):
            problems.append(f"{name}: 证据缺少 what/detail/ref，无法追溯")

    if status == "EXPERIMENTAL":
        return (not problems), problems

    def need(level):
        return LEVEL_ORDER[status] >= LEVEL_ORDER[level]

    if need("TESTED") and "unit_test" not in kinds:
        problems.append(f"{name}: {status} 需要至少一条 unit_test 证据")
    if need("CROSS_CHECKED"):
        if not ({"analytic", "cross_tool", "golden_corpus"} & kinds):
            problems.append(
                f"{name}: {status} 需要独立信源证据（analytic / cross_tool / golden_corpus），"
                f"当前只有 {sorted(kinds) or '无'}")
    if need("CORRELATED"):
        meas = [e for e in evs if str(e.get("type", "")).lower() in ("measurement",
                                                                    "field_solver")]
        if not meas:
            problems.append(f"{name}: CORRELATED/QUALIFIED 需要实测或全波证据，当前没有")
        else:
            bad = []
            for e in meas:
                ok, why = _measurement_ok(e) if e.get("type") == "measurement" else (True, None)
                if not ok:
                    bad.append(why)
            if bad and len(bad) == len(meas):
                problems.append(f"{name}: 实测证据不合格 —— " + "; ".join(bad))
    if need("QUALIFIED") and not entry.get("regression"):
        problems.append(f"{name}: QUALIFIED 需要 regression=true（长期回归无回退）")
    return (not problems), problems


def check_all(registry=None):
    reg = registry if isinstance(registry, dict) else load()
    problems, by_status = [], {}
    seen = set()
    for entry in reg.get("features") or []:
        name = entry.get("feature")
        if name in seen:
            problems.append(f"{name}: 重复登记")
        seen.add(name)
        ok, probs = check_entry(entry)
        problems.extend(probs)
        by_status[str(entry.get("status", "")).upper()] = \
            by_status.get(str(entry.get("status", "")).upper(), 0) + 1
    return {"ok": not problems, "problems": problems, "counts": by_status,
            "n_features": len(seen)}


# --------------------------------------------------------------------------
# 文档渲染（表格由注册表生成，禁止手改）
# --------------------------------------------------------------------------
STATUS_MEANING = {
    "IMPLEMENTED": "代码可运行，无测试要求",
    "TESTED": "有单元测试（断言我们自己定义的行为）",
    "CROSS_CHECKED": "与独立信源一致（解析式 / 另一工具 / 原始 CAD 数据 / 闭式真值）",
    "CORRELATED": "与**仪器实测**（OUT_OF_SAMPLE）或全波提取一致",
    "QUALIFIED": "TESTED + CROSS_CHECKED + CORRELATED 全满足，且长期回归无回退",
    "EXPERIMENTAL": "接口存在、结果不可用于决策",
}

EVIDENCE_LABEL = {
    "unit_test": "单元测试",
    "analytic": "解析式",
    "cross_tool": "交叉工具 / 原始 CAD",
    "golden_corpus": "闭式黄金语料",
    "measurement": "实测",
    "field_solver": "全波求解",
}


def render_markdown(registry=None):
    reg = registry if isinstance(registry, dict) else load()
    res = check_all(reg)
    out = ["# VALIDATION_STATUS", "",
           "> 本文件由 `scripts/validation.py` 从 `validation_registry.json` 生成，"
           "**请勿手改表格**。",
           "> 修改等级请改注册表并跑 `python -m pytest tests/test_validation.py`。", "",
           "## 等级定义（唯一权威）", "",
           "| 状态 | 含义 | 最低证据要求 |", "|---|---|---|"]
    req = {"IMPLEMENTED": "可运行", "TESTED": "≥1 条 unit_test",
           "CROSS_CHECKED": "≥1 条 analytic / cross_tool / golden_corpus",
           "CORRELATED": "≥1 条 measurement（instrument + OUT_OF_SAMPLE）或 field_solver",
           "QUALIFIED": "TESTED + CROSS_CHECKED + CORRELATED + regression",
           "EXPERIMENTAL": "无（也不会被证据升格）"}
    for lvl in LEVELS:
        out.append(f"| `{lvl}` | {STATUS_MEANING[lvl]} | {req[lvl]} |")
    out += ["", "纪律：`missing evidence != pass` · `solver available != numerically "
                "qualified` · `plot exists != validated`。", ""]
    out += [f"## 登记表（{res['n_features']} 项）", "",
            "| 功能 | 等级 | 证据类型 | 证据摘要 |", "|---|---|---|---|"]
    for e in reg.get("features") or []:
        kinds = sorted({EVIDENCE_LABEL.get(str(x.get("type", "")).lower(),
                                           str(x.get("type"))) for x in e.get("evidence") or []})
        summary = "; ".join(
            str(x.get("what") or x.get("detail") or x.get("ref") or "")
            for x in (e.get("evidence") or [])[:2])
        if len(e.get("evidence") or []) > 2:
            summary += f" …(+{len(e['evidence']) - 2})"
        out.append(f"| {e.get('feature')} | `{e.get('status')}` | {', '.join(kinds) or '—'} "
                   f"| {summary} |")
    out += ["", "## 自检结果", ""]
    if res["ok"]:
        out.append(f"✅ 全部 {res['n_features']} 项等级都被证据支持（"
                   + ", ".join(f"{k}:{v}" for k, v in sorted(res["counts"].items())) + "）")
    else:
        out.append("❌ 存在等级与证据不匹配的条目：")
        for p in res["problems"]:
            out.append(f"- {p}")
    out += ["", "## 什么情况下不能说\"已验证\"", "",
            "- 只有 `.raw` 文件、没跟解析值或实测比 → 最多 `TESTED`",
            "- 跟\"自己另一份实现\"比 → 不算 `CROSS_CHECKED`（要独立信源）",
            "- 拟合残差小 → 不是 `CORRELATED`（那是模型 vs 数据）",
            "- 合成/拟合内数据 → 永远不能升 `CORRELATED`（`sample` 必须 OUT_OF_SAMPLE）",
            "- 依赖本机没装的外部求解器 → `EXPERIMENTAL`", ""]
    return "\n".join(out) + "\n"


def main():
    argv = sys.argv[1:]
    if "--write" in argv:
        path = os.path.join(SKILL_DIR, "VALIDATION_STATUS.md")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(render_markdown())
        print(f"written: {path}")
    res = check_all()
    print(f"features: {res['n_features']}  ok: {res['ok']}")
    for k, v in sorted(res["counts"].items()):
        print(f"  {k}: {v}")
    for p in res["problems"]:
        print(f"  [PROBLEM] {p}")
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
