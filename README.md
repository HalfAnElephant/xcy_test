# xcy_test

C/C++ 编译、格式化与对拍测试工具。

## 功能

- **build**: 自动格式化源码（添加 header comment + clang-format），然后使用 VS2026 和 RedPanda 双编译器构建
- **test**: 编译源码后，将编译产物与 `demo.exe` 按 `data.txt` 分组对拍，输出差异详情

## 使用方法

### build — 格式化 + 编译

```bash
python xcy_test.py build [target_dir]
```

可选参数：
- `--config xcy.config.json` — 指定配置文件
- `--format-ext .c .cpp` — 仅对指定后缀的文件进行格式化
- `--no-color` — 关闭终端颜色输出

示例：
```bash
python xcy_test.py build .
python xcy_test.py build . --format-ext .c .cpp
```

### test — 对拍测试

```bash
python xcy_test.py test [target_dir]
```

可选参数：
- `--config xcy.config.json` — 指定配置文件
- `--timeout 20` — 单次运行超时时间（秒）
- `--context 3` — unified diff 上下文行数
- `--git` — 使用 git unified diff 输出差异
- `--detail` — 打印每组完整 stdout/stderr
- `--compiler vs|gcc` — 选择对比编译器产物来源

## 配置文件

`xcy.config.json` 示例：

```json
{
  "headerComment": "/* 2551155 计算机 谢朝阳*/",
  "encoding": "gbk",
  "extensions": {
    "format": [".c", ".cpp", ".h"],
    "compile": [".c", ".cpp"]
  },
  "tools": {
    "clangFormatPath": "./clang-format.exe",
    "clangFormatStyle": "{Language: Cpp, BasedOnStyle: LLVM, IndentWidth: 4}"
  },
  "output": {
    "buildDir": "./build",
    "logDir": "./build_logs"
  },
  "vs2026": {
    "enabled": true,
    "useVsWhere": true,
    "cppFlags": ["/nologo", "/std:c++20", "/Zi", "/EHsc", "/W4", "/D_DEBUG", "/MDd"],
    "cFlags": ["/nologo", "/std:c11", "/Zi", "/W4", "/D_DEBUG", "/MDd"]
  },
  "redPanda": {
    "enabled": true,
    "compilerRoot": "C:/Users/Haer/RedPanda-CPP/mingw64",
    "cppCompiler": "g++.exe",
    "cCompiler": "gcc.exe",
    "cppFlags": ["-g3", "-fno-ms-extensions", "-std=c++2a", "-pipe", "-Wall", "-D_DEBUG", "-Wl,--stack,12582912", "-static"],
    "cFlags": ["-g3", "-std=c11", "-pipe", "-Wall", "-D_DEBUG", "-Wl,--stack,12582912", "-static"]
  }
}
```

## 使用 PyInstaller 构建可执行文件

### 安装依赖

```bash
pip install pyinstaller
```

### 构建

```bash
pyinstaller --onefile xcy_test.py
```

构建完成后，可执行文件位于 `dist/xcy_test.exe`。

### 打包为 Release

将生成的 `dist/xcy_test.exe` 上传到 GitHub Release 即可。
