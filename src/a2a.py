"""8D 客訴單 Agent 的 A2A 層：把查詢層包成工具、檢索卡片與 system 提示。

在多 Agent 架構中，它是有兩個上游的葉節點：

    產品估價 Agent（rfq :8300）
        ├── BOM Agent（bom :8500）
        │       └── 8D 客訴單 Agent（本專案 :8700）
        └── 8D 客訴單 Agent（本專案 :8700）

- `downstream` 為空、`can_delegate=False`：資料全部來自 PLM 來源庫，不需要問任何人，
  所以不可能繞回上游形成環。
- 工具刻意只有四支（查一張 / 查一顆料 / 找一批 / 算統計）：上游會把每支工具
  都掛給自己的模型，多掛的工具會吃掉上游有限的工具步數。
- 資料限制寫在工具 description 裡，因為那是上游模型唯一看得到的地方。
"""
from __future__ import annotations

import os
import sys
from datetime import date
from typing import Any

# agentkit 為獨立維護的 A2A 框架（不在本 repo），以 AGENTKIT_PATH 指定位置
_HUB = os.environ.get("AGENTKIT_PATH")
if _HUB and os.path.isdir(_HUB) and _HUB not in sys.path:
    sys.path.insert(0, _HUB)

from agentkit import AgentSpec, Block, Hit, ToolSpec, build_router  # noqa: E402

from . import car, db, fields, guard  # noqa: E402

AGENT_ID = "car8d"

# 所有能力都已實作，blocked_by 只在來源庫連不上時顯示。
BLOCKED_BY = [
    "連不上 PLM 來源庫（本 agent 的全部能力來源，不落地、每次直接查）",
]

_CORPUS = fields.CORPUS
_FILL = _CORPUS["fill"]
_UNDECIDED_PCT = round(_CORPUS["undecided"] / _CORPUS["rows"] * 100, 1)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
_FILTER_KEYS = ("customer", "factory", "result", "work_type", "state", "since", "until")


def _picked(a: dict, keys: tuple[str, ...] = _FILTER_KEYS) -> dict[str, Any]:
    return {k: a[k] for k in keys if a.get(k) not in (None, "")}


def _t_get_complaint(a: dict) -> dict:
    return car.detail(a.get("car_no") or a.get("item_number") or "")


def _t_get_part_complaints(a: dict) -> dict:
    return car.by_part(a.get("part_number") or "")


def _t_search_complaints(a: dict) -> dict:
    return car.search(a.get("text") or "", top_k=a.get("top_k") or guard.MAX_HITS, **_picked(a))


def _t_complaint_stats(a: dict) -> dict:
    f = _picked(a, (*_FILTER_KEYS, "part_number"))
    return car.stats(top_customers=a.get("top_customers") or 10, **f)


_FILTER_SCHEMA: dict[str, Any] = {
    "customer": {"type": "string",
                 "description": "受影響客戶的名稱片段，例「範例電腦」（子字串比對）"},
    "factory": {"type": "string",
                "description": "生產廠區。原代碼或中文都吃，例 'W' / 'F04_西區廠' / '西區'。"
                               f"可用：{'、'.join(fields.WORKING_FACTORY.values())}"},
    "result": {"type": "string",
               "description": "客訴是否成立：'客訴成立' / '客訴不成立' / '未判定'。"
                              f"全庫 {_UNDECIDED_PCT}% 是未判定（欄位為 NULL），那是「還沒判」"
                              "不是「不成立」，兩者不可混用"},
    "work_type": {"type": "string",
                  "description": f"申請種類：{'、'.join(fields.WORK_TYPE.values())}"},
    "state": {"type": "string",
              "description": f"單據狀態：{'、'.join(fields.STATE.values())}。"
                             "'Released' 的中文是「已結案」不是「已發行」"},
    "since": {"type": "string", "description": "建立日期起（YYYY-MM-DD，含）"},
    "until": {"type": "string", "description": "建立日期迄（YYYY-MM-DD，不含）"},
}


