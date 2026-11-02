# leonskill-easyeda-ngspice-sim

Agent skill. 使用 NGspice 对嘉立创 EDA 专业版工程进行仿真。

- **在线入口**：通过 easyeda-api skill 提供的 EasyEDA Bridge 从运行中的 EDA 提取原理图页
- **离线入口**：依赖 [`leonskill-easyeda-project-analysis`](https://github.com/LeonhardtHUST/leonskill-easyeda-project-analysis) 解析 `.epro2` 工程文件（本仓库不再自带容器/记录流解析）

支持从原理图自动生成 NGspice 仿真工程。详见 `SKILL.md`。

> Leonskill EasyEDA CDP Connect 的集成计划在下一版本提供，当前版本不支持。
