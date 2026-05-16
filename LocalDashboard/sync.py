"""
AdXray 本地数据同步

两种模式：
  主机 (is_master=true):  D1 → SQLite → gzip → 上传 R2
  客户端 (is_master=false): R2 下载 → gunzip → SQLite

所有电脑只读本地 SQLite，每天只需一次网络请求。
"""
import gzip
import hashlib
import hmac
import json
import sqlite3
import ssl
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib import request
from urllib.error import HTTPError, URLError

# ─── 路径 ────────────────────────────────────────
_DIR = Path(__file__).parent
DB_PATH = _DIR / "cache.db"
GZ_PATH = _DIR / "cache.db.gz"
_CONFIG_CANDIDATES = [
    _DIR.parent / "config.json",               # LocalDashboard 在子项目内部
    _DIR.parent / "4seeall" / "config.json",    # LocalDashboard 与子项目同级
    _DIR.parent / "4others" / "config.json",
]

_SSL_CTX = ssl.create_default_context()

# 需要同步的表
SYNC_TABLES = ["materials", "drama_stats", "violation_checks", "haibao_dramas"]

R2_OBJECT_KEY = "cache.db.gz"


# ═══════════════════════════════════════════════════
# 配置加载
# ═══════════════════════════════════════════════════

def _load_config():
    for p in _CONFIG_CANDIDATES:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    raise FileNotFoundError("找不到 config.json")


# ═══════════════════════════════════════════════════
# D1 拉取（仅主机使用）
# ═══════════════════════════════════════════════════

def _load_d1_config():
    """从 d1_config.json 加载 D1 配置。"""
    for p in [_DIR.parent / "d1_config.json", _DIR.parent.parent / "d1_config.json"]:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    raise FileNotFoundError("找不到 d1_config.json")


def _d1_fetch(account_id, db_id, token, sql):
    """执行一条 D1 SQL 并返回 (cols, rows)。"""
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{db_id}/query"
    body = json.dumps({"sql": sql}).encode()
    req = request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    with request.urlopen(req, timeout=300, context=_SSL_CTX) as resp:
        data = json.loads(resp.read())
    if not data.get("success"):
        errs = data.get("errors", [])
        raise RuntimeError(errs[0].get("message", "?") if errs else "unknown")
    result_arr = data.get("result", [])
    if not result_arr:
        return [], []
    r0 = result_arr[0]
    results = r0.get("results", [])
    if not results:
        return [], []
    cols = list(results[0].keys())
    rows = [[row.get(c) for c in cols] for row in results]
    return cols, rows


def _d1_fetch_raw(account_id, db_id, token, sql):
    """原始查询，返回 results 数组（list[dict]）。"""
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{db_id}/query"
    body = json.dumps({"sql": sql}).encode()
    req = request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    with request.urlopen(req, timeout=300, context=_SSL_CTX) as resp:
        data = json.loads(resp.read())
    if not data.get("success"):
        errs = data.get("errors", [])
        raise RuntimeError(errs[0].get("message", "?") if errs else "unknown")
    result_arr = data.get("result", [])
    if not result_arr:
        return []
    return result_arr[0].get("results", [])


def _d1_fetch_paged(account_id, db_id, token, table, page_size=5000):
    """游标分页拉取大表。返回 (cols, all_rows)。
    先用 PRAGMA 取真实 schema，按列名从 JSON dict 取值，
    避免首行某字段为 NULL 时被 D1 JSON 省略导致丢列。"""
    schema = _d1_fetch_raw(
        account_id, db_id, token, f"PRAGMA table_info([{table}])")
    cols = [r["name"] for r in schema]
    if not cols:
        return [], []
    # 找 INTEGER PRIMARY KEY 作游标（D1 会把 rowid 别名为该列）；否则用 rowid
    int_pk = None
    for r in schema:
        if r.get("pk") and (r.get("type") or "").upper() == "INTEGER":
            int_pk = r["name"]
            break
    cursor_col = int_pk or "rowid"
    select_extra = "" if int_pk else "rowid, "
    all_rows = []
    last_id = 0
    while True:
        print(f"  {table}: 拉取中 ({cursor_col}>{last_id})...", flush=True)
        sql = (f"SELECT {select_extra}* FROM [{table}] "
               f"WHERE {cursor_col} > {last_id} ORDER BY {cursor_col} LIMIT {page_size}")
        results = _d1_fetch_raw(account_id, db_id, token, sql)
        if not results:
            break
        new_id = results[-1].get(cursor_col)
        if new_id is None:
            print(f"  警告: 末行无游标列 {cursor_col}，停止", flush=True)
            break
        last_id = new_id
        # 按 PRAGMA 顺序取值，缺字段视为 NULL
        for row in results:
            all_rows.append([row.get(c) for c in cols])
        print(f"  {table}: 已拉取 {len(all_rows)} 行", flush=True)
        if len(results) < page_size:
            break
        time.sleep(0.5)
    return cols, all_rows


