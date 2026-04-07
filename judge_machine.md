# 测评机要求与方案

测评机用于测评该项目。该项目为依照`hw5_prompt.md`编写的程序。测评机分为两部分：测试点生成器 `data_generator` 与评判器 `judger`。

当前实现文件：

- `test/data_generator.py`
- `test/judger.py`

## data_generator

将测试点生成在 `test/in/<n>.in` 中，其中 `<n>` 为测试点编号，例如 `1.in`，`2.in`。

测试点数据满足 `hw5_prompt.md` 中的要求。输入文件最后包含一个空行，表示输入结束。

## judger

按顺序执行如下步骤：

1. 检查 `test/project.jar` 是否存在，若不存在，则将 `src` 下的工程打包到 `test/project.jar`

2. 运行 `project.jar` 时，为兼容 `datainput_student_win64.exe` 对文件名和同目录的要求，评判器会为每个测试点创建临时工作目录，并在其中放置：
   `stdin.txt`、`code.jar`、`datainput_student_win64.exe` 以及依赖 jar。随后在该临时目录中执行投喂。程序的 `stdout` 重定向到 `test/out/<i>.out`，`stderr` 重定向到 `test/out/<i>.err.out`。一组运行完毕后运行下一组，直到所有测试点全部运行完毕

3. 根据 `hw5_prompt.md` 中的约束，校验 `test/out/<i>.out`。对于每个 `i`，如果 `test/out/<i>.err.out` 存在且非空，跳过当前测试点，将该测试点视为错误。如果发现错误，创建 `test/judge/<i>.log`，并写入错误信息与对应输出行

注意：由于项目为多线程的，输出不唯一且有不确定性。

## 使用方式

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