TOOLS = [
    ToolSpec(
        name="get_part_complaints",
        description=(
            "最常用的一支：查**某個料號**過去有沒有被客訴過，以及每一張的結果。"
            "回傳每一張單的完整內容（是否成立、狀態、責任廠區/供應商、"
            "D3 根本原因、D4 臨時對策、D5 永久改善對策…）。"
            "報價前評估品質風險、換料前確認這顆料的歷史，用這一支。"
            "\n必須如實轉達的三件事："
            "\n1. 語料只有客訴抱怨單(8D) 全部 "
            f"{_CORPUS['rows']} 張（{_CORPUS['date_from']} ~ {_CORPUS['date_to']}）。"
            "查不到只代表「這份語料裡沒有」，**不等於這顆料沒出過事**。"
            "\n2. 每一筆帶 `matched_by`：`relationship` 是精確比對料號（可信），"
            "`part_list_text` 是對「料號清單」文字欄做子字串比對（**可能誤中相近料號**，"
            "例如查 -1 會命中 -10）。引用 part_list_text 的命中時要提醒使用者自己核對。"
            "\n3. `undecided` 是還沒判定成立與否的張數，**不可以算成「不成立」**。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "part_number": {"type": "string",
                                "description": "料號，例 CON-A70090-1"},
            },
            "required": ["part_number"],
        },
        handler=_t_get_part_complaints,
    ),
    ToolSpec(
        name="get_complaint",
        description=(
            "用 CAR 編號查**一張**客訴抱怨單的全部欄位："
            "申請種類、客訴是否成立、狀態、受影響客戶/終端客戶/供應商、"
            "訂單號碼、交貨日期與數量、退回數/不良數、料號清單與描述、"
            "問題主旨/抱怨描述/問題證明、"
            "D3 根本原因 / D4 臨時對策 / D5 永久改善對策 / D6 效果確認 / "
            "D7 再發防止 / D8 結果、異常歸類（人機料法環/大類/細項）、"
            "責任廠區與責任單位、負責品保(RD-PM)、建立者與其主要部門、通知人員、"
            "關聯料號、處理天數、版本版次。"
            "\n編號長相：CAR-xxxxxx / CAF-xxxxxx / NPI-xxxxxx。"
            "\n欄位是 NULL 就是「表單上沒填」，不是查不到。D5~D8 的填答率只有 "
            "66% / 63% / 61% / 32%，「沒有永久對策」與「還沒填」是兩件事。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "car_no": {"type": "string", "description": "CAR 編號，例 CAR-W250602"},
            },
            "required": ["car_no"],
        },
        handler=_t_get_complaint,
    ),
    ToolSpec(
        name="search_complaints",
        description=(
            "找一批客訴單：自由文字（掃問題主旨、抱怨描述、問題證明、"
            "D3~D8 六段對策、備註、料號清單與描述共 16 個欄位）"
            "＋ 客戶／廠區／是否成立／申請種類／狀態／日期區間過濾。"
            "回傳每一筆的完整欄位與 `why`（說明是哪個欄位命中的）。"
            "\n過濾值認不出來時會放進 `ignored` 並在 `notes` 警告 —— "
            "看到 ignored 非空就代表**回傳的範圍不是你要的那個**，要照實說，"
            "不要拿全庫的數字回答一個有條件的問題。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "自由文字，例「彈高偏規格下限」「端子變形」"},
                **_FILTER_SCHEMA,
                "top_k": {"type": "integer",
                          "description": f"最多回幾筆，預設 {guard.MAX_HITS}"},
            },
        },
        handler=_t_search_complaints,
    ),
    ToolSpec(
        name="complaint_stats",
        description=(
            "算分佈：張數、成立率、狀態分佈、生產廠區分佈、申請種類分佈、"
            "客戶排名、異常歸類（人/機/料/法/環、大類、細項）分佈、"
            "平均與最長處理天數。可按客戶／廠區／料號／期間過濾。"
            "\n成立率的分母是「**已判定**的張數」，不含未判定那組。"
            "三個數字（成立/不成立/未判定）都會回，引用時要講清楚分母是什麼。"
            "\n**不要用這支回答「哪個廠區或哪個供應商出最多事」** —— "
            f"責任歸屬欄位的填答率只有 {_FILL['error_factory']}%（責任廠區）"
            f"與 {_FILL['error_vendor']}%（責任供應商），拿它排名等於用個位數樣本下結論。"
            "回傳的 `生產廠區` 是「單子登記的生產廠區」，不是「責任在誰」。"
            "\n不提供金額：客訴的費用表幾乎沒有資料。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                **_FILTER_SCHEMA,
                "part_number": {"type": "string", "description": "只算這顆料號的客訴"},
                "top_customers": {"type": "integer", "description": "客戶排名取前幾名，預設 10"},
            },
        },
        handler=_t_complaint_stats,
    ),
]