def _build_sqlite(cfg):
    """从 D1 拉取全部表，写入本地 SQLite。"""
    d1_cfg = _load_d1_config()
    account_id = d1_cfg["account_id"]
    db_id = d1_cfg["database_id"]
    token = d1_cfg["api_token"]
    print("[sync] 从 D1 拉取数据...", flush=True)
    t0 = time.time()

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")

    for table in SYNC_TABLES:
        try:
            cols, rows = _d1_fetch_paged(account_id, db_id, token, table)
            if not cols:
                print(f"  {table}: 空表，跳过")
                continue
            conn.execute(f"DROP TABLE IF EXISTS [{table}]")
            col_defs = ", ".join(f'"{c}" TEXT' for c in cols)
            conn.execute(f"CREATE TABLE [{table}] ({col_defs})")
            placeholders = ", ".join(["?"] * len(cols))
            conn.executemany(f"INSERT INTO [{table}] VALUES ({placeholders})", rows)
            print(f"  {table}: {len(rows)} 行")
        except Exception as e:
            print(f"  {table}: 拉取失败 - {e}")

    conn.execute("CREATE TABLE IF NOT EXISTS _sync_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("REPLACE INTO _sync_meta VALUES ('last_sync', ?)", (date.today().isoformat(),))
    conn.execute("REPLACE INTO _sync_meta VALUES ('sync_time', ?)", (time.strftime("%H:%M:%S"),))
    conn.commit()

    # 建索引加速查询
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_m_theater ON materials(theater)",
        "CREATE INDEX IF NOT EXISTS idx_m_lang ON materials(language)",
        "CREATE INDEX IF NOT EXISTS idx_m_first_seen ON materials(first_seen)",
        "CREATE INDEX IF NOT EXISTS idx_m_remark ON materials(remark)",
        "CREATE INDEX IF NOT EXISTS idx_m_view ON materials(view_status)",
        "CREATE INDEX IF NOT EXISTS idx_m_anomaly ON materials(anomaly_tag)",
        "CREATE INDEX IF NOT EXISTS idx_ds_date ON drama_stats(check_date)",
    ]:
        try:
            conn.execute(idx_sql)
        except Exception:
            pass
    conn.commit()
    conn.close()

    dt = time.time() - t0
    print(f"[sync] D1 拉取完成: {dt:.1f}s, {DB_PATH.stat().st_size/1024/1024:.1f} MB")


# ═══════════════════════════════════════════════════
# R2 上传/下载（S3v4 签名，纯标准库）
# ═══════════════════════════════════════════════════

