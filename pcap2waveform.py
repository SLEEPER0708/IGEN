#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcap2waveform.py
pcap 文件 -> PPDU PHY 参数 JSON -> 拼接 IQ 波形 .mat，支持三种运行模式：
内部依次调用同目录下的 extract_ppdu_phy.py（提取 PPDU PHY 参数）
和 MATLAB（generate_ppdu_waveform.m，生成拼接波形）。

运行模式（--mode）：
    all      （默认）pcap 一步生成波形。
             适用：本机同时安装 tshark 和 MATLAB。
    extract  仅 pcap -> ppdu_phy.json。
             适用：本机只有 tshark、没有 MATLAB；之后把 json 拷贝到装有
             MATLAB 的机器上，用 generate 模式生成波形。
    generate 仅 ppdu_phy.json -> 波形。
             适用：本机只有 MATLAB、没有 tshark；json 由其他机器的
             extract 模式生成。

输出（all / generate 模式均输出两个波形文件）：
    ppdu_concat_fs<fs>MHz.mat  MATLAB 格式复数 IQ 波形（w/fs/p_segment_meta）
    ppdu_concat_fs<fs>MHz.txt  定点十六进制波形（默认 S16Q11，每行 <I_hex><Q_hex>，
                               文件头注明 I/Q 位置与格式）

依赖：
    - Python 3.6+（仅标准库）
    - tshark（Wireshark）：all / extract 模式需要
    - MATLAB + WLAN Toolbox：all / generate 模式需要
      （DSSS 重采样时另需 Signal Processing Toolbox）

用法示例：
    :: 模式一：本机有 tshark + MATLAB，一键生成
    python pcap2waveform.py capture.pcapng
    :: 模式二：本机只有 tshark，先提取 PHY 参数
    python pcap2waveform.py capture.pcapng --mode extract
    :: 模式三：本机只有 MATLAB，由 json 生成波形
    python pcap2waveform.py ppdu_phy.json --mode generate
    python pcap2waveform.py capture.pcapng --start-time 1.0 --duration 0.2 --fs 80
    python pcap2waveform.py capture.pcapng -o D:\\out --keep-json --chan-mode sum
    python pcap2waveform.py capture.pcapng --pwr-threshold 30 --low-pwr-mode clamp
    python pcap2waveform.py capture.pcapng --seed 12345
    python pcap2waveform.py capture.pcapng --tshark "C:\\Program Files\\Wireshark\\tshark.exe" ^
        --matlab "C:\\Program Files\\MATLAB\\R2025a\\bin\\matlab.exe"

默认输出目录为本脚本所在目录下的 ppdu_waveforms 文件夹。
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

FS_CHOICES_MHZ = (20, 40, 80, 160)
MAX_CONCAT_SAMPLES = 1e8  # 与 generate_ppdu_waveform.m 中的拼接内存上限一致


