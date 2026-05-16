"""
AdXray 云端素材下载器（轻量独立版）
直连 D1 云数据库写入 + 批量下载视频，无需额外依赖。
使用前请确保目录下有 d1_config.json。
"""
import calendar
import csv
import io
import json
import re
import ssl
import sys
import time
import tkinter as tk
from datetime import date, datetime, timedelta
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from threading import Thread, Event
from urllib import request

_DL_LOG = Path(__file__).parent / "download_debug.log"


def _dl_log(msg: str):
    """写入下载日志并立即刷盘。"""
    with open(_DL_LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        f.flush()

# 导入本地同步模块（支持多种目录结构）
_HERE = Path(__file__).parent
for _candidate in [_HERE / "LocalDashboard", _HERE.parent / "LocalDashboard"]:
    if (_candidate / "sync.py").exists():
        sys.path.insert(0, str(_candidate))
        break
else:
    sys.path.insert(0, str(_HERE.parent / "LocalDashboard"))
from sync import get_db, sync, DB_PATH

# SSL 兜底：部分 Windows 机器缺少根证书，直接跳过验证
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _urlopen(req, timeout=30):
    return request.urlopen(req, timeout=timeout, context=_SSL_CTX)

# ─── 配置 ────────────────────────────────────────
CONFIG_FILE = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {
    "turso_url": "libsql://adxray-xxx.turso.io",
    "turso_token": "",
    "download_root": str(Path.home() / "Downloads" / "adx_videos"),
    "is_master": True,
    "r2_account_id": "",
    "r2_access_key_id": "",
    "r2_secret_key": "",
    "r2_bucket": "adxray-cache",
}

# ─── 我的剧场（已知的 7 个主要剧场关键词，用于快速筛选） ──
MY_THEATER_KEYWORDS = [
    "ShortMax", "FlareFlow", "ReelShort", "DramaBox",
    "GoodShort", "MoboReels", "FlickReels",
]

# ─── 缩写映射 ────────────────────────────────────
THEATER_ABBR = {
    "ShortMax - 精選短劇，掌上輕鬆看": "SM",
    "测试剧场": "TEST",
}

LANG_ABBR = {
    "中文": "zh", "英文": "en", "西班牙语": "es", "葡萄牙语": "pt",
    "德语": "de", "法语": "fr", "越南语": "vi", "泰语": "th",
    "日语": "ja", "韩语": "ko", "阿拉伯语": "ar", "俄语": "ru",
    "印尼语": "id", "印地语": "hi", "挪威语": "no", "爱尔兰语": "ga",
    "马来语": "ms", "菲律宾语": "tl", "土耳其语": "tr", "意大利语": "it",
    "波兰语": "pl", "荷兰语": "nl", "瑞典语": "sv", "丹麦语": "da",
    "芬兰语": "fi", "罗马尼亚语": "ro", "匈牙利语": "hu", "捷克语": "cs",
    "希腊语": "el", "希伯来语": "he", "乌克兰语": "uk", "孟加拉语": "bn",
    "缅甸语": "my", "高棉语": "km", "老挝语": "lo", "斯瓦希里语": "sw",
}

REMARK_ABBR = {"精选": "JX", "待备注": "DBZ", "违规": "WG"}

ANOMALY_ABBR = {"有ADX-无分销": "NOFX", "有分销-但素材不对版": "SCBDP"}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    # 首次运行生成模板
    save_config(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ─── 本地 SQLite 查询 ──────────────────────────────

def _turso_val(v):
    """参数值：直接返回原始值供 SQLite 使用。"""
    return v


def local_query(sql, params=None):
    """查询本地 SQLite 缓存，返回 list[dict]。"""
    conn = get_db()
    cur = conn.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.close()
    return rows


def local_distinct(column):
    """从本地 SQLite 获取 DISTINCT 值。"""
    rows = local_query(f"SELECT DISTINCT {column} FROM materials ORDER BY {column}")
    return [r[column] for r in rows if r.get(column)]


# ─── D1 写入（仅标注 remark 等写操作需要）───────

def _escape_val(v) -> str:
    """值转 SQL 内联安全字符串。"""
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _load_d1_config():
    """加载 D1 配置。"""
    for p in [Path(__file__).parent / "d1_config.json",
              Path(__file__).parent.parent / "d1_config.json"]:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return None


def d1_execute(sql):
    """执行非查询语句（UPDATE/INSERT），返回受影响行数。"""
    cfg = _load_d1_config()
    if not cfg:
        raise RuntimeError("找不到 d1_config.json")
    url = (f"https://api.cloudflare.com/client/v4/accounts/"
           f"{cfg['account_id']}/d1/database/{cfg['database_id']}/query")
    body = json.dumps({"sql": sql}).encode()
    req = request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {cfg['api_token']}",
        "Content-Type": "application/json",
    })
    with _urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    if not data.get("success"):
        errs = data.get("errors", [])
        msg = errs[0].get("message", "?") if errs else "unknown"
        raise RuntimeError(f"D1 执行错误: {msg}")
    total = 0
    for r in data.get("result", []):
        total += r.get("meta", {}).get("changes", 0)
    return total


# ─── 语种映射 ────────────────────────────────────

_LANG_CODE_TO_CN: dict[str, str] = {
    "az": "阿塞拜疆语", "et": "爱沙尼亚语", "hr": "克罗地亚语",
    "is": "冰岛语", "kk": "哈萨克语", "sk": "斯洛伐克语",
    "sl": "斯洛文尼亚语", "ms": "马来语", "fil": "菲律宾语",
    "sv": "瑞典语", "da": "丹麦语", "fi": "芬兰语",
    "ro": "罗马尼亚语", "hu": "匈牙利语", "cs": "捷克语",
    "el": "希腊语", "he": "希伯来语", "uk": "乌克兰语",
    "bn": "孟加拉语", "my": "缅甸语", "km": "高棉语",
    "lo": "老挝语", "sw": "斯瓦希里语", "bg": "保加利亚语",
    "lt": "立陶宛语", "lv": "拉脱维亚语", "sr": "塞尔维亚语",
    "ca": "加泰罗尼亚语", "gl": "加利西亚语", "eu": "巴斯克语",
    "sq": "阿尔巴尼亚语", "mk": "马其顿语", "bs": "波斯尼亚语",
    "ka": "格鲁吉亚语", "hy": "亚美尼亚语", "uz": "乌兹别克语",
    "mn": "蒙古语", "ne": "尼泊尔语", "si": "僧伽罗语",
    "am": "阿姆哈拉语", "zu": "祖鲁语", "af": "南非荷兰语",
}

