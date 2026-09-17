# pcap2waveform.py 用户使用手册

## 1. 工具简介

`pcap2waveform.py` 将 Wireshark 抓包文件（.pcap/.pcapng）中的 WLAN PPDU 一键转换为基带 IQ 波形文件，用于后续仿真或硬件验证。

处理链路：

```
pcap 文件 --(tshark 提取 PHY 参数)--> ppdu_phy.json --(MATLAB 生成波形)--> 拼接 IQ 波形 (.mat + .txt)
```

**支持的 PHY 规格**：802.11a/g（OFDM）、802.11b（DSSS/CCK）、802.11n（HT）、802.11ac（VHT）、802.11ax（HE SU）。
EHT（802.11be）/ DMG（802.11ad）暂不支持，对应 PPDU 会被跳过并在命令行打印原因。

**输出特点**：

- 所有 PPDU 按抓包时刻顺序**拼接为单个波形**，PPDU 之间的空隙填 0；
- 各 PPDU 按其 RSSI 相对最大功率做幅度缩放，体现真实功率差异；
- 同时输出 `.mat`（MATLAB 复数波形）和 `.txt`（定点十六进制波形，可直接用于数字硬件仿真）。

## 2. 环境与依赖

| 依赖 | 版本要求 | 需要的运行模式 |
|---|---|---|
| Python | 3.6 及以上（仅标准库，无需安装第三方包） | 所有模式 |
| tshark（Wireshark 自带） | 无特殊要求 | all / extract |
| MATLAB + WLAN Toolbox | 建议 R2019a 及以上（低于 R2019a 时自动回退兼容调用形式） | all / generate |
| Signal Processing Toolbox | 仅 DSSS（802.11b）波形重采样时需要 | all / generate |

**文件部署**：`pcap2waveform.py`、`extract_ppdu_phy.py`、`generate_ppdu_waveform.m` 三个文件默认放在**同一目录**。

支持分机器部署（两台机器各装一部分依赖）：

- 仅有 tshark 的机器：拷贝 `pcap2waveform.py` + `extract_ppdu_phy.py`，用 extract 模式；
- 仅有 MATLAB 的机器：拷贝 `pcap2waveform.py` + `generate_ppdu_waveform.m`，用 generate 模式。

## 3. 快速上手

最常用的一条命令（默认提取前 0.5 秒、160 MHz 采样率）：

```bash
python pcap2waveform.py capture.pcapng
```

指定时间窗与采样率：

```bash
python pcap2waveform.py capture.pcapng --start-time 1.0 --duration 0.2 --fs 80
```

运行结束后，输出位于脚本所在目录下的 `ppdu_waveforms` 文件夹：

```
ppdu_waveforms/
  ppdu_concat_fs160MHz.mat   <- MATLAB 复数 IQ 波形
  ppdu_concat_fs160MHz.txt   <- 定点十六进制 IQ 波形（S16Q11）
```

> Linux 服务器上若 `python` 指向 Python 2，请改用 `python3`。

## 4. 三种运行模式

通过 `--mode` 选择，默认为 `all`：

| 模式 | 功能 | 适用场景 | 所需依赖 |
|---|---|---|---|
| `all`（默认） | pcap 一步生成波形 | 本机同时安装 tshark 和 MATLAB | tshark + MATLAB |
| `extract` | 仅 pcap → ppdu_phy.json | 本机只有 tshark；json 拷到 MATLAB 机器上再生成 | 仅 tshark |
| `generate` | 仅 ppdu_phy.json → 波形 | 本机只有 MATLAB；json 由其他机器的 extract 模式生成 | 仅 MATLAB |

分两步跨机使用的完整流程：

```bash
:: 第一步：在抓包/有 tshark 的机器上提取（json 输出到输出目录）
python pcap2waveform.py capture.pcapng --mode extract

:: 第二步：把 ppdu_phy.json 拷贝到有 MATLAB 的机器后生成波形
python pcap2waveform.py ppdu_phy.json --mode generate
```

