"""
一键体检：签名器 → 登录态 → 业务接口闭环

    python xhs_check.py

退出码：0 = 全链路正常；1 = 有环节失败。
站点改版、cookie 过期、签名载荷轮换后，先跑这个再跑采集器。
"""
from __future__ import annotations

import sys

from xhs_collect import Signer, Xhs, load_cookie

OK, FAIL = "[ OK ]", "[FAIL]"


def main() -> int:
    fail = 0

    # 1) 常驻签名服务
    try:
        h = Signer().health()
        print(f"{OK} 签名服务在线 pid={h.get('pid')} uptime={h.get('uptime_s')}s")
    except Exception as e:
        print(f"{FAIL} 签名服务未启动: {e}")
        print("       先跑：node xhs_signer_server.js 18080")
        return 1

    # 2) 本地产出签名，且格式正确
    try:
        sg = Signer().sign("/api/sns/web/v1/homefeed", {"num": 20}, "")
        xs = sg.get("x-s", "")
        assert xs.startswith("XYS_"), xs[:40]
        assert len(xs) > 60, len(xs)
        assert sg.get("x-s-common"), "缺 x-s-common"
        print(f"{OK} 本地签名 XYS_ 前缀正常，长度 {len(xs)}，"
              f"耗时 {sg.get('cost_ms')}ms")
    except Exception as e:
        print(f"{FAIL} 签名生成失败: {e}")
        return 1

    # 3) cookie 登录态
    try:
        cookie = load_cookie()
        x = Xhs(cookie, "http://127.0.0.1:7897", rate=1.0)
        j = x.call("GET", "/api/sns/web/v2/user/me")
        u = j.get("data") or {}
        print(f"{OK} 登录态有效: {u.get('nickname')} "
              f"(user_id={u.get('user_id')}, guest={u.get('guest')})")
    except Exception as e:
        print(f"{FAIL} 登录态校验失败: {e}")
        print("       重新导出浏览器 Cookie 覆盖 _xhs_cookie.txt")
        return 1

    # 4) 业务接口
    home = {"cursor_score": "", "num": 20, "refresh_type": 1,
            "note_index": 0, "unread_begin_note_id": "",
            "unread_end_note_id": "", "unread_note_count": 0,
            "category": "homefeed_recommend", "search_key": "",
            "need_num": 20, "image_formats": ["jpg", "webp", "avif"],
            "need_filter_image": False}
    try:
        j = x.call("POST", "/api/sns/web/v1/homefeed", home)
        d = j.get("data") or {}
        n = len(d.get("items") or [])
        assert n > 0, "返回空列表"
        print(f"{OK} 推荐流 code=0，返回 {n} 条")
    except Exception as e:
        print(f"{FAIL} 推荐流失败: {e}")
        fail = 1

    # 5) 搜索接口
    kw = "咖啡"
    from urllib.parse import quote
    sid = "2a1b3c4d5e"
    path = (f"/api/sns/web/v1/search/notes?keyword={quote(kw)}&page=1"
            f"&page_size=20&search_id={sid}&sort=general&note_type=0")
    body = {"keyword": kw, "page": 1, "page_size": 20,
            "search_id": sid, "sort": "general", "note_type": 0}
    ref = f"https://www.xiaohongshu.com/search_result?keyword={quote(kw)}"
    try:
        j = x.call("POST", path, body, referer=ref)
        d = j.get("data") or {}
        n = len(d.get("items") or [])
        assert n > 0, "返回空列表"
        print(f"{OK} 搜索 code=0，返回 {n} 条")
    except Exception as e:
        print(f"{FAIL} 搜索失败: {e}")
        fail = 1

    print()
    if fail:
        print("结论：签名与登录正常，但业务接口被拒 —— 检查是否触发风控 / 频率过高。")
    else:
        print("结论：全链路闭环，可以开始采集。")
    return fail


if __name__ == "__main__":
    sys.exit(main())