# 优先排序：英 德 日 韩 法 繁体 中文
_LANG_SORT_PRIORITY = ["英文", "德语", "日语", "韩语", "法语", "繁體中文", "中文"]


def _lang_display(db_val: str) -> str:
    """数据库语种值 → 显示用中文名。"""
    return _LANG_CODE_TO_CN.get(db_val, db_val)


def _sort_languages(langs: list[str]) -> list[str]:
    """按优先级排序语种列表（显示名）。"""
    def _key(name: str) -> tuple[int, str]:
        try:
            return (0, str(_LANG_SORT_PRIORITY.index(name)).zfill(3))
        except ValueError:
            return (1, name)
    return sorted(langs, key=_key)


# ─── 下载逻辑 ────────────────────────────────────

HASH_RE = re.compile(r"[a-f0-9]{32}", re.IGNORECASE)


def download_file(url: str, dest: Path, cancel_event: Event = None,
                  file_timeout: int = 120) -> bool:
    """下载单个文件。支持 cancel_event 中断和整体超时。"""
    tmp = dest.with_suffix(dest.suffix + ".part")
    _dl_log(f"START {dest.name} <- {url[:120]}")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        deadline = time.time() + file_timeout
        t0 = time.time()
        with _urlopen(req, timeout=15) as resp:
            _dl_log(f"  CONNECTED {dest.name} ({time.time()-t0:.1f}s)")
            size = 0
            with open(tmp, "wb") as f:
                while True:
                    if cancel_event and cancel_event.is_set():
                        _dl_log(f"  CANCELLED {dest.name}")
                        tmp.unlink(missing_ok=True)
                        return False
                    if time.time() > deadline:
                        _dl_log(f"  TIMEOUT {dest.name} ({size} bytes so far)")
                        tmp.unlink(missing_ok=True)
                        return False
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    f.write(chunk)
        tmp.rename(dest)
        _dl_log(f"  OK {dest.name} ({size} bytes, {time.time()-t0:.1f}s)")
        return True
    except Exception as e:
        _dl_log(f"  FAIL {dest.name}: {e}")
        tmp.unlink(missing_ok=True)
        return False


def _date_to_ts(date_str: str, end_of_day: bool = False) -> int:
    """'YYYY-MM-DD' → Unix timestamp。"""
    try:
        dt = datetime.strptime(date_str.strip(), "%Y-%m-%d")
        if end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59)
        return int(dt.timestamp())
    except ValueError:
        return 0


def make_folder_name(theater: str, language: str, drama_name: str,
                     first_seen: str, remark: str,
                     style: str = "abbr") -> str:
    drama_clean = re.sub(r'[<>:"/\\|?*]', '_', drama_name or "unknown")
    if style == "drama":
        return drama_clean
    t = THEATER_ABBR.get(theater, theater[:6])
    la = LANG_ABBR.get(language, language[:4])
    d = first_seen.replace("-", "") if first_seen else "nodate"
    r = REMARK_ABBR.get(remark or "待备注", remark[:4] if remark else "DBZ")
    return f"{t}-{la}-{drama_clean}-{d}-{r}"


# ─── 日历弹窗（纯 tkinter，零依赖） ─────────────

class CalendarPopup(tk.Toplevel):
    """点击后弹出月历选日期，选中后写入目标 Entry。"""

    WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]

    def __init__(self, parent, target_entry: ttk.Entry):
        super().__init__(parent)
        self.overrideredirect(True)
        self.grab_set()
        self._target = target_entry
        self._today = date.today()

        # 初始月份：如果 entry 已有值就解析，否则用今天
        try:
            parts = target_entry.get().strip().split("-")
            self._year = int(parts[0])
            self._month = int(parts[1])
        except Exception:
            self._year = self._today.year
            self._month = self._today.month

        self._build()
        self._position()
        self.bind("<FocusOut>", lambda e: self.destroy())

    def _position(self):
        x = self._target.winfo_rootx()
        y = self._target.winfo_rooty() + self._target.winfo_height() + 2
        self.geometry(f"+{x}+{y}")

    def _build(self):
        for w in self.winfo_children():
            w.destroy()

        frm = tk.Frame(self, bg="#1e1e1e", bd=1, relief="solid")
        frm.pack()

        # 月份导航
        nav = tk.Frame(frm, bg="#1e1e1e")
        nav.pack(fill="x", pady=(6, 2))

        tk.Button(nav, text="\u25c0", command=self._prev_month,
                  bg="#333", fg="white", bd=0, font=("", 10)).pack(side="left", padx=8)
        self._lbl_month = tk.Label(nav, text=f"{self._year}/{self._month:02d}",
                                    bg="#1e1e1e", fg="white", font=("", 12, "bold"))
        self._lbl_month.pack(side="left", expand=True)
        tk.Button(nav, text="\u25b6", command=self._next_month,
                  bg="#333", fg="white", bd=0, font=("", 10)).pack(side="right", padx=8)

        # 星期头
        hdr = tk.Frame(frm, bg="#1e1e1e")
        hdr.pack(fill="x")
        for wd in self.WEEKDAYS:
            tk.Label(hdr, text=wd, width=4, bg="#1e1e1e", fg="#888",
                     font=("", 9)).pack(side="left")

        # 日期格子
        cal = calendar.monthcalendar(self._year, self._month)
        for week in cal:
            row = tk.Frame(frm, bg="#1e1e1e")
            row.pack(fill="x")
            for day in week:
                if day == 0:
                    tk.Label(row, text="", width=4, bg="#1e1e1e").pack(side="left")
                else:
                    is_today = (self._year == self._today.year
                                and self._month == self._today.month
                                and day == self._today.day)
                    bg = "#2563eb" if is_today else "#1e1e1e"
                    fg = "white"
                    btn = tk.Button(row, text=str(day), width=4, bg=bg, fg=fg,
                                    bd=0, font=("", 10),
                                    activebackground="#3b82f6", activeforeground="white",
                                    command=lambda d=day: self._select(d))
                    btn.pack(side="left")

        # 底部快捷
        bot = tk.Frame(frm, bg="#1e1e1e")
        bot.pack(fill="x", pady=(4, 6))
        tk.Button(bot, text="清除", bg="#1e1e1e", fg="#f87171", bd=0,
                  command=self._clear).pack(side="left", padx=12)
        tk.Button(bot, text="今天", bg="#1e1e1e", fg="#60a5fa", bd=0,
                  command=self._pick_today).pack(side="right", padx=12)

    def _prev_month(self):
        if self._month == 1:
            self._month = 12
            self._year -= 1
        else:
            self._month -= 1
        self._build()

    def _next_month(self):
        if self._month == 12:
            self._month = 1
            self._year += 1
        else:
            self._month += 1
        self._build()

    def _select(self, day: int):
        val = f"{self._year}-{self._month:02d}-{day:02d}"
        self._target.delete(0, "end")
        self._target.insert(0, val)
        self.destroy()

    def _clear(self):
        self._target.delete(0, "end")
        self.destroy()

    def _pick_today(self):
        self._target.delete(0, "end")
        self._target.insert(0, self._today.isoformat())
        self.destroy()