## 5. 命令行参数详解

| 参数 | 默认值 | 生效模式 | 说明 |
|---|---|---|---|
| `input`（位置参数） | 必填 | 所有 | all/extract 模式为 .pcap/.pcapng；generate 模式为 PHY 参数 .json |
| `--mode` | `all` | 所有 | 运行模式：`all` / `extract` / `generate` |
| `--start-time 秒` | `0.0` | all / extract | 起始相对时刻（秒），**以 pcap 首帧为 0**（即 Wireshark 的 `frame.time_relative`），不能为负 |
| `--duration 秒` | `0.5` | all / extract | 提取时长（秒），必须大于 0。时间窗为 `[start-time, start-time+duration]`，含端点 |
| `--fs MHz` | `160` | all / generate | 统一采样率（MHz），仅允许 `20` / `40` / `80` / `160` |
| `-o, --output-dir 路径` | 脚本目录下 `ppdu_waveforms` | 所有 | 输出目录，不存在时自动创建 |
| `--chan-mode` | `first` | all / generate | 多天线（Nsts>1）波形单通道合成方式：`first`=只取第一列并做功率补偿（×√Nsts）；`sum`=所有列等权求和 |
| `--pwr-threshold dB` | `50` | all / generate | 低功率 PPDU 功率差阈值（dB）。以最大有效 RSSI 为基准，低于基准超过该阈值的 PPDU 按 `--low-pwr-mode` 处理；`0` 表示不启用 |
| `--low-pwr-mode` | `skip` | all / generate | 低功率 PPDU 处理策略：`skip`=不生成该 PPDU 波形（效率高）；`clamp`=仍生成但功率差按阈值钳位。仅在 `--pwr-threshold` 非 0 时生效 |
| `--seed` | `default` | all / generate | 随机数种子：`default` 每次运行波形数据一致（可复现）；或输入非负整数使用自定义可复现种子 |
| `--keep-json` | 不保留 | all | 保留中间文件 ppdu_phy.json（复制到输出目录）；默认运行结束后删除 |
| `--tshark 路径` | 自动探测 | 高级 | tshark 可执行文件完整路径 |
| `--matlab 路径` | 自动探测 | 高级 | MATLAB 可执行文件完整路径（多版本并存时自动选最新） |

**关于 `--start-time` 的取值**：在 Wireshark 中菜单 *View → Time Display Format* 选择 *Seconds Since First Captured Packet*，时间列显示的值即为 `frame.time_relative`，可直接读出目标 PPDU 的起始时刻。

**关于 `--seed`**：波形的数据字段由随机比特填充（PHY 参数与真实抓包一致）。种子只影响数据比特内容，不影响速率/MCS/带宽等 PHY 参数。默认 `default` 下两次运行的输出文件完全一致。

## 6. 输出文件说明

### 6.1 ppdu_concat_fs\<N\>MHz.mat

MATLAB 数据文件，包含三个变量：

| 变量 | 说明 |
|---|---|
| `w` | 拼接后的复数基带 IQ 波形（列向量），采样率为 `fs` |
| `fs` | 统一采样率（Hz），即 `--fs` 换算值 |
| `p_segment_meta` | 每个 PPDU 段的元数据结构体数组，字段如下 |

`p_segment_meta` 字段：

| 字段 | 说明 |
|---|---|
| `frame_number` | PPDU 在 pcap 中的帧号（与 Wireshark No. 列一致） |
| `phy_code` | PHY 类型：`OFDM` / `DSSS` / `HT` / `VHT` / `HE` |
| `time_epoch` | PPDU 抓包时刻（Unix epoch 秒） |
| `rssi_dbm` | 接收功率（dBm）；`-999` 表示抓包未提供 |
| `scale` | 该段幅度缩放系数，`10^((rssi-max(rssi))/20)`；clamp 模式下为 `10^(-阈值/20)` |
| `nsts` | 空时流数 |
| `start_sample` | 该段在 `w` 中的起始样点索引（1 起始） |
| `num_samples` | 该段样点数 |

