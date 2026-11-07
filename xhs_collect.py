"""
小红书采集器（零浏览器）

依赖常驻签名服务：
    node xhs_signer_server.js 18080

用法：
    # 1) 推荐流（首页）
    python xhs_collect.py --mode homefeed --pages 5

    # 2) 关键词搜索
    python xhs_collect.py --mode search --keyword 咖啡 --pages 20

    # 3) 指定用户主页
    python xhs_collect.py --mode user --user-id 5f3e... --pages 10

    # 4) 指定笔记（文件一行一个 noteId，可选第二列 xsec_token）
    python xhs_collect.py --mode note --notes notes.txt

    # 公共参数
    --detail            对每条笔记抓详情（正文全文/图片/标签）
    --comments N        抓每篇笔记的评论，最多 N 页
    --sub-comments      连二级评论一起抓
    --skip-done-comments  已抓过评论的笔记跳过接口请求
    --db xhs.db         落库文件（自动去重 + 断点续跑）
    --csv out.csv       额外导出 CSV（默认只含本次运行的数据）
    --csv-all           导出整库
    --rate 1.5          请求间隔秒数（保守，别低于 1）
    --proxy URL         默认 http://127.0.0.1:7897
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sqlite3
import sys
import time
import urllib.parse
from typing import Any

import requests

# ---------------------------------------------------------------- 常量

HOST = "https://edith.xiaohongshu.com"
REF_HOME = "https://www.xiaohongshu.com/explore"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
SIGNER = os.environ.get("XHS_SIGNER", "http://127.0.0.1:18080")
IMG_FMT = ["jpg", "webp", "avif"]
COOKIE_FILES = ("_xhs_cookie.txt", "xhs_cookie.txt")

# 业务码 -> 含义
CODE_MSG = {
    0: "成功",
    -100: "签名错误",
    300011: "账号存在异常，被风控标记",
    300012: "登录已失效",
    300013: "请求过于频繁",
    300031: "笔记暂时无法浏览（token 无效或过期）",
    461: "请求被拒绝（缺 xsec_token 或风控）",
}

# ---------------------------------------------------------------- 基础工具


def log(msg: str) -> None:
    print(msg, flush=True)


def load_cookie() -> str:
    """优先读文件，其次读环境变量。"""
    for f in COOKIE_FILES:
        if os.path.exists(f):
            v = open(f, encoding="utf-8").read().strip()
            if v:
                return v
    v = os.environ.get("XHS_COOKIE", "").strip()
    if v:
        return v
    raise SystemExit("缺少 cookie：请把浏览器 Cookie 存到 _xhs_cookie.txt，或设 XHS_COOKIE")


def cookie_a1(cookie: str) -> str:
    m = None
    for part in cookie.split(";"):
        k, _, val = part.strip().partition("=")
        if k == "a1":
            m = val
            break
    if not m:
        raise SystemExit("cookie 里找不到 a1，缺少它 X-S-Common 无法生成")
    return m


def num(v: Any) -> int:
    """互动数可能是 '1.2万' / '10+' / 数字，统一成整数。"""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace("+", "")
    if not s:
        return 0
    if s.endswith("万"):
        try:
            return int(float(s[:-1]) * 10000)
        except ValueError:
            return 0
    if s.endswith("亿"):
        try:
            return int(float(s[:-1]) * 100000000)
        except ValueError:
            return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


# ---------------------------------------------------------------- 签名客户端


class Signer:
    def __init__(self, base: str = SIGNER):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.trust_env = False          # 绝不能让本地请求走代理
        self.calls = 0

    def health(self) -> dict:
        return self.s.get(f"{self.base}/health", timeout=5).json()

    def sign(self, path: str, data: Any = None, a1: str = "") -> dict:
        r = self.s.post(f"{self.base}/sign",
                        json={"url": path, "data": data, "a1": a1}, timeout=30)
        r.raise_for_status()
        j = r.json()
        if "x-s" not in j:
            raise RuntimeError(f"签名服务返回异常: {j}")
        self.calls += 1
        return j


# ---------------------------------------------------------------- 存储


class Store:
    def __init__(self, path: str = "xhs.db"):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            user_id TEXT, nickname TEXT,
            type TEXT, title TEXT, description TEXT,
            liked INTEGER, collected INTEGER, comment INTEGER, share INTEGER,
            images INTEGER, tags TEXT, ip_location TEXT,
            publish_time TEXT, url TEXT, xsec_token TEXT,
            source TEXT, keyword TEXT,
            detail_done INTEGER DEFAULT 0,
            comment_done INTEGER DEFAULT 0,
            fetched_at TEXT
        );
        CREATE TABLE IF NOT EXISTS comments (
            id TEXT PRIMARY KEY,
            note_id TEXT, parent_id TEXT,
            user_id TEXT, nickname TEXT,
            content TEXT, like_count INTEGER,
            create_time TEXT, ip_location TEXT,
            is_sub INTEGER DEFAULT 0,
            fetched_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_comments_note ON comments(note_id);
        CREATE INDEX IF NOT EXISTS idx_notes_user ON notes(user_id);
        """)
        self.db.commit()

    def known(self, nid: str) -> bool:
        cur = self.db.execute("SELECT detail_done FROM notes WHERE id=?", (nid,))
        return cur.fetchone() is not None

    def need_detail(self, nid: str) -> bool:
        cur = self.db.execute("SELECT detail_done FROM notes WHERE id=?", (nid,))
        r = cur.fetchone()
        return r is None or not r[0]

    def need_comments(self, nid: str) -> bool:
        cur = self.db.execute("SELECT comment_done FROM notes WHERE id=?", (nid,))
        r = cur.fetchone()
        return r is None or not r[0]

    def comment_count(self, nid: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM comments WHERE note_id=?",
                               (nid,)).fetchone()[0]

    def upsert_note(self, n: dict) -> None:
        cols = ["id", "user_id", "nickname", "type", "title", "description",
                "liked", "collected", "comment", "share", "images", "tags",
                "ip_location", "publish_time", "url", "xsec_token", "source",
                "keyword", "fetched_at"]
        vals = [n.get(c) for c in cols]
        sql = (f"INSERT INTO notes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
               f"ON CONFLICT(id) DO UPDATE SET "
               + ",".join(f"{c}=COALESCE(excluded.{c},{c})" for c in cols if c != "id"))
        self.db.execute(sql, vals)
        self.db.commit()

    def mark(self, nid: str, field: str) -> None:
        assert field in ("detail_done", "comment_done")
        self.db.execute(f"UPDATE notes SET {field}=1 WHERE id=?", (nid,))
        self.db.commit()

    def add_comments(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        cols = ["id", "note_id", "parent_id", "user_id", "nickname", "content",
                "like_count", "create_time", "ip_location", "is_sub", "fetched_at"]
        sql = (f"INSERT OR IGNORE INTO comments ({','.join(cols)}) "
               f"VALUES ({','.join('?' * len(cols))})")
        before = self.db.total_changes
        self.db.executemany(sql, [[r.get(c) for c in cols] for r in rows])
        self.db.commit()
        return self.db.total_changes - before

    def stat(self) -> tuple[int, int]:
        n = self.db.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        c = self.db.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        return n, c

    def export_csv(self, path: str, note_ids: list[str] | None = None,
                   keyword: str | None = None) -> None:
        """导出 CSV。

        默认只导出本次运行的笔记（note_ids）；没传时按 keyword 过滤；
        两者都为 None 才导出整库。评论按同一范围过滤，避免串进别的话题。
        """
        cols = ["id", "user_id", "nickname", "type", "title", "description",
                "liked", "collected", "comment", "share", "images", "tags",
                "ip_location", "publish_time", "url", "xsec_token",
                "source", "keyword", "fetched_at"]

        scope, where, args = "整库", "", []
        if note_ids:
            # 用临时表而不是 IN (?,?,...)，避开 SQLite 变量数上限
            self.db.execute("DROP TABLE IF EXISTS _export_ids")
            self.db.execute("CREATE TEMP TABLE _export_ids (id TEXT PRIMARY KEY)")
            self.db.executemany("INSERT OR IGNORE INTO _export_ids VALUES (?)",
                               [(i,) for i in note_ids if i])
            where = "WHERE id IN (SELECT id FROM _export_ids)"
            scope = f"本次运行 {len(note_ids)} 条"
        elif keyword:
            where, args = "WHERE keyword=?", [keyword]
            scope = f"关键词 {keyword}"

        rows = self.db.execute(
            f"SELECT {','.join(cols)} FROM notes {where} ORDER BY fetched_at, id",
            args).fetchall()
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
        log(f"[export] {len(rows)} 条笔记（{scope}）-> {path}")

        cpath = path.replace(".csv", "_comments.csv")
        ccols = ["id", "note_id", "parent_id", "user_id", "nickname", "content",
                 "like_count", "create_time", "ip_location", "is_sub"]
        if note_ids:
            cwhere = "WHERE note_id IN (SELECT id FROM _export_ids)"
            cargs: list[str] = []
        elif keyword:
            cwhere = "WHERE note_id IN (SELECT id FROM notes WHERE keyword=?)"
            cargs = [keyword]
        else:
            cwhere, cargs = "", []
        crows = self.db.execute(
            f"SELECT {','.join(ccols)} FROM comments {cwhere} ORDER BY note_id, id",
            cargs).fetchall()
        if crows:
            with open(cpath, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(ccols)
                w.writerows(crows)
            log(f"[export] {len(crows)} 条评论（同范围）-> {cpath}")
        else:
            # 别留下上次的旧评论文件造成误解
            if os.path.exists(cpath):
                os.remove(cpath)
            log("[export] 本次范围内无评论")


# ---------------------------------------------------------------- 采集主体


class Xhs:
    def __init__(self, cookie: str, proxy: str | None, rate: float = 1.5,
                 retry: int = 3):
        self.cookie = cookie
        self.a1 = cookie_a1(cookie)
        self.rate = rate
        self.retry = retry
        self.signer = Signer()
        self.s = requests.Session()
        self.s.trust_env = False
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.s.headers.update({"User-Agent": UA})
        self._last = 0.0
        self.req = 0

    # -------- 底层请求

    def _throttle(self) -> None:
        gap = self.rate * (0.8 + random.random() * 0.4)   # +-20% 抖动
        wait = self._last + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def call(self, method: str, path: str, body: Any = None,
             referer: str = REF_HOME) -> dict:
        """返回服务端 JSON；失败重试，签名每次重算。"""
        last_err = None
        for attempt in range(1, self.retry + 1):
            self._throttle()
            try:
                sg = self.signer.sign(path, body, self.a1)
            except Exception as e:
                last_err = f"签名服务不可用: {e}"
                time.sleep(2)
                continue
            h = {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json;charset=UTF-8",
                "Cookie": self.cookie,
                "Origin": "https://www.xiaohongshu.com",
                "Referer": referer,
                "x-s": sg["x-s"],
                "x-t": sg["x-t"],
                "x-s-common": sg["x-s-common"],
            }
            url = HOST + path
            try:
                if method == "GET":
                    r = self.s.get(url, headers=h, timeout=30)
                else:
                    r = self.s.post(url, headers=h,
                                    data=json.dumps(body, separators=(",", ":")),
                                    timeout=30)
                self.req += 1
            except requests.RequestException as e:
                last_err = f"网络错误: {type(e).__name__}"
                time.sleep(2 * attempt)
                continue

            if r.status_code == 461:
                last_err = "HTTP 461 风控拦截"
                time.sleep(3 * attempt)
                continue
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:120]}"
                time.sleep(2 * attempt)
                continue
            try:
                j = r.json()
            except ValueError:
                last_err = f"非 JSON 响应: {r.text[:120]}"
                continue

            code = j.get("code")
            if code == 0:
                return j
            if code in (-100,):
                last_err = f"签名被拒({code})"
                time.sleep(1)
                continue
            if code in (300011, 300012):
                raise SystemExit(
                    f"\n[中止] 服务端返回 {code}: {CODE_MSG.get(code, j.get('msg'))}\n"
                    f"       这不是代码问题，换账号或降低频率后再跑。已落库数据不受影响。")
            if code in (300013, 300031) or code == 461:
                last_err = f"业务码 {code}: {j.get('msg')}"
                time.sleep(3 * attempt)
                continue
            last_err = f"业务码 {code}: {j.get('msg')}"
            break
        raise RuntimeError(f"请求失败 {path} -> {last_err}")

    # -------- 字段提取

    @staticmethod
    def _fmt_time(v: Any) -> str:
        if not v:
            return ""
        try:
            n = int(v)
            if n > 10_000_000_000:      # 毫秒时间戳
                return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(n / 1000))
            if n > 1_000_000_000:       # 秒时间戳
                return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(n))
        except (TypeError, ValueError, OSError):
            pass
        return str(v)

    @classmethod
    def _card_to_note(cls, item: dict, source: str, keyword: str = "") -> dict:
        nid = item.get("id") or item.get("note_id") or ""
        nc = item.get("note_card") or item.get("note") or {}
        # 用户主页 /user_posted 返回的是扁平结构，字段直接挂在 item 上
        if not nc and (item.get("display_title") or item.get("cover")):
            nc = item
        ii = nc.get("interact_info") or {}
        user = nc.get("user") or {}
        tags = [t.get("name") for t in (nc.get("tag_list") or []) if t.get("name")]
        token = item.get("xsec_token") or nc.get("xsec_token") or ""
        return {
            "id": nid,
            "user_id": user.get("user_id") or user.get("id") or "",
            "nickname": user.get("nickname") or user.get("nick_name") or "",
            "type": nc.get("type") or item.get("model_type") or "normal",
            "title": nc.get("display_title") or nc.get("title") or "",
            "description": nc.get("desc") or "",
            "liked": num(ii.get("liked_count")),
            "collected": num(ii.get("collected_count")),
            "comment": num(ii.get("comment_count")),
            "share": num(ii.get("share_count")),
            # 用户主页只有 cover，没有 image_list
            "images": len(nc.get("image_list") or []) or (1 if nc.get("cover") else 0),
            "tags": "|".join(tags),
            "ip_location": nc.get("ip_location") or "",
            "publish_time": cls._fmt_time(nc.get("time")),
            "url": (f"https://www.xiaohongshu.com/explore/{nid}"
                    f"?xsec_token={token}&xsec_source=pc_feed") if token else
                   f"https://www.xiaohongshu.com/explore/{nid}",
            "xsec_token": token,
            "source": source,
            "keyword": keyword,
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    # -------- 列表：推荐流

    def homefeed(self, pages: int, store: Store) -> list[dict]:
        out, cursor, idx = [], "", 0
        for p in range(pages):
            body = {
                "cursor_score": cursor, "num": 20, "refresh_type": 1,
                "note_index": idx, "unread_begin_note_id": "",
                "unread_end_note_id": "", "unread_note_count": 0,
                "category": "homefeed_recommend", "search_key": "",
                "need_num": 20, "image_formats": IMG_FMT,
                "need_filter_image": False,
            }
            j = self.call("POST", "/api/sns/web/v1/homefeed", body)
            d = j.get("data") or {}
            items = d.get("items") or []
            for it in items:
                if it.get("model_type") not in (None, "note"):
                    continue
                n = self._card_to_note(it, "homefeed")
                if n["id"]:
                    store.upsert_note(n)
                    out.append(n)
            cursor = d.get("cursor_score") or ""
            idx += len(items)
            log(f"  [homefeed] 第 {p + 1}/{pages} 页 +{len(items)} 条"
                f"（累计 {len(out)}，cursor={str(cursor)[:16]}）")
            if not items or not d.get("has_more", True):
                break
        return out

    # -------- 列表：搜索

    def search(self, keyword: str, pages: int, store: Store,
               sort: str = "general", note_type: int = 0) -> list[dict]:
        out = []
        q = urllib.parse.quote(keyword)
        ref = f"https://www.xiaohongshu.com/search_result?keyword={q}&source=web_explore_feed"
        for page in range(1, pages + 1):
            sid = "".join(random.choice("0123456789abcdef") for _ in range(16))
            path = (f"/api/sns/web/v1/search/notes?keyword={q}&page={page}"
                    f"&page_size=20&search_id={sid}&sort={sort}&note_type={note_type}")
            body = {"keyword": keyword, "page": page, "page_size": 20,
                    "search_id": sid, "sort": sort, "note_type": note_type}
            j = self.call("POST", path, body, referer=ref)
            d = j.get("data") or {}
            items = d.get("items") or []
            got = 0
            for it in items:
                n = self._card_to_note(it, "search", keyword)
                if not n["id"]:
                    continue
                store.upsert_note(n)
                out.append(n)
                got += 1
            log(f"  [search:{keyword}] 第 {page}/{pages} 页 +{got} 条"
                f"（累计 {len(out)}，has_more={d.get('has_more')}）")
            if not items or not d.get("has_more", False):
                break
        return out

    # -------- 列表：用户主页

    def user_notes(self, user_id: str, pages: int, store: Store) -> list[dict]:
        out, cursor = [], ""
        ref = f"https://www.xiaohongshu.com/user/profile/{user_id}"
        for p in range(pages):
            path = (f"/api/sns/web/v1/user_posted?num=30&cursor={cursor}"
                    f"&user_id={user_id}&image_formats=jpg,webp,avif")
            j = self.call("GET", path, referer=ref)
            d = j.get("data") or {}
            items = d.get("notes") or []
            for it in items:
                n = self._card_to_note(it, "user")
                if not n["id"]:
                    continue
                n["user_id"] = n["user_id"] or user_id
                store.upsert_note(n)
                out.append(n)
            cursor = d.get("cursor") or ""
            log(f"  [user:{user_id}] 第 {p + 1}/{pages} 页 +{len(items)} 条"
                f"（累计 {len(out)}）")
            if not items or not d.get("has_more", False):
                break
        return out

    # -------- 详情

    def detail(self, nid: str, token: str, store: Store) -> dict | None:
        body = {"source_note_id": nid, "xsec_token": token,
                "image_formats": IMG_FMT, "extra": {"need_body_topic": 1}}
        ref = (f"https://www.xiaohongshu.com/explore/{nid}"
               f"?xsec_token={token}&xsec_source=pc_feed")
        j = self.call("POST", "/api/sns/web/v1/feed", body, referer=ref)
        items = (j.get("data") or {}).get("items") or []
        if not items:
            return None
        nc = items[0].get("note_card") or {}
        # 保留列表阶段的来源与关键词，别把来源覆盖成 detail
        row = store.db.execute("SELECT source,keyword FROM notes WHERE id=?",
                               (nid,)).fetchone()
        src, kw = row if row else ("detail", "")
        n = self._card_to_note({"id": nid, "note_card": nc, "xsec_token": token},
                              src, kw)
        # 详情里有更全的字段，覆盖列表的简略值
        n["description"] = nc.get("desc") or n["description"]
        imgs = nc.get("image_list") or []
        if imgs:
            n["images"] = len(imgs)
        store.upsert_note(n)
        store.mark(nid, "detail_done")
        return n

    # -------- 评论

    def comments(self, nid: str, token: str, store: Store, max_pages: int = 5,
                 with_sub: bool = False) -> dict:
        """抓评论并返回本次统计（都是「本次」，不含库里已有的历史数据）：

        added        真正写库的新增行数（INSERT OR IGNORE 去重后）
        root         接口返回的一级评论条数（去重前）
        sub          接口返回的楼中楼条数（去重前，仅 --sub-comments 时有值）
        sub_pending  未展开楼中楼时，按 sub_comment_count 累加出的待抓回复数（估算）
        """
        cursor, page, pages_ok = "", 0, 0
        stat = {"added": 0, "root": 0, "sub": 0, "sub_pending": 0}
        ref = (f"https://www.xiaohongshu.com/explore/{nid}"
               f"?xsec_token={token}&xsec_source=pc_feed")
        while page < max_pages:
            path = (f"/api/sns/web/v2/comment/page?note_id={nid}&cursor={cursor}"
                    f"&top_comment_id=&image_formats=jpg,webp,avif&comment_id="
                    f"&xsec_token={urllib.parse.quote(token, safe='')}")
            try:
                j = self.call("GET", path, referer=ref)
            except RuntimeError as e:
                log(f"    [comment] 跳过: {e}")
                break
            pages_ok += 1
            d = j.get("data") or {}
            cs = d.get("comments") or []
            stat["root"] += len(cs)
            rows = [self._comment_row(c, nid, "") for c in cs]
            if with_sub:
                for c in cs:
                    if num(c.get("sub_comment_count")) > 0:
                        subs = self.sub_comments(nid, token, c.get("id", ""), ref)
                        stat["sub"] += len(subs)
                        rows += subs
            else:
                # 没开 --sub-comments 时也能算出「差额在哪」，供日志解释总评
                stat["sub_pending"] += sum(num(c.get("sub_comment_count"))
                                           for c in cs)
            stat["added"] += store.add_comments(rows)
            cursor = d.get("cursor") or ""
            page += 1
            if not cs or not d.get("has_more"):
                break
        # 只有至少一页真正成功才标记完成；失败不能标，否则下次会被跳过
        if pages_ok:
            store.mark(nid, "comment_done")
        return stat

    def sub_comments(self, nid: str, token: str, root_id: str, ref: str) -> list[dict]:
        rows, cursor = [], ""
        for _ in range(3):
            path = (f"/api/sns/web/v2/comment/sub/page?note_id={nid}"
                    f"&root_comment_id={root_id}&num=10&cursor={cursor}"
                    f"&image_formats=jpg,webp,avif&top_comment_id="
                    f"&xsec_token={urllib.parse.quote(token, safe='')}")
            try:
                j = self.call("GET", path, referer=ref)
            except RuntimeError:
                break
            d = j.get("data") or {}
            cs = d.get("comments") or []
            rows += [self._comment_row(c, nid, root_id, is_sub=1) for c in cs]
            cursor = d.get("cursor") or ""
            if not cs or not d.get("has_more"):
                break
        return rows

    @staticmethod
    def _comment_row(c: dict, nid: str, parent: str, is_sub: int = 0) -> dict:
        u = c.get("user_info") or {}
        return {
            "id": c.get("id") or "",
            "note_id": nid,
            "parent_id": parent,
            "user_id": u.get("user_id") or "",
            "nickname": u.get("nickname") or "",
            "content": (c.get("content") or "").replace("\n", " "),
            "like_count": num(c.get("like_count")),
            "create_time": str(c.get("create_time") or ""),
            "ip_location": c.get("ip_location") or "",
            "is_sub": is_sub,
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }


# ---------------------------------------------------------------- 主流程


def main() -> None:
    ap = argparse.ArgumentParser(description="小红书零浏览器采集器")
    ap.add_argument("--mode", required=True,
                    choices=["homefeed", "search", "user", "note"])
    ap.add_argument("--keyword", default="")
    ap.add_argument("--user-id", default="")
    ap.add_argument("--notes", default="", help="noteId 列表文件")
    ap.add_argument("--pages", type=int, default=5)
    ap.add_argument("--detail", action="store_true", help="抓笔记详情")
    ap.add_argument("--comments", type=int, default=0,
                    help="每篇笔记抓评论的最大页数，0=不抓")
    ap.add_argument("--sub-comments", action="store_true")
    ap.add_argument("--skip-done-comments", action="store_true",
                    help="已抓过评论的笔记不再请求接口（省请求；默认仍复查以捕捉新增评论）")
    ap.add_argument("--db", default="xhs.db")
    ap.add_argument("--csv", default="")
    ap.add_argument("--csv-all", action="store_true",
                    help="导出整库（默认只导出本次运行拿到的笔记，避免混入历史关键词）")
    ap.add_argument("--rate", type=float, default=1.5)
    ap.add_argument("--proxy", default="http://127.0.0.1:7897")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少篇笔记")
    ap.add_argument("--no-detail-if-known", action="store_true",
                    help="已抓过详情的跳过")
    a = ap.parse_args()

    # 前置检查
    try:
        h = Signer().health()
        log(f"[signer] ok pid={h.get('pid')} uptime={h.get('uptime_s')}s")
    except Exception as e:
        raise SystemExit(f"签名服务未启动（{e}）。先跑：node xhs_signer_server.js 18080")

    cookie = load_cookie()
    store = Store(a.db)
    x = Xhs(cookie, a.proxy or None, rate=a.rate)

    log(f"[start] mode={a.mode} rate={a.rate}s db={a.db}")

    if a.mode == "homefeed":
        notes = x.homefeed(a.pages, store)
    elif a.mode == "search":
        if not a.keyword:
            raise SystemExit("--mode search 需要 --keyword")
        notes = x.search(a.keyword, a.pages, store)
    elif a.mode == "user":
        if not a.user_id:
            raise SystemExit("--mode user 需要 --user-id")
        notes = x.user_notes(a.user_id, a.pages, store)
    else:
        if not a.notes:
            raise SystemExit("--mode note 需要 --notes 文件")
        notes = []
        for line in open(a.notes, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            nid = parts[0]
            tok = parts[1] if len(parts) > 1 else ""
            notes.append({"id": nid, "xsec_token": tok, "title": "",
                          "description": "", "user_id": "", "nickname": "",
                          "type": "", "liked": 0, "collected": 0, "comment": 0,
                          "share": 0, "images": 0, "tags": "", "ip_location": "",
                          "publish_time": "", "source": "manual", "keyword": "",
                          "url": f"https://www.xiaohongshu.com/explore/{nid}",
                          "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")})

    log(f"[list] 拿到 {len(notes)} 条笔记")

    # 详情 + 评论
    todo = notes[: a.limit] if a.limit else notes
    n_detail = n_comment = 0
    n_root = n_sub = n_pending = 0
    for i, n in enumerate(todo, 1):
        nid, tok = n["id"], n.get("xsec_token") or ""
        if not nid:
            continue
        # 每篇固定打印一行，避免「有的有详情行、有的没有」的错觉
        detail_txt, comment_txt, title_txt = "详情 未启用", "评论 未启用", ""

        if a.detail or a.mode == "note":
            if a.no_detail_if_known and not store.need_detail(nid):
                detail_txt = "详情 已存在(跳过)"
            else:
                try:
                    r = x.detail(nid, tok, store)
                    if r:
                        n_detail += 1
                        # 「总评」是笔记展示的评论总数（含楼中楼回复），
                        # 「入库评论」是本次去重后真正写库的新行数，两者口径不同
                        detail_txt = f"详情 OK 赞{r['liked']} 总评{r['comment']}"
                        title_txt = (r["title"] or r["description"])[:30]
                    else:
                        detail_txt = "详情 空响应"
                except RuntimeError as e:
                    detail_txt = f"详情 失败({str(e)[:60]})"
                except SystemExit:
                    raise

        if a.comments > 0:
            already = store.comment_count(nid)
            if a.skip_done_comments and not store.need_comments(nid):
                comment_txt = f"评论 已抓过(跳过，库内 {already} 条)"
            else:
                try:
                    st = x.comments(nid, tok, store, max_pages=a.comments,
                                    with_sub=a.sub_comments)
                    got = st["added"]
                    n_comment += got
                    n_root += st["root"]
                    n_sub += st["sub"]
                    n_pending += st["sub_pending"]
                    if got:
                        # 自解释格式：新增多少 + 本次抓到什么 + 差额在哪
                        if a.sub_comments:
                            parts = f"一级{st['root']} · 楼中楼{st['sub']}"
                        elif st["sub_pending"]:
                            parts = (f"一级{st['root']} · "
                                     f"楼中楼{st['sub_pending']} 未抓")
                        else:
                            parts = f"一级{st['root']} · 无楼中楼"
                        dup = st["root"] + st["sub"] - got
                        if dup > 0:
                            parts += f" · 去重{dup}"
                        comment_txt = f"入库评论 +{got}（{parts}）"
                    elif already:
                        comment_txt = f"入库评论 0（库内已有 {already} 条，全为重复）"
                    else:
                        comment_txt = "入库评论 0（接口未返回评论）"
                except RuntimeError as e:
                    comment_txt = f"评论 失败({str(e)[:60]})"

        log(f"  [{i:>2}/{len(todo)}] {nid} | {detail_txt} | {comment_txt}"
            + (f" | {title_txt}" if title_txt else ""))
        if i % 20 == 0 or i == len(todo):
            log(f"  -- 进度 {i}/{len(todo)}，累计请求 {x.req} 次 --")

    tn, tc = store.stat()
    log("")
    log("=" * 58)
    log(f"本次请求 {x.req} 次 | 详情成功 {n_detail} 篇 | 评论新入库(去重后) {n_comment} 条")
    if a.comments > 0:
        extra = (f"；另有 {n_pending} 条楼中楼未展开（加 --sub-comments 可抓）"
                 if n_pending else "")
        log(f"  本次接口返回：一级 {n_root} 条 / 楼中楼 {n_sub} 条{extra}")
    log(f"库内合计：笔记 {tn} 条，评论 {tc} 条")
    log("=" * 58)

    if a.csv:
        if a.csv_all:
            store.export_csv(a.csv)
        else:
            store.export_csv(a.csv,
                             note_ids=[n["id"] for n in notes if n.get("id")],
                             keyword=a.keyword if a.mode == "search" else None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n[中断] 已落库的数据完整，重跑同一命令会自动续跑。")
        sys.exit(130)