# ---------------------------------------------------------------------------
# 檢索：前端與委派迴圈共用的入口
# ---------------------------------------------------------------------------
def _search(query: str, top_k: int, filters: dict) -> dict[str, Any]:
    """從問句判斷是在問哪一張單、哪一顆料，還是在找一批。確定性分流，不問 LLM：

      認出 CAR 編號   -> 查那幾張單（最精確，優先於料號）
      認出料號        -> 料號反查客訴史
      都沒有          -> 問句拆成過濾器 + 關鍵字
    """
    # filters 來自呼叫端的任意 dict，只收白名單的鍵，避免撞到 search() 的參數名
    allowed = (*_FILTER_KEYS, "part_number")
    f = {k: v for k, v in (filters or {}).items() if k in allowed and v not in (None, "")}
    top_k = max(1, min(int(top_k or guard.MAX_HITS), guard.MAX_ROWS))

    cars = car.car_numbers(query)
    parts = car.part_numbers(query)

    if cars:
        rows, missing = [], []
        for no in cars[:top_k]:
            d = car.detail(no)
            if not d.get("ok"):
                missing.append({"car_no": no, "error": d.get("error")})
                continue
            row = d["record"]
            row["料號關聯"] = d["parts"]
            row["通知人員"] = d["notify"]
            rows.append(row)
        return {
            "query": query, "mode": "detail", "results": rows, "not_found": missing,
            "total": len(rows), "truncated": False,
            "filters": [], "ignored": [],
            "source": db.source(), "notes": [car.CORPUS_NOTE],
        }

    if parts:
        rows, empty = [], []
        for pn in parts[:3]:
            r = car.by_part(pn)
            if not r.get("ok"):
                empty.append({"part_number": pn, "error": r.get("error")})
                continue
            if not r["results"]:
                empty.append({"part_number": pn, "note": "語料裡查無客訴紀錄"})
                continue
            for row in r["results"][:top_k]:
                row["查詢料號"] = pn
                rows.append(row)
        notes = [car.CORPUS_NOTE]
        if empty:
            notes.append("★ 有料號在語料裡查不到客訴 —— 那是「這份語料裡沒有」，"
                         "不等於它沒出過事。")
        return {
            "query": query, "mode": "part", "part_numbers": parts[:3],
            "results": rows, "not_found": empty,
            "total": len(rows), "truncated": False,
            "filters": [], "ignored": [],
            "source": db.source(), "notes": notes,
        }

    # 整句問句不能當 LIKE 字串：先拆出過濾器，呼叫端明確給的 filters 優先
    text, guessed = car.parse_question(query)
    f = {**guessed, **f}
    terms = text.split()

    if len(terms) <= 1:
        out = car.search(text, top_k=top_k, **f)
    else:
        # 多段關鍵字：逐段查、依 CAR 編號合併
        merged: dict[str, dict] = {}
        out = {}
        for t in terms[:4]:
            r = car.search(t, top_k=top_k, **f)
            if not r.get("ok"):
                out = r
                break
            out = out or r
            for row in r["results"]:
                merged.setdefault(row["CAR編號"], row)
        if out.get("ok"):
            rows = sorted(merged.values(), key=lambda r: str(r.get("建立日期") or ""),
                          reverse=True)
            out = {**out, "query": text, "results": rows[:top_k], "total": len(rows),
                   "returned": min(len(rows), top_k), "truncated": len(rows) > top_k,
                   "filters": [*out.get("filters", [])[:-1], f"文字包含「{'」或「'.join(terms[:4])}」"]}

    # 文字欄全部落空時，關鍵字多半是客戶名（客戶是參照欄位，不在文字欄裡）
    if out.get("ok") and not out.get("results") and terms and "customer" not in f:
        for t in terms[:2]:
            r = car.search("", top_k=top_k, **{**f, "customer": t})
            if r.get("ok") and r.get("results"):
                out = r
                break

    out["mode"] = "search"
    out.setdefault("not_found", [])
    return out


