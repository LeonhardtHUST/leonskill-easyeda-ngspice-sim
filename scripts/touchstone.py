#!/usr/bin/env python3
"""v2.3 P0-E(1)：Touchstone 解析（.s1p / .s2p / .sNp）。

支持：
  频率单位  Hz / kHz / MHz / GHz
  数据格式  RI / MA / DB
  归一化阻抗 R <z0>（默认 50）
  `!` 注释、选项行 `# ...`、多行续写（2 端口每点 9 个数，允许折行）

不支持（会明确报错而不是猜）：
  Y / Z / H / G 参数格式、噪声参数块、混合模式端口（.s2p 的 5 列以上变体）
"""
import cmath
import math
import os
import re

FREQ_SCALE = {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9}
FORMATS = ("ri", "ma", "db")


class TouchstoneError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Touchstone:
    def __init__(self, path=None):
        self.path = path
        self.name = os.path.basename(path) if path else None
        self.num_ports = None
        self.z0 = 50.0
        self.freq_unit = "hz"
        self.data_format = "ma"
        self.frequencies = []      # Hz
        self.s = []                # s[k][i][j] 复数，k = 频点索引
        self.comments = []
        self.warnings = []

    # ---------------------------------------------------------------- 访问
    def at(self, i, j, k):
        return self.s[k][i][j]

    def freqs(self):
        return list(self.frequencies)

    def magnitude(self, i, j):
        return [abs(self.s[k][i][j]) for k in range(len(self.s))]

    def db(self, i, j):
        return [20 * math.log10(max(abs(self.s[k][i][j]), 1e-30)) for k in range(len(self.s))]

    def phase(self, i, j):
        return [math.degrees(cmath.phase(self.s[k][i][j])) for k in range(len(self.s))]

    def to_dict(self):
        return {
            "file": self.name, "path": self.path, "num_ports": self.num_ports,
            "z0": self.z0, "freq_unit": self.freq_unit, "data_format": self.data_format,
            "n_points": len(self.frequencies),
            "f_start_hz": self.frequencies[0] if self.frequencies else None,
            "f_stop_hz": self.frequencies[-1] if self.frequencies else None,
            "warnings": self.warnings,
        }


def _to_complex(a, b, fmt):
    fmt = fmt.lower()
    if fmt == "ri":
        return complex(a, b)
    if fmt == "ma":
        return cmath.rect(a, math.radians(b))
    if fmt == "db":
        return cmath.rect(10 ** (a / 20.0), math.radians(b))
    raise TouchstoneError("TOUCHSTONE_FORMAT_UNSUPPORTED", f"不支持的数据格式 {fmt}")


def parse(path):
    if not os.path.exists(path):
        raise TouchstoneError("TOUCHSTONE_FILE_MISSING", f"文件不存在: {path}")
    lower = path.lower()
    m = re.search(r"\.s(\d+)p$", lower)
    if not m:
        raise TouchstoneError("TOUCHSTONE_NOT_SPARAM",
                              f"{os.path.basename(path)} 不是 .sNp 文件")
    nports = int(m.group(1))
    if nports < 1:
        raise TouchstoneError("TOUCHSTONE_NOT_SPARAM", "端口数必须 >= 1")
    ts = Touchstone(path)
    ts.num_ports = nports
    per_point = 1 + 2 * nports * nports

    nums = []          # 扁平数字流（首个数是频率）
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("!"):
                ts.comments.append(line[1:].strip())
                continue
            if line.startswith("#"):
                _parse_option(line, ts)
                continue
            toks = line.split()
            for t in toks:
                try:
                    nums.append(float(t))
                except ValueError:
                    raise TouchstoneError(
                        "TOUCHSTONE_PARSE_ERROR",
                        f"{os.path.basename(path)}: 无法解析的数值 {t!r}（行: {line[:60]}）")
    if not nums:
        raise TouchstoneError("TOUCHSTONE_PARSE_ERROR",
                              f"{os.path.basename(path)} 里没有数据点")
    if len(nums) % per_point:
        raise TouchstoneError(
            "TOUCHSTONE_PARSE_ERROR",
            f"{os.path.basename(path)}: 数据个数 {len(nums)} 不是每点 {per_point} 个数的整数倍"
            f"（{nports} 端口应有 1+2*n^2 = {per_point} 列）")
    scale = FREQ_SCALE[ts.freq_unit]
    for base in range(0, len(nums), per_point):
        chunk = nums[base:base + per_point]
        freq = chunk[0] * scale
        pairs = chunk[1:]
        mat = []
        idx = 0
        for i in range(nports):
            row = []
            for j in range(nports):
                a, b = pairs[idx], pairs[idx + 1]
                idx += 2
                row.append(_to_complex(a, b, ts.data_format))
            mat.append(row)
        ts.frequencies.append(freq)
        ts.s.append(mat)
    if len(ts.frequencies) < 2:
        ts.warnings.append("只有 1 个频点，拟合/插值没有意义")
    _check_monotonic(ts)
    return ts


def _parse_option(line, ts):
    toks = line[1:].split()
    i = 0
    while i < len(toks):
        t = toks[i].lower()
        if t in FREQ_SCALE:
            ts.freq_unit = t
        elif t in FORMATS:
            ts.data_format = t
        elif t == "r" and i + 1 < len(toks):
            try:
                ts.z0 = float(toks[i + 1])
            except ValueError:
                pass
            i += 1
        elif t in ("y", "z", "h", "g"):
            raise TouchstoneError(
                "TOUCHSTONE_FORMAT_UNSUPPORTED",
                f"只支持 S 参数，文件里写的是 {t.upper()} 参数（{line.strip()}）")
        i += 1


def _check_monotonic(ts):
    bad = [i for i in range(1, len(ts.frequencies))
           if ts.frequencies[i] <= ts.frequencies[i - 1]]
    if bad:
        ts.warnings.append(f"频率不是单调递增（{len(bad)} 处），已按文件顺序保留")


def load_or_none(path):
    try:
        return parse(path)
    except TouchstoneError:
        return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    ts = parse(sys.argv[1])
    print(f"{ts.name}: {ts.num_ports}-port, {len(ts.frequencies)} pts, "
          f"{ts.frequencies[0]:g}..{ts.frequencies[-1]:g} Hz, z0={ts.z0:g}")
    for j in range(ts.num_ports):
        for i in range(ts.num_ports):
            print(f"  S{i + 1}{j + 1}: {ts.db(i, j)[0]:.2f} dB @ f0")
