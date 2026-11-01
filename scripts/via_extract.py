#!/usr/bin/env python3
"""v2.3 P0-B(3)：过孔寄生 R/L/C 估计。

L(nH) = 0.2·h·[ln(4h/d)+1]         h = 过孔长度(mm)，d = 钻孔直径(mm)
C(pF) = 0.0555·εr·T·D1/(D2−D1)     T = 板厚(mm)，D1 = 焊盘，D2 = 反焊盘
R(Ω)  = ρ·h/(π·d·t_plating)        孔壁镀铜直流电阻

精度声明：ENGINEERING_ESTIMATE。PCB 文件里没有反焊盘尺寸，按"焊盘+间隙"估计，
允许用户在 override 里覆盖（绝不把经验式标成 datasheet 值）。
"""
import math

VIA_FIDELITY = "ENGINEERING_ESTIMATE"
DEFAULT_ANTIPAD_CLEARANCE_MM = 0.25
COPPER_RESISTIVITY = 1.72e-8      # Ω·m
PLATING_THICKNESS_MM = 0.025      # 孔壁镀铜 25 µm


class ExtractError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _er_between(via, stackup):
    if stackup is not None and stackup.dielectrics:
        vals = [d.get("er") for d in stackup.dielectrics if d.get("er")]
        if vals:
            return sum(vals) / len(vals)
    raise ExtractError("STACKUP_INCOMPLETE",
                       "过孔电容需要 εr：叠层里没有介电常数，请补 stackup")


def _via_length(via, stackup, board_thickness_mm):
    if via.get("length_mm"):
        return float(via["length_mm"])
    if board_thickness_mm:
        return float(board_thickness_mm)
    if stackup is not None:
        tot = sum((d.get("thickness_mm") or 0) for d in stackup.dielectrics)
        tot += sum((l.get("copper_um") or 0) / 1000.0 for l in stackup.layers)
        if tot > 0:
            return tot
    raise ExtractError(
        "VIA_LENGTH_UNKNOWN",
        "过孔长度未知：请给 board_thickness_mm，或提供完整 stackup（按介质+铜厚求和）")


def via_parasitics(via, stackup=None, board_thickness_mm=None,
                   antipad_clearance_mm=None, plating_mm=PLATING_THICKNESS_MM):
    h = _via_length(via, stackup, board_thickness_mm)
    d = via.get("drill_mm") or 0.3
    d1 = via.get("pad_mm") or (d + 0.25)
    clearance = DEFAULT_ANTIPAD_CLEARANCE_MM if antipad_clearance_mm is None \
        else float(antipad_clearance_mm)
    d2 = d1 + 2 * clearance
    er = _er_between(via, stackup)
    if d <= 0 or h <= 0:
        raise ExtractError("VIA_GEOMETRY_INVALID",
                           f"过孔 {via.get('id')}: 孔径/长度非法（h={h}, d={d}）")
    l_nh = 0.2 * h * (math.log(4 * h / d) + 1.0)
    c_pf = 0.0555 * er * h * d1 / max(d2 - d1, 1e-6)
    r_ohm = COPPER_RESISTIVITY * (h * 1e-3) / (math.pi * (d * 1e-3) * (plating_mm * 1e-3))
    return {
        "id": via.get("id"),
        "length_mm": round(h, 4),
        "drill_mm": round(d, 4),
        "pad_mm": round(d1, 4),
        "antipad_mm": round(d2, 4),
        "er": round(er, 3),
        "r_ohm": round(r_ohm, 6),
        "l_nh": round(l_nh, 4),
        "c_pf": round(c_pf, 4),
        "model_fidelity": VIA_FIDELITY,
        "assumptions": {
            "antipad_clearance_mm": clearance,
            "plating_mm": plating_mm,
            "note": "反焊盘尺寸 PCB 文件里没有，按焊盘+间隙估计；如需精度请 override",
        },
    }


def estimate_vias(geo_net, stackup, board_thickness_mm=None):
    out, warnings = [], []
    for v in geo_net.get("vias") or []:
        try:
            p = via_parasitics(v, stackup, board_thickness_mm)
        except ExtractError as e:
            warnings.append(f"{e.code}: {e.message}")
            continue
        p["nodes"] = v.get("nodes") or []
        out.append(p)
    return out, warnings


def summary_line(v):
    return (f"{v['id']}: L={v['l_nh']:g} nH C={v['c_pf']:g} pF "
            f"R={v['r_ohm'] * 1e3:g} mΩ [{v['model_fidelity']}]")
