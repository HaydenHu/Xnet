#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Xnet - KiCad 10 Differential Pair Length Calculator Plugin
实时查看计算差分等长，自动串联电阻追踪完整 Xnet，电阻等效长度 = 两焊盘中心距
"""

import os
import re
import wx
import wx.grid
from pcbnew import ActionPlugin, GetBoard, GetUserUnits, PCB_IU_PER_MM, EDA_UNITS_MM, EDA_UNITS_MILS, EDA_UNITS_INCH
import math

DEFAULT_RESISTOR_EQ_MM = 0.8  # 未知封装默认的焊盘中心距

# 等长容差(mm)
TOLERANCE_GOOD = 0.5
TOLERANCE_WARNING = 2.0

# 状态值
STATUS_PASS = '√ 达标'
STATUS_WARN = '~ 需调整'
STATUS_FAIL = '✗ 超差'

# 预编译正则
_RE_RESISTOR_REF = re.compile(r'^R\d')
_RE_RES_VALUE = re.compile(r'^(\d+(?:\.\d+)?)\s*([kKmM]?)[ΩR]?\s*$')


# ═══════════════════════════════════════════════════════════════════════
#  网络名解析工具
# ═══════════════════════════════════════════════════════════════════════

def _parse_diff_net(name):
    """解析差分网络名 -> (base, side, pattern)

    - base: 去掉 P/N 后缀后的基名
    - side: 'P' | 'N' | 'POS' | 'NEG' | None
    - pattern: 原始后缀格式('_P','P','+','_POS'等)
    """
    if not name:
        return name, None, None

    rules = [
        (re.compile(r'^(.+)_([Pp])$'),   '_P',   'P'),
        (re.compile(r'^(.+)_([Nn])$'),   '_N',   'N'),
        (re.compile(r'^(.+)_(POS)$'),    '_POS', 'POS'),
        (re.compile(r'^(.+)_(NEG)$'),    '_NEG', 'NEG'),
        (re.compile(r'^(.+?)([+])$'),    '+',    'P'),
        (re.compile(r'^(.+?)([-])$'),    '-',    'N'),
        (re.compile(r'^(.+)_([+])$'),    '_+',   'P'),
        (re.compile(r'^(.+)_([-])$'),    '_-',   'N'),
        (re.compile(r'^(.+?)([Pp])$'),   'P',    'P'),
        (re.compile(r'^(.+?)([Nn])$'),   'N',    'N'),
    ]

    for regex, pat, val in rules:
        m = regex.match(name)
        if m and len(m.group(1)) > 0:
            return m.group(1), val, pat
    return name, None, None


def _make_opposite_name(base, side, pattern):
    """根据基名和模式生成对侧网络名"""
    out = 'N' if side in ('P', 'POS', '+') else 'P'

    m = {
        '_P':   f'{base}_N',
        '_N':   f'{base}_P',
        'P':    f'{base}N',
        'N':    f'{base}P',
        '+':    f'{base}-',
        '-':    f'{base}+',
        '_+':   f'{base}-',
        '_-':   f'{base}+',
        '_POS': f'{base}_NEG',
        '_NEG': f'{base}_POS',
    }
    return m.get(pattern, f'{base}_{out}')


def _pad_distance(fp):
    """计算电阻两焊盘中心距离(mm)"""
    pads = list(fp.Pads())
    if len(pads) < 2:
        return DEFAULT_RESISTOR_EQ_MM
    p1 = pads[0].GetPosition()
    p2 = pads[1].GetPosition()
    dx = (p1.x - p2.x) / PCB_IU_PER_MM
    dy = (p1.y - p2.y) / PCB_IU_PER_MM
    return math.hypot(dx, dy)


def _classify_diff(diff_mm):
    """根据差值返回状态字符串"""
    d = abs(diff_mm)
    if d < TOLERANCE_GOOD:
        return STATUS_PASS
    elif d < TOLERANCE_WARNING:
        return STATUS_WARN
    return STATUS_FAIL


# ═══════════════════════════════════════════════════════════════════════
#  分段信息
# ═══════════════════════════════════════════════════════════════════════

class XnetSegment:
    """一段连续导线（不含元件）"""
    def __init__(self, net_name, items=None):
        self.net_name = net_name
        self.items = items or []
        self._length_nm = sum(it.GetLength() for it in self.items)
        self.via_count = sum(1 for it in self.items if it.GetClass() == 'PCB_VIA')

    def add(self, item):
        self.items.append(item)
        self._length_nm += item.GetLength()
        if item.GetClass() == 'PCB_VIA':
            self.via_count += 1

    @property
    def length_mm(self):
        return self._length_nm / PCB_IU_PER_MM

    def __repr__(self):
        return f'<Seg net={self.net_name} len={self.length_mm:.3f}mm {len(self.items)}items>'


class XnetResistor:
    """串联电阻信息"""
    def __init__(self, ref, value, eq_len_mm):
        self.ref = ref
        self.value = value
        self.eq_len_mm = eq_len_mm  # 两焊盘中心距

    def label(self, factor=1.0, unit='mm'):
        v = f'={self.value}' if self.value else ''
        return f'{self.ref}{v}({self.eq_len_mm * factor:.2f}{unit})'

    def __repr__(self):
        return f'R({self.label})'


class XnetChain:
    """一条 Xnet 完整路径（P 或 N 单侧）"""
    def __init__(self, side_label):
        self.side_label = side_label
        self.segments = []
        self.resistors = []

    @property
    def total_len_mm(self):
        return sum(s.length_mm for s in self.segments)

    @property
    def resistor_eq_len_mm(self):
        return sum(r.eq_len_mm for r in self.resistors)

    def detail_str(self, factor=1.0, unit='mm'):
        """分段明细字符串，如 Seg1[ETH_TXP]=12.340mm → [R1=0.80] → Seg2[...]=5.678mm"""
        parts = []
        n_res = len(self.resistors)
        for i, seg in enumerate(self.segments):
            parts.append(f'Seg{i+1}[{seg.net_name}]={seg.length_mm * factor:.3f}{unit}')
            if i < n_res:
                parts.append(f'[{self.resistors[i].ref}={self.resistors[i].eq_len_mm * factor:.2f}{unit}]')
        return ' → '.join(parts)

    def __repr__(self):
        return f'<Chain {self.side_label}: {self.total_len_mm:.3f}mm {len(self.resistors)}R>'


# ═══════════════════════════════════════════════════════════════════════
#  核心分析引擎
# ═══════════════════════════════════════════════════════════════════════

class XnetAnalyzer:
    """差分 Xnet 追踪分析器"""

    def __init__(self, board):
        self.board = board

    def analyze(self):
        """入口，返回 [dict] 供表格显示"""
        board = self.board
        if not board:
            return []

        net_items = self._collect_net_items(board)
        diff_pairs = self._find_diff_pairs(net_items)
        if not diff_pairs:
            return []

        # 预建电阻邻接索引: net_name -> [(fp, other_net), ...]
        resistor_map = self._build_resistor_map(board)

        results = []
        for base, p_name, n_name in diff_pairs:
            result = self._trace_xnet_chain(
                net_items, resistor_map, base, p_name, n_name)
            if result:
                results.append(result)

        results.sort(key=lambda r: r['pair_name'])
        return results

    def _collect_net_items(self, board):
        """net_name -> [PCB_TRACK, PCB_VIA, PCB_ARC]"""
        m = {}
        for item in board.GetTracks():
            nm = item.GetNetname()
            m.setdefault(nm, []).append(item)
        return m

    def _build_resistor_map(self, board):
        """net_name -> [(fp, other_net), ...]"""
        rmap = {}
        for fp in board.GetFootprints():
            if not _RE_RESISTOR_REF.match(fp.GetReference() or ''):
                continue
            pads = list(fp.Pads())
            if len(pads) != 2:
                continue
            net0 = pads[0].GetNetname()
            net1 = pads[1].GetNetname()
            if not net0 or not net1 or net0 == net1:
                continue
            rmap.setdefault(net0, []).append((fp, net1))
            rmap.setdefault(net1, []).append((fp, net0))
        return rmap

    def _find_diff_pairs(self, net_items):
        """从所有有线网的网络名中找出差分对"""
        pairs = []
        used = set()
        net_names = {nm for nm in net_items if nm}

        for nm in sorted(net_names):
            if nm in used:
                continue
            base, side, pat = _parse_diff_net(nm)
            if side is None:
                continue

            target = _make_opposite_name(base, side, pat)
            if target in net_names and target not in used:
                is_p = side in ('P', 'POS', '+')
                pn = nm if is_p else target
                nn = target if is_p else nm
                pair_base = base.strip('_').strip('-') or 'Unknown'
                pairs.append((pair_base, pn, nn))
                used.add(nm)
                used.add(target)

        return pairs

    def _trace_xnet_chain(self, net_items, resistor_map,
                          pair_base, p_name, n_name):
        """追踪一条差分的 P 侧和 N 侧整条 Xnet 链。"""
        p_chain = self._trace_single_side(
            net_items, resistor_map, p_name, 'P')
        n_chain = self._trace_single_side(
            net_items, resistor_map, n_name, 'N')

        if p_chain is None and n_chain is None:
            return None

        total_p = p_chain.total_len_mm if p_chain else 0
        total_n = n_chain.total_len_mm if n_chain else 0
        diff_mm = total_p - total_n

        # 获取当前显示单位用于标签（电阻标签、分段明细中的单位）
        _, unit_label, factor = _get_display_unit()

        res_p_str = ', '.join(r.label(factor, unit_label) for r in p_chain.resistors) if p_chain and p_chain.resistors else '无'
        res_n_str = ', '.join(r.label(factor, unit_label) for r in n_chain.resistors) if n_chain and n_chain.resistors else '无'
        res_eq_p = p_chain.resistor_eq_len_mm if p_chain else 0
        res_eq_n = n_chain.resistor_eq_len_mm if n_chain else 0

        return {
            'pair_name':    pair_base,
            'net_p':        p_name,
            'net_n':        n_name,
            'len_p_mm':     total_p,
            'len_n_mm':     total_n,
            'diff_mm':      diff_mm,
            'res_p_str':    res_p_str,
            'res_n_str':    res_n_str,
            'res_eq_p':     res_eq_p,
            'res_eq_n':     res_eq_n,
            'status':       _classify_diff(diff_mm),
        }

    def _trace_single_side(self, net_items, resistor_map,
                           start_net, side_label):
        """
        从 start_net 出发，沿电阻链追踪一整条 Xnet。
        使用预建的 resistor_map O(1) 查找。
        """
        chain = XnetChain(side_label)
        visited_nets = set()
        queue = [start_net]

        while queue:
            net_name = queue.pop(0)
            if net_name in visited_nets:
                continue
            visited_nets.add(net_name)

            items = net_items.get(net_name, [])
            if not items:
                continue

            seg = XnetSegment(net_name)
            for it in items:
                seg.add(it)
            chain.segments.append(seg)

            # 使用预建索引查找电阻邻接网络，O(1)  vs 遍历所有封装
            # 电阻物理连接保证另一端属于同一 Xnet，不依赖网络名过滤
            for fp, other_net in resistor_map.get(net_name, []):
                if other_net in visited_nets:
                    continue

                eq = _pad_distance(fp)
                value = fp.GetValue() or ''
                vm = _RE_RES_VALUE.match(value.strip())
                res_val = value if vm else None

                chain.resistors.append(
                    XnetResistor(fp.GetReference(), res_val, eq))

                if other_net not in visited_nets:
                    queue.append(other_net)

        return chain if chain.segments else None

# ═══════════════════════════════════════════════════════════════════════
#  单位工具
# ═══════════════════════════════════════════════════════════════════════

def _get_display_unit():
    """获取 KiCad 当前显示单位。返回 (unit_enum, unit_label, mm_to_unit_factor)"""
    u = GetUserUnits()
    if u == EDA_UNITS_MILS:
        return u, 'mil', 39.37007874
    elif u == EDA_UNITS_INCH:
        return u, 'in', 0.03937008
    else:  # EDA_UNITS_MM or -1 (default)
        return EDA_UNITS_MM, 'mm', 1.0


# ═══════════════════════════════════════════════════════════════════════
#  GUI
# ═══════════════════════════════════════════════════════════════════════

class XnetDialog(wx.Frame):
    def __init__(self, parent, board):
        wx.Frame.__init__(
            self, parent,
            title="Xnet - 差分等长分析",
            size=(1000, 600),
            style=wx.DEFAULT_FRAME_STYLE | wx.FRAME_FLOAT_ON_PARENT,
        )
        self.analyzer = XnetAnalyzer(board)
        self._last_result_hash = None
        self._last_unit = None
        self._build_ui()
        self.CentreOnParent()
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self._refresh()

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda e: self._refresh(), self.timer)
        self.timer.Start(1000)

    def _board_modified_since_last_check(self, results):
        """比较分析结果哈希，判断是否有实质变化。"""
        h = hash(str(results)) if results else 0
        if h == self._last_result_hash:
            return False
        self._last_result_hash = h
        return True

    def _build_ui(self):
        self.toolbar = wx.Panel(self)
        self.btn_refresh = wx.Button(self.toolbar, label="刷新")
        self.btn_refresh.Bind(wx.EVT_BUTTON, lambda e: self._refresh())
        self.cb_auto = wx.CheckBox(self.toolbar, label="自动刷新")
        self.cb_auto.SetValue(True)
        self.st_status = wx.StaticText(self.toolbar, label="就绪")

        _, unit_label, _ = _get_display_unit()

        self.grid = wx.grid.Grid(self)
        self.cols = [
            ("差分分组",  120),
            ("侧别",      40),
            ("网络",      140),
            (f"P({unit_label})",   80),
            (f"N({unit_label})",   80),
            (f"差值({unit_label})", 80),
            ("串联电阻",  200),
            (f"R 等效({unit_label})", 80),
            ("等长状态",  70),
        ]
        self.grid.CreateGrid(0, len(self.cols))
        for i, (label, width) in enumerate(self.cols):
            self.grid.SetColLabelValue(i, label)
            self.grid.SetColSize(i, width)

        lf = self.grid.GetLabelFont()
        lf.SetWeight(wx.FONTWEIGHT_BOLD)
        lf.SetPointSize(lf.GetPointSize() + 1)
        self.grid.SetLabelFont(lf)

        self.grid.SetDefaultCellTextColour(wx.BLACK)

        self.grid.SetDefaultRowSize(24)
        self.grid.EnableEditing(False)
        self.grid.EnableGridLines(True)
        self.grid.SetGridLineColour(wx.Colour(180, 180, 180))
        self.grid.SetSelectionMode(wx.grid.Grid.GridSelectRows)

        self.st_bottom = wx.StaticText(self, label="")

        hs = wx.BoxSizer(wx.HORIZONTAL)
        hs.Add(self.btn_refresh, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        hs.Add(self.cb_auto, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        hs.Add(self.st_status, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        self.toolbar.SetSizer(hs)

        vs = wx.BoxSizer(wx.VERTICAL)
        vs.Add(self.toolbar, 0, wx.EXPAND)
        vs.Add(self.grid, 1, wx.EXPAND | wx.ALL, 4)
        vs.Add(self.st_bottom, 0, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(vs)

    def _refresh(self):
        try:
            new_board = GetBoard()
            self.analyzer.board = new_board

            # 检测单位切换，更新列名
            _, unit_label, _ = _get_display_unit()
            if unit_label != self._last_unit:
                self._last_unit = unit_label
                self._update_column_labels(unit_label)

            self.st_status.SetLabel("分析中...")
            results = self.analyzer.analyze()
            if not self._board_modified_since_last_check(results):
                return
            self._fill_grid(results)
            n = len(results)
            self.st_status.SetLabel(f"找到 {n} 个差分对")
            _, unit_label, factor = _get_display_unit()
            tol_good = TOLERANCE_GOOD * factor
            tol_warn = TOLERANCE_WARNING * factor
            self.st_bottom.SetLabel(
                f"共 {n} 个差分对({n*2}行)  |  "
                f"绿 <{tol_good:.1f}{unit_label}  |  "
                f"黄 {tol_good:.1f}~{tol_warn:.1f}{unit_label}  |  "
                f"红 >={tol_warn:.1f}{unit_label}"
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.st_status.SetLabel(f"错误: {e}")

    def _update_column_labels(self, unit_label):
        """单位切换时更新表头"""
        labels = {
            'P': 3,
            'N': 4,
            '差值': 5,
            'R 等效': 7,
        }
        for name, col in labels.items():
            self.grid.SetColLabelValue(col, f'{name}({unit_label})')

    def _fill_grid(self, results):
        g = self.grid
        if g.GetNumberRows() > 0:
            g.DeleteRows(0, g.GetNumberRows())
        if not results:
            return

        g.AppendRows(len(results) * 2)  # 每对占两行

        GREEN = wx.Colour(210, 240, 210)
        YELLOW = wx.Colour(255, 245, 190)
        RED = wx.Colour(255, 210, 210)
        BLACK = wx.BLACK
        # 克隆字体避免修改网格默认字体
        BF = wx.Font(g.GetDefaultCellFont())
        BF.SetWeight(wx.FONTWEIGHT_BOLD)

        _, unit_label, factor = _get_display_unit()

        for idx, r in enumerate(results):
            row_p = idx * 2
            row_n = row_p + 1

            diff_abs = abs(r['diff_mm'])
            bg = GREEN if diff_abs < TOLERANCE_GOOD else (
                YELLOW if diff_abs < TOLERANCE_WARNING else RED)

            diff_val = r['diff_mm'] * factor
            diff_str = f'{diff_val:.3f}'
            status = r['status']

            p_net = r['net_p']
            n_net = r['net_n']

            p_len = f'{r["len_p_mm"] * factor:.3f}'
            n_len = f'{r["len_n_mm"] * factor:.3f}'
            res_eq_p_str = f'{r["res_eq_p"] * factor:.2f}' if r.get('res_eq_p') else ''
            res_eq_n_str = f'{r["res_eq_n"] * factor:.2f}' if r.get('res_eq_n') else ''

            # --- P 行 ---
            g.SetCellValue(row_p, 0, r['pair_name'])
            g.SetCellValue(row_p, 1, 'P')
            g.SetCellValue(row_p, 2, p_net)
            g.SetCellValue(row_p, 3, p_len)
            g.SetCellValue(row_p, 4, n_len)
            g.SetCellValue(row_p, 5, diff_str)
            g.SetCellValue(row_p, 6, r['res_p_str'])
            g.SetCellValue(row_p, 7, res_eq_p_str)
            g.SetCellValue(row_p, 8, status)

            # --- N 行 ---
            g.SetCellValue(row_n, 0, '')
            g.SetCellValue(row_n, 1, 'N')
            g.SetCellValue(row_n, 2, n_net)
            g.SetCellValue(row_n, 3, p_len)
            g.SetCellValue(row_n, 4, n_len)
            g.SetCellValue(row_n, 5, diff_str)
            g.SetCellValue(row_n, 6, r['res_n_str'])
            g.SetCellValue(row_n, 7, res_eq_n_str)
            g.SetCellValue(row_n, 8, status)

            # 背景色
            for row in (row_p, row_n):
                for col in range(g.GetNumberCols()):
                    g.SetCellBackgroundColour(row, col, bg)
                    g.SetCellTextColour(row, col, BLACK)

            # 差分分组 + 差值 + 等长状态 加粗
            g.SetCellFont(row_p, 0, BF)
            g.SetCellFont(row_p, 5, BF)
            g.SetCellFont(row_p, 8, BF)

    def _on_close(self, event):
        self.timer.Stop()
        self.Destroy()


# ═══════════════════════════════════════════════════════════════════════
#  插件注册
# ═══════════════════════════════════════════════════════════════════════

class XnetPlugin(ActionPlugin):
    def defaults(self):
        self.name = "Xnet - 差分等长分析"
        self.category = "PCB 分析"
        self.description = "实时查看差分对等长，自动追踪电阻分割的 Xnet，等效长度=焊盘中心距"
        icon_path = os.path.join(os.path.dirname(__file__), "xnet.png")
        if os.path.exists(icon_path):
            self.icon_file_name = icon_path
        self.show_toolbar_button = True

    def Run(self):
        board = GetBoard()
        if board is None:
            wx.MessageBox("无法获取PCB板数据", "错误", wx.OK | wx.ICON_ERROR)
            return

        parent = None
        for win in wx.GetTopLevelWindows():
            title = win.GetTitle().lower()
            if "pcb" in title or "pcbnew" in title:
                parent = win
                break

        dlg = XnetDialog(parent, board)
        dlg.Show()
