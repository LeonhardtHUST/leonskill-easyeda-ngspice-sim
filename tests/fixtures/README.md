# Wolf-Lite regression fixtures

这四套 fixture 是 v2.2 的**回归基线**，也是需求文档 §23/§24/§25/§29 的可执行版本。
它们只用于测试与需求表达——**不涉及任何真实 Wolf-Lite 原理图或 PCB 文档**。

每套目录结构一致：

```
<fixture>/
├── ir.json              IR（含 nets，等价于 connectivity.py 的输出）
├── model_map.json       外部模型映射（多数为空表）
├── behavioral.json      行为级占位
├── sim_override.json    PCB 寄生 / 网段切分
├── sim_config*.json     分析 + 扫描 + 指标
└── external/*.lib       自写 .SUBCKT（仅 acceptance 用，明确标注 TEST DOUBLES）
```

跑法（先 `netlist_gen.py` 生成网表，再 `sim_exec.py` 执行）：

```bash
S=~/.workbuddy/skills/easyeda-ngspice-sim/scripts
F=~/.workbuddy/skills/easyeda-ngspice-sim/tests/fixtures/wolf_lite_clock
mkdir -p /tmp/caseA && cd /tmp/caseA

python "$S/netlist_gen.py" "$F/ir.json" ./schematic.cir \
  --config "$F/sim_config.json" --report ./netlist_report.json
python "$S/sim_exec.py" ./schematic.cir tran "10p 120n" \
  --config "$F/sim_config.json" --report ./netlist_report.json --workdir .
```

或者直接跑 pytest：`python -m pytest tests/test_regression_fixtures.py -q`

---

## Case A — 61.44 MHz clock 分支

拓扑：

```
OSC(behavioral switching_output, Rout=10, Cload=3p)
  │ CLK_OSC
C1 220p（AC 耦合）
  │ $CLK_SRC
R_CLK_DAMP  ← 扫参 0/10/22/33/47/68 Ω
  │ $CLK_TX
T_CLK_MAIN 45.08 mm × 6.0 ps/mm = 270.5 ps
  │ $CLK_BRANCH
  ├── T_CLK_ADC  5 mm ── $CLK_ADC ── Cin 4p + 100Ω 到 1.65 V
  └── T_CLK_FPGA 5 mm ── $CLK_FPGA ── Cin 6p + 100Ω 到 1.65 V
```

- `sim_override.json`：Case A2（有传输线，扫源端阻尼）
- `sim_override_a0.json`：Case A0（理想短导线，保留端接与偏置，只去掉 T 线与阻尼）
- `sim_config.json`：`sweep R_CLK_DAMP ∈ {0,10,22,33,47,68}`，tran `10p 120n`，
  指标窗口 `t0 = 60 ns`（避开 AC 耦合的直流建立）

**断言**：TD 落在 265–275 ps（防止出现 ns 级单位错误）；无阻尼时过冲最大、
峰值与峰峰值随阻尼单调下降；skew 能量化。

> 电气说明：接收端 100 Ω 到 1.65 V 既是端接也是直流回路——没有它，纯容性负载会让
> `NET-CLOCK` 变成奇异矩阵。AC 耦合的建立时间常数 ≈ C·(R_bias‖Z_line) ≈ 10 ns，
> 所以指标窗口必须从 60 ns 起。

## Case B — ADC PDN

```
V1 5V ── R_TRACE/L_TRACE ── AVDD ─┬─ C_BULK 10µ (ESR 5m, ESL 1.2n) → AGND
                                  ├─ C_HF 100n (ESR 20m, ESL 0.7n) → $AGND_CP
                                  │                                  └─ L_VIA → AGND
                                  └─ AD9226 动态电流负载（behavioral, 30m↔110m）
AGND ── R_AGND_LINK(1m,1.5n) ── GND(0)
```

- `sim_config_ac.json`：`ac dec 20 1k 100meg`，扫 `C_HF × L_VIA`（4×3=12），
  指标 `impedance`（1 A 激励）
- `sim_config_tran.json`：`tran 10n 40u`，同参数，指标 `tran_stats`（纹波）
- **AGND 与 GND 是两个节点**，只有 0 Ω+1.5 nH 的连接——这是 v2.2 地策略的核心验证点

**断言**：`ground_mapping == {"AGND": "AGND", "GND": "0"}`；via 电感变大 → 高频 |Z| 变大。

## Case C — Front LCD PWM

```
V1 13.8V ── MAIN_RAIL ── R_CABLE/L_CABLE ── $FRONT_RAIL ─┬─ C_BULK 220µ
                                                        └─ R_FB/L_FB ── LED_VIN ─┬─ C_FILT 100µ
                                                                                └─ PWM 电流负载
```

- 扫 `DUTY ∈ {0.1,0.5,1.0} × L_FB ∈ {10p,1u,10u}`（9 case）
- `DUTY` 是 `.param`，波形写成 `PULSE(0 500m 1u 100n 100n {DUTY*10u} 10u)`
  ——演示"扫参可以改 `{表达式}` 里的参数"
- 指标：`I(V1)` 的 `tran_stats`（主电源侧电流纹波）与 `fft_harmonics`
  （100 kHz 基波 + 谐波）

## Acceptance — §29 验收电路

```
V1 13.8V ── U1 BUCK_3V3(.SUBCKT，power_output) ── 3V3 ── U2 LDO_2V5(.SUBCKT) ── V_ADC_RAW
                                                                                   │ R/L 走线
                                                                                AVDD ─┬─ C1 10µ + ESR/ESL
                                                                                      ├─ C2 100n + ESR/ESL → via → AGND
                                                                                      └─ AD9226 动态负载
```

- `external/fixture_power.lib` 是**自写的理想化 test double**（文件头有声明），
  用来验证 `.SUBCKT` 映射 / pin mapping / `power_output` 角色，**不是任何厂商模型**
- 验证点：
  1. 关键部分一个都不能被挖掉（`excluded` 为空）
  2. `XU1`/`XU2` 按 `subckt_order` 生成，`.include` 带引号
  3. `3V3` 由 buck 的 `power_output` 驱动 → `--power-policy auto` 下**不补** `VPWR_3V3`
  4. 电容 ESR/ESL 真的串进去了（`C_C1_*` 三段链）
  5. AGND 通过 `segments` 在 C2 地脚处切开，串一个 via 电感再接回 AGND