# ---------------------------------------------------------------------------
# 卡片
# ---------------------------------------------------------------------------
def _tone(row: dict[str, Any]) -> str:
    """成立 = warn、不成立 = green、未判定 = yellow。"""
    v = row.get("客訴是否成立_原值")
    return {"Yes": "warn", "No": "green"}.get(v, "yellow")


def _kv(title: str, rows: list[tuple[str, Any]], tone: str = "neutral") -> Block | None:
    """整個區塊都沒有值就不畫。

    判斷用 `v is not None` 而不是 `if v`：退回數 = 0 是「真的 0 個不良」，
    falsy 檢查會把它和「未填」一起丟掉。
    """
    keep = [[k, v] for k, v in rows if v is not None]
    if not keep:
        return None
    return Block(kind="kv", title=title, tone=tone, rows=keep)


def _hits(found: dict[str, Any]) -> list[Hit]:
    out: list[Hit] = []

    for miss in found.get("not_found") or []:
        key = miss.get("car_no") or miss.get("part_number") or "?"
        out.append(Hit(
            id=key, title=key,
            subtitle=miss.get("error") or miss.get("note") or "查無資料",
            body=car.CORPUS_NOTE,
            badges=[["查無", "語料內"]],
        ))

    for row in found.get("results") or []:
        no = row.get("CAR編號") or "?"
        tone = _tone(row)
        result = row.get("客訴是否成立") or "未判定"

        badges = [["客訴", result], ["狀態", row.get("狀態") or "?"]]
        if fab := row.get("生產廠區"):
            badges.append(["生產廠區", str(fab)])
        if d := row.get("Discipline進度"):
            badges.append(["Discipline", f"到 {d}"])
        if mb := row.get("matched_by"):
            badges.append(["命中方式",
                           "精確料號" if mb == "relationship" else "料號清單文字（可能誤中）"])

        blocks: list[Block] = []
        for b in (
            _kv("判定與進度", [
                ("申請種類", row.get("申請種類")),
                ("客訴是否成立", row.get("客訴是否成立") or row.get("客訴是否成立_說明")),
                ("狀態", row.get("狀態")),
                ("Discipline 進度", row.get("Discipline進度")),
                ("處理天數", row.get("處理天數")),
                ("需轉入 QMS 客訴代碼", "是" if row.get("需轉QMS客訴代碼") else "否"),
                ("8D 報告上傳完成日", row.get("8D報告上傳完成日")),
            ], tone),
            _kv("客戶與料號", [
                ("受影響客戶", row.get("受影響客戶")),
                ("終端客戶", row.get("終端客戶")),
                ("供應商", row.get("供應商")),
                ("訂單號碼 SO No.", row.get("訂單號碼")),
                ("料號清單", "、".join(row.get("料號清單_陣列") or []) or None),
                ("料號描述", row.get("料號描述")),
                ("關聯料號（complaint_part）",
                 "、".join(p["料號"] for p in (row.get("料號關聯") or []) if p.get("料號")) or None),
            ]),
            _kv("數量", [
                ("交貨日期", row.get("交貨日期")),
                ("交貨數量", row.get("交貨數量")),
                ("退回數/不良數", row.get("退回數不良數")),
            ]),
            _kv("異常歸類", [
                ("人機料法環", "、".join(row["異常歸類"]["人機料法環"]) or None),
                ("大類", "、".join(row["異常歸類"]["大類"]) or None),
                ("細項", "、".join(row["異常歸類"]["細項"]) or None),
            ] if row.get("異常歸類") else []),
            _kv(f"責任歸屬（全庫填答率僅 {_FILL['error_factory']}% / "
                f"{_FILL['error_vendor']}%，多數為空）", [
                ("責任廠區", row.get("責任廠區")),
                ("責任單位（供應商）", row.get("責任單位供應商")),
            ]),
            _kv("人員", [
                ("負責品保 / RD-PM", row.get("負責品保RDPM")),
                ("建立者主要部門", row.get("建立者主要部門")),
                ("建立人員 / 日期",
                 f"{row.get('建立人員') or '?'}　{row.get('建立日期') or ''}".strip()),
                ("修改人員 / 日期",
                 f"{row.get('修改人員') or '?'}　{row.get('修改日期') or ''}".strip()),
                ("通知人員", "、".join(row.get("通知人員") or []) or None),
                ("填單廠區", row.get("填單廠區")),
                ("版本-版次", f"{row.get('版本') or '?'}-{row.get('版次') or '?'}"),
            ]),
        ):
            if b is not None:
                blocks.append(b)

        if v := row.get("問題證明"):
            blocks.append(Block(kind="text", title="問題證明", text=str(v)))

        # D3~D8，對照來自 src/fields.py
        for label, text in car.d_sections(row):
            blocks.append(Block(kind="text", title=label, text=text,
                                tone="warn" if label.startswith("D3") else "neutral"))

        if v := row.get("備註說明"):
            blocks.append(Block(kind="text", title="備註說明", text=str(v)))

        out.append(Hit(
            id=no,
            title=f"{no}　{row.get('問題主旨') or ''}".strip(),
            subtitle=(row.get("抱怨描述") or "")[:160],
            body=(f"{result}／{row.get('狀態') or '?'}"
                  f"　客戶：{row.get('受影響客戶') or '?'}"
                  f"　料號：{'、'.join(row.get('料號清單_陣列') or []) or '?'}"),
            why=row.get("why") or ([f"料號 {row['查詢料號']} 的客訴紀錄"]
                                   if row.get("查詢料號") else []),
            badges=badges,
            blocks=blocks,
            raw=row,
        ))
    return out


