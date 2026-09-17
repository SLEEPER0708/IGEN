#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_ppdu_phy.py
从本地 .pcapng 文件中提取所有 PPDU 的 PHY 层信息（基于 Wireshark/tshark），
结果以 JSON 形式保存，供后续 MATLAB 脚本生成 IQ 波形使用。

依赖：
    - 已安装 tshark（Wireshark），并可在 PATH 中找到，或用 --tshark 指定完整路径。
      Ubuntu/Debian: sudo apt install tshark
      Windows:       安装 Wireshark 后把 tshark.exe 加入 PATH

用法示例：
    python extract_ppdu_phy.py capture.pcapng
    python extract_ppdu_phy.py capture.pcapng -o ppdu_phy.json
    python extract_ppdu_phy.py capture.pcapng --limit 1000 --tshark "C:/Program Files/Wireshark/tshark.exe"
    # 按帧号范围（起始索引 + 总索引数）：提取第 101~150 帧
    python extract_ppdu_phy.py capture.pcapng --start-index 101 --count 50
    # 按时间范围（起始相对时刻 + 总时间数）：提取抓包起始后 5.0s~15.0s 内的帧
    python extract_ppdu_phy.py capture.pcapng --start-time 5.0 --duration 10
    # 两种方式可同时使用（取交集），也可再叠加 --limit 限制输出上限
    python extract_ppdu_phy.py capture.pcapng --start-index 101 --count 500 --start-time 5.0 --duration 30

输出 JSON 结构：
    {
      "source": "<pcapng 路径>",
      "filter": "<实际应用的 tshark 显示过滤器>",
      "count": <PPDU 数量>,
      "range": { <仅指定索引/时间范围时存在> },
      "ppdus": [ { ..., 见 normalise_ppdu() }, ... ]
    }
