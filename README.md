# OO Unit2 测评机（hw7）

代码由AI生成

本文档以测评机放在 `<repo_root>/test` 且在项目根目录下运行命令下为例。

测评机依赖（设测评机所在目录为`<machine_root>`）：
- 数据投喂程序：`<machine_root>/dependency/datainput`
- 官方输入输出包：`<machine_root>/dependency/elevator3-2026.jar`

## 1. 快速开始

指定主类单论生成+评测：

```bash
python test/run.py --once --judger-args --main-class YourMainClass
```

默认主类（`oo.Main`）单轮生成+评测：

```bash
python test/run.py --once
```

持续循环评测（直到 Ctrl+C）：

```bash
python test/run.py
```

互测模式单轮：

```bash
python test/run.py --once --mutual
```

## 2. 目录与依赖

默认依赖路径：

- 数据投喂程序：`test/dependency/datainput`
- 官方输入输出包：`test/dependency/elevator3-2026.jar`

默认产物路径：

- 生成输入：`test/in`
- 程序输出：`test/out`
- 判题日志：`test/judge`

## 3. run.py（调度入口）

`run.py` 的职责：

1. 调用 `data_generator.py` 生成输入。
2. 调用 `judger.py` 构建并评测。
3. 循环执行并自动归档日志（除 `--once` 外）。

### 3.1 run.py 自身参数

- `--once`：只跑一轮。
- `--mutual`：向生成器和判题器同时透传 `--mutual`。
- `--sleep-seconds`：多轮之间休眠秒数。

### 3.2 参数透传规则

- `--generator-args` 后的参数，原样传给 `data_generator.py`。
- `--judger-args` 后的参数，原样传给 `judger.py`。
- `run.py` 自身参数要写在透传段之前。

示例：

```bash
python test/run.py --once --generator-args --count 10 --stress-mode auto --judger-args --rebuild --cases 1 2 3
```

## 4. data_generator.py（数据生成）

基础命令：

```bash
python test/data_generator.py
```

每次运行会生成：

- 带时间戳输入：`<i>.in`
- 去时间戳输入：`<i>.no.in`

并在终端打印本次随机种子 `seed`，便于复现。

### 4.1 主要参数

- `--count`：测试点个数，默认 `20`。
- `--mutual`：按互测限制生成。
- `--min-requests` / `--max-requests`：每个测试点总请求数范围。
- `--output-dir`：输出目录。
- `--last-request-limit`：默认模式下最后一条输入时间上限（秒），默认 `80.0`。
- `--maint-ratio`：普通模式中 MAINT 目标占比，默认 `0.60`，范围 `[0, 0.6]`。
- `--update-ratio`：普通模式中 UPDATE/RECYCLE 目标占比，默认 `0.05`，范围 `[0, 0.6]`。
- `--time-mode`：`auto | uniform | burst`。
- `--pickup-mode`：`auto | clustered | uniform`。
- `--dropoff-mode`：`auto | clustered | uniform`。
- `--stress-mode`：`none | auto | special-burst | shaft-chain | maint-wave | transfer-flood`，默认 `auto`。

### 4.2 stress-mode 说明

- `none`：关闭场景化压测，回到比例驱动生成。
- `auto`：按 case 轮转所有压测 profile。
- `special-burst`：多井道高密度 MAINT/UPDATE/RECYCLE 混合冲击。
- `shaft-chain`：单井道链式 UPDATE-RECYCLE（含 MAINT 交错）连续冲击。
- `maint-wave`：多波次 MAINT 集中到达，夹杂 UPDATE/RECYCLE。
- `transfer-flood`：围绕 F2 换乘与上下区跨区流动的乘客洪峰，叠加特殊请求。

### 4.3 常用示例

默认压力轮转：

```bash
python test/data_generator.py --count 20 --stress-mode auto
```

只测链式特殊请求：

```bash
python test/data_generator.py --count 20 --stress-mode shaft-chain
```

互测约束下生成压力点：

```bash
python test/data_generator.py --mutual --count 20 --min-requests 55 --max-requests 70 --stress-mode auto
```

## 5. judger.py（构建与评判）

基础命令：

```bash
python test/judger.py
```

默认会从 `src` 打包为 `test/project.jar`，主类默认 `oo.Main`。

### 5.1 常用参数

- `--rebuild`：强制重新构建 `project.jar`。
- `--main-class`：指定主类。
- `--source-dir`：指定源码目录。
- `--project-jar`：指定待测 jar。
- `--lib-jar`：指定官方依赖 jar。
- `--datainput`：指定投喂程序路径。
- `--input-dir` / `--output-dir` / `--log-dir`：输入、输出、日志目录。
- `--cases`：只跑指定 case（例如 `--cases 1 2 3`）。
- `--mutual`：启用互测输入限制校验。
- `--timeout`：单测点超时秒数。

### 5.2 超时与互测校验

- 默认超时：`120s`。
- `--mutual` 下默认超时：`180s`。
- 显式传入 `--timeout` 时，以显式值为准。

互测输入校验（由 `judger.py` 和 `data_generator.py` 共同保证）：

- 第一条请求时间 `>= 1.0s`
- 最后一条请求时间 `<= 50.0s`
- 请求总数 `<= 70`
- 每部主电梯最多 1 条 MAINT 请求

## 6. 组合示例

单轮、强制重编译、只测部分 case：

```bash
python test/run.py --once --judger-args --rebuild --cases 1 2 3
```

生成到自定义目录并用同目录评测：

```bash
python test/run.py --once --generator-args --output-dir test/custom_in --stress-mode auto --judger-args --input-dir test/custom_in --output-dir test/custom_out --log-dir test/custom_judge
```

使用自定义主类：

```bash
python test/run.py --once --judger-args --main-class YourMainClass --rebuild
```