# ---------------------------------------------------------------------------
# 講給模型聽
# ---------------------------------------------------------------------------
_D_CLIP = 500


def _context(query: str, found: dict[str, Any]) -> str:
    """把檢索結果整理成給模型的文字。

    D3~D8 是這個 agent 最有價值的內容，必須明確放進 context，
    否則卡片上畫著根本原因，模型卻回答「資料未提供」。
    """
    parts: list[str] = [
        f"我的語料：{_CORPUS['table']}（客訴抱怨單 8D）共 {_CORPUS['rows']} 張，"
        f"{_CORPUS['date_from']} ~ {_CORPUS['date_to']}。",
        # 模型不知道今天幾號，換算「今年 / 近 N 個月」要靠這行
        f"今天是 {date.today().isoformat()}（換算「今年」「去年」「近 N 個月」用）。",
    ]
    if said := found.get("filters"):
        parts.append(f"這次的過濾條件：{'、'.join(said)}")
    if ig := found.get("ignored"):
        parts.append("★ 下列過濾條件認不出來、已被忽略，所以以下結果**不是**"
                     f"使用者要的範圍，回答時必須講明：{'；'.join(ig)}")

    rows = found.get("results") or []
    if not rows:
        # 這只是初步檢索，不可寫成「語料裡沒有」，否則模型會放棄用工具再查
        parts.append("初步檢索（把問句拆成過濾條件後直接查）沒有命中。"
                     "請改用工具換條件或關鍵字再查一次；工具也查不到時，"
                     "要如實說「客訴單語料裡查不到」，不要說「沒有客訴」。")
    else:
        total = found.get("total", len(rows))
        parts.append(f"命中 {total} 張，以下是 {min(len(rows), 3)} 張的內容：")

    for row in rows[:3]:
        seg = [
            f"\n【{row.get('CAR編號')}】{row.get('問題主旨') or ''}",
            f"  申請種類：{row.get('申請種類') or '未填'}"
            f"　客訴是否成立：{row.get('客訴是否成立') or '★ 尚未判定（不可算成不成立）'}"
            f"　狀態：{row.get('狀態') or '未填'}",
            f"  受影響客戶：{row.get('受影響客戶') or '未填'}"
            f"　生產廠區：{row.get('生產廠區') or '未填'}"
            f"　料號：{'、'.join(row.get('料號清單_陣列') or []) or '未填'}",
            f"  交貨 {row.get('交貨數量')} / 退回不良 {row.get('退回數不良數')}"
            f"　處理天數：{row.get('處理天數')}",
            f"  負責品保/RD-PM：{row.get('負責品保RDPM') or '未填'}"
            f"　建立者部門：{row.get('建立者主要部門') or '未填'}",
        ]
        if row.get("責任廠區") or row.get("責任單位供應商"):
            seg.append(f"  責任廠區：{row.get('責任廠區') or '未填'}"
                       f"　責任單位：{row.get('責任單位供應商') or '未填'}")
        if row.get("異常歸類"):
            c = row["異常歸類"]
            if any(c.values()):
                seg.append(f"  異常歸類：{'/'.join('、'.join(c[k]) or '-' for k in c)}")
        if row.get("抱怨描述"):
            seg.append(f"  抱怨描述：{str(row['抱怨描述'])[:_D_CLIP]}")
        for label, text in car.d_sections(row):
            seg.append(f"  {label}：{text[:_D_CLIP]}")
        if row.get("matched_by") == "part_list_text":
            seg.append("  ★ 這一筆是靠「料號清單」文字欄的子字串比對命中的，"
                       "可能是相近料號，引用時要提醒使用者核對。")
        parts.append("\n".join(seg))

    if len(rows) > 3:
        parts.append(f"\n（還有 {len(rows) - 3} 張沒有展開，卡片上都有；"
                     "需要某一張的細節就用 get_complaint 指名查。）")
    for n in found.get("notes") or []:
        parts.append(n)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
