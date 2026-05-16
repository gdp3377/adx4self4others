"""
AdXray 数据库变更通知工具（独立版）
每天早中晚自动查询 Turso 数据库，统计素材入库/标注/删除情况，发飞书通知。
配置复用 config.json（turso_url, turso_token），额外需填 feishu_webhook。
"""
import json
import ssl
import sys
import time
from datetime import datetime, date
from pathlib import Path
from urllib import request

# 导入本地同步模块（支持多种目录结构）
_HERE = Path(__file__).parent
for _candidate in [_HERE / "LocalDashboard", _HERE.parent / "LocalDashboard"]:
    if (_candidate / "sync.py").exists():
        sys.path.insert(0, str(_candidate))
        break
else:
    sys.path.insert(0, str(_HERE.parent / "LocalDashboard"))
from sync import get_db, sync

# ─── SSL 兜底 ────────────────────────────────────
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _urlopen(req, timeout=45):
    return request.urlopen(req, timeout=timeout, context=_SSL_CTX)


# ─── 配置 ────────────────────────────────────────
CONFIG_FILE = Path(__file__).parent / "config.json"
SNAPSHOT_FILE = Path(__file__).parent / "db_snapshot.json"

# 每天三次运行时间（时:分）
SCHEDULE_TIMES = ["09:00", "13:00", "21:00"]


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_snapshot() -> dict:
    if SNAPSHOT_FILE.exists():
        with open(SNAPSHOT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_snapshot(data: dict):
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ─── 本地 SQLite 查询 ────────────────────────

def local_query(sql, params=None):
    """查询本地 SQLite 缓存，返回 list[dict]。"""
    conn = get_db()
    cur = conn.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.close()
    return rows


# ─── 飞书发送 ────────────────────────────────────

def send_feishu(webhook_url: str, text: str):
    body = json.dumps({"msg_type": "text", "content": {"text": text}}).encode()
    req = request.Request(webhook_url, data=body, method="POST", headers={
        "Content-Type": "application/json",
    })
    with _urlopen(req, timeout=10) as resp:
        resp.read()


# ─── 语种缩写 ────────────────────────────────────

LANG_SHORT = {
    "中文": "中", "英文": "英", "西班牙语": "西", "葡萄牙语": "葡",
    "德语": "德", "法语": "法", "越南语": "越", "泰语": "泰",
    "日语": "日", "韩语": "韩", "阿拉伯语": "阿", "俄语": "俄",
    "印尼语": "印尼", "印地语": "印地", "挪威语": "挪", "爱尔兰语": "爱",
    "马来语": "马", "菲律宾语": "菲", "土耳其语": "土", "意大利语": "意",
    "波兰语": "波", "荷兰语": "荷", "瑞典语": "瑞", "丹麦语": "丹",
    "芬兰语": "芬", "罗马尼亚语": "罗", "匈牙利语": "匈", "捷克语": "捷",
    "希腊语": "希", "希伯来语": "希伯", "乌克兰语": "乌", "孟加拉语": "孟",
    "缅甸语": "缅", "高棉语": "高", "老挝语": "老", "斯瓦希里语": "斯",
}


# ─── 核心逻辑 ────────────────────────────────────

def query_current_stats() -> dict:
    """查询本地数据库当前快照：总量 + 各剧场语种分布 + 备注分布。"""
    # 1. 各剧场×语种 总量
    rows = local_query(
        "SELECT theater, language, COUNT(*) as cnt "
        "FROM materials GROUP BY theater, language ORDER BY theater, language")
    by_theater_lang = {}
    for r in rows:
        key = f"{r['theater']}|{r['language']}"
        by_theater_lang[key] = int(r["cnt"])

    # 2. 各剧场×语种×备注
    rows = local_query(
        "SELECT theater, language, COALESCE(remark,'待备注') as remark, COUNT(*) as cnt "
        "FROM materials GROUP BY theater, language, remark ORDER BY theater, language, remark")
    by_theater_lang_remark = {}
    for r in rows:
        key = f"{r['theater']}|{r['language']}|{r['remark']}"
        by_theater_lang_remark[key] = int(r["cnt"])

    # 3. 总量
    rows = local_query("SELECT COUNT(*) as cnt FROM materials")
    total = int(rows[0]["cnt"]) if rows else 0

    # 4. 今日新增（synced_at >= 今天 00:00）
    today_start = int(datetime.combine(date.today(), datetime.min.time()).timestamp())
    rows = local_query(
        "SELECT theater, language, COUNT(*) as cnt "
        "FROM materials WHERE synced_at >= ? "
        "GROUP BY theater, language ORDER BY theater, language",
        [today_start])
    today_new = {}
    for r in rows:
        key = f"{r['theater']}|{r['language']}"
        today_new[key] = int(r["cnt"])

    return {
        "total": total,
        "by_theater_lang": by_theater_lang,
        "by_theater_lang_remark": by_theater_lang_remark,
        "today_new": today_new,
        "ts": time.time(),
        "date": date.today().isoformat(),
    }


def build_report(current: dict, previous: dict) -> str:
    """对比当前与上次快照，生成变更报告。"""
    now_str = datetime.now().strftime("%Y/%m/%d %H:%M")
    lines = [f"📊 数据库日报  {now_str}"]
    lines.append(f"素材总量：{current['total']}")

    prev_total = previous.get("total", 0)
    diff_total = current["total"] - prev_total
    if diff_total != 0:
        sign = "+" if diff_total > 0 else ""
        lines.append(f"较上次变化：{sign}{diff_total}")
    lines.append("")

    # ── 今日入库 ──
    today_new = current.get("today_new", {})
    if today_new:
        lines.append("【今日入库】")
        # 按剧场分组
        theaters = {}
        for key, cnt in today_new.items():
            t, lang = key.split("|", 1)
            theaters.setdefault(t, []).append((lang, cnt))
        for t in sorted(theaters):
            items = theaters[t]
            total_t = sum(c for _, c in items)
            lang_parts = []
            for lang, cnt in sorted(items, key=lambda x: -x[1]):
                short = LANG_SHORT.get(lang, lang)
                lang_parts.append(f"{short}:{cnt}")
            # 每行两个
            pair_lines = []
            for i in range(0, len(lang_parts), 2):
                pair_lines.append(" | ".join(lang_parts[i:i+2]))
            lines.append(f"{t}  新增:{total_t}")
            for pl in pair_lines:
                lines.append(f"  {pl}")
        lines.append("")

    # ── 标注变化（精选/违规） ──
    prev_remark = previous.get("by_theater_lang_remark", {})
    curr_remark = current.get("by_theater_lang_remark", {})
    all_keys = set(list(prev_remark.keys()) + list(curr_remark.keys()))

    featured_changes = []  # (theater, lang, diff)
    violation_changes = []
    deleted_changes = []  # 总量减少的

    for key in all_keys:
        parts = key.split("|", 2)
        if len(parts) < 3:
            continue
        t, lang, remark = parts
        prev_cnt = prev_remark.get(key, 0)
        curr_cnt = curr_remark.get(key, 0)
        diff = curr_cnt - prev_cnt
        if diff == 0:
            continue
        if remark == "精选":
            featured_changes.append((t, lang, diff))
        elif remark == "违规":
            violation_changes.append((t, lang, diff))

    # 总量减少 → 可能删除
    prev_tl = previous.get("by_theater_lang", {})
    curr_tl = current.get("by_theater_lang", {})
    for key in prev_tl:
        prev_c = prev_tl.get(key, 0)
        curr_c = curr_tl.get(key, 0)
        diff = curr_c - prev_c
        if diff < 0:
            t, lang = key.split("|", 1)
            deleted_changes.append((t, lang, abs(diff)))

    if featured_changes:
        lines.append("【新增精选】")
        for t, lang, diff in sorted(featured_changes, key=lambda x: -x[2]):
            short = LANG_SHORT.get(lang, lang)
            lines.append(f"  {t} {short}:+{diff}")
        lines.append("")

    if violation_changes:
        lines.append("【新增违规】")
        for t, lang, diff in sorted(violation_changes, key=lambda x: -x[2]):
            short = LANG_SHORT.get(lang, lang)
            lines.append(f"  {t} {short}:+{diff}")
        lines.append("")

    if deleted_changes:
        lines.append("【疑似删除】")
        for t, lang, cnt in sorted(deleted_changes, key=lambda x: -x[2]):
            short = LANG_SHORT.get(lang, lang)
            lines.append(f"  {t} {short}:-{cnt}")
        lines.append("")

    if not today_new and not featured_changes and not violation_changes and not deleted_changes:
        lines.append("暂无变更")

    return "\n".join(lines)


# ─── 定时调度 ────────────────────────────────────

def run_once(cfg: dict):
    """执行一次查询 + 对比 + 发送。"""
    webhook = cfg.get("feishu_webhook", "")

    if not webhook:
        print("[ERROR] config.json 中 feishu_webhook 为空")
        return

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 查询本地数据库...")
    try:
        current = query_current_stats()
    except Exception as e:
        print(f"[ERROR] 查询失败: {e}")
        try:
            send_feishu(webhook, f"⚠️ 数据库日报查询失败: {e}")
        except Exception:
            pass
        return

    previous = load_snapshot()
    report = build_report(current, previous)
    print(report)
    print()

    try:
        send_feishu(webhook, report)
        print("[OK] 飞书通知已发送")
    except Exception as e:
        print(f"[ERROR] 飞书发送失败: {e}")

    save_snapshot(current)


def should_trigger(last_run_date, last_times):
    """检查当前时刻是否应触发某个时间点。返回触发的时间点或 None。"""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    current_hm = now.strftime("%H:%M")

    for t in SCHEDULE_TIMES:
        if current_hm >= t:
            key = f"{today}_{t}"
            if key not in last_times:
                return key
    return None


def main():
    print("=" * 50)
    print("  AdXray 数据库变更通知工具")
    print(f"  通知时间：{', '.join(SCHEDULE_TIMES)}")
    print("=" * 50)

    cfg = load_config()
    if not cfg.get("feishu_webhook"):
        print("\n[提示] config.json 中未配置 feishu_webhook，正在添加...")
        cfg["feishu_webhook"] = "https://open.feishu.cn/open-apis/bot/v2/hook/27882394-3305-4474-83ca-fd74c8a53f48"
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print("[OK] 已写入 feishu_webhook")

    triggered: list[str] = []

    # 启动时立即执行一次
    print("\n[启动] 执行首次查询...")
    run_once(cfg)

    print(f"\n[运行中] 等待下一个通知时间点...")
    while True:
        key = should_trigger(date.today().isoformat(), triggered)
        if key:
            triggered.append(key)
            cfg = load_config()  # 重新读取配置
            run_once(cfg)
            print(f"\n[运行中] 等待下一个通知时间点...")

        # 每日清理已触发记录
        today = date.today().isoformat()
        triggered = [t for t in triggered if t.startswith(today)]

        time.sleep(30)


if __name__ == "__main__":
    main()
