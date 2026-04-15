#!/usr/bin/env python3
"""Compile C/C++ sources using config and compare VS build output with demo.exe.

Usage:
    python xcy_test.py build [target_dir]    # Format + compile workflow
    python xcy_test.py test [target_dir]     # Compare outputs against demo.exe

Behavior:
    - Scans target directory for C/C++ source files.
    - Loads compiler settings from xcy.config.json.
    - build: formats sources (adds header + clang-format), then compiles with VS and RedPanda.
    - test: builds non-demo source files using VS and/or RedPanda compilers,
            then uses only VS-compiled executable(s) to compare against demo.exe with data.txt groups.
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
            return groups if groups else [""]
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    raise RuntimeError(f"读取 {path.name} 失败，尝试编码 {encodings} 均失败: {last_error}")


def find_demo_executable(cwd: Path) -> Path:
    exes = sorted(cwd.glob("*.exe"), key=lambda p: p.name.lower())

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
    compile_exts = ext_config.get("compile", []) if isinstance(ext_config, dict) else []
    normalized = [str(x).lower() for x in compile_exts if isinstance(x, str)]
    return normalized or [".c", ".cpp"]


def get_format_extensions(config: Dict[str, object]) -> List[str]:
    ext_config = config.get("extensions", {})
    format_exts = ext_config.get("format", []) if isinstance(ext_config, dict) else []
    normalized = [str(x).lower() for x in format_exts if isinstance(x, str)]
    return normalized or [".c", ".cpp", ".h"]


def scan_source_files(target_dir: Path, compile_exts: List[str]) -> List[Path]:
    files: List[Path] = []
    for p in target_dir.iterdir():
        if p.is_file() and p.suffix.lower() in compile_exts:
            files.append(p)
    return sorted(files, key=lambda x: x.name.lower())


def scan_format_files(target_dir: Path, format_exts: List[str]) -> List[Path]:
    files: List[Path] = []
    for p in target_dir.iterdir():
        if p.is_file() and p.suffix.lower() in format_exts:
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
    if p.endswith('"') and not p.startswith('"'):
        p = p[:-1].rstrip()
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
    flags = as_str_list(vs_conf.get("cppFlags" if is_cpp_source(source) else "cFlags"))
    cl_cmd = ["cl", *flags, str(source), f"/Fe:{out_exe}"]

    use_vswhere = bool(vs_conf.get("useVsWhere", True))
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
    compiler_name = str(rp_conf.get("cppCompiler" if is_cpp_source(source) else "cCompiler", ""))
    compiler = root / "bin" / compiler_name
    flags = as_str_list(rp_conf.get("cppFlags" if is_cpp_source(source) else "cFlags"))

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
    vs_enabled = isinstance(vs_conf, dict) and bool(vs_conf.get("enabled", False))
    rp_enabled = isinstance(rp_conf, dict) and bool(rp_conf.get("enabled", False))

    if not vs_enabled and not rp_enabled:
        raise RuntimeError("配置中未启用任何编译器。")

    build_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    for src in sources:
        stem = src.stem
        if vs_enabled:
            vs_exe = build_dir / f"{stem}_vs.exe"
            res = compile_with_vs(src, vs_exe, vs_conf if isinstance(vs_conf, dict) else {}, target_dir)
            (log_dir / f"{stem}_vs.log").write_text(res.stdout + "\n" + res.stderr, encoding="utf-8", errors="replace")
            print_compiler_output_with_highlight("VS", res.stdout + "\n" + res.stderr)
            results.append(res)
        if rp_enabled:
            rp_exe = build_dir / f"{stem}_redpanda.exe"
            res = compile_with_redpanda(src, rp_exe, rp_conf if isinstance(rp_conf, dict) else {}, target_dir)
            (log_dir / f"{stem}_redpanda.log").write_text(res.stdout + "\n" + res.stderr, encoding="utf-8", errors="replace")
            print_compiler_output_with_highlight("RedPanda", res.stdout + "\n" + res.stderr)
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
        return text[start:index], text[index], text[index + 1 : end]

    start = max(0, len(text) - radius)
    return text[start : len(text)], "", ""


def print_char_diff(left_name: str, right_name: str, left_text: str, right_text: str) -> None:
    idx = first_diff_index(left_text, right_text)

    left_prefix, left_char, left_suffix = char_window(left_text, idx)
    right_prefix, right_char, right_suffix = char_window(right_text, idx)

    print(colorize("差异详情 (字符比对):", Colors.YELLOW))
    print(f"首个差异位置: index={idx}")

    print(colorize(f"[{left_name}]", Colors.CYAN))
    print(f"...{left_prefix}{colorize(readable_char(left_char), Colors.RED)}{left_suffix}...")

    print(colorize(f"[{right_name}]", Colors.CYAN))
    print(f"...{right_prefix}{colorize(readable_char(right_char), Colors.YELLOW)}{right_suffix}...")


def print_group_report(
    index: int,
    demo_name: str,
    other_name: str,
    demo_res: RunResult,
    other_res: RunResult,
    context_lines: int,
    detail: bool = False,
    input_text: Optional[str] = None,
    use_git_diff: bool = False,
) -> bool:
    print(colorize(f"\n=== 第 {index} 组数据 ===", Colors.CYAN))
    print(f"{demo_name}: returncode={demo_res.returncode}, timeout={demo_res.timeout}, stdout_len={len(demo_res.stdout)}")
    print(f"{other_name}: returncode={other_res.returncode}, timeout={other_res.timeout}, stdout_len={len(other_res.stdout)}")

    if detail:
        print(f"[{demo_name}] stdout:\n{demo_res.stdout if demo_res.stdout else '<empty>'}")
        print(f"[{other_name}] stdout:\n{other_res.stdout if other_res.stdout else '<empty>'}")
        print(f"[{demo_name}] stderr:\n{demo_res.stderr if demo_res.stderr else '<empty>'}")
        print(f"[{other_name}] stderr:\n{other_res.stderr if other_res.stderr else '<empty>'}")

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
        print_unified_diff(demo_name, other_name, demo_res.stdout, other_res.stdout, context_lines)
    else:
        print_char_diff(demo_name, other_name, demo_res.stdout, other_res.stdout)

    return False


# ---------------------------------------------------------------------------
# Build workflow ported from xcy.cmd
# ---------------------------------------------------------------------------


def read_text_auto(file_path: Path) -> str:
    if not file_path.exists():
        return ""
    data = file_path.read_bytes()
    if len(data) == 0:
        return ""
    if len(data) >= 3 and data[0] == 0xEF and data[1] == 0xBB and data[2] == 0xBF:
        return data[3:].decode("utf-8", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gbk", errors="replace")


def ensure_header_comment(file_path: Path, header: str, encoding: str) -> None:
    content = read_text_auto(file_path)
    newline = "\r\n" if "\r\n" in content else ("\n" if "\n" in content else "\r\n")
    lines = content.splitlines() if content else []

    if not lines:
        lines = [header]
    elif lines[0].strip() != header:
        first = lines[0].strip()
        if first.startswith("/*") and first.endswith("*/"):
            lines[0] = header
        else:
            lines = [header] + lines

    new_content = newline.join(lines)
    if new_content and not new_content.endswith(newline):
        new_content += newline
    file_path.write_text(new_content, encoding=encoding, errors="replace")


def run_clang_format(file_path: Path, clang_path: Path, clang_style: str) -> None:
    cmd = [str(clang_path), "-i", f"--style={clang_style}", str(file_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"clang-format failed for {file_path}: {result.stderr}")


def build_workflow(target_dir: Path, config: Dict[str, object], format_exts: Optional[List[str]] = None) -> int:
    header_comment = str(config.get("headerComment", ""))
    encoding_name = str(config.get("encoding", "utf-8"))
    encoding = "gbk" if encoding_name.lower() == "gbk" else encoding_name

    all_format_exts = get_format_extensions(config)
    effective_format_exts = format_exts if format_exts else all_format_exts

    all_compile_exts = get_compile_extensions(config)

    tools = config.get("tools", {})
    clang_path = Path(tools.get("clangFormatPath", "./clang-format.exe")) if isinstance(tools, dict) else Path("./clang-format.exe")
    clang_style = str(tools.get("clangFormatStyle", "{BasedOnStyle: LLVM}")) if isinstance(tools, dict) else "{BasedOnStyle: LLVM}"

    if not clang_path.is_absolute():
        clang_path = Path(sys.executable).resolve().parent / clang_path if getattr(sys, "frozen", False) else Path(__file__).resolve().parent / clang_path

    format_files = scan_format_files(target_dir, effective_format_exts)
    compile_files = scan_source_files(target_dir, all_compile_exts)

    output_conf = config.get("output", {})
    build_dir = target_dir / str(output_conf.get("buildDir", "./build")) if isinstance(output_conf, dict) else target_dir / "build"
    build_dir = build_dir.resolve()
    build_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Formatting
    print(colorize("=== Step 1/3: Formatting ===", Colors.CYAN))
    if format_files:
        for file in format_files:
            ensure_header_comment(file, header_comment, encoding)
            if clang_path.exists():
                run_clang_format(file, clang_path, clang_style)
                ensure_header_comment(file, header_comment, encoding)
            print(colorize(f"[format] Processed: {file}", Colors.GREEN))
    else:
        print(colorize("[format] No source files found to process.", Colors.YELLOW))

    if not clang_path.exists():
        print(colorize(f"[format] clang-format not found: {clang_path}", Colors.YELLOW))

    # Step 2: VS2026 Build
    print("")
    print(colorize("=== Step 2/3: VS2026 Debug/x64 Build ===", Colors.CYAN))
    vs_conf = config.get("vs2026", {})
    vs_enabled = isinstance(vs_conf, dict) and bool(vs_conf.get("enabled", False))
    vs_fail = vs_warn = vs_err = 0

    if vs_enabled and compile_files:
        vs_output_root = build_dir / "vs2026"
        vs_output_root.mkdir(parents=True, exist_ok=True)
        use_vswhere = bool(vs_conf.get("useVsWhere", True))
        vsdev = detect_vsdevcmd_path() if use_vswhere else None

        for src in compile_files:
            lang = "c" if src.suffix.lower() == ".c" else "cpp"
            flags = as_str_list(vs_conf.get("cppFlags" if lang == "cpp" else "cFlags"))
            flags_str = " ".join(flags)
            out_dir = vs_output_root
            out_dir.mkdir(parents=True, exist_ok=True)
            obj_path = out_dir / f"{src.stem}.obj"
            exe_path = out_dir / f"{src.stem}.exe"
            cl_cmd = f'cl {flags_str} /Fo"{obj_path}" /Fe"{exe_path}" "{src}"'
            if vsdev:
                shell_cmd = f'"{vsdev}" -arch=x64 -host_arch=x64 >nul && {cl_cmd}'
            else:
                shell_cmd = cl_cmd

            print(f"[vs2026][{lang}] Compiling: {src}")
            completed = subprocess.run(shell_cmd, cwd=str(target_dir), shell=True, capture_output=True, text=True, errors="replace")
            txt = completed.stdout + completed.stderr
            if txt.strip():
                print_compiler_output_with_highlight("vs2026", txt)
                for line in txt.splitlines():
                    lower = line.lower()
                    if "warning" in lower and ("warning" in lower or ":" in lower):
                        vs_warn += 1
                    if "error" in lower and ("error" in lower or ":" in lower):
                        vs_err += 1
            if completed.returncode != 0:
                vs_fail += 1

    print(f"[vs2026] Summary: files={len(compile_files)}, failed={vs_fail}, warnings={vs_warn}, errors={vs_err}")

    # Step 3: RedPanda Build
    print("")
    print(colorize("=== Step 3/3: Red Panda mingw64 64-bit Debug Build ===", Colors.CYAN))
    rp_conf = config.get("redPanda", {})
    rp_enabled = isinstance(rp_conf, dict) and bool(rp_conf.get("enabled", False))
    rp_fail = rp_warn = rp_err = 0

    if rp_enabled and compile_files:
        rp_output_root = build_dir / "redpanda"
        rp_output_root.mkdir(parents=True, exist_ok=True)
        root = Path(str(rp_conf.get("compilerRoot", "")))

        for src in compile_files:
            lang = "c" if src.suffix.lower() == ".c" else "cpp"
            compiler_name = str(rp_conf.get("cppCompiler" if lang == "cpp" else "cCompiler", "g++.exe" if lang == "cpp" else "gcc.exe"))
            compiler = root / "bin" / compiler_name
            if not compiler.exists():
                print(colorize(f"Red Panda compiler not found: {compiler}", Colors.RED))
                rp_fail += 1
                continue

            flags = as_str_list(rp_conf.get("cppFlags" if lang == "cpp" else "cFlags"))
            out_dir = rp_output_root
            out_dir.mkdir(parents=True, exist_ok=True)
            exe_path = out_dir / f"{src.stem}.exe"
            cmd = [str(compiler), str(src), "-o", str(exe_path), *flags]

            print(f"[redpanda][{lang}] Compiling: {src}")
            completed = run_cmd(cmd, target_dir)
            txt = completed.stdout + completed.stderr
            if txt.strip():
                print_compiler_output_with_highlight("redpanda", txt)
                for line in txt.splitlines():
                    lower = line.lower()
                    if "warning:" in lower:
                        rp_warn += 1
                    if "error:" in lower:
                        rp_err += 1
            if completed.returncode != 0:
                rp_fail += 1

    print(f"[redpanda] Summary: files={len(compile_files)}, failed={rp_fail}, warnings={rp_warn}, errors={rp_err}")

    if vs_fail > 0 or vs_err > 0 or rp_fail > 0 or rp_err > 0:
        print(colorize(f"Workflow completed with failures. vsFail={vs_fail} vsErr={vs_err} rpFail={rp_fail} rpErr={rp_err}", Colors.RED))
        return 1

    print(colorize("Workflow finished successfully: format + dual build completed.", Colors.GREEN))
    return 0


# ---------------------------------------------------------------------------
# Test workflow (original xcy_test behavior)
# ---------------------------------------------------------------------------


def test_workflow(args: argparse.Namespace) -> int:
    global COLOR_ENABLED
    COLOR_ENABLED = not args.no_color
    enable_windows_ansi_if_possible()

    cwd = Path(os.getcwd())
    script_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
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
    build_dir = target_dir / str(output_conf.get("buildDir", "./build")) if isinstance(output_conf, dict) else target_dir / "build"
    log_dir = target_dir / str(output_conf.get("logDir", "./build_logs")) if isinstance(output_conf, dict) else target_dir / "build_logs"

    build_dir = build_dir.resolve()
    log_dir = log_dir.resolve()

    print(colorize("开始编译源码...", Colors.CYAN))
    print(f"配置文件: {config_file}")
    print(f"源码数量: {len(source_files)}")
    print(f"构建目录: {build_dir}")
    print(f"日志目录: {log_dir}")

    try:
        build_results = compile_sources(target_dir, config, source_files, build_dir, log_dir)
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
        print(colorize(f"错误: {selected_compiler_label} 编译未产出可执行文件，无法进行测试", Colors.RED))
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
        print(colorize(f"\n>>> 测试 {selected_compiler_label} 产物: {test_exe.name}", Colors.CYAN))
        all_equal = True
        equal_count = 0

        for idx in range(1, len(groups) + 1):
            group_input = build_group_input(groups[idx - 1])
            demo_full_res = run_program(demo_exe, group_input, timeout_sec=args.timeout)
            other_full_res = run_program(test_exe, group_input, timeout_sec=args.timeout)

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="xcy_test: build + test C/C++ sources")
    subparsers = parser.add_subparsers(dest="command", help="可用子命令")

    # build command
    build_parser = subparsers.add_parser("build", help="格式化并编译源码（对应 xcy.cmd 功能）")
    build_parser.add_argument("target_dir", nargs="?", default=".", help="目标目录名或路径，默认当前目录")
    build_parser.add_argument("--config", default="xcy.config.json", help="编译配置文件名，默认 xcy.config.json")
    build_parser.add_argument("--format-ext", nargs="+", default=None, help="指定要格式化的文件后缀，如 .c .cpp .h")
    build_parser.add_argument("--no-color", action="store_true", help="关闭终端颜色高亮输出")

    # test command
    test_parser = subparsers.add_parser("test", help="对拍测试：对比编译产物与 demo.exe 的输出")
    test_parser.add_argument("target_dir", nargs="?", default=".", help="目标目录名或路径，默认当前目录")
    test_parser.add_argument("--config", default="xcy.config.json", help="编译配置文件名，默认 xcy.config.json")
    test_parser.add_argument("--timeout", type=float, default=20.0, help="每个程序单次运行超时时间(秒)，默认 20")
    test_parser.add_argument("--no-color", action="store_true", help="关闭终端颜色高亮输出")
    test_parser.add_argument("--context", type=int, default=3, help="unified diff 上下文行数，默认 3，设为 0 仅显示变更行")
    test_parser.add_argument("--git", action="store_true", help="启用 git unified diff 模式输出差异；默认使用字符比对模式")
    test_parser.add_argument("--detail", action="store_true", help="打印 demo 与被测程序每组的完整 stdout/stderr")
    test_parser.add_argument("--compiler", default="vs", type=str.lower, choices=["vs", "gcc"], help="选择用于对照 demo 的被测程序来源: vs 或 gcc，默认 vs")

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.command is None:
        # Backward compatibility: default to test behavior
        args = parse_args()
        # Re-parse with test defaults when no subcommand given
        # argparse already parsed; we can just treat it as test if target_dir present
        # But subparsers require a subcommand. To keep compatibility, we fallback to test.
        # Actually argparse will error on no subcommand. We workaround by detecting.
        pass

    global COLOR_ENABLED
    COLOR_ENABLED = not getattr(args, "no_color", False)
    enable_windows_ansi_if_possible()

    if args.command == "build":
        cwd = Path(os.getcwd())
        script_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
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

        try:
            config = read_config_file(config_file)
        except RuntimeError as exc:
            print(f"错误: {exc}")
            return 2

        format_exts = [str(x).lower() for x in args.format_ext] if args.format_ext else None
        return build_workflow(target_dir, config, format_exts)

    # Default to test workflow for backward compatibility
    return test_workflow(args)


if __name__ == "__main__":
    # Handle backward compatibility when no subcommand is provided.
    # argparse with subparsers errors on no subcommand; patch sys.argv to default to test.
    if len(sys.argv) > 1 and sys.argv[1] not in ("build", "test", "-h", "--help"):
        sys.argv.insert(1, "test")
    elif len(sys.argv) == 1:
        sys.argv.append("test")

    raise SystemExit(main())