def _status() -> tuple[str, dict[str, Any]]:
    """能力全部來自 PLM 來源庫，所以「來源庫通不通」就是「能不能用」。"""
    ok, why = db.available()
    detail: dict[str, Any] = {
        "plm": {"ok": ok, "detail": why},
        "corpus": {
            "table": _CORPUS["table"],
            "rows": _CORPUS["rows"],
            "range": f"{_CORPUS['date_from']} ~ {_CORPUS['date_to']}",
            "landed": False,
            "note": "不落地，每次直接查 PLM 來源庫",
        },
    }
    if not ok:
        return "not_ready", {**detail, "note": "連不上 PLM 來源庫，所有客訴單查詢都不能用"}
    if db.is_test_db():
        # 上游會把答案當事實使用，資料來源是測試庫這件事要從 card 就看得到
        detail["note"] = f"目前指向**測試庫**（{db.source()}），資料可能不是最新。"
    return "ready", detail


SYSTEM = f"""你是「8D 客訴單 Agent」：查 PLM 的**客訴抱怨單(8D)**，回答某一張單、
某一顆料、某一個客戶的客訴事實。

你的資料就是 {_CORPUS['table']}（表單類型 {_CORPUS['record_type']}，
label「{_CORPUS['record_type_label']}」）全部 {_CORPUS['rows']} 張單，
{_CORPUS['date_from']} ~ {_CORPUS['date_to']}。不落地，每次直接查，所以是現況。

你必須遵守的事實與限制：

* **「查不到」要講成「客訴單語料裡查不到」，不可以講成「沒有客訴」或「品質沒問題」。**
  上游（產品估價 Agent、BOM Agent）會拿你的答案去決定報價與換料，
  把「語料沒有」講成「沒出過事」是這個 agent 能造成的最大傷害。

* **「未判定」不是「不成立」。** 客訴是否成立這一欄有 {_UNDECIDED_PCT}%
  是空的 =「還沒判」。工具回傳 `客訴是否成立` 為 null 時要說「這張還沒判定成立與否」，
  絕對不要寫成「客訴不成立」。統計的成立率分母是**已判定**的張數。

* **狀態 `Released` 的意思是「已結案」，不是「已發行」。** 約八成的單是這個狀態。
  講成「已發行」會讓人以為單子還在跑。其餘：品保追蹤中 / 品保確認中 / 審核中 /
  新建立 / 已取消。

* **責任歸屬幾乎全空，不要用它做排名。** 責任廠區填答率
  {_FILL['error_factory']}%、責任單位(供應商) {_FILL['error_vendor']}%。
  被問「哪個廠區/供應商問題最多」時要直說這個欄位沒在維護、答不出來；
  `生產廠區` 是「單子登記的生產廠區」，**不是**「責任在誰」，不可以拿它代答。

* **D3~D8 的填答率是 69% / 66% / 66% / 63% / 61% / 32%。**
  某一段是空的代表「表單上沒填」，不是「沒有對策」。
  尤其 D8 結果只有 32% 有值 —— 不要因為 D8 空白就說改善沒有效果。

* **料號反查有兩種命中方式，要分清楚。** `matched_by=relationship` 是精確
  比對料號；`matched_by=part_list_text` 是對「料號清單」文字欄做子字串比對，
  **可能誤中相近料號**（查 -1 會命中 -10）。引用後者時要請使用者自己核對。

* **不提供金額。** 客訴造成多少損失、賠了多少錢 —— 客訴費用表幾乎沒有資料，
  答不出來。被問就說沒有這份資料。

* **不提供 8D 報告附件。** PLM 上多數單有 8D 報告附件，但本 agent 不提供下載或解析。
  被問「8D 報告在哪」要說「PLM 的這張單上有附件，但我這邊不提供取檔」——
  **不要說沒有附件**。

* **申請種類看欄位，不看編號前綴。** 有少數單的 work_type 與編號前綴不一致。
  工具回的 `申請種類` 已經是正確的那個，不要自己從編號推。

* 有些單子的生產廠區會顯示成 `F05_海外廠（舊代碼…）` 或裸代碼 `O` ——
  那是海外廠拆成 O1/O2 之前的舊代碼，不是資料壞掉。

* 工具回傳的 `ignored` 非空時，代表有過濾條件認不出來被丟掉了，
  **回傳的範圍不是使用者要的**。這時要先講這件事，再講數字。

回答用繁體中文的一般文字（可用條列），**不要輸出 JSON**，直接講結論。引用某一張單時帶上它的 CAR 編號，
讓使用者可以自己去 PLM 開那張單核對。"""


