# -*- coding: utf-8 -*-
"""SQLite 持久化层 (全栈专家为「山河回响」引入的数据库层).

把民族曲库(原 JSON 文件)与 UI 状态(原 JSON 文件)迁移到本地 SQLite,
作为后端的主存储: 读取优先 SQLite, 原 JSON 文件保留为 fallback / 种子。

运行目录口径与 mountainriverechoes.py 保持一致:
  - 源码运行: DATA_DIR = 仓库根
  - 冻结运行(PyInstaller exe): DATA_DIR = exe 同级 MountainRiverEchoes-Naxi-data 目录
"""
import sys
import json
import re
import sqlite3
import threading
from pathlib import Path
from contextlib import suppress

# ---------------------------------------------------------------- 运行目录解析
if getattr(sys, 'frozen', False):
    _EXE_DIR = Path(sys.executable).resolve().parent
    DATA_DIR = _EXE_DIR / 'MountainRiverEchoes-Naxi-data'
else:
    DATA_DIR = Path(__file__).resolve().parent.parent  # webui/.. -> 仓库根

DB_PATH = DATA_DIR / 'webui' / 'ethnos.db'
CACHE_DIR = DATA_DIR / 'webui' / 'ethnos_cache'
UI_STATE_PATH = DATA_DIR / 'webui' / 'ui_state.json'

_LOCK = threading.RLock()