def find_tshark(user_path):
    """定位 tshark：--tshark > PATH > 常见安装路径。"""
    if user_path:
        if os.path.isfile(user_path):
            return user_path
        print("[错误] 指定的 tshark 路径不存在：%s" % user_path, file=sys.stderr)
        sys.exit(2)
    found = shutil.which("tshark")
    if found:
        return found
    candidates = [
        r"C:\Program Files\Wireshark\tshark.exe",
        r"D:\Softwares\Wireshark\tshark.exe",
        "/usr/bin/tshark",
        "/usr/local/bin/tshark",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    print("[错误] 未找到 tshark。请安装 Wireshark，或用 --tshark 指定 tshark.exe 完整路径。",
          file=sys.stderr)
    sys.exit(2)


def _matlab_version_key(path):
    """从路径解析 MATLAB 版本（R20XXa/b）作为排序键；无版本信息者排最后。"""
    m = re.search(r"[rR](20\d{2})([aAbB])", path)
    return (int(m.group(1)), m.group(2).lower()) if m else (0, "")


def find_matlab(user_path):
    """定位 MATLAB：--matlab > PATH > 常见安装路径（取版本号最大者）。"""
    if user_path:
        if not os.path.isfile(user_path):
            print("[错误] 指定的 MATLAB 路径不存在：%s" % user_path, file=sys.stderr)
            sys.exit(2)
        matlab = user_path
        warn_matlab_version(matlab)
        return matlab
    found = shutil.which("matlab")
    if found:
        warn_matlab_version(found)
        return found
    patterns = [
        r"C:\Program Files\MATLAB\R*\bin\matlab.exe",
        "/usr/local/MATLAB/R*/bin/matlab",
        "/opt/MATLAB/R*/bin/matlab",
        "/opt/*/matlab_r*/bin/matlab",
        "/opt/matlab_r*/bin/matlab",
    ]
    candidates = []
    for pat in patterns:
        candidates.extend(glob.glob(pat))
    if candidates:
        # 按解析出的版本号排序，最新者在前（字符串排序在多个安装根
        # 并存时会按路径字母而非版本号排序，可能误选旧版本）
        found = sorted(candidates, key=_matlab_version_key, reverse=True)[0]
        warn_matlab_version(found)
        return found
    print("[错误] 未找到 MATLAB。请安装 MATLAB（含 WLAN Toolbox），"
          "或用 --matlab 指定 matlab 可执行文件完整路径。", file=sys.stderr)
    sys.exit(2)


def warn_matlab_version(matlab):
    """路径中含 R20xx 版本号且低于 R2019a 时给出警告（-batch 需 R2019a+，
    HE SU 波形生成亦需要较新 WLAN Toolbox）。"""
    m = re.search(r"[rR](20\d{2})([aAbB])", matlab)
    if m and int(m.group(1)) < 2019:
        print("[警告] MATLAB 路径显示版本为 R%s%s，低于 R2019a：-batch 选项不可用，"
              "将自动回退 -r 调用形式；且旧版 WLAN Toolbox 可能不支持 "
              "HE/DSSS 波形生成，建议升级或改用 --matlab 指定新版本。"
              % (m.group(1), m.group(2).lower()))


def check_companions(script_dir, need_extractor, need_generator):
    """确认同目录下的依赖脚本存在（按运行模式检查，不需要的不检查，
    便于在仅有 tshark 或仅有 MATLAB 的机器上分开部署）。"""
    extractor = os.path.join(script_dir, "extract_ppdu_phy.py")
    generator = os.path.join(script_dir, "generate_ppdu_waveform.m")
    missing = []
    if need_extractor and not os.path.isfile(extractor):
        missing.append(extractor)
    if need_generator and not os.path.isfile(generator):
        missing.append(generator)
    if missing:
        print("[错误] 缺少依赖文件：%s\n请把 pcap2waveform.py 与所需依赖脚本"
              "放在同一目录。" % "\n".join(missing), file=sys.stderr)
        sys.exit(2)
    return extractor


def _decode(data):
    """按字节解码子进程输出：先 UTF-8，失败后回退 GBK（中文 Windows 控制台默认）。

    Python 子进程已用 PYTHONIOENCODING=utf-8 强制 UTF-8；MATLAB -batch 在中文
    Windows 上按系统区域设置（GBK）输出，需要回退解码。
    """
    if not data:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def run_matlab(matlab, script_dir, json_path, out_dir, fs_hz,
               chan_mode, pwr_threshold, low_pwr_mode, seed):
    """调用 MATLAB 执行 generate_ppdu_waveform。

    不把 MATLAB 命令放在命令行参数中（部分服务器上的 matlab wrapper 脚本
    用 eval 转发参数，含空格/括号/引号的命令会被拆解破坏），而是写入临时
    .m 脚本后以"裸脚本名"调用：参数中不含任何特殊字符，wrapper 无法破坏。
    优先使用 -batch（R2019a+），失败自动回退旧版 -r 调用形式。
    """
    pwr_arg = "[]" if pwr_threshold == 0 else "%g" % pwr_threshold
    if seed.lower() == "default":
        seed_arg = "'default'"
    else:
        seed_arg = "%d" % int(seed)
    script_name = "pcap2waveform_run"
    m_path = os.path.join(out_dir, script_name + ".m")
    lines = [
        "try",
        "    addpath('%s');" % script_dir.replace("\\", "/"),
        "    generate_ppdu_waveform('%s', '%s', %d, '%s', %s, '%s', [], [], %s);"
        % (json_path.replace("\\", "/"), out_dir.replace("\\", "/"),
           fs_hz, chan_mode, pwr_arg, low_pwr_mode, seed_arg),
        "catch ME",
        "    fprintf(2, '%s\\n', getReport(ME));",
        "    exit(1);",
        "end",
        "exit(0);",
        "",
    ]
    with open(m_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    hint = ("提示：若服务器上的 matlab 是 wrapper 脚本或版本低于 R2019a，"
            "建议用 --matlab 指定真实 MATLAB 可执行文件（R2019a 及以上，"
            "且需安装 WLAN Toolbox）。")
    try:
        # 裸脚本名作为参数：无空格/括号/引号，wrapper 的 eval 无法破坏
        _, rc = run_step([matlab, "-batch", script_name], "MATLAB 波形生成",
                         cwd=out_dir, allow_fail=True)
        if rc != 0:
            # -batch 需 R2019a+；旧版本回退 -r 形式（同样裸脚本名）
            print("[注意] -batch 调用失败，回退 -r 形式重试（旧版 MATLAB）...")
            run_step([matlab, "-nodisplay", "-nosplash", "-nodesktop",
                      "-r", script_name],
                     "MATLAB 波形生成（-r 兼容形式）", hint=hint, cwd=out_dir)
    finally:
        if os.path.isfile(m_path):
            os.remove(m_path)


def run_step(cmd, step_name, env=None, hint=None, cwd=None, allow_fail=False):
    """运行子进程并透传输出。

    失败时以中文错误退出；allow_fail=True 时不退出，返回退出码交由调用者
    处理（用于可回退的尝试性调用）。返回 (stdout, 退出码)。
    """
    proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, cwd=cwd)
    stdout = _decode(proc.stdout)
    stderr = _decode(proc.stderr)
    if stdout:
        print(stdout.strip())
    if proc.returncode != 0:
        if allow_fail:
            return stdout, proc.returncode
        if stderr:
            print(stderr.strip(), file=sys.stderr)
        print("[错误] %s 失败（退出码 %d），已终止。" % (step_name, proc.returncode),
              file=sys.stderr)
        if hint:
            print(hint, file=sys.stderr)
        sys.exit(2)
    return stdout, proc.returncode


def run_extract(tshark, extractor, pcap_path, json_out, start_time, duration):
    """调用 extract_ppdu_phy.py 提取 PHY 参数 JSON。

    强制 Python 子进程以 UTF-8 输出，避免中文 Windows 默认 GBK 造成乱码。
    """
    run_step([sys.executable, extractor, pcap_path,
              "-o", json_out, "--tshark", tshark,
              "--start-time", "%.6f" % start_time,
              "--duration", "%.6f" % duration],
             "PHY 参数提取", env=dict(os.environ, PYTHONIOENCODING="utf-8"))


def load_ppdu_count(json_path):
    """读取 JSON 顶层 count 字段；文件无法解析时以中文错误退出。"""
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f).get("count", 0)
    except (OSError, ValueError) as exc:
        print("[错误] 无法解析 JSON 文件：%s（%s）" % (json_path, exc),
              file=sys.stderr)
        sys.exit(2)


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))

    ap = argparse.ArgumentParser(
        description="pcap -> PHY 参数 JSON -> 拼接 IQ 波形 .mat（支持三种模式，详见文件头说明）")
    ap.add_argument("input",
                    help="输入文件路径：all/extract 模式为 .pcap/.pcapng；"
                         "generate 模式为包含 PPDU PHY 参数的 .json")
    ap.add_argument("--mode", choices=("all", "extract", "generate"), default="all",
                    help="运行模式：all=pcap 一步生成波形（默认，需 tshark+MATLAB）；"
                         "extract=仅 pcap -> ppdu_phy.json（仅需 tshark）；"
                         "generate=仅 json -> 波形（仅需 MATLAB）")
    ap.add_argument("--start-time", type=float, default=0.0,
                    help="起始相对时刻（秒，pcap 首帧为 0；默认 0；仅 all/extract 有效）")
    ap.add_argument("--duration", type=float, default=0.5,
                    help="提取时长（秒；默认 0.5；仅 all/extract 有效）")
    ap.add_argument("--fs", type=int, default=160, metavar="MHz",
                    help="统一采样率（MHz），仅允许 20/40/80/160；默认 160；"
                         "仅 all/generate 有效")
    ap.add_argument("-o", "--output-dir", default=None,
                    help="输出目录（默认为本脚本所在目录下的 ppdu_waveforms 文件夹）")
    ap.add_argument("--chan-mode", choices=("first", "sum"), default="first",
                    help="多天线 (Nsts>1) 波形的单通道合成方式：first=只取第一列并 "
                         "补偿功率（默认）；sum=所有列等权求和")
    ap.add_argument("--pwr-threshold", type=float, default=50.0, metavar="dB",
                    help="低功率 PPDU 功率差阈值（dB）。以最大有效 rssi_dbm "
                         "为基准，低于基准超过该阈值的 PPDU 按 --low-pwr-mode "
                         "策略处理；默认 50（启用）；配置为 0 表示不启用")
    ap.add_argument("--low-pwr-mode", choices=("skip", "clamp"), default="skip",
                    help="低功率 PPDU 处理策略：skip=不生成该 PPDU 波形（默认）；"
                         "clamp=仍生成但 scale 按阈值钳位。"
                         "仅在提供 --pwr-threshold 时生效")
    ap.add_argument("--seed", default="default", metavar="SEED",
                    help="随机数种子：default（默认，每次运行波形数据一致）"
                         "或非负整数（自定义可复现种子）")
    ap.add_argument("--keep-json", action="store_true",
                    help="保留中间文件 ppdu_phy.json（默认运行结束后删除）")
    ap.add_argument("--tshark", default=None, help="高级：tshark 可执行文件路径")
    ap.add_argument("--matlab", default=None, help="高级：MATLAB 可执行文件路径")
    args = ap.parse_args()

    # ---- 参数校验（按模式区分输入与生效参数）----
    if not os.path.isfile(args.input):
        print("[错误] 输入文件不存在：%s" % args.input, file=sys.stderr)
        sys.exit(2)
    need_tshark = args.mode in ("all", "extract")
    need_matlab = args.mode in ("all", "generate")
    if args.mode == "generate" and not args.input.lower().endswith(".json"):
        print("[注意] generate 模式输入应为 .json 文件，当前：%s" % args.input)
    if args.mode in ("all", "extract"):
        if args.duration <= 0:
            print("[错误] --duration 必须大于 0，当前：%s" % args.duration,
                  file=sys.stderr)
            sys.exit(2)
        if args.start_time < 0:
            print("[错误] --start-time 不能为负数，当前：%s" % args.start_time,
                  file=sys.stderr)
            sys.exit(2)
    if need_matlab and args.fs not in FS_CHOICES_MHZ:
        print("[错误] --fs 必须为 20/40/80/160 之一（MHz），当前：%s" % args.fs,
              file=sys.stderr)
        sys.exit(2)
    if args.pwr_threshold < 0:
        print("[错误] --pwr-threshold 不能为负数（0 表示不启用），当前：%s"
              % args.pwr_threshold, file=sys.stderr)
        sys.exit(2)
    if args.pwr_threshold == 0 and args.low_pwr_mode != "skip":
        print("[注意] --pwr-threshold 为 0（不启用低功率处理），"
              "--low-pwr-mode 不生效。")
    if args.seed.lower() != "default":
        try:
            if int(args.seed) < 0:
                raise ValueError
        except ValueError:
            print("[错误] --seed 必须为 default 或非负整数，当前：%s" % args.seed,
                  file=sys.stderr)
            sys.exit(2)
    if args.mode == "all":
        est = args.duration * args.fs * 1e6
        if est > MAX_CONCAT_SAMPLES:
            print("[错误] 拼接波形估计 %.2e 样本（%.3f s @ %d MHz），超过 %.0e 上限。\n"
                  "       请减小 --duration 或降低 --fs。" % (est, args.duration,
                  args.fs, MAX_CONCAT_SAMPLES), file=sys.stderr)
            sys.exit(2)

    # ---- 定位依赖（按模式探测，不需要的不探测）----
    extractor = check_companions(script_dir, need_tshark, need_matlab)
    tshark = find_tshark(args.tshark) if need_tshark else None
    matlab = find_matlab(args.matlab) if need_matlab else None
    out_dir = os.path.abspath(args.output_dir if args.output_dir
                              else os.path.join(script_dir, "ppdu_waveforms"))
    os.makedirs(out_dir, exist_ok=True)
    fs_hz = args.fs * 1e6
    out_mat = os.path.join(out_dir, "ppdu_concat_fs%dMHz.mat" % args.fs)

    # ---- extract 模式：pcap -> ppdu_phy.json（直接输出到输出目录）----
    if args.mode == "extract":
        json_out = os.path.join(out_dir, "ppdu_phy.json")
        print("[提取] pcap -> PHY 参数 JSON（tshark）...")
        run_extract(tshark, extractor, args.input, json_out,
                    args.start_time, args.duration)
        count = load_ppdu_count(json_out)
        if count == 0:
            print("[错误] 指定时间窗内未提取到任何 PPDU，请调整 "
                  "--start-time/--duration。", file=sys.stderr)
            sys.exit(2)
        print("\n完成（extract 模式）！PHY 参数 JSON -> %s（%d 个 PPDU）\n"
              "将本 JSON 文件拷贝到装有 MATLAB 的机器后，运行：\n"
              "  python pcap2waveform.py <该JSON文件路径> --mode generate"
              % (json_out, count))
        return

    # ---- all / generate 模式需要生成波形 ----
    if args.mode == "generate":
        tmp_json = os.path.abspath(args.input)
        tmp_created = False
        if load_ppdu_count(tmp_json) == 0:
            print("[错误] 输入 JSON 中不含任何 PPDU：%s" % tmp_json, file=sys.stderr)
            sys.exit(2)
    else:
        # all 模式：中间 JSON 用临时文件，默认运行结束后删除
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json",
                                          prefix="ppdu_phy_")
        tmp_json = tmp.name
        tmp.close()
        tmp_created = True

    try:
        if args.mode == "all":
            # 第一步：pcap -> PHY 参数 JSON
            print("[1/2] 提取 PPDU PHY 参数（tshark）...")
            run_extract(tshark, extractor, args.input, tmp_json,
                        args.start_time, args.duration)

            # 空结果提前终止，避免无意义地启动 MATLAB
            if load_ppdu_count(tmp_json) == 0:
                print("[错误] 指定时间窗内未提取到任何 PPDU，请调整 "
                      "--start-time/--duration。", file=sys.stderr)
                sys.exit(2)

        # 第二步（all / generate 共用）：JSON -> 拼接波形 .mat
        step_tag = "[2/2]" if args.mode == "all" else "[生成]"
        print("%s 生成拼接波形（MATLAB 启动中，请耐心等待）..." % step_tag)
        run_matlab(matlab, script_dir, tmp_json, out_dir, fs_hz,
                   args.chan_mode, args.pwr_threshold, args.low_pwr_mode,
                   args.seed)

        if not os.path.isfile(out_mat):
            print("[错误] MATLAB 运行结束但未生成预期文件：%s" % out_mat,
                  file=sys.stderr)
            sys.exit(2)

        if args.mode == "all" and args.keep_json:
            keep_path = os.path.join(out_dir, "ppdu_phy.json")
            shutil.copyfile(tmp_json, keep_path)
            print("[保留] 中间文件 -> %s" % keep_path)

        # all / generate 模式均输出 .mat（MATLAB 波形）+ .txt（S16Q11 定点十六进制波形）
        out_txt = os.path.splitext(out_mat)[0] + ".txt"
        print("\n完成！输出波形文件（.mat + .txt 各一）：\n"
              "  .mat -> %s\n  .txt -> %s" % (out_mat, out_txt))
    finally:
        # 只清理 all 模式自己创建的临时 JSON；generate 模式的输入是用户文件
        if tmp_created and os.path.isfile(tmp_json):
            os.remove(tmp_json)


if __name__ == "__main__":
    main()
