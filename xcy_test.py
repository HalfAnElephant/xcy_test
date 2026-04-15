#!/usr/bin/env python3
"""Compile C/C++ sources using config and compare VS build output with demo.exe.

Usage:
    python xcy_test.py [target_dir]

Behavior:
    - Scans target directory for C/C++ source files.
    - Loads compiler settings from xcy.config.json.
    - Builds non-demo source files using VS and RedPanda compilers.
    - Color-highlights all warning/error compiler diagnostics.
    - Uses only VS-compiled executable(s) to compare against demo.exe with data.txt groups.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class Colors:
    RESET = "\033[0m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    DIM = "\033[2m"


COLOR_ENABLED = True


@dataclass
class RunResult:
    stdout: str
    stderr: str
    returncode: int
    timeout: bool = False


@dataclass
class BuildResult:
    compiler_name: str
    source_file: Path
    exe_path: Path
    success: bool
    stdout: str
    stderr: str
    returncode: int


def any_output_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    if isinstance(value, memoryview):
        return value.tobytes().decode(errors="replace")
    return str(value)


def enable_windows_ansi_if_possible() -> None:
    if os.name != "nt":
        return
    # Try enabling ANSI escape processing on Windows terminals.
    if hasattr(os, "system"):
        os.system("")


def colorize(text: str, color_code: str) -> str:
    if not COLOR_ENABLED:
        return text
    return f"{color_code}{text}{Colors.RESET}"


def print_compiler_output_with_highlight(compiler_name: str, output_text: str) -> None:
    if not output_text.strip():
        return
    print(colorize(f"[{compiler_name}] 编译输出:", Colors.CYAN))
    for line in output_text.splitlines():
        lower = line.lower()
        if "error" in lower:
            print(colorize(line, Colors.RED))
        elif "warning" in lower:
            print(colorize(line, Colors.YELLOW))
        else:
            print(line)


def readable_char(ch: str) -> str:
    if ch == "\n":
        return "\\n"
    if ch == "\r":
        return "\\r"
    if ch == "\t":
        return "\\t"
    if ch == " ":
        return "<space>"
    if ch == "":
        return "<EOF>"
    return ch


def split_groups(raw_text: str) -> List[str]:
    # Prefer splitting by delimiter line; fallback to simple token split.
    if re.search(r"(?m)^\s*---\s*$", raw_text):
        parts = re.split(r"(?m)^\s*---\s*$", raw_text)
    else:
        parts = raw_text.split("---")

    groups = [p.strip("\n\r") for p in parts]
    return [g for g in groups if g.strip()]


def read_data_file(path: Path) -> List[str]:
    encodings = ["utf-8", "gbk", "utf-16"]
    last_error = None
    for enc in encodings:
        try:
            text = path.read_text(encoding=enc)
            groups = split_groups(text)
            # Keep one no-input run for programs that do not require stdin.
            return groups if groups else [""]
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    raise RuntimeError(f"读取 {path.name} 失败，尝试编码 {encodings} 均失败: {last_error}")


def find_demo_executable(cwd: Path) -> Path:
    exes = sorted(cwd.glob("*.exe"), key=lambda p: p.name.lower())

    # Exclude self executable when running as packaged binary.
    self_exe = None
    if getattr(sys, "frozen", False):
        self_exe = Path(sys.executable).resolve()

    filtered: List[Path] = []
    for p in exes:
        try:
            rp = p.resolve()
        except OSError:
            rp = p
        if self_exe is not None and rp == self_exe:
            continue
        filtered.append(p)

    demo_exes = [p for p in filtered if "demo" in p.name.lower()]
    if len(demo_exes) != 1:
        names = ", ".join(p.name for p in filtered) or "<none>"
        raise RuntimeError(
            "当前目录下 demo.exe 条件不满足。"
            f"\n检测到: {names}"
            f"\n要求: 恰好 1 个包含 demo 的 exe（不含当前脚本自身）。"
        )

    return demo_exes[0]


def read_config_file(path: Path) -> Dict[str, object]:
    encodings = ["utf-8", "gbk", "utf-16"]
    last_error = None
    for enc in encodings:
        try:
            text = path.read_text(encoding=enc)
            return json.loads(text)
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    raise RuntimeError(f"读取配置失败 {path.name}: {last_error}")


def get_compile_extensions(config: Dict[str, object]) -> List[str]:
    ext_config = config.get("extensions", {})
    compile_exts = ext_config.get(
        "compile", []) if isinstance(ext_config, dict) else []
    normalized = [str(x).lower() for x in compile_exts if isinstance(x, str)]
    return normalized or [".c", ".cpp"]


def scan_source_files(target_dir: Path, compile_exts: List[str]) -> List[Path]:
    files: List[Path] = []
    for p in target_dir.iterdir():
        if p.is_file() and p.suffix.lower() in compile_exts:
            files.append(p)
    return sorted(files, key=lambda x: x.name.lower())


def is_cpp_source(path: Path) -> bool:
    return path.suffix.lower() == ".cpp"


def as_str_list(value: object) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(x) for x in value]


def normalize_user_path_arg(raw_path: str) -> str:
    p = raw_path.strip()
    # Handle accidental trailing quote from shell/input methods.
    if p.endswith('"') and not p.startswith('"'):
        p = p[:-1].rstrip()
    # Handle wrapped quotes, e.g. "C:\\My Folder".
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        p = p[1:-1].strip()
    return p


def run_cmd(args: List[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            errors="replace",
        )
    except FileNotFoundError as exc:
        missing = args[0] if args else "<unknown>"
        return subprocess.CompletedProcess(
            args=args,
            returncode=127,
            stdout="",
            stderr=f"命令不存在: {missing}\n{exc}",
        )


def detect_vsdevcmd_path() -> Optional[Path]:
    base = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    vswhere = base / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.exists():
        return None

    query = [
        str(vswhere),
        "-latest",
        "-products",
        "*",
        "-requires",
        "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
        "-property",
        "installationPath",
    ]
    completed = subprocess.run(query, text=True, capture_output=True)
    install = completed.stdout.strip()
    if completed.returncode != 0 or not install:
        return None

    path = Path(install) / "Common7" / "Tools" / "VsDevCmd.bat"
    if path.exists():
        return path
    return None


def compile_with_vs(source: Path, out_exe: Path, vs_conf: Dict[str, object], cwd: Path) -> BuildResult:
    flags = as_str_list(vs_conf.get(
        "cppFlags" if is_cpp_source(source) else "cFlags"))
    cl_cmd = ["cl", *flags, str(source), f"/Fe:{out_exe}"]

    use_vswhere = bool(vs_conf.get("useVsWhere", True))
    # Use Windows-native argument escaping to survive spaces/unicode/special chars in paths.
    cmd_line = subprocess.list2cmdline(cl_cmd)
    if use_vswhere:
        vsdev = detect_vsdevcmd_path()
        if vsdev:
            shell_cmd = f'call "{vsdev}" -no_logo && {cmd_line}'
            completed = subprocess.run(
                shell_cmd,
                cwd=str(cwd),
                text=True,
                capture_output=True,
                shell=True,
                errors="replace",
            )
        else:
            completed = run_cmd(cl_cmd, cwd)
    else:
        completed = run_cmd(cl_cmd, cwd)

    success = completed.returncode == 0 and out_exe.exists()
    return BuildResult(
        compiler_name="vs",
        source_file=source,
        exe_path=out_exe,
        success=success,
        stdout=completed.stdout,
        stderr=completed.stderr,
        returncode=completed.returncode,
    )


def compile_with_redpanda(source: Path, out_exe: Path, rp_conf: Dict[str, object], cwd: Path) -> BuildResult:
    root = Path(str(rp_conf.get("compilerRoot", "")))
    compiler_name = str(rp_conf.get(
        "cppCompiler" if is_cpp_source(source) else "cCompiler", ""))
    compiler = root / "bin" / compiler_name
    flags = as_str_list(rp_conf.get(
        "cppFlags" if is_cpp_source(source) else "cFlags"))

    if not compiler_name.strip():
        return BuildResult(
            compiler_name="redpanda",
            source_file=source,
            exe_path=out_exe,
            success=False,
            stdout="",
            stderr="RedPanda 编译器名称未配置（cppCompiler/cCompiler 为空）。",
            returncode=127,
        )

    if not compiler.exists():
        return BuildResult(
            compiler_name="redpanda",
            source_file=source,
            exe_path=out_exe,
            success=False,
            stdout="",
            stderr=f"未找到 RedPanda 编译器: {compiler}",
            returncode=127,
        )

    cmd = [str(compiler), str(source), "-o", str(out_exe), *flags]

    completed = run_cmd(cmd, cwd)
    success = completed.returncode == 0 and out_exe.exists()
    return BuildResult(
        compiler_name="redpanda",
        source_file=source,
        exe_path=out_exe,
        success=success,
        stdout=completed.stdout,
        stderr=completed.stderr,
        returncode=completed.returncode,
    )


def compile_sources(target_dir: Path, config: Dict[str, object], sources: List[Path], build_dir: Path, log_dir: Path) -> List[BuildResult]:
    results: List[BuildResult] = []
    vs_conf = config.get("vs2026", {})
    rp_conf = config.get("redPanda", {})
    vs_enabled = isinstance(vs_conf, dict) and bool(
        vs_conf.get("enabled", False))
    rp_enabled = isinstance(rp_conf, dict) and bool(
        rp_conf.get("enabled", False))

    if not vs_enabled and not rp_enabled:
        raise RuntimeError("配置中未启用任何编译器。")

    build_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    for src in sources:
        stem = src.stem
        if vs_enabled:
            vs_exe = build_dir / f"{stem}_vs.exe"
            res = compile_with_vs(src, vs_exe, vs_conf if isinstance(
                vs_conf, dict) else {}, target_dir)
            (log_dir / f"{stem}_vs.log").write_text(res.stdout +
                                                    "\n" + res.stderr, encoding="utf-8", errors="replace")
            print_compiler_output_with_highlight(
                "VS", res.stdout + "\n" + res.stderr)
            results.append(res)
        if rp_enabled:
            rp_exe = build_dir / f"{stem}_redpanda.exe"
            res = compile_with_redpanda(src, rp_exe, rp_conf if isinstance(
                rp_conf, dict) else {}, target_dir)
            (log_dir / f"{stem}_redpanda.log").write_text(res.stdout +
                                                          "\n" + res.stderr, encoding="utf-8", errors="replace")
            print_compiler_output_with_highlight(
                "RedPanda", res.stdout + "\n" + res.stderr)
            results.append(res)

    return results


def collect_vs_executables(results: List[BuildResult]) -> List[Path]:
    exes: List[Path] = []
    for r in results:
        if r.compiler_name == "vs" and r.success:
            exes.append(r.exe_path)
    return exes


def collect_gcc_executables(results: List[BuildResult]) -> List[Path]:
    exes: List[Path] = []
    for r in results:
        if r.compiler_name == "redpanda" and r.success:
            exes.append(r.exe_path)
    return exes


def run_program(exe_path: Path, input_text: str, timeout_sec: float) -> RunResult:
    try:
        completed = subprocess.run(
            [str(exe_path)],
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            cwd=exe_path.parent,
        )
        return RunResult(
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
            timeout=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = any_output_to_text(exc.stdout)
        err = any_output_to_text(exc.stderr)
        return RunResult(stdout=out, stderr=err, returncode=-1, timeout=True)


def ensure_trailing_newline(text: str) -> str:
    if not text:
        return "\n"
    if text.endswith("\n"):
        return text
    return text + "\n"


def build_group_input(group: str) -> str:
    return ensure_trailing_newline(group)


def print_unified_diff(left_name: str, right_name: str, left_text: str, right_text: str, context_lines: int) -> None:
    if context_lines < 0:
        context_lines = 0

    diff_lines = list(
        difflib.unified_diff(
            left_text.splitlines(keepends=True),
            right_text.splitlines(keepends=True),
            fromfile=left_name,
            tofile=right_name,
            n=context_lines,
            lineterm="",
        )
    )

    if not diff_lines:
        return

    print(colorize("差异详情 (git unified diff):", Colors.YELLOW))
    for line in diff_lines:
        if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
            print(colorize(line, Colors.CYAN))
        elif line.startswith("-"):
            print(colorize(line, Colors.RED))
        elif line.startswith("+"):
            print(colorize(line, Colors.YELLOW))
        else:
            print(line)


def first_diff_index(left_text: str, right_text: str) -> int:
    limit = min(len(left_text), len(right_text))
    for i in range(limit):
        if left_text[i] != right_text[i]:
            return i
    return limit


def char_window(text: str, index: int, radius: int = 12) -> Tuple[str, str, str]:
    if not text:
        return "", "", ""

    if index < len(text):
        start = max(0, index - radius)
        end = min(len(text), index + radius + 1)
        return text[start:index], text[index], text[index + 1:end]

    start = max(0, len(text) - radius)
    return text[start:len(text)], "", ""


def print_char_diff(left_name: str, right_name: str, left_text: str, right_text: str) -> None:
    idx = first_diff_index(left_text, right_text)

    left_prefix, left_char, left_suffix = char_window(left_text, idx)
    right_prefix, right_char, right_suffix = char_window(right_text, idx)

    print(colorize("差异详情 (字符比对):", Colors.YELLOW))
    print(f"首个差异位置: index={idx}")

    print(colorize(f"[{left_name}]", Colors.CYAN))
    print(
        f"...{left_prefix}{colorize(readable_char(left_char), Colors.RED)}{left_suffix}..."
    )

    print(colorize(f"[{right_name}]", Colors.CYAN))
    print(
        f"...{right_prefix}{colorize(readable_char(right_char), Colors.YELLOW)}{right_suffix}..."
    )


def print_group_report(index: int, demo_name: str, other_name: str, demo_res: RunResult, other_res: RunResult, context_lines: int, detail: bool = False, input_text: Optional[str] = None, use_git_diff: bool = False) -> bool:
    print(colorize(f"\n=== 第 {index} 组数据 ===", Colors.CYAN))
    print(f"{demo_name}: returncode={demo_res.returncode}, timeout={demo_res.timeout}, stdout_len={len(demo_res.stdout)}")
    print(f"{other_name}: returncode={other_res.returncode}, timeout={other_res.timeout}, stdout_len={len(other_res.stdout)}")

    if detail:
        print(
            f"[{demo_name}] stdout:\n{demo_res.stdout if demo_res.stdout else '<empty>'}")
        print(
            f"[{other_name}] stdout:\n{other_res.stdout if other_res.stdout else '<empty>'}")
        print(
            f"[{demo_name}] stderr:\n{demo_res.stderr if demo_res.stderr else '<empty>'}")
        print(
            f"[{other_name}] stderr:\n{other_res.stderr if other_res.stderr else '<empty>'}")

    if (not detail) and demo_res.stderr.strip():
        print(f"[{demo_name}] stderr:\n{demo_res.stderr}")
    if (not detail) and other_res.stderr.strip():
        print(f"[{other_name}] stderr:\n{other_res.stderr}")

    if demo_res.stdout == other_res.stdout:
        print(colorize("结果: 输出完全一致", Colors.GREEN))
        return True

    print(colorize("结果: 输出不一致", Colors.RED))
    if input_text is not None:
        shown_input = input_text if input_text else "<empty>"
        print(colorize("触发该差异的输入数据:", Colors.YELLOW))
        print(shown_input)
    if use_git_diff:
        print_unified_diff(
            demo_name,
            other_name,
            demo_res.stdout,
            other_res.stdout,
            context_lines,
        )
    else:
        print_char_diff(
            demo_name,
            other_name,
            demo_res.stdout,
            other_res.stdout,
        )

    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对比当前目录下两个 exe 的输出结果")
    parser.add_argument("target_dir", nargs="?",
                        default=".", help="目标目录名或路径，默认当前目录")
    parser.add_argument("--config", default="xcy.config.json",
                        help="编译配置文件名，默认 xcy.config.json")
    parser.add_argument("--timeout", type=float,
                        default=20.0, help="每个程序单次运行超时时间(秒)，默认 20")
    parser.add_argument("--no-color", action="store_true", help="关闭终端颜色高亮输出")
    parser.add_argument("--context", type=int, default=3,
                        help="unified diff 上下文行数，默认 3，设为 0 仅显示变更行")
    parser.add_argument("--git", action="store_true",
                        help="启用 git unified diff 模式输出差异；默认使用字符比对模式")
    parser.add_argument("--detail", action="store_true",
                        help="打印 demo 与被测程序每组的完整 stdout/stderr")
    parser.add_argument("--compiler", default="vs", type=str.lower, choices=["vs", "gcc"],
                        help="选择用于对照 demo 的被测程序来源: vs 或 gcc，默认 vs")
    return parser.parse_args()


def main() -> int:
    global COLOR_ENABLED

    args = parse_args()
    COLOR_ENABLED = not args.no_color
    enable_windows_ansi_if_possible()

    cwd = Path(os.getcwd())
    script_dir = Path(sys.executable).resolve().parent if getattr(
        sys, "frozen", False) else Path(__file__).resolve().parent
    target_dir_arg = normalize_user_path_arg(args.target_dir)
    target_dir = Path(target_dir_arg)
    if not target_dir.is_absolute():
        target_dir = cwd / target_dir
    target_dir = target_dir.resolve()

    if not target_dir.exists() or not target_dir.is_dir():
        print(f"错误: 目标目录不存在或不是文件夹: {target_dir}")
        return 2

    config_arg = Path(args.config)
    if config_arg.is_absolute():
        config_file = config_arg
    else:
        config_file = script_dir / config_arg
    if not config_file.exists():
        print(f"错误: 配置文件不存在: {config_file}")
        return 2

    data_file = target_dir / "data.txt"
    if not data_file.exists():
        print(f"错误: 目标目录缺少 data.txt: {target_dir}")
        return 2

    try:
        config = read_config_file(config_file)
    except RuntimeError as exc:
        print(f"错误: {exc}")
        return 2

    try:
        demo_exe = find_demo_executable(target_dir)
    except RuntimeError as exc:
        print(f"错误: {exc}")
        return 2

    compile_exts = get_compile_extensions(config)
    all_sources = scan_source_files(target_dir, compile_exts)
    source_files = [s for s in all_sources if "demo" not in s.stem.lower()]
    if not source_files:
        print("错误: 未找到可编译的非 demo C/C++ 源文件")
        return 2

    output_conf = config.get("output", {})
    build_dir = target_dir / str(output_conf.get("buildDir", "./build")
                                 ) if isinstance(output_conf, dict) else target_dir / "build"
    log_dir = target_dir / str(output_conf.get("logDir", "./build_logs")
                               ) if isinstance(output_conf, dict) else target_dir / "build_logs"

    build_dir = build_dir.resolve()
    log_dir = log_dir.resolve()

    print(colorize("开始编译源码...", Colors.CYAN))
    print(f"配置文件: {config_file}")
    print(f"源码数量: {len(source_files)}")
    print(f"构建目录: {build_dir}")
    print(f"日志目录: {log_dir}")

    try:
        build_results = compile_sources(
            target_dir, config, source_files, build_dir, log_dir)
    except RuntimeError as exc:
        print(f"错误: {exc}")
        return 2

    if args.compiler == "vs":
        test_exes = collect_vs_executables(build_results)
        selected_compiler_label = "VS"
    else:
        test_exes = collect_gcc_executables(build_results)
        selected_compiler_label = "GCC"

    if not test_exes:
        print(
            colorize(f"错误: {selected_compiler_label} 编译未产出可执行文件，无法进行测试", Colors.RED))
        return 2

    try:
        groups = read_data_file(data_file)
    except RuntimeError as exc:
        print(f"错误: {exc}")
        return 2

    print(colorize("开始比对...", Colors.CYAN))
    print(f"目标目录: {target_dir}")
    print(f"demo 程序: {demo_exe.name}")
    print(f"对拍编译器: {selected_compiler_label}")
    print(f"产物数量: {len(test_exes)}")
    print(f"数据组数量: {len(groups)}")

    overall_ok = True
    for test_exe in test_exes:
        print(colorize(
            f"\n>>> 测试 {selected_compiler_label} 产物: {test_exe.name}", Colors.CYAN))
        all_equal = True
        equal_count = 0

        for idx in range(1, len(groups) + 1):
            group_input = build_group_input(groups[idx - 1])
            demo_full_res = run_program(
                demo_exe, group_input, timeout_sec=args.timeout)
            other_full_res = run_program(
                test_exe, group_input, timeout_sec=args.timeout)

            demo_res = RunResult(
                stdout=demo_full_res.stdout,
                stderr=demo_full_res.stderr,
                returncode=demo_full_res.returncode,
                timeout=demo_full_res.timeout,
            )
            other_res = RunResult(
                stdout=other_full_res.stdout,
                stderr=other_full_res.stderr,
                returncode=other_full_res.returncode,
                timeout=other_full_res.timeout,
            )

            group_equal = print_group_report(
                idx,
                demo_exe.name,
                test_exe.name,
                demo_res,
                other_res,
                max(0, args.context),
                args.detail,
                group_input,
                args.git,
            )

            if group_equal:
                equal_count += 1
            else:
                all_equal = False

        print(colorize("\n=== 单个程序总结 ===", Colors.CYAN))
        print(f"程序: {test_exe.name}")
        print(f"总组数: {len(groups)}")
        print(f"一致组数: {equal_count}")
        print(f"不一致组数: {len(groups) - equal_count}")

        if all_equal:
            print(colorize("结论: 与 demo 输出完全一致", Colors.GREEN))
        else:
            print(colorize("结论: 与 demo 存在输出差异", Colors.RED))
            overall_ok = False

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
