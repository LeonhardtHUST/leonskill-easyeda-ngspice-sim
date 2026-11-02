# leonskill-easyeda-ngspice-sim

Agent skill. 使用 NGspice 对嘉立创 EDA 专业版工程进行仿真。

- **在线入口**：通过 `easyeda-api` skill 提供的 EasyEDA Bridge 从运行中的 EDA 提取原理图页
- **离线入口**：依赖 [`leonskill-easyeda-project-analysis`](https://github.com/LeonhardtHUST/leonskill-easyeda-project-analysis) 解析 `.epro2` 工程文件（本仓库不再自带容器/记录流解析）

支持从原理图自动生成 NGspice 仿真工程。详见 `SKILL.md`。

> 与 [`leonskill-easyeda-cdp-connect`](https://github.com/LeonhardtHUST/leonskill-easyeda-cdp-connect) 的集成计划在下一版本提供，当前版本不支持。

## 安装

**注意目录名**：仓库名比技能名多了 `leonskill-` 前缀，而 `SKILL.md` 里的命令按
`~/.workbuddy/skills/easyeda-ngspice-sim/` 这个路径写死。所以 clone 时要显式指定目标目录名：

```bash
cd ~/.workbuddy/skills
git clone https://github.com/LeonhardtHUST/leonskill-easyeda-ngspice-sim.git easyeda-ngspice-sim
```

升级：

```bash
cd ~/.workbuddy/skills/easyeda-ngspice-sim && git pull
```

仓库根就是技能根（`SKILL.md` 在顶层），不需要再摘子目录。

### 离线入口还需要装依赖 skill

离线入口按目录名 `leonskill-easyeda-project-analysis` 查找依赖（`scripts/analysis_dep.py`）。
该仓库名与目录名一致，直接 clone 即可：

```bash
cd ~/.workbuddy/skills
git clone https://github.com/LeonhardtHUST/leonskill-easyeda-project-analysis.git
```

依赖装在别处时用 `EASYEDA_PROJECT_ANALYSIS_DIR` 指向它的根目录。装完确认：

```bash
python ~/.workbuddy/skills/easyeda-ngspice-sim/scripts/analysis_dep.py --locate
```

找不到依赖时报 `DEPENDENCY_MISSING` 并列出尝试过的路径，不会静默降级。

## 运行前提

| 项 | 要求 |
|---|---|
| ngspice | 47 版。默认查找 `~/.workbuddy/binaries/ngspice/ngspice-47/Spice64/bin/ngspice_con.exe`，可用 `NGSPICE_BIN` 覆盖 |
| Python | 核心路径只用标准库，无需 pip 安装 |
| 数值 / 优化（可选） | `pip install -r requirements-core.txt`（numpy / scipy）。缺失时纯 stdlib 路径仍可用 |
| RF（可选） | `pip install -r requirements-rf.txt`（含 scikit-rf）。缺失时 `backend=vectorfit` 报 `RF_BACKEND_UNAVAILABLE` |
| EM（可选） | openEMS / CSXCAD 需自行安装；本 skill 只生成 CSXCAD 脚本，见 `requirements-em.txt` |
| 在线入口 | 需要 `easyeda-api` skill 的 EasyEDA Bridge |

## 快速开始

```bash
SKILL=~/.workbuddy/skills/easyeda-ngspice-sim/scripts

# 离线入口先确认依赖在不在
python $SKILL/analysis_dep.py --locate

# 工程文件 → 仿真
bash $SKILL/sim_file.sh ~/Downloads/board.epro2 op
bash $SKILL/sim_from_sch.sh "2026秋季硬件2" P1 ac "dec 50 1 10meg" ./out
```

## 自测

```bash
cd ~/.workbuddy/skills/easyeda-ngspice-sim && python -m pytest tests
```

v2.6.0 基线：231 passed, 1 skipped（约 38 s）。依赖缺失时依赖相关用例自动 skip。

## 文档

| 文件 | 内容 |
|---|---|
| `SKILL.md` | 技能主体：入口、CLI 选项、工作流、模型政策、文档索引 |
| `skill-dependencies.json` | 能力声明、依赖与降级映射（机器可读：Python / ngspice / project-analysis / easyeda-api） |
| `ARCHITECTURE_v2.6.md` | 与 `project-analysis` 的依赖边界与调用契约 |
| `VALIDATION_STATUS.md` | 验证等级表（由 `scripts/validation.py` 生成，勿手改） |
| `CHANGELOG_v2.*.md` | 各版本变更 |