"""

import argparse
import csv
import io
import json
import os
import shutil
import subprocess
import sys

# 需要从 tshark 提取的字段（Wireshark display filter 字段名，均已对照官方参考核对）。
# 实际运行时会先用 `tshark -G fields` 探测可用字段，自动剔除旧版本不支持的字段。
FIELDS = [
    "frame.number",
    "frame.time_epoch",
    "frame.len",
    "frame.cap_len",
    # Wireshark 合成的“802.11 radio information”高层字段（最稳定）
    "wlan_radio.phy",
    "wlan_radio.frequency",
    "wlan_radio.data_rate",
    "wlan_radio.signal_dbm",
    # 802.11n (HT)
    "wlan_radio.11n.mcs_index",
    "wlan_radio.11n.bandwidth",
    "wlan_radio.11n.short_gi",
    "wlan_radio.11n.fec",
    "wlan_radio.11n.greenfield",
    "wlan_radio.11n.stbc_streams",
    # 802.11ac (VHT)
    "wlan_radio.11ac.mcs",
    "wlan_radio.11ac.bandwidth",
    "wlan_radio.11ac.nss",
    "wlan_radio.11ac.short_gi",
    "wlan_radio.11ac.fec",
    "wlan_radio.11ac.stbc",
    # 802.11ax (HE SU)
    "wlan_radio.11ax.mcs",
    "wlan_radio.11ax.bandwidth",
    "wlan_radio.11ax.short_gi",
    # 802.11b (HR/DSSS) 短前导码指示
    "wlan_radio.short_preamble",
    # radiotap 原始字段作为回退（sometime wlan_radio 不完整）
    "radiotap.length",
    "radiotap.datarate",
    "radiotap.mcs.index",
    "radiotap.mcs.bw",
    "radiotap.mcs.gi",
    "radiotap.mcs.fec",
    "radiotap.mcs.stbc",
    "radiotap.vht.mcs.0",
    "radiotap.vht.nss.0",
    "radiotap.vht.bw",
    "radiotap.vht.gi",
    "radiotap.vht.coding.0",
    # 802.11ax (HE) HE-LTF 符号大小与 GI 组合（radiotap.he.data_5）
    "radiotap.he.data_5.gi",
    "radiotap.he.data_5.ltf_symbol_size",
]

# 非 HT OFDM（802.11a/g）数据速率 -> (调制, 码率)
OFDM_RATE_MAP = {
    6.0: ("BPSK", "1/2"),
    9.0: ("BPSK", "3/4"),
    12.0: ("QPSK", "1/2"),
    18.0: ("QPSK", "3/4"),
    24.0: ("16QAM", "1/2"),
    36.0: ("16QAM", "3/4"),
    48.0: ("64QAM", "2/3"),
    54.0: ("64QAM", "3/4"),
}

# 802.11b DSSS/CCK 速率
DSSS_RATES = {1.0, 2.0, 5.5, 11.0}

# 802.11ax HE GI 枚举（radiotap.he.data_5.gi，对应 Wireshark he_gi_vals）
HE_GI_US = {0: 0.8, 1: 1.6, 2: 3.2}

# 802.11ax HE-LTF 符号大小枚举（radiotap.he.data_5.ltf_symbol_size，
# 对应 Wireshark he_ltf_symbol_size_vals；0=unknown 不映射）
HE_LTF_SIZE = {1: "1xLTF", 2: "2xLTF", 3: "4xLTF"}

# wlan_radio.phy 数字枚举值（已通过实际抓包验证：
#   frame 1 = "(4)" -> Wireshark 显示 "802.11b (HR/DSSS)"，速率 1.0 Mb/s
#   frame 6 = "(6)" -> 速率 24 Mb/s，属于 ERP-OFDM
# 仅作字符串缺失时的回退）
PHY_NUMERIC = {
    "0": "UNKNOWN",  # Unknown
    "1": "FHSS",     # 802.11 FHSS
    "2": "IR",       # 802.11 IR
    "3": "DSSS",     # 802.11 DSSS (1/2 Mbps)
    "4": "DSSS",     # 802.11b (HR/DSSS)
    "5": "OFDM",     # 802.11a (OFDM, 5 GHz)
    "6": "OFDM",     # 802.11g (ERP-OFDM, 2.4 GHz)
    "7": "HT",       # 802.11n
    "8": "VHT",      # 802.11ac
    "9": "DMG",      # 802.11ad
    "10": "S1G",     # 802.11ah
    "11": "HE",      # 802.11ax
    "12": "EHT",     # 802.11be
}


def to_int(s, default=-1):
    """把 tshark 输出字符串尽量转成 int；失败返回 default。"""
    if s is None:
        return default
    s = str(s).strip()
    if not s:
        return default
    try:
        return int(float(s))
    except ValueError:
        # 例如 "20 MHz"、"20L"、"40" 里取第一个整数
        digits = ""
        for ch in s:
            if ch.isdigit() or ch == "-":
                digits += ch
            elif digits and digits not in ("-",):
                break
        if digits in ("", "-"):
            return default
        try:
            return int(digits)
        except ValueError:
            return default


def to_float(s, default=-1.0):
    """把 tshark 输出字符串尽量转成 float；失败返回 default。"""
    if s is None:
        return default
    s = str(s).strip()
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        try:
            return float(s.split()[0])
        except (ValueError, IndexError):
            return default


def v(row, *fields):
    """返回行中第一个非空字段值，否则 ''。"""
    for f in fields:
        val = row.get(f, "").strip()
        if val:
            return val
    return ""


def parse_phy(raw):
    """把 wlan_radio.phy 的值规整为 PHY code。返回 ('HE'/'VHT'/'HT'/'OFDM'/'DSSS'/'DMG'/'EHT'/'UNKNOWN') 或 None。"""
    if not raw:
        return None
    s = raw.strip()
    low = s.lower()
    if low.isdigit():
        return PHY_NUMERIC.get(s, "UNKNOWN")
    if "ax" in low or "he" in low:
        return "HE"
    if "eht" in low or "be" in low:
        return "EHT"
    if "ac" in low:
        return "VHT"
    if "ad" in low or "dmg" in low:
        return "DMG"
    if "n" in low:
        return "HT"
    if "g" in low:
        return "OFDM"
    if "a" in low:
        return "OFDM"
    if "b" in low:
        return "DSSS"
    return "UNKNOWN"


def detect_phy_by_fields(row):
    """当 wlan_radio.phy 缺失时，按字段存在性推测 PHY code。"""
    if v(row, "wlan_radio.11ax.mcs", "wlan_radio.11ax.bandwidth"):
        return "HE"
    if v(row, "wlan_radio.11ac.mcs", "radiotap.vht.mcs.0"):
        return "VHT"
    if v(row, "wlan_radio.11n.mcs_index", "radiotap.mcs.index"):
        return "HT"
    dr = to_float(v(row, "wlan_radio.data_rate", "radiotap.datarate"))
    if dr > 0:
        if dr in DSSS_RATES:
            return "DSSS"
        return "OFDM"
    return "OFDM"


def bandwidth_mhz(row, phy_code):
    """解析信道带宽（MHz）。"""
    if phy_code == "HT":
        raw = v(row, "wlan_radio.11n.bandwidth", "radiotap.mcs.bw")
        bw = to_int(raw, -1)
        # radiotap MCS 带宽：0=20,1=40,2=20L,3=20U
        return 40 if bw in (1, 40) else (20 if bw in (0, 20, 2, 3) else -1)
    if phy_code in ("VHT", "HE"):
        raw = v(row, "wlan_radio.11ac.bandwidth", "wlan_radio.11ax.bandwidth",
                "radiotap.vht.bw")
        low = raw.lower().replace(" ", "")
        if "80+80" in low or "160" in low:
            return 160
        bw = to_int(raw, -1)
        if bw in (0, 20):
            return 20
        if bw in (1, 40):
            return 40
        if bw in (2, 80):
            return 80
        if bw in (3, 160):
            return 160
        return -1
    if phy_code == "OFDM":
        return 20
    if phy_code == "DSSS":
        return 22  # 802.11b 标称 22 MHz
    return -1


def mcs_index(row, phy_code):
    """解析 MCS 索引。"""
    if phy_code == "HT":
        return to_int(v(row, "wlan_radio.11n.mcs_index", "radiotap.mcs.index"), -1)
    if phy_code == "VHT":
        return to_int(v(row, "wlan_radio.11ac.mcs", "radiotap.vht.mcs.0"), -1)
    if phy_code == "HE":
        return to_int(v(row, "wlan_radio.11ax.mcs"), -1)
    return -1


def nss_value(row, phy_code, mcs):
    """解析空间流数 NSS。HT/HE 常缺显式 NSS，按 MCS 粗略推断。"""
    if phy_code == "VHT":
        return to_int(v(row, "wlan_radio.11ac.nss", "radiotap.vht.nss.0"), -1)
    if phy_code == "HT":
        if mcs >= 0:
            return mcs // 8 + 1
        return -1
    # HE 无高层 NSS 字段时保守取 1，后续可按 radiotap.he 扩展
    if phy_code == "HE":
        return 1
    return -1


def short_gi(row, phy_code):
    """解析是否使用短保护间隔。返回 bool。"""
    raw = ""
    if phy_code == "HT":
        raw = v(row, "wlan_radio.11n.short_gi", "radiotap.mcs.gi")
    elif phy_code == "VHT":
        raw = v(row, "wlan_radio.11ac.short_gi", "radiotap.vht.gi")
    elif phy_code == "HE":
        raw = v(row, "wlan_radio.11ax.short_gi")
    low = raw.lower()
    return low in ("1", "true", "yes", "short")


def coding_value(row, phy_code):
    """解析信道编码 BCC/LDPC。返回 'bcc' 或 'ldpc'。"""
    raw = ""
    if phy_code == "HT":
        raw = v(row, "wlan_radio.11n.fec", "radiotap.mcs.fec")
    elif phy_code == "VHT":
        raw = v(row, "wlan_radio.11ac.fec", "radiotap.vht.coding.0")
    low = raw.lower()
    if low in ("1", "ldpc"):
        return "ldpc"
    return "bcc"


def stbc_value(row, phy_code):
    """解析 STBC 流数。"""
    if phy_code == "HT":
        return to_int(v(row, "wlan_radio.11n.stbc_streams", "radiotap.mcs.stbc"), -1)
    if phy_code == "VHT":
        return to_int(v(row, "wlan_radio.11ac.stbc"), -1)
    return -1


def short_preamble_value(row, phy_code):
    """解析 802.11b/g DSSS 帧的短前导码指示。返回 True/False/None（未提供）。

    来源：wlan_radio.short_preamble（Wireshark 合成字段，仅 11b/g PLCP 含
    长短前导码信息时出现；tshark 布尔字段输出 "1"/"0"）。
    """
    if phy_code not in ("DSSS", "OFDM"):
        return None
    raw = v(row, "wlan_radio.short_preamble").strip().lower()
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    return None


def he_ltf_gi(row, phy_code):
    """解析 802.11ax 的 HE-LTF 类型与 GI 组合。

    来源（radiotap.he.data_5，字段可能为原始数值或显示字符串，两者兼容）：
      - ltf_symbol_size: 1=1x, 2=2x, 3=4x（0=unknown）
      - gi:              0=0.8us, 1=1.6us, 2=3.2us
    返回 (ltf_type, gi_us)；未知时 ltf_type=""、gi_us=-1.0。
    """
    if phy_code != "HE":
        return "", -1.0

    ltf_raw = v(row, "radiotap.he.data_5.ltf_symbol_size").strip().lower()
    if ltf_raw in ("1x", "2x", "4x"):
        ltf_type = {"1x": "1xLTF", "2x": "2xLTF", "4x": "4xLTF"}[ltf_raw]
    else:
        ltf_type = HE_LTF_SIZE.get(to_int(ltf_raw, -1), "")

    gi_raw = v(row, "radiotap.he.data_5.gi").strip().lower()
    if "0.8" in gi_raw:
        gi_us = 0.8
    elif "1.6" in gi_raw:
        gi_us = 1.6
    elif "3.2" in gi_raw:
        gi_us = 3.2
    else:
        gi_us = HE_GI_US.get(to_int(gi_raw, -1), -1.0)

    return ltf_type, gi_us


def psdu_length(row):
    """估算 PSDU（MPDU）长度：总帧长减去 radiotap 头长度。"""
    frame_len = to_int(v(row, "frame.len"), -1)
    if frame_len <= 0:
        return -1
    rt_len = to_int(v(row, "radiotap.length"), 0)
    if rt_len > 0 and rt_len < frame_len:
        return frame_len - rt_len
    return frame_len


def normalise_ppdu(row):
    """把一行 tshark 输出规整成结构化 PPDU 信息。"""
    phy_code = parse_phy(v(row, "wlan_radio.phy"))
    if phy_code in (None, "UNKNOWN"):
        phy_code = detect_phy_by_fields(row)

    mcs = mcs_index(row, phy_code) if phy_code in ("HT", "VHT", "HE") else -1
    nss = nss_value(row, phy_code, mcs)
    bw = bandwidth_mhz(row, phy_code)
    freq = to_int(v(row, "wlan_radio.frequency"), -1)
    datarate = to_float(v(row, "wlan_radio.data_rate", "radiotap.datarate"), -1.0)
    gi = short_gi(row, phy_code)
    coding = coding_value(row, phy_code) if phy_code in ("HT", "VHT") else "bcc"
    stbc = stbc_value(row, phy_code) if phy_code in ("HT", "VHT") else -1
    psdu = psdu_length(row)
    short_pre = short_preamble_value(row, phy_code)
    he_ltf, he_gi = he_ltf_gi(row, phy_code)

    band = ""
    if 0 < freq < 2500:
        band = "2.4 GHz"
    elif freq >= 2500:
        band = "5 GHz"

    modulation = ""
    code_rate = ""
    supported = True
    reason = ""

    if phy_code == "OFDM":
        mod, rate = OFDM_RATE_MAP.get(datarate, ("", ""))
        modulation, code_rate = mod, rate
        if not modulation:
            supported = False
            reason = "无法由数据速率反推调制方式"
    elif phy_code == "DSSS":
        modulation = "DSSS/CCK"
        # DSSS/CCK 自 R2015b（WLAN Toolbox 首个版本）起即被 wlanNonHTConfig 支持
        supported = True
    elif phy_code in ("DMG", "EHT"):
        supported = False
        reason = "暂不支持 %s 波形生成" % phy_code
    elif phy_code not in ("HT", "VHT", "HE"):
        supported = False
        reason = "未知 PHY 类型"

    return {
        "frame_number": to_int(v(row, "frame.number"), -1),
        "time_epoch": to_float(v(row, "frame.time_epoch"), -1.0),
        "phy_code": phy_code,           # OFDM / HT / VHT / HE / DSSS / DMG / EHT
        "phy_label": v(row, "wlan_radio.phy"),
        "band": band,
        "frequency_mhz": freq,
        "bandwidth_mhz": bw,
        "data_rate_mbps": datarate,
        "rssi_dbm": to_int(v(row, "wlan_radio.signal_dbm"), -999),
        "mcs": mcs,
        "nss": nss,
        "short_gi": gi,
        "coding": coding,
        "stbc": stbc,
        "modulation": modulation,
        "code_rate": code_rate,
        "psdu_length": psdu,
        "short_preamble": short_pre,   # 802.11b/g DSSS: true/false/null(未提供)
        "he_ltf_type": he_ltf,         # 802.11ax: "1xLTF"/"2xLTF"/"4xLTF"/""
        "he_gi_us": he_gi,             # 802.11ax: 0.8/1.6/3.2/-1
        "supported": supported,
        "reason": reason,
    }


def get_available_fields(tshark):
    """通过 tshark -G fields 探测可用字段名，返回 set 或 None（无法探测）。"""
    try:
        proc = subprocess.run(
            [tshark, "-G", "fields"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            return None
        fields = set()
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            # -G fields 每行格式：F\t<字段描述>\t<字段缩写>\t<类型>\t...
            if len(parts) >= 3 and parts[0] == "F" and parts[2]:
                fields.add(parts[2])
        return fields or None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="从 pcapng 提取 PPDU PHY 层信息")
    ap.add_argument("pcapng", help="输入 .pcapng 文件路径")
    ap.add_argument("-o", "--output", help="输出 JSON 路径（默认 <输入名>_phy.json）")
    ap.add_argument("--tshark", default="tshark", help="tshark 可执行文件路径")
    ap.add_argument("--filter", default="wlan", help="tshark 显示过滤器（默认 wlan）")
    ap.add_argument("--limit", type=int, default=0,
                    help="最多输出的 PPDU 数（0 表示不限制；作用在最终结果上）")
    ap.add_argument("--start-index", type=int, default=0,
                    help="起始索引（pcap 内帧号，从 1 开始、与 Wireshark No. 列一致，"
                         "含起始帧；0 表示不限）")
    ap.add_argument("--count", type=int, default=0,
                    help="总索引数：与起始索引构成帧号范围 [start-index, start-index+count-1]；"
                         "范围内不匹配基础过滤器的帧会被排除，实际提取数可能小于该值")
    ap.add_argument("--start-time", type=float, default=None,
                    help="起始相对时刻（秒，含；即抓包首帧为 0.0 的 frame.time_relative）"
                         "。在 Wireshark 中将时间显示格式设为 "
                         "\"Seconds Since First Captured Packet\" 即可直接读出该值")
    ap.add_argument("--duration", type=float, default=None,
                    help="总时间数（秒）：与起始时刻构成时间窗 [start-time, start-time+duration]，"
                         "含端点；需与 --start-time 连用")
    args = ap.parse_args()

    if args.start_index < 0 or args.count < 0:
        ap.error("--start-index 与 --count 不能为负数")
    if args.duration is not None:
        if args.start_time is None:
            ap.error("--duration 需要与 --start-time 一起使用")
        if args.duration < 0:
            ap.error("--duration 不能为负数")

    if shutil.which(args.tshark) is None and not os.path.isabs(args.tshark):
        print("[错误] 未在 PATH 中找到 tshark。请安装 Wireshark 或用 --tshark 指定完整路径。",
              file=sys.stderr)
        print("  Ubuntu/Debian: sudo apt install tshark", file=sys.stderr)
        sys.exit(2)

    # 探测可用字段，剔除旧版本不支持的字段
    available = get_available_fields(args.tshark)
    use_fields = FIELDS
    if available is not None:
        dropped = [f for f in FIELDS if f not in available]
        use_fields = [f for f in FIELDS if f in available]
        if not use_fields:
            # 探测结果异常（如 -G fields 格式变化）时回退，避免 -e 为空
            print("[警告] 可用字段探测结果为空，回退使用全部字段。")
            use_fields = FIELDS
        elif dropped:
            print("[注意] 当前 tshark 不支持以下字段，已自动忽略：%s" % ", ".join(dropped))

    # 组合显示过滤器：基础过滤 + 帧号范围 + 时间窗（同时指定时取交集，互不冲突）
    conds = ["(%s)" % args.filter]
    has_index_range = args.start_index > 0 or args.count > 0
    has_time_range = args.start_time is not None
    if has_index_range:
        lo = args.start_index if args.start_index > 0 else 1
        if args.count > 0:
            conds.append("(frame.number >= %d && frame.number <= %d)"
                         % (lo, lo + args.count - 1))
        else:
            conds.append("(frame.number >= %d)" % lo)
    if has_time_range:
        if args.duration is not None:
            conds.append("(frame.time_relative >= %.6f && frame.time_relative <= %.6f)"
                         % (args.start_time, args.start_time + args.duration))
        else:
            conds.append("(frame.time_relative >= %.6f)" % args.start_time)
    display_filter = " && ".join(conds)
    has_range = has_index_range or has_time_range
    if has_range:
        print("[范围] 应用显示过滤器：%s" % display_filter)

    cmd = [args.tshark, "-r", args.pcapng, "-Y", display_filter,
           "-T", "fields", "-E", "header=y", "-E", "separator=|",
           "-E", "occurrence=a"]
    # 使用索引/时间范围时不用 tshark -c 截断（其计数在“读取包数/过滤后包数”上
    # 的语义随版本有差异，可能提前停止读取导致漏帧），改由脚本自身按 --limit 截断
    if args.limit > 0 and not has_range:
        cmd += ["-c", str(args.limit)]
    for f in use_fields:
        cmd += ["-e", f]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              universal_newlines=True,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print("[错误] tshark 无法执行：%s" % args.tshark, file=sys.stderr)
        sys.exit(2)

    if proc.returncode != 0:
        print("[错误] tshark 执行失败：", file=sys.stderr)
        print(proc.stderr.strip(), file=sys.stderr)
        print("\n提示：若报 “Some fields aren't valid”，请升级 tshark/Wireshark 到较新版本。",
              file=sys.stderr)
        sys.exit(2)

    reader = csv.DictReader(io.StringIO(proc.stdout), delimiter="|")
    ppdus = []
    for row in reader:
        # 清理键名两侧空白（DictReader 一般不会，但稳妥处理）
        clean = {k.strip(): (val.strip() if val else "") for k, val in row.items()}
        ppdus.append(normalise_ppdu(clean))
    # 范围过滤模式下，--limit 在最终结果上截断（替代 tshark -c）
    if has_range and args.limit > 0:
        ppdus = ppdus[:args.limit]

    out_path = args.output or (args.pcapng.rsplit(".", 1)[0] + "_phy.json")
    payload = {
        "source": args.pcapng,
        "filter": display_filter,
        "count": len(ppdus),
        "generated_by": "extract_ppdu_phy.py",
        "ppdus": ppdus,
    }
    if has_range:
        # 记录实际应用的提取范围，便于追溯（null 表示该维度未限制）
        payload["range"] = {
            "start_index": args.start_index if args.start_index > 0 else 1,
            "index_count": args.count if args.count > 0 else None,
            "start_time": args.start_time,
            "duration": args.duration,
        }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print("已提取 %d 个 PPDU，写入 %s" % (len(ppdus), out_path))
    # 简要统计
    from collections import Counter
    stat = Counter(p["phy_code"] for p in ppdus)
    print("PHY 类型统计：%s" % {k: stat[k] for k in sorted(stat)})


if __name__ == "__main__":
    main()