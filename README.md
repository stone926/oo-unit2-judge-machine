# OO Unit2 测评机

全部由 `Codex GPT-5.4 xhigh`编写。根目录中的两个`md`包含部分prompt

## 使用方式

将数据投喂程序和官方输入输出`jar`包放在同目录下的`dependency`文件夹中

永不停息地测评：
```bash
python test/run.py
```

单轮测评：
```bash
python test/run.py --once
```

生成测试数据：

```bash
python test/data_generator.py
```

常用参数示例：

```bash
python test/data_generator.py --count 20 --min-requests 10 --max-requests 40 --seed 20260407
```

运行评判：

```bash
python test/judger.py
```

强制重新打包并只测指定测试点：

```bash
python test/judger.py --rebuild --cases 1 2 3
```