利用 `start_sample` 和 `num_samples` 可从拼接波形中精确切出任意单个 PPDU：

```matlab
S = load('ppdu_concat_fs160MHz.mat');
k = 1;  % 第 k 个 PPDU
seg = S.w(S.p_segment_meta(k).start_sample : ...
          S.p_segment_meta(k).start_sample + S.p_segment_meta(k).num_samples - 1);
```

### 6.2 ppdu_concat_fs\<N\>MHz.txt

与 .mat 同波形的**定点十六进制**版本，供数字硬件仿真直接读取：

- 默认定标 **S16Q11**（共 16 bit：1 符号位 + 4 整数位 + 11 小数位），有符号二进制补码；
- 每行一个样点，共 8 个十六进制字符：**左（高）4 位为 I 路，右（低）4 位为 Q 路**；
- 文件头以 `#` 注释行注明格式、采样率、样点数与 I/Q 位置，读取时请跳过 `#` 行。

示例：

```
# PPDU fixed-point IQ waveform
# format : S16Q11 (total=16 bits, frac=11 bits, signed, two's complement)
# fs     : 160000000 Hz
# samples: 123456
# layout : each line = <I><Q>, LEFT/HIGH 4 hex chars = I, RIGHT/LOW 4 hex chars = Q
01A3FE52
FFEF0120
...
```

> 如需自定义位宽（总位宽 2~52、小数位宽可调），请直接调用 `generate_ppdu_waveform.m` 的第 7/8 个参数（`fix_total_bits` / `fix_frac_bits`）；`pcap2waveform.py` 固定使用 S16Q11。

### 6.3 ppdu_phy.json（中间文件）

extract 模式的输出，或 all 模式加 `--keep-json` 时保留在输出目录。顶层字段：

| 字段 | 说明 |
|---|---|
| `source` | 源 pcap 文件路径 |
| `filter` | 实际应用的 tshark 显示过滤器 |
| `count` | 提取到的 PPDU 数量 |
| `range` | 提取范围信息（仅指定范围时存在） |
| `ppdus` | PPDU 数组，单条字段见下表 |

单条 PPDU 主要字段：

| 字段 | 说明 |
|---|---|
| `frame_number` / `time_epoch` | 帧号 / 抓包时刻（Unix epoch 秒） |
| `phy_code` / `phy_label` | PHY 类型（映射值 / Wireshark 原始标签） |
| `band` / `frequency_mhz` / `bandwidth_mhz` | 频段 / 中心频率 / 带宽（MHz） |
| `data_rate_mbps` / `mcs` / `nss` | 标称速率 / MCS / 空时流数 |
| `short_gi` / `coding` / `stbc` | 短 GI / 编码方式 / STBC |
| `modulation` / `code_rate` / `psdu_length` | 调制方式 / 码率 / PSDU 字节数 |
| `rssi_dbm` | 接收功率（dBm），`-999` 表示未提供 |
| `short_preamble` | 802.11b/g DSSS 短前导码指示（true/false/null） |
| `he_ltf_type` / `he_gi_us` | 802.11ax 的 HE-LTF 类型 / GI（µs） |
| `supported` / `reason` | 是否支持波形生成 / 不支持时的原因 |

## 7. 波形拼接规则（all / generate 模式）

- **时间位置**：以第一个成功生成的 PPDU 时刻为基准，各 PPDU 按 `time_epoch` 相对偏移放置，PPDU 之间的空隙填 0；
- **功率缩放**：以最大有效 RSSI 为基准（缩放 1），其余 PPDU 按 `10^((rssi-max)/20)` 缩放，空隙不缩放；
- **低功率处理**（默认启用，阈值 50 dB）：低于基准超过阈值的 PPDU 默认跳过不生成；选 `clamp` 时仍生成但功率差被钳位到阈值；
- **内存保护**：拼接总长度超过 1e8 样点（复数 double 约 1.6 GB）时报错终止，此时请减小 `--duration` 或降低 `--fs`。