_TRACK_KEY_RE = re.compile(r'^(e\d{2}|t\d+)(_.*)?\.json$')
_HAN_RE = re.compile(r'^han(_.*)?\.json$')


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with suppress(Exception):
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn=None):
    own = conn is None
    c = conn or _connect()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS ethnic_groups (
        key          TEXT PRIMARY KEY,
        name         TEXT,
        group_name   TEXT,
        cover        TEXT,
        count        INTEGER,
        built_at     REAL,
        sources_json TEXT,
        kws_json     TEXT,
        partial      INTEGER,
        payload_json TEXT
    );
    CREATE TABLE IF NOT EXISTS tracks (
        group_key    TEXT,
        id           TEXT,
        idx          INTEGER,
        song_name    TEXT,
        singers      TEXT,
        album        TEXT,
        source       TEXT,
        source_cn    TEXT,
        ext          TEXT,
        duration_s   INTEGER,
        cover_url    TEXT,
        lyric        INTEGER,
        previewable  INTEGER,
        downloadable INTEGER,
        album_url    TEXT,
        raw_json     TEXT,
        PRIMARY KEY (group_key, id)
    );
    CREATE INDEX IF NOT EXISTS ix_tracks_group ON tracks(group_key);
    CREATE TABLE IF NOT EXISTS ui_state (
        key        TEXT PRIMARY KEY,
        value_json TEXT
    );
    """)
    if own:
        c.commit()
        c.close()


def _track_to_row(group_key, idx, t):
    return (
        group_key, t.get('id'), idx, t.get('song_name'), t.get('singers'),
        t.get('album'), t.get('source'), t.get('source_cn'), t.get('ext'),
        t.get('duration_s'), t.get('cover_url'),
        1 if t.get('lyric') else 0,
        1 if t.get('previewable') else 0,
        1 if t.get('downloadable') else 0,
        t.get('album_url'), json.dumps(t, ensure_ascii=False),
    )


def migrate_group(key, payload):
    """把一个民族的 payload(含 tracks 列表)写入 SQLite. 幂等: 先删后插."""
    if not payload:
        return 0
    tracks = payload.get('tracks') or []
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO ethnic_groups(key,name,group_name,cover,count,built_at,sources_json,kws_json,partial,payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     name=excluded.name, group_name=excluded.group_name, cover=excluded.cover,
                     count=excluded.count, built_at=excluded.built_at, sources_json=excluded.sources_json,
                     kws_json=excluded.kws_json, partial=excluded.partial, payload_json=excluded.payload_json""",
                (key, payload.get('name'), payload.get('group'), payload.get('cover'),
                 payload.get('count', len(tracks)), payload.get('built_at'),
                 json.dumps(payload.get('sources') or [], ensure_ascii=False),
                 json.dumps(payload.get('kws') or [], ensure_ascii=False),
                 1 if payload.get('partial') else 0,
                 json.dumps(payload, ensure_ascii=False)))
            conn.execute("DELETE FROM tracks WHERE group_key=?", (key,))
            conn.executemany(
                "INSERT OR REPLACE INTO tracks(group_key,id,idx,song_name,singers,album,source,source_cn,ext,duration_s,cover_url,lyric,previewable,downloadable,album_url,raw_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [_track_to_row(key, i, t) for i, t in enumerate(tracks)])
            conn.commit()
            return len(tracks)
        finally:
            conn.close()


def migrate_json_dir():
    """遍历 ethnos_cache 下所有合法曲库 JSON 并迁移. 返回迁移的民族数."""
    if not CACHE_DIR.exists():
        return 0
    n = 0
    for p in sorted(CACHE_DIR.glob('*.json')):
        name = p.name
        m = _TRACK_KEY_RE.match(name) or _HAN_RE.match(name)
        if not m:
            continue  # 跳过 index_snapshot.json 等非曲库文件
        key = m.group(1)
        with suppress(Exception):
            payload = json.loads(p.read_text(encoding='utf-8'))
            if 'tracks' not in payload:
                continue
            migrate_group(key, payload)
            n += 1
    return n


def migrate_ui_state():
    if not UI_STATE_PATH.exists():
        return 0
    with suppress(Exception):
        data = json.loads(UI_STATE_PATH.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            with _LOCK:
                conn = _connect()
                try:
                    conn.executemany(
                        "INSERT OR REPLACE INTO ui_state(key,value_json) VALUES(?,?)",
                        [(k, json.dumps(v, ensure_ascii=False)) for k, v in data.items()])
                    conn.commit()
                finally:
                    conn.close()
            return len(data)
    return 0


def ensure_migrated():
    """幂等: 首次(库空)从 JSON 全量迁移; 之后的增量由写入路径触发."""
    init_schema()
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute("SELECT COUNT(*) FROM ethnic_groups").fetchone()[0]
        finally:
            conn.close()
    if cur == 0:
        migrate_json_dir()
        migrate_ui_state()


# ---------------------------------------------------------------- 查询
def get_group(key):
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM ethnic_groups WHERE key=?", (key,)).fetchone()
        finally:
            conn.close()
    return row


def get_tracks(key):
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT raw_json FROM tracks WHERE group_key=? ORDER BY idx", (key,)).fetchall()
        finally:
            conn.close()
    out = []
    for r in rows:
        with suppress(Exception):
            out.append(json.loads(r['raw_json']))
    return out


def count_tracks(key):
    with _LOCK:
        conn = _connect()
        try:
            v = conn.execute("SELECT COUNT(*) FROM tracks WHERE group_key=?", (key,)).fetchone()[0]
        finally:
            conn.close()
    return v


def iter_groups():
    """遍历所有民族(key, payload_dict), 用于全局索引聚合(替代遍历 JSON)."""
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT key, payload_json FROM ethnic_groups ORDER BY key").fetchall()
        finally:
            conn.close()
    for r in rows:
        with suppress(Exception):
            yield (r['key'], json.loads(r['payload_json']))


def group_to_payload(key):
    """从 SQLite 重建一个民族的完整 payload(含 tracks), 供 load_ethnos_playlist 使用."""
    row = get_group(key)
    if not row:
        return None
    payload = json.loads(row['payload_json']) if row['payload_json'] else {}
    payload['tracks'] = get_tracks(key)
    payload['count'] = len(payload['tracks'])
    return payload


# ---------------------------------------------------------------- UI 状态
def get_ui_state():
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute("SELECT key, value_json FROM ui_state").fetchall()
        finally:
            conn.close()
    return {r['key']: json.loads(r['value_json']) for r in rows}


def set_ui_state(key, value):
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("INSERT OR REPLACE INTO ui_state(key,value_json) VALUES(?,?)",
                         (key, json.dumps(value, ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------- 健康/统计
def status():
    with _LOCK:
        conn = _connect()
        try:
            g = conn.execute("SELECT COUNT(*) FROM ethnic_groups").fetchone()[0]
            t = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            u = conn.execute("SELECT COUNT(*) FROM ui_state").fetchone()[0]
            by_group = {}
            for row in conn.execute(
                    "SELECT key, name, COUNT(t.id) AS n FROM ethnic_groups g "
                    "LEFT JOIN tracks t ON t.group_key=g.key GROUP BY g.key"):
                by_group[row['key']] = {'name': row['name'], 'tracks': row['n']}
        finally:
            conn.close()
    return {'db_path': str(DB_PATH), 'groups': g, 'tracks': t,
            'ui_state_keys': u, 'by_group': by_group}
