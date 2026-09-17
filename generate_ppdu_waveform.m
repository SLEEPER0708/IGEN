function generate_ppdu_waveform(json_path, out_dir, fs_target, chan_mode, pwr_threshold, low_pwr_mode, fix_total_bits, fix_frac_bits, seed)
%GENERATE_PPDU_WAVEFORM 根据 PHY 层信息 JSON 生成 PPDU IQ 波形并保存为 .mat
%
%   generate_ppdu_waveform(json_path)
%   generate_ppdu_waveform(json_path, out_dir)
%   generate_ppdu_waveform(json_path, out_dir, fs_target)
%   generate_ppdu_waveform(json_path, out_dir, fs_target, chan_mode)
%   generate_ppdu_waveform(json_path, out_dir, fs_target, chan_mode, pwr_threshold, low_pwr_mode)
%   generate_ppdu_waveform(json_path, out_dir, fs_target, chan_mode, pwr_threshold, low_pwr_mode, fix_total_bits, fix_frac_bits)
%
%   输入:
%     json_path - 由 extract_ppdu_phy.py 生成的 PHY 信息 JSON 文件路径
%     out_dir   - 输出目录 (默认为当前目录下的 ppdu_waveforms)
%     fs_target - 可选，统一输出采样率 (Hz)，仅允许 20e6/40e6/80e6/160e6。
%                 省略或为空时，各 PPDU 按其原生采样率输出
%                 （OFDM 类=信道带宽，DSSS=11 MHz 码片速率）。
%     chan_mode - 可选，拼接模式下多天线 (Nsts>1) 波形的单通道构造方式：
%                 'first' - 只取第一列，并乘以 sqrt(Nsts) 使平均功率
%                           放大 Nsts 倍（默认）；
%                 'sum'   - 所有列等权（权值 1）求和，即 sum(w, 2)。
%                 仅在指定 fs_target（拼接模式）时生效。
%     pwr_threshold - 可选，功率差阈值 (dB)。以最大有效 rssi_dbm 为基准，
%                 当某 PPDU rssi_dbm 有效且低于基准超过该阈值时，按
%                 low_pwr_mode 处理；省略或为空时回退原有逻辑（不启用）。
%                 仅在指定 fs_target（拼接模式）时生效。
%     low_pwr_mode  - 低功率 PPDU 处理策略：
%                 'skip'  - 不生成该 PPDU 的波形（默认，提升脚本效率）；
%                 'clamp' - 仍生成，但 scale 钳位为 10^(-pwr_threshold/20)
%                           （真实功率差被钳平）。
%                 仅在拼接模式且提供 pwr_threshold 时生效。
%     fix_total_bits - 可选，定点输出总位宽 [2,52]，默认 16（S16Q11）。
%     fix_frac_bits  - 可选，定点输出小数位宽 [0, fix_total_bits-1]，默认 11。
%     seed       - 可选，随机数种子：'default'（默认，MATLAB 启动种子，
%                  每次运行波形数据一致）或非负整数标量（自定义可复现种子）。
%
%   输出:
%     未指定 fs_target（逐文件模式，每个受支持的 PPDU 一个 .mat 文件）:
%        ppdu_<frame_number>.mat
%           w   - 复数 IQ 基带波形 (double, 列向量)
%           fs  - 采样率 (Hz)
%           cfg - 用于 wlanWaveformGenerator 的配置对象
%           p   - 对应的 PHY 信息结构体
%     指定 fs_target（拼接模式，全部 PPDU 拼接为单个波形文件）:
%        ppdu_concat_fs<fs_target/1e6>MHz.mat
%           w              - 拼接后的复数 IQ 波形 (列向量)
%           fs             - 统一采样率 fs_target (Hz)
%           p_segment_meta - 每段元数据结构体数组：
%               frame_number/phy_code/time_epoch/rssi_dbm/scale/nsts/
%               start_sample(在 w 中的起始样本)/num_samples
%        拼接规则：以第一个 PPDU 的起始时刻为 t=0，后续 PPDU 按 time_epoch
%        相对偏移确定起始样本，相邻段空隙填 0（时间重叠时顺序紧接）；
%        各段按 10^((rssi_dbm-max(rssi_dbm))/20) 做幅度缩放以体现平均功率
%        差异（rssi 无效时不缩放，空隙为 0 无需缩放）。
%     两种模式均额外输出同名定点波形 txt 文件（与 .mat 同目录）：
%        ppdu_<frame_number>.txt / ppdu_concat_fs<fs_target/1e6>MHz.txt
%        定标 S<fix_total_bits>Q<fix_frac_bits>（默认 S16Q11），二进制补码；
%        每行一个样点，左（高）ceil(fix_total_bits/4) 个 16 进制字符为 I 路，
%        右（低）为 Q 路；文件头以 '#' 注释行注明格式与 I/Q 位置。
%
%   依赖: MATLAB WLAN Toolbox (wlanWaveformGenerator)；
%         指定 fs_target 且需 resample 重采样时另需 Signal Processing Toolbox。
%
%   说明:
%     - 支持 802.11a/g (NonHT OFDM)、802.11b (DSSS/CCK)、802.11n (HT)、
%       802.11ac (VHT)、802.11ax (HE SU)。DSSS 自 R2015b 起即被支持。
%     - EHT/DMG 暂不支持，脚本会跳过并在命令行打印原因。
%     - 波形用随机数据比特填充数据字段，PHY 参数与实际抓包一致；
%       随机种子由 seed 控制（默认 'default'，每次运行结果一致）。

    if nargin < 1 || isempty(json_path)
        error('请提供 PHY 信息 JSON 路径');
    end
    if nargin < 2 || isempty(out_dir)
        out_dir = fullfile(pwd, 'ppdu_waveforms');
    end
    if nargin < 3 || isempty(fs_target)
        fs_target = [];              % 不统一采样率，各 PPDU 按原生采样率输出
    elseif ~isscalar(fs_target) || ~ismember(fs_target, [20e6, 40e6, 80e6, 160e6])
        error('fs_target 必须为 20e6 / 40e6 / 80e6 / 160e6 之一（Hz）');
    end
    if nargin < 4 || isempty(chan_mode)
        chan_mode = 'first';         % 默认只取第一列
    elseif ~ischar(chan_mode) && ~isstring(chan_mode)
        error('chan_mode 必须为 ''first'' 或 ''sum''');
    else
        chan_mode = lower(char(chan_mode));
        if ~ismember(chan_mode, {'first', 'sum'})
            error('chan_mode 必须为 ''first''（只取第一列）或 ''sum''（所有列求和）');
        end
    end
    if nargin < 5 || isempty(pwr_threshold)
        pwr_threshold = [];          % 未提供功率差阈值，回退原有处理逻辑
    elseif ~isscalar(pwr_threshold) || ~isnumeric(pwr_threshold) || pwr_threshold <= 0
        error('pwr_threshold 必须为正数（dB），或留空表示不启用');
    end
    if nargin < 6 || isempty(low_pwr_mode)
        low_pwr_mode = 'skip';       % 默认：不生成低功率 PPDU 的波形（提升效率）
    elseif ~ischar(low_pwr_mode) && ~isstring(low_pwr_mode)
        error('low_pwr_mode 必须为 ''skip'' 或 ''clamp''');
    else
        low_pwr_mode = lower(char(low_pwr_mode));
        if ~ismember(low_pwr_mode, {'skip', 'clamp'})
            error(['low_pwr_mode 必须为 ''skip''（不生成低功率 PPDU 波形）或 ' ...
                   '''clamp''（按阈值钳位 scale 仍生成）']);
        end
    end
    if nargin < 7 || isempty(fix_total_bits)
        fix_total_bits = 16;         % 默认 S16Q11
    end
    if nargin < 8 || isempty(fix_frac_bits)
        fix_frac_bits = 11;
    end
    if ~isscalar(fix_total_bits) || ~isnumeric(fix_total_bits) || ...
            fix_total_bits < 2 || fix_total_bits > 52 || fix_total_bits ~= round(fix_total_bits)
        error('fix_total_bits 必须为 [2, 52] 内的整数（上限 52 保证补码在 double 内精确表示），当前：%s', ...
            mat2str(fix_total_bits));
    end
    if ~isscalar(fix_frac_bits) || ~isnumeric(fix_frac_bits) || ...
            fix_frac_bits < 0 || fix_frac_bits >= fix_total_bits || fix_frac_bits ~= round(fix_frac_bits)
        error('fix_frac_bits 必须为 [0, fix_total_bits-1] 内的整数，当前：%s', mat2str(fix_frac_bits));
    end
    if nargin < 9 || isempty(seed)
        seed = 'default';            % 默认种子，每次运行波形数据一致（可复现）
    end
    if isnumeric(seed)
        if ~isscalar(seed) || seed < 0 || seed ~= round(seed)
            error('seed 数值形式时必须为非负整数标量，当前：%s', mat2str(seed));
        end
    elseif ischar(seed) || isstring(seed)
        seed = char(seed);
        if ~strcmpi(seed, 'default')
            error('seed 字符串形式时仅支持 ''default''，当前：%s', seed);
        end
        seed = 'default';
    else
        error('seed 必须为 ''default'' 或非负整数标量');
    end
    rng(seed);                       % 初始化随机数发生器（psdu_bits 用）
    if ~exist(out_dir, 'dir')
        mkdir(out_dir);
    end

    if exist('wlanWaveformGenerator', 'file') ~= 2
        error('未找到 WLAN Toolbox，请安装并授权后重试。');
    end

    info = jsondecode(fileread(json_path));
    ppdus = info.ppdus;

    concat_mode = ~isempty(fs_target);
    if concat_mode
        % 功率基准：最大有效 rssi_dbm（无效值 -999 不参与）。
        % 仅拼接模式使用（scale 缩放及低功率阈值判定），故仅在此计算。
        r_all = [ppdus.rssi_dbm];
        r_valid = r_all(r_all > -200);
        if isempty(r_valid)
            rssi_ref = 0;
        else
            rssi_ref = max(r_valid);
        end
        % 内存保护：按时间跨度估计拼接总长度，超出上限直接报错
        max_concat_samples = 1e8;   % 复数 double 约 1.6 GB
        t_all = [ppdus.time_epoch];
        t_valid = t_all(t_all >= 0);
        if numel(t_valid) >= 2
            span_s = max(t_valid) - min(t_valid);
            est = span_s * fs_target;
            if est > max_concat_samples
                error(['拼接波形估计长度 %.2e 样本（时间跨度 %.3f s @ %g MHz）超过 %.0e 上限。\n' ...
                       '请用 extract_ppdu_phy.py 的 --start-index/--count 或 ' ...
                       '--start-time/--duration 缩小提取范围后再生成。'], ...
                       est, span_s, fs_target / 1e6, max_concat_samples);
            end
        end
        w_segs = {};
        seg_info = struct('frame_number', {}, 'phy_code', {}, 'time_epoch', {}, ...
                          'rssi_dbm', {}, 'scale', {}, 'nsts', {}, ...
                          'start_sample', {}, 'num_samples', {});
        t0 = [];
    end

    n_ok = 0;
    n_skip = 0;
    for i = 1:numel(ppdus)
        p = ppdus(i);
        fn = round(p.frame_number);

        % 低功率 PPDU 处理策略（仅在拼接模式且提供 pwr_threshold 时生效，
        % 逐文件模式不做任何低功率处理，保持原有逻辑）：
        % rssi 有效且低于基准 rssi_ref 超过阈值时，
        %   'skip'  - 不生成该 PPDU 的波形（默认，提升脚本效率）；
        %   'clamp' - 仍生成，但 scale 按阈值钳位为 10^(-pwr_threshold/20)，
        %             不再使用真实功率差。
        low_pwr = false;
        if concat_mode && ~isempty(pwr_threshold) && p.rssi_dbm > -200 && ...
                (rssi_ref - p.rssi_dbm) > pwr_threshold
            if strcmp(low_pwr_mode, 'skip')
                n_skip = n_skip + 1;
                fprintf('[跳过] frame %d (低功率: rssi %.1f dBm，低于基准 %.1f dB > 阈值 %.1f dB)\n', ...
                    fn, p.rssi_dbm, rssi_ref - p.rssi_dbm, pwr_threshold);
                continue;
            end
            low_pwr = true;   % clamp 模式：标记，在拼接收集时钳位 scale
        end

        try
            [cfg, err] = build_config(p);
        catch ME
            % 配置对象属性赋值时会即时校验（如非法 MCS/NSS/带宽组合），
            % 单个坏帧不应中断整个批处理
            n_skip = n_skip + 1;
            fprintf('[跳过] frame %d (配置失败): %s\n', fn, ME.message);
            continue;
        end
        if ~isempty(err)
            n_skip = n_skip + 1;
            fprintf('[跳过] frame %d: %s\n', fn, err);
            continue;
        end

        try
            bits = psdu_bits(cfg);
            [w, fs] = gen_waveform(bits, cfg, fs_target);
        catch ME
            n_skip = n_skip + 1;
            fprintf('[跳过] frame %d (生成失败): %s\n', fn, ME.message);
            continue;
        end

        if concat_mode
            % 拼接模式：收集波形段，循环结束后统一组装（见文末）
            if isempty(t0)
                t0 = p.time_epoch;   % 以第一个成功生成的 PPDU 时刻为基准
            end
            if p.rssi_dbm > -200
                if low_pwr
                    % clamp：低功率段按阈值钳位 scale（真实功率差被钳平）
                    scale = 10^(-pwr_threshold / 20);
                else
                    scale = 10^((p.rssi_dbm - rssi_ref) / 20);   % 幅度比 = 10^(dB差/20)
                end
            else
                scale = 1;             % rssi 无效时不缩放
            end
            w_segs{end+1} = combine_channels(w, chan_mode) * scale;
            seg_info(end+1) = struct('frame_number', fn, 'phy_code', p.phy_code, ...
                'time_epoch', p.time_epoch, 'rssi_dbm', p.rssi_dbm, 'scale', scale, ...
                'nsts', size(w, 2), 'start_sample', 0, 'num_samples', size(w, 1)); %#ok<AGROW>
            n_ok = n_ok + 1;
            continue;
        end

        out_file = fullfile(out_dir, sprintf('ppdu_%06d.mat', fn));
        save(out_file, 'w', 'fs', 'cfg', 'p'); %#ok<NASGU>
        txt_file = fullfile(out_dir, sprintf('ppdu_%06d.txt', fn));
        write_fixed_txt(w, fs, fix_total_bits, fix_frac_bits, txt_file);
        n_ok = n_ok + 1;
        fprintf('[生成] frame %d -> %s (%.3f us, %d samples)\n', ...
            fn, out_file, numel(w) / fs * 1e6, numel(w));
    end

    if concat_mode
        out_file = assemble_concat(w_segs, seg_info, t0, fs_target, out_dir, ...
            fix_total_bits, fix_frac_bits);
        fprintf('\n完成（拼接模式）：成功 %d 个，跳过 %d 个\n拼接波形 -> %s\n', ...
            n_ok, n_skip, out_file);
        return;
    end

    fprintf('\n完成：成功生成 %d 个，跳过 %d 个，输出目录：%s\n', n_ok, n_skip, out_dir);
end


function txt_path = write_fixed_txt(w, fs, total_bits, frac_bits, txt_path)
%WRITE_FIXED_TXT 将复数基带波形量化为定点并写出为 16 进制 txt 文件。
%   定标 S<total>Q<frac>：1 符号位 + (total-1-frac) 整数位 + frac 小数位，
%   量化值 = floor(w * 2^frac + 0.5)（加半个量化步长后截断，即数字硬件
%   实现的四舍五入，0.5 边界向 +inf 进位），饱和到 [-2^(total-1), 2^(total-1)-1]，
%   负数用二进制补码表示。
%   文件头部以 '#' 注释行注明格式与 I/Q 位置；数据区每行一个样点：
%   左（高）nhex 个 16 进制字符为 I 路，右（低）nhex 个为 Q 路，
%   nhex = ceil(total/4)，每行总 16 进制字符数 = 2*nhex（对应比特数
%   2*ceil(total/4)*4 >= 2*total）。
    nhex = ceil(total_bits / 4);
    qmax = 2^(total_bits - 1) - 1;
    qmin = -2^(total_bits - 1);
    scale = 2^frac_bits;

    % 定点量化（展开 round()，按数字硬件实现逻辑）：
    %   量化步长 Delta = 2^-frac_bits，缩放后 x' = w / Delta = w * 2^frac_bits；
    %   四舍五入 = 加半个量化步长（0.5）后向下截断 floor()，0.5 边界向 +inf 进位；
    %   硬件上加 0.5 即在截断位最高位补 1，随后直接舍弃小数位，无需符号分支。
    %   实部/虚部分别处理（min/max 对复数按模值比较，不能整体操作）。
    iu = min(max(floor(real(w) * scale + 0.5), qmin), qmax);
    qu = min(max(floor(imag(w) * scale + 0.5), qmin), qmax);
    % 二进制补码：mod 将负数映射到无符号区间
    iu = mod(iu, 2^total_bits);
    qu = mod(qu, 2^total_bits);

    fid = fopen(txt_path, 'w');
    if fid < 0
        error('无法写出定点波形文件：%s', txt_path);
    end
    cleanup = onCleanup(@() fclose(fid));

    % 头部注明格式与 I/Q 位置，避免使用时 IQ 取反
    fprintf(fid, '# PPDU fixed-point IQ waveform\n');
    fprintf(fid, '# format : S%dQ%d (total=%d bits, frac=%d bits, signed, two''s complement)\n', ...
        total_bits, frac_bits, total_bits, frac_bits);
    fprintf(fid, '# fs     : %d Hz\n', round(fs));
    fprintf(fid, '# samples: %d\n', numel(w));
    fprintf(fid, '# layout : each line = <I><Q>, LEFT/HIGH %d hex chars = I, RIGHT/LOW %d hex chars = Q\n', ...
        nhex, nhex);

    % 分块写出，避免一次性构建超大字符数组
    fmt = sprintf('%%0%dX%%0%dX\\n', nhex, nhex);
    N = numel(w);
    blk = 1e6;
    for s = 1:blk:N
        e = min(s + blk - 1, N);
        data = [iu(s:e).'; qu(s:e).'];
        fprintf(fid, fmt, data);
    end
end


function w1 = combine_channels(w, chan_mode)
%COMBINE_CHANNELS 将多天线波形 w (N×Nsts) 构造为单通道波形 (N×1)。
%   'first' - 只取第一列，并乘以 sqrt(Nsts) 使平均功率放大 Nsts 倍
%             （等效补偿多天线总功率；功率放大 Nsts 对应幅度 sqrt(Nsts)）；
%   'sum'   - 所有列等权（权值 1）求和：sum(w, 2)。
    nsts = size(w, 2);
    if nsts == 1
        w1 = w;
        return;
    end
    switch chan_mode
        case 'first'
            w1 = w(:, 1) * sqrt(nsts);
        case 'sum'
            w1 = sum(w, 2);
    end
end


function out_file = assemble_concat(w_segs, seg_info, t0, fs, out_dir, fix_total_bits, fix_frac_bits)
%ASSEMBLE_CONCAT 将各 PPDU 波形段按 time_epoch 相对时刻拼接为单个波形。
%   以第一个 PPDU 的起始时刻为 t=0；相邻段之间的空隙填 0；
%   若下一段理论起点早于当前末尾（时间重叠），则顺序紧接放置。
    n = numel(w_segs);
    if n == 0
        error('没有可拼接的 PPDU 波形');
    end
    starts = zeros(n, 1);
    cursor = 1;                      % 当前写入位置（样本索引，1 起始）
    for k = 1:n
        t = seg_info(k).time_epoch;
        if t >= 0 && t0 >= 0
            s = round((t - t0) * fs) + 1;
        else
            s = cursor;              % 时间戳无效时顺序紧接
        end
        if s < cursor
            s = cursor;              % 时间重叠时顺序紧接
        end
        starts(k) = s;
        cursor = s + numel(w_segs{k});
    end
    w = zeros(cursor - 1, 1);
    for k = 1:n
        idx = starts(k):(starts(k) + numel(w_segs{k}) - 1);
        w(idx) = w_segs{k};
        seg_info(k).start_sample = starts(k);
    end
    out_file = fullfile(out_dir, sprintf('ppdu_concat_fs%gMHz.mat', fs / 1e6));
    p_segment_meta = seg_info; %#ok<NASGU>
    save(out_file, 'w', 'fs', 'p_segment_meta');
    txt_file = fullfile(out_dir, sprintf('ppdu_concat_fs%gMHz.txt', fs / 1e6));
    write_fixed_txt(w, fs, fix_total_bits, fix_frac_bits, txt_file);
    fprintf('[拼接] %d 段波形，总时长 %.3f ms（%d samples @ %g MHz）\n', ...
        n, (cursor - 1) / fs * 1e3, cursor - 1, fs / 1e6);
end


function [cfg, err] = build_config(p)
%BUILD_CONFIG 依据 PHY 信息结构体构造对应的 WLAN 配置对象。
    cfg = [];
    err = '';

    if ~p.supported
        err = trim(p.reason, '暂不支持的 PHY 类型');
        return;
    end

    switch p.phy_code
        case 'OFDM'
            cfg = wlanNonHTConfig;
            mcs = nonht_ofdm_mcs(p);
            if mcs < 0
                err = '非 HT OFDM 缺少调制/码率信息';
                return;
            end
            cfg.Modulation = 'OFDM';
            cfg.MCS = mcs;
            cfg.ChannelBandwidth = 'CBW20';
            if p.psdu_length >= 1
                cfg.PSDULength = clamp(p.psdu_length, 1, 4095);
            end

        case 'DSSS'
            % 802.11b/g DSSS/CCK（自 R2015b WLAN Toolbox 首个版本起支持）
            cfg = wlanNonHTConfig;
            cfg.Modulation = 'DSSS';
            r = p.data_rate_mbps;
            if abs(r - 1) < 0.01,        cfg.DataRate = '1Mbps';
            elseif abs(r - 2) < 0.01,    cfg.DataRate = '2Mbps';
            elseif abs(r - 5.5) < 0.01,  cfg.DataRate = '5.5Mbps';
            elseif abs(r - 11) < 0.01,   cfg.DataRate = '11Mbps';
            else
                err = sprintf('暂不支持的 DSSS 数据速率: %g Mbps', r);
                return;
            end
            % 短前导码（1Mbps 仅允许长前导码）；short_preamble 为 true/false/NaN(未提供)
            if isfield(p, 'short_preamble') && islogical(p.short_preamble) && ...
                    p.short_preamble && ~strcmp(cfg.DataRate, '1Mbps')
                cfg.Preamble = 'Short';
            else
                cfg.Preamble = 'Long';
            end
            if p.psdu_length >= 1
                cfg.PSDULength = clamp(p.psdu_length, 1, 4095);
            end

        case 'HT'
            cfg = wlanHTConfig;
            cfg.MCS = clamp(p.mcs, 0, 31);
            cfg.ChannelBandwidth = ht_bw(p.bandwidth_mhz);
            cfg.GuardInterval = gi_str(p);
            cfg.ChannelCoding = coding_str(p);
            nsts = max(1, p.nss);               % p.nss 未知时为 -1，取 1
            if p.stbc >= 1
                nsts = nsts + 1;
            end
            cfg.NumSpaceTimeStreams = clamp(nsts, 1, 4);
            cfg.NumTransmitAntennas = cfg.NumSpaceTimeStreams;
            if p.psdu_length >= 1
                cfg.PSDULength = clamp(p.psdu_length, 1, 65535);
            end

        case 'VHT'
            cfg = wlanVHTConfig;
            cfg.MCS = clamp(p.mcs, 0, 9);
            cfg.ChannelBandwidth = vht_bw(p.bandwidth_mhz);
            cfg.GuardInterval = gi_str(p);
            cfg.ChannelCoding = coding_str(p);
            nsts = max(1, p.nss);
            if p.stbc >= 1
                nsts = nsts + 1;
            end
            cfg.NumSpaceTimeStreams = clamp(nsts, 1, 8);
            cfg.NumTransmitAntennas = cfg.NumSpaceTimeStreams;
            if p.psdu_length >= 1
                cfg.APEPLength = clamp(p.psdu_length, 1, 1048575);
            end

        case 'HE'
            cfg = wlanHESUConfig;
            cfg.MCS = clamp(p.mcs, 0, 11);
            cfg.ChannelBandwidth = vht_bw(p.bandwidth_mhz);
            cfg.LTFType = he_ltf(p);
            cfg.GuardInterval = he_gi(p, cfg.LTFType);
            cfg.ChannelCoding = coding_str(p);
            nsts = max(1, p.nss);
            cfg.NumSpaceTimeStreams = clamp(nsts, 1, 4);
            cfg.NumTransmitAntennas = cfg.NumSpaceTimeStreams;
            if p.psdu_length >= 1
                cfg.APEPLength = clamp(p.psdu_length, 1, 1048575);
            end

        otherwise
            err = sprintf('未知 PHY code: %s', p.phy_code);
    end
end


function bw = ht_bw(mhz)
    if mhz == 40
        bw = 'CBW40';
    else
        bw = 'CBW20';
    end
end


function bw = vht_bw(mhz)
    switch mhz
        case 160, bw = 'CBW160';
        case 80,  bw = 'CBW80';
        case 40,  bw = 'CBW40';
        otherwise, bw = 'CBW20';
    end
end


function gi = gi_str(p)
    if p.short_gi
        gi = 'Short';
    else
        gi = 'Long';
    end
end


function ltf = he_ltf(p)
%HE_LTF 依据提取到的 he_ltf_type 设置 HE-LTF 类型，未知时默认 1xLTF。
    if isfield(p, 'he_ltf_type') && any(strcmpi(p.he_ltf_type, {'1xLTF', '2xLTF', '4xLTF'}))
        ltf = p.he_ltf_type;
    else
        ltf = '1xLTF';
    end
end


function gi = he_gi(p, ltf)
%HE_GI 依据提取到的 he_gi_us 设置 HE GI（单位 us），未知时默认 0.8。
%   IEEE 802.11ax 组合约束：1xLTF 仅允许 0.8us；2xLTF 允许 0.8/1.6us；
%   4xLTF 允许 0.8/1.6/3.2us。非法组合自动回退到该 LTF 下最大允许 GI。
    if isfield(p, 'he_gi_us') && ismember(p.he_gi_us, [0.8, 1.6, 3.2])
        gi = p.he_gi_us;
    else
        gi = 0.8;
    end
    if strcmpi(ltf, '1xLTF')
        gi = 0.8;
    elseif strcmpi(ltf, '2xLTF') && gi == 3.2
        gi = 1.6;
    end
end


function c = coding_str(p)
    if strcmpi(p.coding, 'ldpc')
        c = 'LDPC';
    else
        c = 'BCC';
    end
end


function v = clamp(x, lo, hi)
    % 把数值限制在 [lo, hi] 并取整；x<0 视为“从默认取”，但调用处已给 lo 兜底。
    if isempty(x) || x < lo
        x = lo;
    end
    v = round(min(max(x, lo), hi));
end


function [w, fs] = gen_waveform(bits, cfg, fs_target)
%GEN_WAVEFORM 生成 PPDU 波形并按需统一输出采样率。
%   fs_target 为空时按原生采样率输出（OFDM 类=信道带宽，DSSS=11 MHz）。
%   否则输出采样率统一为 fs_target：
%     - OFDM 类格式且 fs_target 为原生采样率整数倍时，优先使用
%       wlanWaveformGenerator 的 OversamplingFactor（标准内置，无插值失真）；
%     - 其余情况（DSSS 11 MHz 或降采样）原生生成后用 resample 重采样，
%       需要 Signal Processing Toolbox。
%   DSSS 波形统一施加 45° 相位旋转：1 Mbps (DBPSK) 原生波形为纯实数
%   （Q 路为 0），旋转后 I/Q 两路均分能量；差分相位关系不变，对
%   DQPSK/CCK 同样旋转以保持所有 DSSS 段一致。
    fs_native = sample_rate(cfg);
    is_dsss = isa(cfg, 'wlanNonHTConfig') && strcmpi(cfg.Modulation, 'DSSS');

    if isempty(fs_target) || fs_target == fs_native
        w = wlanWaveformGenerator(bits, cfg);
        fs = fs_native;
    else
        ratio = fs_target / fs_native;
        if ~is_dsss && ratio >= 1 && abs(ratio - round(ratio)) < 1e-9
            % OversamplingFactor 仅适用于非 DSSS 格式（见 wlanWaveformGenerator 文档）
            w = wlanWaveformGenerator(bits, cfg, 'OversamplingFactor', round(ratio));
        else
            if exist('resample', 'file') ~= 2
                error('需要 Signal Processing Toolbox 的 resample 函数，将 %g MHz 重采样到 %g MHz', ...
                    fs_native / 1e6, fs_target / 1e6);
            end
            w = wlanWaveformGenerator(bits, cfg);
            [pup, qdown] = rat(fs_target / fs_native);
            w = resample(w, pup, qdown);
        end
        fs = fs_target;
    end

%     if is_dsss
%         w = w * exp(1i * pi / 4);
%     end
end


function bits = psdu_bits(cfg)
%PSDU_BITS 生成与 PSDU 长度匹配的随机数据比特（每次运行不同）。
%   比特数超过所需时函数自动截取，不足时自动循环，因此长度不必严格精确。
    if isprop(cfg, 'PSDULength')
        n = cfg.PSDULength;
    elseif isprop(cfg, 'APEPLength')
        n = cfg.APEPLength;
    else
        n = 1024;
    end
    bits = randi([0 1], 8 * max(1, round(n)), 1, 'int8');
end


function mcs = nonht_ofdm_mcs(p)
%NONHT_OFDM_MCS 由调制方式+码率映射 R2023a+ 的 wlanNonHTConfig MCS(0~7)。
    key = sprintf('%s|%s', p.modulation, p.code_rate);
    switch key
        case 'BPSK|1/2',  mcs = 0;
        case 'BPSK|3/4',  mcs = 1;
        case 'QPSK|1/2',  mcs = 2;
        case 'QPSK|3/4',  mcs = 3;
        case '16QAM|1/2', mcs = 4;
        case '16QAM|3/4', mcs = 5;
        case '64QAM|2/3', mcs = 6;
        case '64QAM|3/4', mcs = 7;
        otherwise,        mcs = -1;
    end
end


function fs = sample_rate(cfg)
    % wlanWaveformGenerator 输出基带波形的采样率由信道带宽决定。
    switch class(cfg)
        case 'wlanNonHTConfig'
            if isprop(cfg, 'Modulation') && strcmpi(cfg.Modulation, 'DSSS')
                % DSSS/CCK 波形采样率等于码片速率 11 MHz
                fs = 11e6;
                return;
            end
            switch cfg.ChannelBandwidth
                case 'CBW5',  fs = 5e6;
                case 'CBW10', fs = 10e6;
                otherwise,     fs = 20e6;
            end
        case 'wlanHTConfig'
            fs = 40e6 * strcmp(cfg.ChannelBandwidth, 'CBW40') + ...
                 20e6 * strcmp(cfg.ChannelBandwidth, 'CBW20');
        case {'wlanVHTConfig', 'wlanHESUConfig'}
            switch cfg.ChannelBandwidth
                case 'CBW160', fs = 160e6;
                case 'CBW80',  fs = 80e6;
                case 'CBW40',  fs = 40e6;
                otherwise,      fs = 20e6;
            end
        otherwise
            fs = 20e6;
    end
end


function s = trim(s, fallback)
    if isempty(s)
        s = fallback;
    end
end