EXAMPLES = [
    {"label": "料號有沒有客訴史",
     "prompt": "CON-A70090-1 有被客訴過嗎？結果是什麼？"},
    {"label": "查一張單的全部內容",
     "prompt": "CAR-W250602 客訴成立嗎？根本原因和永久對策是什麼？負責品保是誰？"},
    {"label": "客戶的客訴統計",
     "prompt": "範例電腦過去有幾張客訴？成立率多少？異常歸類集中在哪裡？"},
    {"label": "自由文字找相似案例",
     "prompt": "有哪些客訴是端子變形或彈高偏規格下限造成的？"},
    {"label": "廠區 + 期間",
     "prompt": "西區廠 2025 年之後的客訴單有哪些？各自結案了嗎？"},
]


SPEC = AgentSpec(
    agent_id=AGENT_ID,
    tools=TOOLS,
    system=SYSTEM,
    examples=EXAMPLES,
    search_fn=_search,
    hits_fn=_hits,
    context_fn=_context,
    answer_stream=None,   # 沒有自己的摘要；文字由工具迴圈產生
    status_fn=_status,
    blocked_by=BLOCKED_BY,
    can_delegate=False,   # 葉節點，沒有下游可委派
)


def mount(app: Any) -> bool:
    try:
        app.include_router(build_router(SPEC))
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[a2a] 掛載失敗：{type(e).__name__}: {e}")
        return False