## 8. 典型使用场景

### 8.1 Windows 本机一键生成

```bash
python pcap2waveform.py capture.pcapng --start-time 0.5 --duration 1.0 --fs 160
```

### 8.2 Linux 服务器（指定 MATLAB 路径）

```bash
python3 pcap2waveform.py ppdu_phy.json --mode generate --matlab /opt/eda/matlab_r2022b/bin/matlab
```

### 8.3 跨机分工（抓包机无 MATLAB）

```bash
:: 抓包机（有 tshark）
python pcap2waveform.py capture.pcapng --mode extract -o D:\out
:: 拷贝 D:\out\ppdu_phy.json 到仿真机后
python pcap2waveform.py ppdu_phy.json --mode generate --fs 80
```

### 8.4 可复现性验证（自定义种子 + 保留中间文件）

```bash
python pcap2waveform.py capture.pcapng --seed 12345 --keep-json
```

## 9. 常见问题（FAQ）

**Q1：提示"未找到 tshark / 未找到 MATLAB"？**
用 `--tshark` / `--matlab` 指定可执行文件完整路径，例如：

```bash
python pcap2waveform.py capture.pcapng --tshark "C:\Program Files\Wireshark\tshark.exe" --matlab "C:\Program Files\MATLAB\R2025a\bin\matlab.exe"
```

**Q2：提示 MATLAB 版本低于 R2019a 的警告？**
`-batch` 选项需 R2019a+，脚本会自动回退到旧版 `-r` 调用形式，一般仍可运行；但过旧的 WLAN Toolbox 可能不支持 HE/DSSS 波形生成，建议升级或用 `--matlab` 指定新版本。

**Q3：Linux 上运行报 `SyntaxError`（print 语句错误）？**
`python` 命令指向了 Python 2，请改用 `python3` 执行脚本。

**Q4：提示"指定时间窗内未提取到任何 PPDU"？**
时间窗内没有 WLAN 帧。请在 Wireshark 中打开 pcap，将时间显示格式设为 *Seconds Since First Captured Packet*，确认目标 PPDU 的实际时刻后调整 `--start-time` / `--duration`。

**Q5：提示拼接波形超过 1e8 样点上限？**
提取时长过长或采样率过高。减小 `--duration`，或用 `--fs 80` 等更低采样率；也可以分多个时间窗分别提取生成。

**Q6：命令行出现"[跳过] frame N ..."是什么原因？**
三种常见原因：该帧为 EHT/DMG（暂不支持）；PHY 参数组合非法（如 MCS/NSS/带宽组合被 MATLAB 校验拒绝）；低功率 PPDU 被 `skip` 策略过滤（可用 `--low-pwr-mode clamp` 改为钳位生成，或 `--pwr-threshold 0` 关闭该功能）。

**Q7：802.11b（DSSS）波形生成报重采样相关错误？**
DSSS 原始码片速率为 11 MHz，统一到 20/40/80/160 MHz 需要 Signal Processing Toolbox 的 `resample` 函数，请确认该工具箱已安装。

**Q8：子进程输出中文乱码？**
脚本已内置 UTF-8/GBK 自适应解码，正常情况下不会乱码；如仍遇到，请确认终端代码页（Windows 可执行 `chcp 65001` 切换 UTF-8）。

**Q9：generate 模式提示"无法解析 JSON 文件"？**
输入 JSON 已损坏或不是本工具链生成的文件。请用 extract 模式重新生成，或检查文件内容是否完整。

## 10. 退出码

| 退出码 | 含义 |
|---|---|
| `0` | 成功 |
| `2` | 失败（参数错误、依赖缺失、子进程执行失败等，错误信息已打印到控制台） |