# ─── GUI ──────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AdXray 云端素材下载器")
        self.geometry("820x820")
        self.resizable(True, True)
        self.cfg = load_config()
        self._downloading = False
        self._cancel_event = Event()
        self._lang_display_to_db: dict[str, str] = {}

        if not _load_d1_config() and self.cfg.get("is_master", True):
            messagebox.showwarning(
                "配置缺失",
                "主机模式需要 d1_config.json，\n"
                "客户端模式请设置 is_master=false\n"
                f"配置目录：{Path(__file__).parent}",
            )

        self._build_ui()
        self._load_filters()

    def _build_ui(self):
        pad = dict(padx=8, pady=4)

        # ── 筛选区 ──
        frm_filter = ttk.LabelFrame(self, text="筛选条件", padding=8)
        frm_filter.pack(fill="x", **pad)

        # 行 0a：剧场（独占一整行，下拉框拉满）
        ttk.Label(frm_filter, text="剧场:").grid(row=0, column=0, sticky="e")
        self.cmb_theater = ttk.Combobox(frm_filter, state="readonly", width=40)
        self.cmb_theater.grid(row=0, column=1, columnspan=7, sticky="ew", padx=4)
        frm_filter.columnconfigure(1, weight=1)

        # 行 0b：语种 / 备注 / 异常值 / 查看
        ttk.Label(frm_filter, text="语种:").grid(row=1, column=0, sticky="e")
        self.cmb_lang = ttk.Combobox(frm_filter, state="readonly", width=14)
        self.cmb_lang.grid(row=1, column=1, sticky="w", padx=4)

        ttk.Label(frm_filter, text="备注:").grid(row=1, column=2, sticky="e")
        self.cmb_remark = ttk.Combobox(frm_filter, state="readonly", width=10,
                                        values=["全部", "待备注", "精选", "违规"])
        self.cmb_remark.set("全部")
        self.cmb_remark.grid(row=1, column=3, sticky="w", padx=4)

        ttk.Label(frm_filter, text="异常值:").grid(row=1, column=4, sticky="e")
        self.cmb_anomaly = ttk.Combobox(frm_filter, state="readonly", width=18,
                                         values=["全部", "有ADX-无分销",
                                                 "有分销-但素材不对版", "仅正常素材"])
        self.cmb_anomaly.set("全部")
        self.cmb_anomaly.grid(row=1, column=5, sticky="w", padx=4)

        ttk.Label(frm_filter, text="查看:").grid(row=1, column=6, sticky="e")
        self.cmb_view_status = ttk.Combobox(frm_filter, state="readonly", width=8,
                                             values=["全部", "未看", "已看"])
        self.cmb_view_status.set("全部")
        self.cmb_view_status.grid(row=1, column=7, sticky="w", padx=4)

        # 行 2a：排除项（多选 checkbutton）
        ttk.Label(frm_filter, text="排除项:").grid(row=2, column=0, sticky="e")
        self._excl_违规 = tk.BooleanVar(value=True)
        self._excl_已删除 = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm_filter, text="违规", variable=self._excl_违规).grid(
            row=2, column=1, sticky="w", padx=4)
        ttk.Checkbutton(frm_filter, text="已删除", variable=self._excl_已删除).grid(
            row=2, column=2, sticky="w", padx=4)

        # 行 2b：音频语种
        ttk.Label(frm_filter, text="音频语种:").grid(row=2, column=4, sticky="e")
        self.cmb_audio_lang = ttk.Combobox(frm_filter, state="readonly", width=14)
        self.cmb_audio_lang.grid(row=2, column=5, sticky="w", padx=4)

        # 行 3：投放日期
        ttk.Label(frm_filter, text="投放起始:").grid(row=3, column=0, sticky="e")
        frm_df = ttk.Frame(frm_filter)
        frm_df.grid(row=3, column=1, sticky="w", padx=4, pady=4)
        self.ent_date_from = ttk.Entry(frm_df, width=12)
        self.ent_date_from.pack(side="left")
        ttk.Button(frm_df, text="\U0001f4c5", width=3,
                   command=lambda: CalendarPopup(self, self.ent_date_from)).pack(side="left", padx=2)

        ttk.Label(frm_filter, text="投放结束:").grid(row=3, column=2, sticky="e")
        frm_dt = ttk.Frame(frm_filter)
        frm_dt.grid(row=3, column=3, sticky="w", padx=4, pady=4)

        self.ent_date_to = ttk.Entry(frm_dt, width=12)
        self.ent_date_to.pack(side="left")
        ttk.Button(frm_dt, text="\U0001f4c5", width=3,
                   command=lambda: CalendarPopup(self, self.ent_date_to)).pack(side="left", padx=2)

        # 行 3：抓取日期
        ttk.Label(frm_filter, text="抓取起始:").grid(row=4, column=0, sticky="e")
        frm_sf = ttk.Frame(frm_filter)
        frm_sf.grid(row=4, column=1, sticky="w", padx=4, pady=4)
        self.ent_synced_from = ttk.Entry(frm_sf, width=12)
        self.ent_synced_from.pack(side="left")
        ttk.Button(frm_sf, text="\U0001f4c5", width=3,
                   command=lambda: CalendarPopup(self, self.ent_synced_from)).pack(side="left", padx=2)

        ttk.Label(frm_filter, text="抓取结束:").grid(row=4, column=2, sticky="e")
        frm_st = ttk.Frame(frm_filter)
        frm_st.grid(row=4, column=3, sticky="w", padx=4, pady=4)
        self.ent_synced_to = ttk.Entry(frm_st, width=12)
        self.ent_synced_to.pack(side="left")
        ttk.Button(frm_st, text="\U0001f4c5", width=3,
                   command=lambda: CalendarPopup(self, self.ent_synced_to)).pack(side="left", padx=2)

        # 行 5：评分 / 剧名搜索
        ttk.Label(frm_filter, text="最低评分:").grid(row=5, column=0, sticky="e")
        self.cmb_score = ttk.Combobox(frm_filter, state="readonly", width=8,
                                       values=["不限", "1", "2", "3", "5", "8", "10"])
        self.cmb_score.set("不限")
        self.cmb_score.grid(row=5, column=1, sticky="w", padx=4, pady=4)

        ttk.Label(frm_filter, text="最少素材:").grid(row=5, column=2, sticky="e")
        self.cmb_min_mat = ttk.Combobox(frm_filter, state="readonly", width=8,
                                         values=["不限", "2", "3", "5", "10", "20", "50"])
        self.cmb_min_mat.set("不限")
        self.cmb_min_mat.grid(row=5, column=3, sticky="w", padx=4, pady=4)

        ttk.Label(frm_filter, text="剧名搜索:").grid(row=5, column=4, sticky="e")
        self.ent_keyword = ttk.Entry(frm_filter, width=30)
        self.ent_keyword.grid(row=5, column=5, columnspan=3, sticky="ew", padx=4, pady=4)

        frm_filter.columnconfigure(1, weight=1)

        # ── 下载设置 ──
        frm_dl = ttk.LabelFrame(self, text="下载设置", padding=8)
        frm_dl.pack(fill="x", **pad)

        # 下载路径
        ttk.Label(frm_dl, text="下载路径:").grid(row=0, column=0, sticky="e")
        self.ent_path = ttk.Entry(frm_dl, width=45)
        self.ent_path.insert(0, self.cfg["download_root"])
        self.ent_path.grid(row=0, column=1, columnspan=4, sticky="ew", padx=4)
        ttk.Button(frm_dl, text="选择", command=self._choose_dir).grid(row=0, column=5, padx=4)

        # 文件夹命名
        ttk.Label(frm_dl, text="文件夹命名:").grid(row=1, column=0, sticky="e")
        self._folder_style = tk.StringVar(value="abbr")
        ttk.Radiobutton(frm_dl, text="缩写格式（SM-en-剧名-日期-JX）",
                         variable=self._folder_style, value="abbr").grid(row=1, column=1, columnspan=2, sticky="w", padx=4)
        ttk.Radiobutton(frm_dl, text="剧名格式（直接用剧名）",
                         variable=self._folder_style, value="drama").grid(row=1, column=3, columnspan=2, sticky="w", padx=4)

        # 时长筛选
        ttk.Label(frm_dl, text="时长筛选:").grid(row=2, column=0, sticky="e")
        self._dur_all = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm_dl, text="全选", variable=self._dur_all,
                         command=self._on_dur_all).grid(row=2, column=1, sticky="w", padx=4)
        self._dur_vars = {}
        for i, (key, label) in enumerate([
            ("0-5", "5分钟以下"), ("5-10", "5-10分钟"),
            ("10-15", "10-15分钟"), ("15+", "15分钟以上"),
        ]):
            var = tk.BooleanVar(value=False)
            self._dur_vars[key] = var
            ttk.Checkbutton(frm_dl, text=label, variable=var,
                             command=self._on_dur_item).grid(
                row=2, column=2 + i, sticky="w", padx=4)

        frm_dl.columnconfigure(1, weight=1)

        # ── 按钮区 ──
        frm_btn = ttk.Frame(self, padding=4)
        frm_btn.pack(fill="x", **pad)

        self.btn_search = ttk.Button(frm_btn, text="搜索预览", command=self._search)
        self.btn_search.pack(side="left", padx=6)

        self.btn_download = ttk.Button(frm_btn, text="下载选中", command=self._start_download)
        self.btn_download.pack(side="left", padx=6)

        self.btn_cancel = ttk.Button(frm_btn, text="取消下载", command=self._cancel_download, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)

        self.btn_export = ttk.Button(frm_btn, text="导出 CSV", command=self._export_csv)
        self.btn_export.pack(side="left", padx=6)

        self.lbl_status = ttk.Label(frm_btn, text="就绪")
        self.lbl_status.pack(side="left", padx=12)

        # ── 进度 ──
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", **pad)

        # ── 素材标注 ──
        frm_mark = ttk.LabelFrame(self, text="素材标注", padding=8)
        frm_mark.pack(fill="x", **pad)
        ttk.Label(frm_mark, text="粘贴文件名/路径/链接（每行一个），自动提取哈希匹配数据库并修改备注",
                  foreground="#888").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))

        # 精选
        ttk.Label(frm_mark, text="⭐ 标为精选", foreground="#58a6ff").grid(row=1, column=0, sticky="nw", padx=(0, 4))
        self.txt_featured = tk.Text(frm_mark, height=3, width=38, bg="#0d1117", fg="#e6e6e6",
                                     insertbackground="white", font=("Consolas", 9))
        self.txt_featured.grid(row=1, column=1, sticky="ew", padx=4)
        self.btn_mark_featured = ttk.Button(frm_mark, text="标为精选", command=lambda: self._do_mark("精选"))
        self.btn_mark_featured.grid(row=2, column=1, sticky="w", padx=4, pady=2)
        self.lbl_mark_featured = ttk.Label(frm_mark, text="", foreground="#888")
        self.lbl_mark_featured.grid(row=2, column=1, sticky="e", padx=4)

        # 违规
        ttk.Label(frm_mark, text="🚫 标为违规", foreground="#f85149").grid(row=1, column=2, sticky="nw", padx=(12, 4))
        self.txt_violation = tk.Text(frm_mark, height=3, width=38, bg="#0d1117", fg="#e6e6e6",
                                      insertbackground="white", font=("Consolas", 9))
        self.txt_violation.grid(row=1, column=3, sticky="ew", padx=4)
        self.btn_mark_violation = ttk.Button(frm_mark, text="标为违规", command=lambda: self._do_mark("违规"))
        self.btn_mark_violation.grid(row=2, column=3, sticky="w", padx=4, pady=2)
        self.lbl_mark_violation = ttk.Label(frm_mark, text="", foreground="#888")
        self.lbl_mark_violation.grid(row=2, column=3, sticky="e", padx=4)

        frm_mark.columnconfigure(1, weight=1)
        frm_mark.columnconfigure(3, weight=1)

        # ── 结果列表（带勾选） ──
        cols = ("sel", "theater", "language", "audio_lang", "drama_name", "cnt", "score")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=12)
        for c, w, t in [
            ("sel", 30, "✓"), ("theater", 120, "剧场"), ("language", 60, "语种"),
            ("audio_lang", 70, "音频语种"),
            ("drama_name", 200, "剧名"), ("cnt", 50, "素材数"), ("score", 50, "评分"),
        ]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center" if c in ("sel", "cnt", "score") else "w")
        self.tree.bind("<Button-1>", self._on_tree_click)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(fill="both", expand=True, padx=8, pady=4)
        scrollbar.place(in_=self.tree, relx=1, rely=0, relheight=1, anchor="ne")

        self._rows: list[dict] = []
        self._selected: set = set()

    def _do_mark(self, remark: str):
        """从文本框提取哈希，批量更新数据库备注。"""
        txt_widget = self.txt_featured if remark == "精选" else self.txt_violation
        lbl_widget = self.lbl_mark_featured if remark == "精选" else self.lbl_mark_violation
        text = txt_widget.get("1.0", "end").strip()
        if not text:
            lbl_widget.config(text="请先粘贴内容")
            return
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        hashes = []
        bad_lines = []
        for line in lines:
            m = HASH_RE.search(line)
            if m:
                hashes.append(m.group(0))
            else:
                bad_lines.append(line)
        if not hashes:
            lbl_widget.config(text="未识别到有效哈希值")
            return
        lbl_widget.config(text=f"正在处理 {len(hashes)} 条…")
        self.update()
        matched = 0
        not_found = 0
        try:
            for h in hashes:
                rows = local_query(
                    "SELECT COUNT(*) as cnt FROM materials WHERE video_url LIKE ?",
                    [f"%{h}%"])
                cnt = int(rows[0]["cnt"]) if rows else 0
                if cnt == 0:
                    not_found += 1
                    continue
                d1_execute(
                    f"UPDATE materials SET remark = {_escape_val(remark)} "
                    f"WHERE video_hash = {_escape_val(h)}")
                matched += cnt
        except Exception as e:
            lbl_widget.config(text=f"失败: {e}")
            return
        msg = f"✅ 已标注 {matched} 条为「{remark}」"
        if not_found > 0:
            msg += f"  ⚠️ {not_found} 条未找到"
        if bad_lines:
            msg += f"  ❌ {len(bad_lines)} 行无法识别"
        lbl_widget.config(text=msg)

    def _choose_dir(self):
        d = filedialog.askdirectory()
        if d:
            self.ent_path.delete(0, "end")
            self.ent_path.insert(0, d)

    def _on_dur_all(self):
        val = self._dur_all.get()
        for v in self._dur_vars.values():
            v.set(val)

    def _on_dur_item(self):
        self._dur_all.set(all(v.get() for v in self._dur_vars.values()))

    def _load_filters(self):
        # 客户端模式不需要 D1 配置，直接读本地 SQLite
        try:
            theaters = local_distinct("theater")
            # 去除 HTML 标签残留
            theaters = [re.sub(r"<[^>]+>", "", t).strip() for t in theaters]
            theaters = sorted(set(t for t in theaters if t))
            # 按关键词合并同剧场（"ShortMax - xxx" 等 → "ShortMax"）
            # _theater_keyword_map: 显示名 → 关键词（用于 LIKE 查询）
            # _theater_fullnames:  显示名 → [完整剧场名列表]（用于精确匹配）
            self._theater_keyword_map = {}   # 显示名 → keyword
            self._theater_fullnames = {}     # 显示名 → [full names]
            merged_my = []     # 合并后的「我的剧场」显示名
            leftover = []      # 未匹配任何关键词的原始剧场名
            kw_buckets: dict[str, list[str]] = {kw: [] for kw in MY_THEATER_KEYWORDS}
            for t in theaters:
                matched_kw = None
                for kw in MY_THEATER_KEYWORDS:
                    if kw.lower() in t.lower():
                        matched_kw = kw
                        break
                if matched_kw:
                    kw_buckets[matched_kw].append(t)
                else:
                    leftover.append(t)
            for kw in MY_THEATER_KEYWORDS:
                names = kw_buckets[kw]
                if not names:
                    continue
                display = kw  # 用关键词作为显示名
                self._theater_keyword_map[display] = kw
                self._theater_fullnames[display] = names
                merged_my.append(display)
            # 其他剧场也做同样的去重（按第一个单词合并）
            other_buckets: dict[str, list[str]] = {}
            for t in leftover:
                prefix = t.split()[0] if t.split() else t
                other_buckets.setdefault(prefix, []).append(t)
            merged_other = []
            for prefix, names in sorted(other_buckets.items()):
                display = prefix
                if display in self._theater_keyword_map:
                    display = f"{prefix} (其他)"  # 防冲突
                self._theater_keyword_map[display] = prefix
                self._theater_fullnames[display] = names
                merged_other.append(display)
            theater_values = ["全部剧场", "── 我的剧场 ──"] + merged_my
            if merged_other:
                theater_values += ["── 其他剧场 ──"] + merged_other
            self._my_theaters = set(merged_my)
            raw_langs = local_distinct("language")
            # 语种：代码 → 中文，排序，保留双向映射
            display_langs = [_lang_display(l) for l in raw_langs]
            self._lang_display_to_db = {_lang_display(l): l for l in raw_langs}
            display_langs = _sort_languages(display_langs)
            self.cmb_theater["values"] = theater_values
            self.cmb_theater.set("全部剧场")
            self.cmb_lang["values"] = ["全部语种"] + display_langs
            self.cmb_lang.set("全部语种")
            # 音频语种（audio_language 列，可能含 "中文+英文" 格式）
            self._audio_lang_zh = _AUDIO_LANG_ZH = {
                "en": "英语", "de": "德语", "fr": "法语", "ja": "日语",
                "ko": "韩语", "es": "西班牙语", "pt": "葡萄牙语",
                "th": "泰语", "vi": "越南语", "ms": "马来语",
                "zh": "中文", "ru": "俄语", "ar": "阿拉伯语",
                "hi": "印地语", "id": "印尼语", "tr": "土耳其语",
                "it": "意大利语", "pl": "波兰语", "nl": "荷兰语",
                "sv": "瑞典语", "da": "丹麦语", "fi": "芬兰语",
                "no": "挪威语", "uk": "乌克兰语", "cs": "捷克语",
                "ro": "罗马尼亚语", "hu": "匈牙利语", "el": "希腊语",
                "he": "希伯来语", "bn": "孟加拉语", "ta": "泰米尔语",
                "tl": "菲律宾语", "sw": "斯瓦希里语", "my": "缅甸语",
                "km": "高棉语", "lo": "老挝语", "mn": "蒙古语",
                "ne": "尼泊尔语", "si": "僧伽罗语", "am": "阿姆哈拉语",
                "jw": "爪哇语", "cy": "威尔士语", "haw": "夏威夷语",
                "nn": "新挪威语", "af": "南非荷兰语", "sq": "阿尔巴尼亚语",
                "bg": "保加利亚语", "hr": "克罗地亚语", "sk": "斯洛伐克语",
                "sl": "斯洛文尼亚语", "sr": "塞尔维亚语", "lt": "立陶宛语",
                "lv": "拉脱维亚语", "et": "爱沙尼亚语", "ka": "格鲁吉亚语",
                "ur": "乌尔都语", "fa": "波斯语", "ml": "马拉雅拉姆语",
                "te": "泰卢固语", "kn": "卡纳达语", "gu": "古吉拉特语",
                "mr": "马拉地语", "pa": "旁遮普语",
                # 中文变体（DB 可能存 "英文" 而非 "en"）
                "英文": "英语", "德文": "德语", "法文": "法语", "日文": "日语",
                "韩文": "韩语", "西班牙文": "西班牙语", "葡萄牙文": "葡萄牙语",
                "泰文": "泰语", "越南文": "越南语", "马来文": "马来语",
                "俄文": "俄语", "阿拉伯文": "阿拉伯语",
            }
            _AUDIO_PRIORITY = [
                "英语", "德语", "法语", "日语", "韩语",
                "西班牙语", "葡萄牙语", "泰语", "越南语", "马来语",
            ]
            try:
                raw_audio = local_query(
                    "SELECT DISTINCT audio_language FROM materials "
                    "WHERE audio_language IS NOT NULL AND audio_language != '' "
                    "ORDER BY audio_language")
                # 拆解组合值，提取所有唯一单语种
                audio_set = set()
                for r in raw_audio:
                    val = r.get("audio_language", "")
                    for part in val.split("+"):
                        p = part.strip()
                        if p:
                            audio_set.add(p)
                # DB值 → 中文显示名，保留双向映射
                self._audio_display_to_db = {}  # 显示名 → DB值
                display_set = set()
                for db_val in audio_set:
                    zh = _AUDIO_LANG_ZH.get(db_val, db_val)  # 无映射则原样显示
                    self._audio_display_to_db[zh] = db_val
                    display_set.add(zh)
                # 按优先顺序排列
                priority_order = {name: i for i, name in enumerate(_AUDIO_PRIORITY)}
                audio_list = sorted(display_set,
                    key=lambda x: (priority_order.get(x, len(_AUDIO_PRIORITY)), x))
                self.cmb_audio_lang["values"] = ["全部", "未识别"] + audio_list
                self.cmb_audio_lang.set("全部")
            except Exception:
                self._audio_display_to_db = {}
                self.cmb_audio_lang["values"] = ["全部", "未识别"]
                self.cmb_audio_lang.set("全部")
            self.lbl_status.config(
                text=f"已连接 | {len(merged_my)} 我的剧场, "
                     f"{len(merged_other)} 其他剧场, {len(display_langs)} 语种"
            )
        except Exception as e:
            self.lbl_status.config(text=f"加载失败: {e}")
            messagebox.showerror(
                "加载失败",
                f"无法加载本地数据，请检查：\n\n"
                f"1. 网络是否正常（首次运行需下载缓存）\n"
                f"2. config.json 中 R2 配置是否正确\n\n"
                f"错误详情：{e}",
            )

    def _on_tree_click(self, event):
        """切换行选中状态。"""
        item = self.tree.identify_row(event.y)
        if not item:
            return
        if item in self._selected:
            self._selected.discard(item)
            self.tree.set(item, "sel", "")
        else:
            self._selected.add(item)
            self.tree.set(item, "sel", "✔")

    def _get_common_where(self) -> tuple[list[str], list[dict]]:
        """从 UI 收集通用 WHERE 条件。"""
        where, args = ["1=1"], []
        theater = self.cmb_theater.get()
        if theater == "── 我的剧场 ──":
            # 筛选所有「我的剧场」（用 LIKE 匹配每个关键词）
            my = getattr(self, "_my_theaters", set())
            kw_map = getattr(self, "_theater_keyword_map", {})
            if my:
                likes = []
                for t in sorted(my):
                    kw = kw_map.get(t, t)
                    likes.append("theater LIKE ?")
                    args.append(_turso_val(f"%{kw}%"))
                where.append(f"({' OR '.join(likes)})")
        elif theater and theater != "全部剧场" and not theater.startswith("──"):
            kw_map = getattr(self, "_theater_keyword_map", {})
            fullnames = getattr(self, "_theater_fullnames", {})
            kw = kw_map.get(theater)
            fnames = fullnames.get(theater, [])
            if kw and len(fnames) > 1:
                # 合并剧场 → 用 LIKE 模糊匹配
                where.append("theater LIKE ?")
                args.append(_turso_val(f"%{kw}%"))
            elif fnames:
                # 只有一个全名 → 精确匹配
                where.append("theater = ?")
                args.append(_turso_val(fnames[0]))
            else:
                where.append("theater = ?")
                args.append(_turso_val(theater))
        lang = self.cmb_lang.get()
        if lang and lang != "全部语种":
            db_lang = getattr(self, "_lang_display_to_db", {}).get(lang, lang)
            where.append("language = ?")
            args.append(_turso_val(db_lang))
        # 排除项：勾选则排除对应备注的记录
        excl_vals = []
        if self._excl_违规.get():
            excl_vals.append("违规")
        if self._excl_已删除.get():
            excl_vals.append("已删除")
        if excl_vals:
            placeholders = ",".join(["?"] * len(excl_vals))
            where.append(f"COALESCE(remark,'待备注') NOT IN ({placeholders})")
            args.extend(_turso_val(v) for v in excl_vals)
        remark = self.cmb_remark.get()
        if remark and remark != "全部":
            where.append("COALESCE(remark,'待备注') = ?")
            args.append(_turso_val(remark))
        anomaly = self.cmb_anomaly.get()
        if anomaly in ("有ADX-无分销", "有分销-但素材不对版"):
            where.append("COALESCE(anomaly_tag,'') = ?")
            args.append(_turso_val(anomaly))
        elif anomaly == "仅正常素材":
            where.append("COALESCE(anomaly_tag,'') = ''")
        vs = self.cmb_view_status.get()
        if vs and vs != "全部":
            where.append("COALESCE(view_status,'未看') = ?")
            args.append(_turso_val(vs))
        audio_lang = self.cmb_audio_lang.get()
        if audio_lang and audio_lang != "全部":
            if audio_lang == "未识别":
                where.append("(audio_language IS NULL OR audio_language = '')")
            else:
                # 中文显示名 → DB原值，严格精确匹配
                db_val = getattr(self, '_audio_display_to_db', {}).get(audio_lang, audio_lang)
                where.append("audio_language = ?")
                args.append(_turso_val(db_val))
        df = self.ent_date_from.get().strip()
        if df:
            where.append("first_seen >= ?")
            args.append(_turso_val(df))
        dt = self.ent_date_to.get().strip()
        if dt:
            where.append("first_seen <= ?")
            args.append(_turso_val(dt))
        sf = self.ent_synced_from.get().strip()
        if sf:
            ts = _date_to_ts(sf)
            if ts:
                where.append("synced_at >= ?")
                args.append(_turso_val(ts))
        st = self.ent_synced_to.get().strip()
        if st:
            ts = _date_to_ts(st, end_of_day=True)
            if ts:
                where.append("synced_at <= ?")
                args.append(_turso_val(ts))
        kw = self.ent_keyword.get().strip()
        if kw:
            where.append("drama_name LIKE ?")
            args.append(_turso_val(f"%{kw}%"))
        return where, args

    def _get_dur_filter(self) -> str:
        """从时长勾选生成 SQL 片段（OR 组合）。
        duration=0 或 NULL 表示无时长数据，始终保留不过滤。"""
        _DR = {
            "0-5": "(duration > 0 AND duration < 300000)",
            "5-10": "(duration >= 300000 AND duration < 600000)",
            "10-15": "(duration >= 600000 AND duration < 900000)",
            "15+": "(duration >= 900000)",
        }
        parts = [_DR[k] for k, v in self._dur_vars.items() if v.get() and k in _DR]
        if not parts:
            return ""
        # 无时长数据的素材始终包含
        parts.append("COALESCE(duration,0) = 0")
        return f"({' OR '.join(parts)})"

    def _build_search_query(self) -> tuple[str, list[dict]]:
        """搜索预览：按剧名分组，返回素材数和评分。"""
        where, args = self._get_common_where()
        score_val = self.cmb_score.get()
        min_score = int(score_val) if score_val.isdigit() else 0
        having = f"HAVING jx_count >= {min_score} " if min_score > 0 else ""
        sql = (
            f"SELECT theater, language, drama_name, COUNT(*) as cnt, "
            f"CAST(SUM(CASE WHEN COALESCE(remark,'待备注')='精选' "
            f"THEN 1 ELSE 0 END) AS INTEGER) as jx_count, "
            f"GROUP_CONCAT(DISTINCT COALESCE(audio_language,'')) as audio_langs "
            f"FROM materials WHERE {' AND '.join(where)} "
            f"GROUP BY theater, language, drama_name "
            f"{having}"
            f"ORDER BY cnt DESC LIMIT 200"
        )
        return sql, args

    _DETAIL_BATCH = 20  # 每批最多 OR 数量，避免 SQLite 表达式树超限

    def _query_detail_batch(self, selections: list[dict]) -> list[dict]:
        """分批查询素材明细，避免 SQLite expression tree too large 错误。"""
        dur = self._get_dur_filter()
        # 收集通用筛选条件（包含音频语种、排除项等）
        common_where, common_args = self._get_common_where()
        all_rows: list[dict] = []
        seen_ids: set[str] = set()
        for i in range(0, len(selections), self._DETAIL_BATCH):
            batch = selections[i:i + self._DETAIL_BATCH]
            where, args = list(common_where), list(common_args)
            or_parts = []
            for sel in batch:
                parts = [
                    "COALESCE(theater,'') = ?",
                    "COALESCE(language,'') = ?",
                    "COALESCE(drama_name,'') = ?",
                ]
                args.append(_turso_val(sel.get("theater", "")))
                args.append(_turso_val(sel.get("language", "")))
                args.append(_turso_val(sel.get("drama_name", "")))
                or_parts.append(f"({' AND '.join(parts)})")
            where.append(f"({' OR '.join(or_parts)})")
            if dur:
                where.append(dur)
            sql = (
                f"SELECT material_id, theater, language, drama_name, "
                f"video_url, first_seen, COALESCE(remark,'待备注') as remark, "
                f"COALESCE(view_status,'未看') as view_status, "
                f"COALESCE(duration,0) as duration "
                f"FROM materials WHERE {' AND '.join(where)} "
                f"ORDER BY first_seen DESC"
            )
            rows = local_query(sql, args)
            for r in rows:
                mid = r.get("material_id", "")
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    all_rows.append(r)
        return all_rows

    def _get_selected_dramas(self) -> list[dict]:
        """从 tree 选中行提取 (theater, language, drama_name)。"""
        iid_map = getattr(self, "_iid_to_row", {})
        result = []
        for item_id in self._selected:
            r = iid_map.get(item_id)
            if r:
                result.append({
                    "theater": r.get("theater") or "",
                    "language": r.get("language") or "",
                    "drama_name": r.get("drama_name") or "",
                })
        return result

    def _search(self):
        sql, args = self._build_search_query()
        self.lbl_status.config(text="搜索中...")
        self.update()
        try:
            rows = local_query(sql, args or None)
        except Exception as e:
            messagebox.showerror("查询失败", str(e))
            self.lbl_status.config(text="查询失败")
            return
        self._rows = rows
        self._selected.clear()
        self.tree.delete(*self.tree.get_children())
        # 最少素材数过滤（低于阈值不自动勾选）
        min_mat_val = self.cmb_min_mat.get()
        min_mat = int(min_mat_val) if min_mat_val.isdigit() else 0
        auto_sel = 0
        self._iid_to_row = {}
        for r in rows:
            cnt = int(r.get("cnt", 0))
            checked = cnt >= min_mat
            # 音频语种：缩写 → 中文
            _raw_audio = r.get("audio_langs") or ""
            _audio_parts = []
            for _p in _raw_audio.split(","):
                _p = _p.strip()
                if _p:
                    _alz = getattr(self, '_audio_lang_zh', {})
                    _audio_parts.append(_alz.get(_p, _p))
            _audio_display = ",".join(_audio_parts) if _audio_parts else ""
            iid = self.tree.insert("", "end", values=(
                "✔" if checked else "",
                r.get("theater") or "", r.get("language") or "",
                _audio_display,
                r.get("drama_name") or "",
                cnt, r.get("jx_count", 0),
            ))
            self._iid_to_row[iid] = r
            if checked:
                self._selected.add(iid)
                auto_sel += 1
        self.lbl_status.config(
            text=f"找到 {len(rows)} 个剧名，已勾选 {auto_sel} 个"
            + (f"（素材≥{min_mat}）" if min_mat > 0 else "（全选）")
        )

    def _export_csv(self):
        """导出选中行明细为 CSV。"""
        sels = self._get_selected_dramas()
        if not sels:
            messagebox.showinfo("提示", "请先搜索并选中至少一行")
            return
        self.lbl_status.config(text="导出中...")
        self.update()
        try:
            rows = self._query_detail_batch(sels)
        except Exception as e:
            messagebox.showerror("查询失败", str(e))
            self.lbl_status.config(text="导出失败")
            return
        if not rows:
            messagebox.showinfo("提示", "无匹配数据")
            self.lbl_status.config(text="就绪")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"turso_export_{len(rows)}.csv")
        if not path:
            self.lbl_status.config(text="就绪")
            return
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=["material_id", "theater", "language", "drama_name",
                         "video_url", "first_seen", "remark", "view_status",
                         "duration"],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(buf.getvalue())
        self.lbl_status.config(text=f"已导出 {len(rows)} 条到 {Path(path).name}")

    def _start_download(self):
        if self._downloading:
            return
        sels = self._get_selected_dramas()
        if not sels:
            messagebox.showinfo("提示", "请先搜索并选中至少一行")
            return
        self.lbl_status.config(text="查询素材明细...")
        self.update()
        try:
            detail_rows = self._query_detail_batch(sels)
        except Exception as e:
            messagebox.showerror("查询失败", str(e))
            self.lbl_status.config(text="查询失败")
            return
        if not detail_rows:
            messagebox.showinfo("提示", "无匹配数据可下载")
            self.lbl_status.config(text="就绪")
            return
        self.cfg["download_root"] = self.ent_path.get().strip()
        save_config(self.cfg)
        self._downloading = True
        self._cancel_event.clear()
        self.btn_download.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self._dl_rows = detail_rows
        Thread(target=self._do_download, daemon=True).start()

    def _cancel_download(self):
        self._cancel_event.set()
        self.btn_cancel.config(state="disabled")
        self.lbl_status.config(text="正在取消...")

    def _do_download(self):
        root = Path(self.ent_path.get().strip())
        style = self._folder_style.get()
        rows = self._dl_rows
        total = len(rows)
        success = skip = failed = 0
        _dl_log(f"=== 开始下载: {total} 条, 目标={root} ===")

        for i, row in enumerate(rows):
            if self._cancel_event.is_set():
                break

            video_url = row.get("video_url") or ""
            if not video_url:
                skip += 1
                _dl_log(f"SKIP [{i+1}/{total}] {row.get('drama_name','')} - 无video_url")
                self.after(0, lambda i=i, s=success, sk=skip, f=failed: self._update_progress(i + 1, total, s, sk, f))
                continue

            folder_name = make_folder_name(
                row.get("theater", ""), row.get("language", ""),
                row.get("drama_name", ""), row.get("first_seen", ""),
                row.get("remark", "待备注"), style=style)
            folder = root / folder_name

            m = HASH_RE.search(video_url)
            fname = (m.group(0) + ".mp4") if m else video_url.split("/")[-1]
            dest = folder / fname

            if dest.exists():
                skip += 1
                _dl_log(f"SKIP [{i+1}/{total}] {fname} - 文件已存在: {dest}")
            elif download_file(video_url, dest, cancel_event=self._cancel_event, file_timeout=120):
                success += 1
            else:
                failed += 1

            self.after(0, lambda i=i, s=success, sk=skip, f=failed: self._update_progress(i + 1, total, s, sk, f))

        cancelled = self._cancel_event.is_set()
        self.after(0, lambda s=success, sk=skip, f=failed, c=cancelled: self._download_done(s, sk, f, total, c))

    def _update_progress(self, current, total, success, skip, failed=0):
        pct = int(current / total * 100) if total else 0
        self.progress["value"] = pct
        self.lbl_status.config(text=f"下载中 {current}/{total}  成功{success} 跳过{skip} 失败{failed}")

    def _download_done(self, success, skip, failed, total, cancelled=False):
        self._downloading = False
        self.btn_download.config(state="normal")
        self.btn_cancel.config(state="disabled")
        self.progress["value"] = 100
        if cancelled:
            self.lbl_status.config(text=f"已取消! 成功{success} 跳过{skip} 失败{failed} 共{total}")
            messagebox.showinfo("已取消", f"成功: {success}\n跳过: {skip}\n失败: {failed}\n共: {total}")
        else:
            self.lbl_status.config(text=f"完成! 成功{success} 跳过{skip} 失败{failed} 共{total}")
            messagebox.showinfo("下载完成", f"成功: {success}\n跳过: {skip}\n失败: {failed}\n共: {total}")


if __name__ == "__main__":
    app = App()
    app.mainloop()
