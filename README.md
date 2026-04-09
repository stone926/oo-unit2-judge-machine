# OO Unit2 测评机

全部由 `Codex GPT-5.4 xhigh` 编写

## 使用方式

### 输入输出

依赖数据投喂程序和官方输入输出 `jar` 包，默认寻找路径：
- 数据投喂：`dependency/datainput`
- 输入输出：`dependency/elevator1-2026.jar`

通过`judger.py`的参数`--datainput`和`--lib-jar`修改依赖路径，若以`run.py`启动，需要通过`--judger-args`透传：
```bash
python run.py --judger-args --datainput in.exe --lib-jar lib.jar
```

### 项目打包

测评需要将待测试的源代码打包为`jar`，默认源代码路径为工程根目录下的`src`，默认`java`主类为`oo.Main`。通过`judger`的参数`--source-dir`和`--main-class`更改，例如：
```bash
python run.py --mutual --judger-args --main-class MainClass
```
其中`--mutual`为互测模式，使`data-generator`生成的数据都符合互测限制，使`judger`的校验都依照互测标准（主要是时间限制）

打包后的项目`jar`包默认为同目录下的`project.jar`。通过`judger`的参数`--project-jar`修改：
```bash
python run.pu --judger-args --project-jar tested.jar
```

### 启动测评

永不停息地测评：
```bash
python test/run.py
```

单轮测评：
```bash
python test/run.py --once
```

互测模式与每轮之间时间间隔：
```bash
python test/run.py --once --mutual --sleep-seconds 1.5
```
### 参数透传

`run.py` 也可以把参数原样透传给 `data_generator.py` 和 `judger.py`：

- `--generator-args` 后面的参数会原样传给 `data_generator.py`
- `--judger-args` 后面的参数会原样传给 `judger.py`
- `run.py` 自身的参数需要放在这两个透传段之前
- 通过 `--generator-args --output-dir` 和 `--judger-args --input-dir` 自定义测试数据目录，建议指向同一目录
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

每个测试点会同时生成带时间戳的 `<i>.in` 和不带时间戳的 `<i>.no.in`。

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
- 运行时间限制：`180s`

互测模式示例：
```bash
python test/data_generator.py --mutual --count 10 --min-requests 30 --max-requests 70 --seed 20260407
```

运行评判：
```bash
python test/judger.py
```

`judger.py` 默认单个测试点超时为 `120s`，互测模式 `--mutual` 下默认变为 `180s`；如果显式传入 `--timeout-seconds`，则以显式值为准。

强制重新打包并只测指定测试点：
```bash
python test/judger.py --rebuild --cases 1 2 3
```
