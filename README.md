# OO Unit2 测评机

全部由 `Codex GPT-5.4 xhigh` 编写

## 使用方式

依赖数据投喂程序和官方输入输出 `jar` 包，默认在同目录下的 `dependency` 文件夹中寻找。

测评需要将待测试的工程打包为`jar`，默认`java`主类为`oo.Main`。通过启动参数`--judger-args --main-class <你的java主类>`更改，例如：
```bash
python run.py --mutual --judger-args --main-class MainClass
```

永不停息地测评：
```bash
python test/run.py
```

单轮测评：
```bash
python test/run.py --once
```

`run.py` 自身支持的参数：

```bash
python test/run.py --once --mutual --sleep-seconds 1.5
```

`run.py` 也可以把参数原样透传给 `data_generator.py` 和 `judger.py`：

- `--generator-args` 后面的参数会原样传给 `data_generator.py`
- `--judger-args` 后面的参数会原样传给 `judger.py`
- `run.py` 自身的参数请放在这两个透传段之前
- 如果自定义测试数据目录，建议让 `--generator-args --output-dir` 和 `--judger-args --input-dir` 指向同一目录
- `run.py` 不再默认追加 `--rebuild`；如果需要每轮强制重新打包，请显式传入

透传参数示例：

```bash
python test/run.py --once --generator-args --count 20 --min-requests 10 --max-requests 40 --seed 20260407 --judger-args --rebuild --cases 1 2 3
```

自定义目录示例：

```bash
python test/run.py --generator-args --output-dir test/custom_in --judger-args --input-dir test/custom_in --output-dir test/custom_out --log-dir test/custom_judge
```

生成测试数据：

```bash
python test/data_generator.py
```

每个测试点会同时生成带时间戳的 `<i>.in` 和去掉时间戳的 `<i>.no.in`。

常用参数示例：

```bash
python test/data_generator.py --count 20 --min-requests 10 --max-requests 40 --seed 20260407
```

互测模式：

```bash
python test/data_generator.py --mutual
```

互测模式约束：

- 第一条请求时间不早于 `1.0s`
- 最后一条请求时间不晚于 `50.0s`
- 每个测试点的请求条数不超过 `70`
- 同一部电梯关联的请求数不超过 `30`

互测模式示例：

```bash
python test/data_generator.py --mutual --count 10 --min-requests 30 --max-requests 70 --seed 20260407
```

运行评判：

```bash
python test/judger.py
```

强制重新打包并只测指定测试点：

```bash
python test/judger.py --rebuild --cases 1 2 3
```
