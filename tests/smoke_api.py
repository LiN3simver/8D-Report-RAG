# -*- coding: utf-8 -*-
"""端點煙霧測試。服務要先跑起來。

    .venv\\Scripts\\python.exe tests\\smoke_api.py

分工：
    python -m src.car --selftest    查詢層（SQL、欄位對照、過濾器）
    tests/smoke_api.py              端點層（卡片、工具回傳形狀、SSE 事件順序、
                                    資料限制有沒有真的被講出來）

中文一律留在 .py 檔裡由 httpx 編碼，避免經過主控台碼頁被弄壞。
"""
import json
import sys

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
B = "http://127.0.0.1:8700"
GOLD = "CAR-W250602"
PART = "CON-A70090-1"
fails = []


def chk(name, cond, extra=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{('  ' + str(extra)) if extra else ''}")
    if not cond:
        fails.append(name)


def post(path, body, t=60):
    return httpx.post(B + path, json=body, timeout=t).json()


def get(path, t=60, **params):
    return httpx.get(B + path, params=params, timeout=t).json()


print("[1] /a2a/card")
c = get("/a2a/card")
chk("protocol a2a/1", c["protocol"] == "a2a/1")
chk("status ready", c["status"] == "ready", c["status"])
chk("downstream 空（葉節點，防環）", c["downstream"] == [], c["downstream"])
chk("delegate=False", c["capabilities"]["delegate"] is False)
chk("4 支工具全 ready", all(t["ready"] for t in c["tools"]) and len(c["tools"]) == 4)

print("\n[2] /a2a/search  中文問句（detail 模式）")
r = post("/a2a/search", {"query": f"{GOLD} 客訴成立嗎？根本原因是什麼", "top_k": 5})
chk("回 200 且有 hits", len(r["hits"]) == 1)
h = r["hits"][0]
chk("mode=detail", r["raw"]["mode"] == "detail", r["raw"]["mode"])
chk(f"id = {GOLD}", h["id"] == GOLD)
kv = {b["title"]: dict(b["rows"]) for b in h["blocks"] if b["kind"] == "kv"}
txt = {b["title"]: b["text"] for b in h["blocks"] if b["kind"] == "text"}
chk("客訴是否成立=客訴成立", kv["判定與進度"]["客訴是否成立"] == "客訴成立")
chk("狀態=已結案（不是「已發行」）", kv["判定與進度"]["狀態"] == "已結案")
chk("負責品保有值", bool(kv["人員"].get("負責品保 / RD-PM")))
chk("通知人員 3 人", len(kv["人員"]["通知人員"].split("、")) == 3)
chk("退回數/不良數=1200", kv["數量"]["退回數/不良數"] == 1200)
chk("D3~D8 六個 text 區塊都有", sum(1 for k in txt if k.startswith("D")) == 6,
    [k for k in txt if k.startswith("D")])
chk("D3 是根本原因（含「产生原因」）", "产生原因" in txt["D3 根本原因"])
chk("D4 是臨時對策（含「退回」）", "退回" in txt["D4 臨時對策"])
chk("D3 沒有被 D4 的內容佔走", "退回換貨" not in txt["D3 根本原因"])
chk("責任歸屬區塊標了填答率警語",
    any("填答率" in b["title"] for b in h["blocks"] if b["kind"] == "kv"))

print("\n[3] /a2a/search  料號（part 模式）")
r = post("/a2a/search", {"query": f"{PART} 有客訴嗎", "top_k": 8})
chk("mode=part", r["raw"]["mode"] == "part", r["raw"]["mode"])
chk("2 張", len(r["hits"]) == 2, len(r["hits"]))
chk("每張都說得出為什麼命中", all(x["why"] for x in r["hits"]))
chk("badges 有命中方式", all(any(b[0] == "命中方式" for b in x["badges"]) for x in r["hits"]))

print("\n[4] /a2a/search  查無（不可以變成空白或 500）")
r = post("/a2a/search", {"query": "CAR-Z999999", "top_k": 5})
chk("查無也回一張說明卡", len(r["hits"]) == 1)
chk("卡片上講清楚是語料裡沒有", "查不到不等於沒出過事" in r["hits"][0]["body"])

print("\n[5] /a2a/search  自由文字（search 模式）+ why")
r = post("/a2a/search", {"query": "端子變形", "top_k": 5})
chk("mode=search", r["raw"]["mode"] == "search")
chk("有命中", r["raw"]["total"] > 0, r["raw"]["total"])
chk("每筆都有 why", all(x["why"] for x in r["hits"]))

print("\n[5b] filters 是呼叫端給的任意 dict —— 不可以撞壞函式簽名")
r = post("/a2a/search", {"query": "彈高", "top_k": 3, "filters": {"text": "亂塞"}})
chk("filters 裡塞 text 不會炸", "hits" in r and not r["raw"].get("error"),
    str(r.get("raw", {}).get("error"))[:120])
r = post("/a2a/search", {"query": "彈高", "top_k": 3, "filters": {"top_k": 999}})
chk("filters 裡塞 top_k 不會炸", "hits" in r and not r["raw"].get("error"))
r = post("/a2a/search", {"query": "彈高", "top_k": 3, "filters": {"nonsense": "x"}})
chk("filters 裡的未知鍵被安靜忽略", "hits" in r and r["raw"]["total"] > 0)
r = post("/a2a/search", {"query": "不良", "top_k": 3, "filters": {"factory": "西區"}})
chk("白名單內的 filters 照樣生效", "生產廠區=F04_西區廠" in r["raw"]["filters"],
    r["raw"]["filters"])
chk("  文字與過濾器同時生效", "文字包含「不良」" in r["raw"]["filters"])
r = httpx.post(B + "/a2a/search", json={"query": "", "top_k": 3}, timeout=30)
chk("空 query 是 422", r.status_code == 422, r.status_code)

print("\n[6] 四支工具")
r = post("/a2a/tool/get_part_complaints", {"part_number": PART})
chk("get_part_complaints ok", r["ok"])
chk("  total=2 / 成立 1 / 未判定 1",
    (r["total"], r["established"], r["undecided"]) == (2, 1, 1),
    (r["total"], r["established"], r["undecided"]))
chk("  未判定有被特別提醒", any("不成立" in n for n in r["notes"]))

r = post("/a2a/tool/get_part_complaints", {"part_number": "NOPE-12345"})
chk("查無料號 ok=True（不是失敗）", r["ok"] is True)
chk("  total=0 且講明是語料裡沒有", r["total"] == 0 and any("沒出過事" in n for n in r["notes"]))

r = post("/a2a/tool/get_complaint", {"car_no": GOLD})
chk("get_complaint ok", r["ok"])
chk("  disciplines 六段", len(r["disciplines"]) == 6)
chk("  parts 關聯到 PART", r["parts"][0]["料號"] == "CON-B30010-0")

r = post("/a2a/tool/complaint_stats", {"customer": "範例電腦"})
chk("complaint_stats ok", r["ok"])
chk("  過濾條件講得出來", r["filters"] == ["客戶包含「範例電腦」"], r["filters"])
s = r["客訴是否成立"]
chk("  成立率分母不含未判定", s["已判定張數"] == s["成立"] + s["不成立"])
chk("  責任歸屬有警語", any("不要用它回答" in n for n in r["notes"]))

r = post("/a2a/tool/complaint_stats", {"factory": "香港"})
chk("認不出的過濾值要回報而不是靜靜忽略", len(r["ignored"]) == 1, r["ignored"])
chk("  notes 第一句就警告範圍不對", "不是你要的範圍" in r["notes"][0])

r = post("/a2a/tool/search_complaints", {"text": "彈高", "result": "成立"})
chk("search_complaints 中文過濾", r["ok"] and "客訴是否成立=客訴成立" in r["filters"],
    r.get("filters"))

r = post("/a2a/tool/no_such_tool", {})
chk("未知工具回 ok=False 不是 500", r["ok"] is False)

print("\n[7] /api/* 原生端點")
chk("/api/complaint/{no}", get(f"/api/complaint/{GOLD}")["ok"])
chk("/api/part/{pn}", get(f"/api/part/{PART}")["total"] == 2)
chk("/api/search?q=", get("/api/search", q="彈高")["ok"])
chk("/api/search 過濾", get("/api/search", q="", factory="西區")["total"] > 0)
chk("/api/stats 有未判定一組", "未判定" in get("/api/stats")["客訴是否成立"])
hl = get("/api/health")
chk("/api/health status ready", hl["status"] == "ready", hl["status"])
chk("/api/health plm ok", hl["plm"]["ok"])
chk("/api/health 標出是否為測試庫", isinstance(hl["plm"]["is_test_db"], bool))
chk("/api/health 說明不提供附件", "不提供" in hl["corpus"]["attachments"])
r = httpx.get(B + "/api/complaint/CAR-Z999999", timeout=30)
chk("查無單回 404 + tried 證據", r.status_code == 404 and "tried" in r.json()["detail"])

print("\n[8] /a2a/tools（上游掛工具用）")
t = get("/a2a/tools")
chk("4 支", len(t["tools"]) == 4)
chk("system 一起給出去", len(t["system"]) > 1000)
d = {x["name"]: x["description"] for x in t["tools"]}
chk("get_part_complaints 的限制寫在 description 裡（上游才看得到）",
    "不等於這顆料沒出過事" in d["get_part_complaints"])
chk("complaint_stats 擋掉責任歸屬排名", "不要用這支回答" in d["complaint_stats"])

print("\n[9] /a2a/message（SSE）—— 事件順序與 results 先於 LLM")
with httpx.stream("POST", B + "/a2a/message",
                  json={"query": f"{GOLD} 的根本原因是什麼", "top_k": 3,
                        "no_llm": True}, timeout=90) as resp:
    evs = [ln[7:].strip() for ln in resp.iter_lines() if ln.startswith("event: ")]
chk("no_llm 時事件是 meta -> results -> done", evs == ["meta", "results", "done"], evs)

print("\n" + "=" * 62)
print("全部通過" if not fails else f"{len(fails)} 項失敗：{fails}")
sys.exit(1 if fails else 0)