def _sign_s3v4(method, url, headers, payload_hash, access_key, secret_key, region="auto", service="s3"):
    """AWS Signature V4 签名（最小实现，仅支持简单请求）。"""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.hostname
    path = parsed.path or "/"
    now = datetime.now(timezone.utc)
    datestamp = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")

    headers["x-amz-date"] = amz_date
    headers["x-amz-content-sha256"] = payload_hash
    headers["host"] = host

    signed_headers_list = sorted(headers.keys())
    signed_headers = ";".join(signed_headers_list)
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in signed_headers_list)

    canonical_request = f"{method}\n{path}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    credential_scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode()).hexdigest()}"

    def _hmac_sha256(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _hmac_sha256(f"AWS4{secret_key}".encode(), datestamp)
    k_region = hmac.new(k_date, region.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode(), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    auth = (f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")
    headers["authorization"] = auth
    return headers


def _r2_url(cfg):
    account_id = cfg["r2_account_id"]
    bucket = cfg.get("r2_bucket", "adxray-cache")
    return f"https://{account_id}.r2.cloudflarestorage.com/{bucket}/{R2_OBJECT_KEY}"


def r2_upload(cfg):
    """压缩 cache.db 并上传到 R2。"""
    if not DB_PATH.exists():
        raise FileNotFoundError("cache.db 不存在，请先同步")

    print("[R2] 压缩中...")
    raw = DB_PATH.read_bytes()
    gz_data = gzip.compress(raw, compresslevel=9)
    GZ_PATH.write_bytes(gz_data)
    print(f"[R2] 压缩完成: {len(raw)/1024/1024:.1f} MB → {len(gz_data)/1024/1024:.1f} MB")

    url = _r2_url(cfg)
    payload_hash = hashlib.sha256(gz_data).hexdigest()
    headers = {"content-type": "application/octet-stream"}
    headers = _sign_s3v4("PUT", url, headers, payload_hash,
                         cfg["r2_access_key_id"], cfg["r2_secret_key"])

    req = request.Request(url, data=gz_data, method="PUT", headers=headers)
    with request.urlopen(req, timeout=120, context=_SSL_CTX) as resp:
        resp.read()
    print(f"[R2] 上传完成: {url}")


def r2_download(cfg):
    """从 R2 下载压缩包并解压为 cache.db。"""
    url = _r2_url(cfg)
    payload_hash = "UNSIGNED-PAYLOAD"
    headers = {}
    headers = _sign_s3v4("GET", url, headers, payload_hash,
                         cfg["r2_access_key_id"], cfg["r2_secret_key"])

    print(f"[R2] 下载中...")
    req = request.Request(url, method="GET", headers=headers)
    with request.urlopen(req, timeout=120, context=_SSL_CTX) as resp:
        gz_data = resp.read()
    print(f"[R2] 下载完成: {len(gz_data)/1024/1024:.1f} MB, 解压中...")

    raw = gzip.decompress(gz_data)
    DB_PATH.write_bytes(raw)
    print(f"[R2] 解压完成: {DB_PATH.stat().st_size/1024/1024:.1f} MB → {DB_PATH}")


# ═══════════════════════════════════════════════════
# 抓取机 API → cache.db 同步音频语种
# ═══════════════════════════════════════════════════

def sync_audio_language(api_base: str | None = None, progress=None) -> tuple[int, int]:
    """从抓取机 FastAPI 拉取 audio_language 数据，写入本地 cache.db。

    api_base: 抓取机 API 基址，例如 http://100.93.81.26:8000；
              不传时从 config.json 的 crawler_api_base 读取。
    progress: 可选回调 progress(done:int, total:int, msg:str)。
    返回 (updated_rows, total_pulled)。"""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"cache.db 不存在: {DB_PATH}")

    if not api_base:
        cfg = _load_config()
        api_base = cfg.get("crawler_api_base") or cfg.get("api_base") or ""
    api_base = (api_base or "").rstrip("/")
    if not api_base:
        raise RuntimeError(
            "未配置抓取机 API 地址。请在 config.json 添加 \"crawler_api_base\": \"http://100.93.81.26:8000\"")

    page_size = 10000
    offset = 0
    pulled: list[tuple[str, str]] = []
    total = None
    while True:
        url = f"{api_base}/api/turso/audio_lang_export?limit={page_size}&offset={offset}"
        if progress:
            progress(len(pulled), total or 0, f"拉取 offset={offset}...")
        req = request.Request(url, method="GET",
                              headers={"User-Agent": "audio-lang-sync/1.0"})
        with request.urlopen(req, timeout=120, context=_SSL_CTX) as resp:
            data = json.loads(resp.read())
        rows = data.get("rows") or []
        total = data.get("total", total)
        for r in rows:
            h = (r.get("video_hash") or "").strip()
            lang = (r.get("audio_language") or "").strip()
            if h and lang:
                pulled.append((h, lang))
        if len(rows) < page_size:
            break
        offset += page_size

    if progress:
        progress(len(pulled), total or len(pulled), "写入本地缓存...")

    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        # 确保列存在
        try:
            conn.execute("ALTER TABLE materials ADD COLUMN audio_language TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        cur = conn.executemany(
            "UPDATE materials SET audio_language = ? WHERE video_hash = ?",
            [(lang, h) for h, lang in pulled])
        affected = cur.rowcount
        conn.execute("CREATE TABLE IF NOT EXISTS _sync_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("REPLACE INTO _sync_meta VALUES ('last_audio_lang_sync', ?)",
                     (datetime.now().isoformat(timespec='seconds'),))
        conn.commit()
    finally:
        conn.close()

    if progress:
        progress(len(pulled), total or len(pulled),
                 f"完成: 拉取 {len(pulled)} 条, 本地更新 {affected} 条")
    return affected, len(pulled)


# ═══════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════

def _need_sync():
    """检查今天是否已经同步过。"""
    if not DB_PATH.exists():
        return True
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.execute("SELECT value FROM _sync_meta WHERE key='last_sync'")
        row = cur.fetchone()
        conn.close()
        if row and row[0] == date.today().isoformat():
            return False
    except Exception:
        pass
    return True


def sync(force=False, force_client=False):
    """智能同步：
    - 主机 (is_master=true): D1 → SQLite → 压缩 → 上传 R2
    - 客户端 (is_master=false / force_client): R2 下载 → 解压 → SQLite
    返回 (synced: bool, msg: str)。
    """
    if not force and not _need_sync():
        return False, "今天已同步，跳过"

    cfg = _load_config()
    # 安全判断：强制客户端 / config 明确设为 false / 无 d1_config.json 都走客户端模式
    if force_client:
        is_master = False
    else:
        try:
            _load_d1_config()
            is_master = cfg.get("is_master", True)
        except FileNotFoundError:
            is_master = False
    has_r2 = all(cfg.get(k) for k in ("r2_account_id", "r2_access_key_id", "r2_secret_key"))

    t0 = time.time()

    if is_master:
        # 主机：从 D1 拉取
        _build_sqlite(cfg)
        # 如果配置了 R2 则上传
        if has_r2:
            try:
                r2_upload(cfg)
            except Exception as e:
                print(f"[R2] 上传失败: {e}")
    else:
        # 客户端：从 R2 下载
        if not has_r2:
            raise RuntimeError("客户端模式需要 R2 配置 (r2_account_id, r2_access_key_id, r2_secret_key)")
        r2_download(cfg)
        # 写入 sync_meta 标记今天已同步
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("CREATE TABLE IF NOT EXISTS _sync_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("REPLACE INTO _sync_meta VALUES ('last_sync', ?)", (date.today().isoformat(),))
        conn.execute("REPLACE INTO _sync_meta VALUES ('sync_time', ?)", (time.strftime("%H:%M:%S"),))
        conn.execute("REPLACE INTO _sync_meta VALUES ('source', 'r2')")
        conn.commit()
        conn.close()

    dt = time.time() - t0
    mode = "主机" if is_master else "客户端"
    size_mb = DB_PATH.stat().st_size / 1024 / 1024 if DB_PATH.exists() else 0
    msg = f"[{mode}] 同步完成: {dt:.1f}s, 本地 {size_mb:.1f} MB"
    print(f"[sync] {msg}")
    return True, msg


def get_db():
    """获取本地 SQLite 连接（只读）。如果当天未同步则先同步。"""
    if _need_sync():
        sync()
    if not DB_PATH.exists():
        raise FileNotFoundError(
            "本地缓存不存在，请先运行 LocalDashboard/sync.bat 拉取数据")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def query(sql, params=None):
    """便捷查询：返回 list[dict]。"""
    conn = get_db()
    cur = conn.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.close()
    return rows


if __name__ == "__main__":
    import sys
    if "--audio-lang" in sys.argv:
        def _p(done, total, msg):
            print(f"  [{done}/{total}] {msg}", flush=True)
        affected, pulled = sync_audio_language(progress=_p)
        print(f"音频语种同步完成: 拉取 {pulled} 条, 本地更新 {affected} 条")
    else:
        force = "--force" in sys.argv
        client = "--client" in sys.argv
        synced, msg = sync(force=force, force_client=client)
        print(msg)
