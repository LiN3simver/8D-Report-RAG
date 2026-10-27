"""客訴抱怨單(8D) 的查詢層。直接對 PLM 來源庫下 SELECT，不落地。

    python -m src.car --selftest              # 回歸測試
    python -m src.car CAR-W250602             # 查一張單
    python -m src.car --part CON-A70090-1     # 料號反查客訴
    python -m src.car --search 彈高偏規格      # 自由文字
    python -m src.car --stats --customer 範例電腦

查詢檔以 `-- @區塊` 分段，由這裡組裝；展示版不附查詢檔本身，查詢設計見 sql/README.md。

這一層負責守住四個語意：
  1. `result` 的 NULL ≠ 'No'。NULL 是「尚未判定」，一律保持 None。
  2. `effective_qty` 的 0 ≠ NULL。0 是「真的 0 個不良」，NULL 是「未填」。
  3. D3~D8 的欄位對照只來自 src/fields.py（欄位名與內容錯位一格）。
  4. 料號反查 UNION 兩條路，回傳帶 `matched_by` 說明命中方式。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import fields, guard
from .db import cursor, mssql, source
from .guard import Budget, BudgetExhausted

_SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "car.sql"
_cache: dict[str, str] = {}
_BLOCK_RE = re.compile(r"^--\s*@(\w+)\s*$", re.MULTILINE)

# 自由文字檢索掃的欄位（皆為 nvarchar，可直接 LIKE）。順序 = Hit.why 的列出順序。
TEXT_COLS: tuple[tuple[str, str], ...] = (
    ("item_number",        "CAR編號"),
    ("title",              "單據標題"),
    ("problem_stmt",       "問題主旨"),
    ("problem_stmt2",      "抱怨描述"),
    ("problem_stmt3",      "問題證明"),
    ("part_list",          "料號清單"),
    ("part_description",   "料號描述"),
    ("containment_action", "D3 根本原因"),      # 錯位，見 fields.py
    ("root_cause",         "D4 臨時對策"),      # 錯位，見 fields.py
    ("verification",       "D5 永久改善對策"),
    ("corrective_actions", "D6 效果確認"),
    ("prevent_recurrence", "D7 再發防止"),
    ("closure_result",     "D8 結果"),
    ("note",               "備註說明"),
    ("order_no",           "訂單號碼"),
    ("end_customer",       "終端客戶"),
)

# 跟著每一筆輸出走，避免上游把「查不到」當成「沒出過事」。
CORPUS_NOTE = (
    f"語料是 PLM {fields.CORPUS['table']}（客訴抱怨單 8D）全部 "
    f"{fields.CORPUS['rows']} 張單，{fields.CORPUS['date_from']} ~ "
    f"{fields.CORPUS['date_to']}。查不到不等於沒出過事。"
)
COST_NOTE = "本 Agent 不提供客訴造成的金額/成本：費用表（complaint_cost）幾乎沒有資料。"


# ---------------------------------------------------------------------------
# SQL 載入
# ---------------------------------------------------------------------------
def _sql() -> str:
    if "car" not in _cache:
        if not _SQL_FILE.is_file():
            raise FileNotFoundError(
                f"找不到查詢檔 {_SQL_FILE}（展示版不附查詢檔）"
            )
        _cache["car"] = _SQL_FILE.read_text(encoding="utf-8")
    return _cache["car"]


def _block(name: str) -> str:
    """取查詢檔裡 `-- @name` 之後、下一個 `-- @` 之前的那段。"""
    text = _sql()
    marks = [(m.group(1), m.end(), m.start()) for m in _BLOCK_RE.finditer(text)]
    for i, (key, start, _) in enumerate(marks):
        if key == name:
            end = marks[i + 1][2] if i + 1 < len(marks) else len(text)
            return text[start:end].strip()
    raise KeyError(f"{_SQL_FILE.name} 裡沒有 -- @{name}")


def _select(where: str = "", *, top: bool = False) -> str:
    """組出 `SELECT [TOP (?)] <columns> <from> <where>`。

    查單張與查清單共用 @columns / @from，欄位對照只有一份。
    """
    head = "SELECT TOP (?)" if top else "SELECT"
    return f"{head}\n{_block('columns')}\n{_block('from')}\n{where}"


# ---------------------------------------------------------------------------
# 型別
# ---------------------------------------------------------------------------
def _val(v: Any) -> Any:
    """pyodbc 的值 -> 可 JSON 序列化的值。空字串 -> None；0 保持 0。"""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(round(v, 6))
    if isinstance(v, (datetime, date)):
        return v.isoformat(sep=" ")[:19]
    if isinstance(v, str):
        return v.strip() or None
    return v


def _rows(cur, sql: str, args: list) -> list[dict[str, Any]]:
    cur.execute(sql, *args) if args else cur.execute(sql)
    cols = [d[0] for d in cur.description]
    return [{c: _val(v) for c, v in zip(cols, r)} for r in cur.fetchall()]


def _one(cur, sql: str, args: list) -> dict[str, Any] | None:
    rs = _rows(cur, sql, args)
    return rs[0] if rs else None


# ---------------------------------------------------------------------------
# 輸入正規化
# ---------------------------------------------------------------------------
_CAR_NO = re.compile(r"\b(CAR|CAF|NPI)-[A-Z0-9]{2,}\d{4,}\b", re.IGNORECASE)

# 料號：長度 >= 8 且至少 4 個數字，可排除 PH2.0 / UL2725 這類規格字串。
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9\-./]{7,}")


def car_numbers(q: str) -> list[str]:
    """從自由文字認出 CAR 編號。"""
    out: list[str] = []
    for m in _CAR_NO.finditer(q or ""):
        t = m.group(0).upper()
        if t not in out:
            out.append(t)
    return out[:4]


def part_numbers(q: str) -> list[str]:
    """從自由文字認出料號。已認成 CAR 編號的不再當料號。"""
    cars = set(car_numbers(q))
    out: list[str] = []
    for m in _CODE.finditer(q or ""):
        t = m.group(0).upper().strip(".-/")
        if len(t) < 8 or sum(c.isdigit() for c in t) < 4:
            continue
        if t in cars or any(t in c for c in cars) or t in out:
            continue
        out.append(t)
    return out[:4]


def _rev(m: dict[str, str]) -> dict[str, str]:
    """{值: 標籤} -> {標籤(小寫): 值}，讓過濾器同時吃原值與中文標籤。"""
    return {v.lower(): k for k, v in m.items()}


_REV_RESULT = _rev(fields.CAR_RESULT)
_REV_WORKTYPE = _rev(fields.WORK_TYPE)
_REV_FACTORY = _rev(fields.WORKING_FACTORY_ALL)   # 含退役代碼
_REV_STATE = _rev(fields.STATE)


def _result_code(v: Any) -> str | None:
    """把「客訴是否成立」的各種寫法正規化成 'Yes' / 'No'。

    「客訴不成立」也包含「成立」，一般子字串比對會判成歧義、把過濾器丟掉，
    結果回傳全庫 —— 而那個數字看起來完全合理。所以先比否定、再比肯定。
    """
    s = str(v or "").strip().lower()
    if not s:
        return None
    if s in ("yes", "no"):
        return "Yes" if s == "yes" else "No"
    if any(t in s for t in ("不成立", "未成立", "不 成立", "invalid")):
        return "No"
    if any(t in s for t in ("成立", "established")):
        return "Yes"
    return _norm(v, fields.CAR_RESULT, _REV_RESULT)


def _norm(v: str | None, table: dict[str, str], rev: dict[str, str]) -> str | None:
    """把過濾值正規化成 DB 原值。

    吃三種寫法：原值（'W'）、完整標籤（'F04_西區廠'）、唯一命中的中文子字串（'西區'）。
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if s in table:
        return s
    if s.upper() in table:
        return s.upper()
    if s.lower() in rev:
        return rev[s.lower()]
    hits = [k for k, lab in table.items() if s.lower() in lab.lower()]
    if len(hits) == 1:
        return hits[0]
    return None


# ---------------------------------------------------------------------------
# 過濾條件
# ---------------------------------------------------------------------------
def _filters(f: dict[str, Any]) -> tuple[str, list[Any], list[str], list[str]]:
    """過濾器 -> (WHERE 片段, 參數, 人看得懂的說明, 被忽略的過濾器)。

    條件只碰 q.* 或子查詢、不依賴 JOIN 別名，所以同一段 WHERE 也能塞進
    沒有 JOIN 的 @stats_* 區塊。認不出來的過濾值放進 ignored 回報，
    不靜靜忽略 —— 否則使用者會拿到全庫數字卻以為是篩選後的結果。
    """
    conds: list[str] = []
    args: list[Any] = []
    said: list[str] = []
    ignored: list[str] = []

    if v := (f.get("car_no") or "").strip() if isinstance(f.get("car_no"), str) else None:
        conds.append("q.item_number = ?")
        args.append(v.upper())
        said.append(f"編號={v.upper()}")

    if isinstance(f.get("customer"), str) and f["customer"].strip():
        conds.append(
            "q.customer IN (SELECT c.id FROM plm.customer c WHERE c.name LIKE ?)"
        )
        args.append(f"%{f['customer'].strip()}%")
        said.append(f"客戶包含「{f['customer'].strip()}」")

    if f.get("factory"):
        code = _norm(f["factory"], fields.WORKING_FACTORY_ALL, _REV_FACTORY)
        if code:
            conds.append("q.working_factory = ?")
            args.append(code)
            said.append(f"生產廠區={fields.WORKING_FACTORY_ALL[code]}")
        else:
            ignored.append(
                f"factory=「{f['factory']}」認不出來，已忽略。"
                f"可用：{'、'.join(fields.WORKING_FACTORY.values())}"
                f"（另有舊代碼 O = {fields.RETIRED_FACTORY['O']}）"
            )

    if f.get("result") is not None:
        raw = str(f["result"]).strip()
        if raw in ("未判定", "null", "NULL", "none", "None", "(未判定)"):
            conds.append("q.result IS NULL")
            said.append("客訴是否成立=未判定")
        else:
            code = _result_code(raw)
            if code:
                conds.append("q.result = ?")
                args.append(code)
                said.append(f"客訴是否成立={fields.CAR_RESULT[code]}")
            else:
                ignored.append(
                    f"result=「{raw}」認不出來，已忽略。"
                    "可用：客訴成立 / 客訴不成立 / 未判定"
                )

    if f.get("work_type"):
        code = _norm(f["work_type"], fields.WORK_TYPE, _REV_WORKTYPE)
        if code:
            # 看 work_type 欄位，不從編號前綴推（兩者有少數不一致）
            conds.append("q.work_type = ?")
            args.append(code)
            said.append(f"申請種類={fields.WORK_TYPE[code]}")
        else:
            ignored.append(
                f"work_type=「{f['work_type']}」認不出來，已忽略。"
                f"可用：{'、'.join(fields.WORK_TYPE.values())}"
            )

    if f.get("state"):
        code = _norm(f["state"], fields.STATE, _REV_STATE)
        if code:
            conds.append("q.[state] = ?")
            args.append(code)
            said.append(f"狀態={fields.STATE[code]}")
        else:
            ignored.append(
                f"state=「{f['state']}」認不出來，已忽略。"
                f"可用：{'、'.join(fields.STATE.values())}"
            )

    for key, op, label in (("since", ">=", "建立日期 >="), ("until", "<", "建立日期 <")):
        if f.get(key):
            d = _date(f[key])
            if d:
                conds.append(f"q.created_on {op} ?")
                args.append(d)
                said.append(f"{label} {d}")
            else:
                ignored.append(f"{key}=「{f[key]}」不是可辨識的日期（要 YYYY-MM-DD），已忽略")

    if isinstance(f.get("part_number"), str) and f["part_number"].strip():
        pn = f["part_number"].strip().upper()
        conds.append(f"q.id IN ({_block('by_part_ids')})")
        args += [pn, f"%{pn}%"]
        said.append(f"料號={pn}")

    if isinstance(f.get("text"), str) and f["text"].strip():
        needle = f["text"].strip()
        conds.append("(" + " OR ".join(f"q.{c} LIKE ?" for c, _ in TEXT_COLS) + ")")
        args += [f"%{needle}%"] * len(TEXT_COLS)
        said.append(f"文字包含「{needle}」")

    where = ("WHERE " + "\n  AND ".join(conds)) if conds else ""
    return where, args, said, ignored


# ---------------------------------------------------------------------------
# 問句 -> 過濾器
#
# 卡片是拿「整句問句」檢索的，而整句不能當 LIKE 字串（沒有任何單據內文含整句話）。
# 日期 / 廠區 / 成立與否 / 狀態都有對應欄位，先拆成過濾器，剩下的才是關鍵字。
# ---------------------------------------------------------------------------
_CN_NUM = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12, "半": 0}
_NUM = r"(\d{1,2}|十[一二]?|[一二兩三四五六七八九]|半)"

# 廠區口語 -> WORKING_FACTORY 代碼。只放不歧義的詞（「海外」同時對應 O1/O2/O，不放）。
_FACTORY_WORDS = {"北區": "N", "東區": "E", "南區": "S", "西區": "W",
                  "海外一": "O1", "海外二": "O2", "外購": "P"}

# 語氣與問法，不是內容。拿掉之後剩不到兩個字就當成「沒有自由文字」。
_FILLER = re.compile(
    r"(請問|請|幫我|給我|列出|查詢|查一下|查|有沒有|有什麼|有哪些|哪些|什麼|多少|幾張|"
    r"各自|分別|目前|現在|一下|所有|全部|相關的?|關於|"
    r"客訴抱怨單|客訴單|客訴|抱怨單|8D|CAR|單子|單據|資料|紀錄|"
    r"過去|曾經|集中在?|哪裡|哪個|分佈|統計|比例|排名|成立率|是否成立|成立|結案|狀態|進度|幾次|造成|導致|引起|原因|"
    r"建立|開立|發生|被|過|的|了|嗎|呢|吧|是|有|在|和|與|及|或|，|。|、|？|\?|！|!|:|：|\s)+",
    re.IGNORECASE,
)


def _n(s: str) -> int:
    return int(s) if s.isdigit() else _CN_NUM.get(s, 0)


def _month_add(y: int, m: int, k: int) -> tuple[int, int]:
    i = y * 12 + (m - 1) + k
    return i // 12, i % 12 + 1


def parse_question(q: str, today: date | None = None) -> tuple[str, dict[str, Any]]:
    """把問句拆成 (剩下的自由文字, 過濾器)。確定性規則，不呼叫 LLM。

    認得：
      今年 / 去年 / 前年 / 2025年 [N月] [之後|以後|以來|起|之前|以前]
      N月（沒寫年就是今年）、最近 N 個月 / 近一年
      廠區（西區、東區…）、成立的 / 不成立的 / 未判定、狀態標籤（已結案…）
    """
    today = today or date.today()
    s = q or ""
    f: dict[str, Any] = {}
    spans: list[tuple[int, int]] = []

    # ---- 日期 -------------------------------------------------------------
    rel_year = {"今年": 0, "去年": -1, "前年": -2}
    m = re.search(
        r"(?:(今年|去年|前年)|(20\d{2})\s*年?)\s*(?:(\d{1,2}|十[一二]?|[一二三四五六七八九])\s*月份?)?"
        r"\s*(之後|以後|以來|起|開始|之前|以前)?", s)
    if m and (m.group(1) or m.group(2)):
        y = today.year + rel_year[m.group(1)] if m.group(1) else int(m.group(2))
        mon = _n(m.group(3)) if m.group(3) else 0
        rel = m.group(4) or ""
        start = date(y, mon or 1, 1)
        end = date(*_month_add(y, mon, 1), 1) if mon else date(y + 1, 1, 1)
        if rel in ("之前", "以前"):
            f["until"] = start.isoformat()
        elif rel:
            f["since"] = start.isoformat()
        else:
            f["since"], f["until"] = start.isoformat(), end.isoformat()
        spans.append(m.span())
    elif m2 := re.search(r"(?:最近|近)\s*" + _NUM + r"\s*(個月|年)", s):
        k = _n(m2.group(1))
        months = (k * 12 if m2.group(2) == "年" else k) or 6   # 「近半年」
        y, mon = _month_add(today.year, today.month, -months)
        f["since"] = date(y, mon, min(today.day, 28)).isoformat()
        spans.append(m2.span())
    elif m3 := re.search(r"(\d{1,2}|十[一二]?|[一二三四五六七八九])\s*月份?", s):
        mon = _n(m3.group(1))
        if 1 <= mon <= 12:
            f["since"] = date(today.year, mon, 1).isoformat()
            f["until"] = date(*_month_add(today.year, mon, 1), 1).isoformat()
            spans.append(m3.span())

    # ---- 廠區 -------------------------------------------------------------
    for word, code in _FACTORY_WORDS.items():
        if (i := s.upper().find(word.upper())) >= 0:
            f["factory"] = code
            j = i + len(word)
            if s[j:j + 2] == "廠區":
                j += 2
            elif s[j:j + 1] == "廠":
                j += 1
            spans.append((i, j))
            break

    # ---- 成立與否：只認「…的」修飾語；「成立嗎」是在問，不是在篩 ------------
    if mr := re.search(r"(未判定|還沒判定|尚未判定)|(不成立|未成立)的|(?<![不未])成立的", s):
        f["result"] = "未判定" if mr.group(1) else ("客訴不成立" if mr.group(2) else "客訴成立")
        spans.append(mr.span())

    # ---- 狀態：只認完整標籤（「結案了嗎」是在問） ---------------------------
    for label in fields.STATE.values():
        if (i := s.find(label)) >= 0:
            f["state"] = label
            spans.append((i, i + len(label)))
            break

    rest = s
    for a, b in sorted(spans, reverse=True):
        rest = rest[:a] + " " + rest[b:]
    rest = _FILLER.sub(" ", rest).strip()
    if len(rest.replace(" ", "")) < 2:
        rest = ""
    else:
        # 多段關鍵字以空白分隔回傳，由呼叫端逐段查
        rest = " ".join(t for t in rest.split() if len(t) >= 2)
    return rest, f


def _date(v: Any) -> str | None:
    """日期正規化成 YYYY-MM-DD。認不出來回 None（呼叫端會回報）。"""
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m", "%Y"):
        try:
            d = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return d.strftime("%Y-%m-%d")
    return None


# ---------------------------------------------------------------------------
# 後處理
# ---------------------------------------------------------------------------
def _enrich(row: dict[str, Any]) -> dict[str, Any]:
    """拆開 mv_list、旗標轉 bool、補上料號陣列。原欄位一律保留。"""
    row["料號清單_陣列"] = fields.split_part_list(row.get("料號清單"))
    row["異常歸類"] = {
        "人機料法環": fields.split_mv(row.get("異常歸類_人機料法環")),
        "大類": fields.split_mv(row.get("異常歸類_大類")),
        "細項": fields.split_mv(row.get("異常歸類_細項")),
    }
    row["需轉QMS客訴代碼"] = fields.as_bool(row.get("需轉QMS客訴代碼"))
    if row.get("客訴是否成立_原值") is None:
        row["客訴是否成立"] = None
        row["客訴是否成立_說明"] = "尚未判定（result 為 NULL）"
    return row


def d_sections(row: dict[str, Any]) -> list[tuple[str, str]]:
    """D3~D8 有內容的段落，(標題, 內文)。對照來自 src/fields.py。"""
    out: list[tuple[str, str]] = []
    for label, _col, _fill in fields.D_FIELDS:
        key = label.replace(" ", "")
        if v := row.get(key):
            out.append((label, str(v)))
    return out


def _why(row: dict[str, Any], needle: str) -> list[str]:
    """這一列是因為哪幾個欄位命中的（Hit.why）。

    在 Python 端重算，比在 SQL 裡寫 16 個 CASE WHEN 簡單，且順序可控。
    """
    if not needle:
        return []
    n = needle.lower()
    out: list[str] = []
    for col, label in TEXT_COLS:
        for key in (_COL2ALIAS.get(col), col):
            if key and (v := row.get(key)) and n in str(v).lower():
                out.append(f"{label} 命中「{needle}」")
                break
    return out


# TEXT_COLS 的 DB 欄位名 -> @columns 裡的中文別名
_COL2ALIAS: dict[str, str] = {
    "item_number": "CAR編號",
    "title": "title",
    "problem_stmt": "問題主旨",
    "problem_stmt2": "抱怨描述",
    "problem_stmt3": "問題證明",
    "part_list": "料號清單",
    "part_description": "料號描述",
    "containment_action": "D3根本原因",
    "root_cause": "D4臨時對策",
    "verification": "D5永久改善對策",
    "corrective_actions": "D6效果確認",
    "prevent_recurrence": "D7再發防止",
    "closure_result": "D8結果",
    "note": "備註說明",
    "order_no": "訂單號碼",
    "end_customer": "終端客戶",
}


# ---------------------------------------------------------------------------
# 對外的四支查詢
# ---------------------------------------------------------------------------
def detail(car_no: str) -> dict[str, Any]:
    """一張客訴單的全部欄位 + 料號關聯 + 通知人員。

    三種找法共用失敗預算：精確 -> 轉大寫 -> 前綴 LIKE。
    """
    raw = (car_no or "").strip()
    if not raw:
        return {"ok": False, "error": "要給 CAR 編號", "input": car_no}

    b = Budget()
    try:
        with mssql() as cn:
            cur = cursor(cn)
            sql = _select(_block("detail_where"))
            row = b.attempt("編號精確", lambda: _one(cur, sql, [raw]))
            if row is None and raw.upper() != raw:
                row = b.attempt("編號轉大寫", lambda: _one(cur, sql, [raw.upper()]))
            if row is None:
                row = b.attempt(
                    "編號前綴",
                    lambda: _one(
                        cur,
                        _select("WHERE q.item_number LIKE ? ORDER BY q.created_on DESC"),
                        [f"{raw.upper()}%"],
                    ),
                )
            if row is None:
                return {
                    "ok": False,
                    "error": f"查無客訴單 {raw}",
                    "input": raw,
                    "tried": b.tried,
                    "note": CORPUS_NOTE,
                }
            aid = row["record_id"]
            parts = _rows(cur, _block("parts"), [aid])
            notify = [r["通知人員"] for r in _rows(cur, _block("notify"), [aid]) if r["通知人員"]]
    except BudgetExhausted as e:
        return {"ok": False, "error": str(e), "input": raw, "tried": e.tried}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "input": raw}

    row = _enrich(row)
    return {
        "ok": True,
        "car_no": row["CAR編號"],
        "record": row,
        "parts": parts,
        "notify": notify,
        "disciplines": [{"標題": t, "內容": c} for t, c in d_sections(row)],
        "source": source(),
        "notes": [CORPUS_NOTE],
    }


def search(text: str = "", top_k: int = guard.MAX_HITS, **filters: Any) -> dict[str, Any]:
    """自由文字 + 結構化過濾，回完整欄位的列。"""
    top_k = max(1, min(int(top_k or guard.MAX_HITS), guard.MAX_ROWS))
    f = {**filters, "text": text or None}
    where, args, said, ignored = _filters(f)

    try:
        with mssql() as cn:
            cur = cursor(cn)
            sql = _select(f"{where}\nORDER BY q.created_on DESC", top=True)
            rows = _rows(cur, sql, [top_k, *args])
            total = _one(cur, f"SELECT COUNT(*) AS n\n{_block('from')}\n{where}", args)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "results": [], "filters": said, "ignored": ignored}

    for r in rows:
        _enrich(r)
        r["why"] = _why(r, text or "")
    n = (total or {}).get("n", len(rows))
    return {
        "ok": True,
        "query": text or "",
        "filters": said,
        "ignored": ignored,
        "total": n,
        "returned": len(rows),
        "truncated": n > len(rows),
        "results": rows,
        "source": source(),
        "notes": [CORPUS_NOTE] + (["★ 有過濾器被忽略，下面的數字不是你要的範圍"] if ignored else []),
    }


def by_part(part_number: str, top_k: int = guard.MAX_ROWS) -> dict[str, Any]:
    """料號反查客訴史。兩條路 UNION，每筆帶 matched_by：

        relationship    complaint_part 關聯，精確比對料號，可信
        part_list_text  料號清單文字欄 LIKE，可能誤中相近料號（查 -1 會命中 -10）

    只走關聯會漏掉約 7% 的單。誤中是刻意接受的：漏掉一張真客訴比多給一張更糟。
    """
    pn = (part_number or "").strip().upper()
    if not pn:
        return {"ok": False, "error": "要給料號", "input": part_number}

    try:
        with mssql() as cn:
            cur = cursor(cn)
            hit = _rows(cur, _block("by_part_ids"), [pn, f"%{pn}%"])
            if not hit:
                return {
                    "ok": True,
                    "part_number": pn,
                    "total": 0,
                    "results": [],
                    "source": source(),
                    "notes": [
                        f"{pn} 在客訴單語料裡查不到紀錄。",
                        CORPUS_NOTE,
                        "★ 這是「語料裡沒有」，不是「這顆料沒出過事」。",
                    ],
                }
            how = {r["id"]: r["matched_by"] for r in hit}
            ids = list(how)[:top_k]
            placeholders = ", ".join("?" * len(ids))
            sql = _select(_block("by_ids").replace("{ids}", placeholders))
            rows = _rows(cur, sql, ids)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "input": pn, "results": []}

    established = 0
    undecided = 0
    for r in rows:
        _enrich(r)
        r["matched_by"] = how.get(r["record_id"])
        if r.get("客訴是否成立_原值") == "Yes":
            established += 1
        elif r.get("客訴是否成立_原值") is None:
            undecided += 1

    notes = [CORPUS_NOTE]
    if any(r["matched_by"] == "part_list_text" for r in rows):
        notes.append(
            "★ 部分命中來自「料號清單」文字欄的子字串比對（matched_by=part_list_text），"
            "可能是相近料號（例：查 -1 會命中 -10）。請看每一筆的料號清單確認。"
        )
    if undecided:
        notes.append(f"★ 其中 {undecided} 張還沒判定成立與否，不可以算成「不成立」。")
    return {
        "ok": True,
        "part_number": pn,
        "total": len(rows),
        "established": established,
        "undecided": undecided,
        "results": rows,
        "source": source(),
        "notes": notes,
    }


def stats(top_customers: int = 10, **filters: Any) -> dict[str, Any]:
    """張數 / 成立率 / 狀態 / 廠區 / 申請種類 / 客戶 / 異常歸類 的分佈。

    成立率的分母是「已判定」的張數：未判定塞進分母會低估成立率，
    塞進「不成立」則是造假。成立 / 不成立 / 未判定三個數字一起回。
    """
    where, args, said, ignored = _filters(filters)

    def group(block: str, extra: list[Any] | None = None) -> list[dict[str, Any]]:
        return _rows(cur, _block(block).replace("{where}", where), [*(extra or []), *args])

    try:
        with mssql() as cn:
            cur = cursor(cn)
            span = _one(cur, _block("stats_span").replace("{where}", where), args) or {}
            g_result = group("stats_result")
            g_state = group("stats_state")
            g_factory = group("stats_factory")
            g_worktype = group("stats_worktype")
            g_customer = group("stats_customer", [max(1, int(top_customers or 10))])
            code_rows = group("stats_codes")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "filters": said, "ignored": ignored}

    res = {r["k"]: r["n"] for r in g_result}
    yes, no = res.get("Yes", 0), res.get("No", 0)
    undecided = res.get("(未判定)", 0)
    decided = yes + no

    codes: dict[str, dict[str, int]] = {"人機料法環": {}, "大類": {}, "細項": {}}
    for r in code_rows:
        for key, col in (("人機料法環", "c1"), ("大類", "c2"), ("細項", "c3")):
            for v in fields.split_mv(r.get(col)):
                codes[key][v] = codes[key].get(v, 0) + 1
    codes = {k: dict(sorted(v.items(), key=lambda x: -x[1])) for k, v in codes.items()}

    fill = fields.CORPUS["fill"]
    notes = [CORPUS_NOTE, COST_NOTE]
    if undecided:
        notes.append(
            f"★ {undecided} 張尚未判定成立與否。成立率的分母是「已判定的 {decided} 張」，"
            "不含這些；不可以把未判定講成不成立。"
        )
    if span.get("有填責任廠區") is not None:
        notes.append(
            f"★ 責任歸屬欄位幾乎全空（本範圍內有填責任廠區 {span.get('有填責任廠區')} 張、"
            f"責任供應商 {span.get('有填責任供應商')} 張，全庫填答率 "
            f"{fill['error_factory']}% / {fill['error_vendor']}%）。"
            "**不要用它回答「哪個廠區或供應商出最多事」。**"
        )
    if ignored:
        notes.insert(0, "★ 有過濾器被忽略，下面的數字不是你要的範圍")

    return {
        "ok": True,
        "filters": said,
        "ignored": ignored,
        "span": span,
        "客訴是否成立": {
            "成立": yes, "不成立": no, "未判定": undecided,
            "已判定張數": decided,
            "成立率": round(yes / decided, 4) if decided else None,
        },
        "狀態": {fields.STATE.get(r["k"], r["k"]): r["n"] for r in g_state},
        "生產廠區": {fields.WORKING_FACTORY_ALL.get(r["k"], r["k"] or "(未填)"): r["n"]
                     for r in g_factory},
        "申請種類": {fields.WORK_TYPE.get(r["k"], r["k"] or "(未填)"): r["n"]
                     for r in g_worktype},
        "客戶": {(r["k"] or "(未填)"): r["n"] for r in g_customer},
        "異常歸類": codes,
        "source": source(),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 回歸測試（需連線到來源庫；基準值見 fields.CORPUS 與下方黃金樣本）
# ---------------------------------------------------------------------------
GOLD = "CAR-W250602"
GOLD_PART = "CON-B30010-0"
GOLD_FIELDS: dict[str, Any] = {
    "CAR編號": "CAR-W250602",
    "申請種類": "MP(量產階段)抱怨",
    "申請種類_原值": "CAR",
    "客訴是否成立": "客訴成立",
    "客訴是否成立_原值": "Yes",
    "狀態": "已結案",
    "狀態_原值": "Released",
    "填單廠區": "W:西區",
    "生產廠區": "F04_西區廠",
    "生產廠區_原值": "W",
    "責任廠區": "W:西區",
    "受影響客戶": "範例電腦股份有限公司",
    "供應商": "範例精密科技有限公司",
    "責任單位供應商": "範例精密科技有限公司",
    "交貨日期": "2025-05-14",
    "交貨數量": 20000,
    "退回數不良數": 1200,
    "Discipline進度": "8",
    "處理天數": 14.0,
    "負責品保RDPM": "qa_wang 王小明",
    "建立者主要部門": "業務管理課",
    "建立人員": "sales01 李小華",
    "修改人員": "qa_chen 陳大文",
    "版本": "A",
    "版次": 1,
}
# 料號反查的樣本：一張成立、一張未判定
SAMPLE_PART = "CON-A70090-1"
# 只登記在料號清單文字欄、沒有關聯表紀錄的料號
SAMPLE_TEXT_ONLY_PART = "CON-D40020-1"


def _check(name: str, got: Any, want: Any) -> bool:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"       want={want!r}\n       got ={got!r}")
    return ok


def selftest() -> bool:  # noqa: C901, PLR0915
    """回歸測試，含反向證據：不只證明現在是對的，也證明寫錯時會被抓到。"""
    ok = True
    t0 = time.perf_counter()
    c = fields.CORPUS

    print(f"[1] PLM 來源庫連線（{source()}）")
    from .db import available

    up, why = available()
    ok &= _check("available()", up, True)
    print(f"       {why}")
    if not up:
        print("\n連不上來源庫，後面全部跳過。")
        return False

    print(f"\n[2] 黃金樣本 {GOLD} 逐格對帳")
    d = detail(GOLD)
    ok &= _check("detail().ok", d.get("ok"), True)
    if d.get("ok"):
        rec = d["record"]
        for k, want in GOLD_FIELDS.items():
            ok &= _check(f"{GOLD}.{k}", rec.get(k), want)
        ok &= _check("料號清單_陣列", rec["料號清單_陣列"], [GOLD_PART])
        ok &= _check("異常歸類.人機料法環", rec["異常歸類"]["人機料法環"], ["人"])
        ok &= _check("異常歸類.大類", rec["異常歸類"]["大類"], ["尺寸異常", "外觀異常"])
        ok &= _check("異常歸類.細項", rec["異常歸類"]["細項"], ["尺寸NG", "其他"])
        ok &= _check("需轉QMS客訴代碼 是真 bool", rec["需轉QMS客訴代碼"], True)
        ok &= _check("parts -> PART", [p["料號"] for p in d["parts"]], [GOLD_PART])
        ok &= _check("notify 3 人", len(d["notify"]), 3)
        ok &= _check("disciplines 六段都有", len(d["disciplines"]), 6)

        print("\n[3] 反向證據：D3/D4 的欄位名與內容錯位一格（對調就會 FAIL）")
        d3 = rec.get("D3根本原因") or ""
        d4 = rec.get("D4臨時對策") or ""
        ok &= _check("D3根本原因 含「产生原因」", "产生原因" in d3, True)
        ok &= _check("D3根本原因 不含「退回換貨」", "退回換貨" in d3, False)
        ok &= _check("D4臨時對策 含「退回」", "退回" in d4, True)
        ok &= _check("D4臨時對策 不含「产生原因」", "产生原因" in d4, False)
        ok &= _check("fields.D_FIELDS[0] 仍指向 containment_action",
                     fields.D_FIELDS[0][1], "containment_action")
        ok &= _check("fields.D_FIELDS[1] 仍指向 root_cause",
                     fields.D_FIELDS[1][1], "root_cause")

    print("\n[4] @from 的 LEFT JOIN 不會放大列數")
    st = stats()
    base = st["span"].get("張數")                    # 統計區塊不經過 JOIN
    joined = search(top_k=1)["total"]               # 清單查詢經過 @from 的全部 JOIN
    ok &= _check("語料列數", base, c["rows"])
    ok &= _check("經過 JOIN 後列數不變（ON 寫寬會讓列數翻倍）", joined, base)

    print("\n[5] 料號反查：兩條路都要查")
    bp = by_part(SAMPLE_PART)
    ok &= _check(f"by_part('{SAMPLE_PART}').total", bp.get("total"), 2)
    # 反向證據：這顆料只登記在文字欄，簡化成只查關聯表就會變成 0 張
    tp = by_part(SAMPLE_TEXT_ONLY_PART)
    ok &= _check(f"只在文字欄的料號 {SAMPLE_TEXT_ONLY_PART} 查得到", tp.get("total", 0) > 0, True)
    ok &= _check("  且標成 part_list_text",
                 {r["matched_by"] for r in tp.get("results", [])}, {"part_list_text"})
    ok &= _check("查無料號時 ok=True 而不是報錯",
                 by_part("NO-SUCH-PART-99999").get("ok"), True)
    ok &= _check("查無料號時 total=0", by_part("NO-SUCH-PART-99999").get("total"), 0)

    print("\n[6] 未判定不是「不成立」")
    ok &= _check("stats().客訴是否成立.未判定", st["客訴是否成立"]["未判定"], c["undecided"])
    ok &= _check("成立 + 不成立 + 未判定 = 全庫",
                 sum(st["客訴是否成立"][k] for k in ("成立", "不成立", "未判定")), c["rows"])
    ok &= _check("成立率的分母不含未判定",
                 st["客訴是否成立"]["已判定張數"], c["rows"] - c["undecided"])
    ok &= _check("filter result=未判定 撈得到全部未判定",
                 search(result="未判定", top_k=1)["total"], c["undecided"])

    print("\n[7] 申請種類看 work_type，不看編號前綴")
    allrows = search(top_k=guard.MAX_ROWS)["results"]
    mism = sum(1 for r in allrows if r["申請種類_原值"] != (r["CAR編號"] or "")[:3])
    ok &= _check("work_type 與前綴不一致的張數", mism, c["work_type_prefix_mismatch"])

    print("\n[8] 兩張廠區清單不同碼")
    ok &= _check("南區：生產廠區清單是 S、填單廠區清單是 X",
                 (fields.WORKING_FACTORY.get("S"), fields.SEQ_FACTORY.get("X")),
                 ("F03_南區廠", "X:南區"))

    print("\n[8b] 退役代碼 'O'：清單解析不到，靠快取欄位救回")
    old = search(factory="O", top_k=guard.MAX_ROWS)["results"]
    rf = c["retired_factory"]
    current = set(fields.WORKING_FACTORY.values())
    ok &= _check("working_factory='O' 的張數", len(old), rf["rows"])
    ok &= _check("其中清單完全解不出來的（= 只用清單會漏的）",
                 sum(1 for r in old if r["生產廠區"] not in current), rf["rows"])
    ok &= _check("快取欄位救得回來的",
                 sum(1 for r in old if r.get("生產廠區_快取")), rf["cache_rescued"])
    ok &= _check("兩邊都沒有的顯示裸代碼 'O'（不是空白）",
                 sum(1 for r in old if r["生產廠區"] == "O"), rf["rows"] - rf["cache_rescued"])
    ok &= _check("stats 把 'O' 標成舊代碼",
                 fields.RETIRED_FACTORY["O"] in st["生產廠區"], True)

    print("\n[9] 過濾器：中文子字串、原值、認不出來要回報")
    ok &= _check("factory='西區' 認得出來",
                 "生產廠區=F04_西區廠" in search(factory="西區", top_k=1)["filters"], True)
    ok &= _check("factory='W' 認得出來",
                 "生產廠區=F04_西區廠" in search(factory="W", top_k=1)["filters"], True)
    bad = search(factory="香港", top_k=1)
    ok &= _check("factory='香港' 要被回報而不是靜靜忽略", len(bad["ignored"]), 1)
    ok &= _check("被忽略時 notes 要警告", any("被忽略" in n for n in bad["notes"]), True)
    ok &= _check("customer='範例電腦' 有命中", search(customer="範例電腦", top_k=1)["total"] > 0, True)

    # 反向證據：「客訴不成立」也含「成立」，處理錯會把過濾器丟掉、回傳全庫
    print("       成立 / 不成立 的歧義（靠否定詞優先）")
    yes_n, no_n = st["客訴是否成立"]["成立"], st["客訴是否成立"]["不成立"]
    ok &= _check("result='成立' -> 只有成立那組", search(result="成立", top_k=1)["total"], yes_n)
    ok &= _check("result='不成立' -> 只有不成立那組", search(result="不成立", top_k=1)["total"], no_n)
    ok &= _check("result='成立' 不等於全庫",
                 search(result="成立", top_k=1)["total"] != c["rows"], True)
    ok &= _check("result='客訴成立' 與 result='Yes' 一致",
                 search(result="客訴成立", top_k=1)["total"] == search(result="Yes", top_k=1)["total"],
                 True)

    print("\n[10] 自由文字檢索與 why")
    s = search("彈高", top_k=5)
    ok &= _check("search('彈高').ok", s.get("ok"), True)
    ok &= _check("search('彈高') 有命中", s["total"] > 0, True)
    ok &= _check("每一筆都說得出為什麼命中", all(r["why"] for r in s["results"]), True)

    print("\n[11] 編號 / 料號辨識")
    ok &= _check("car_numbers", car_numbers("請問 CAF-W260601 跟 car-w250602 的結果"),
                 ["CAF-W260601", "CAR-W250602"])
    ok &= _check("part_numbers 擋掉規格 token",
                 part_numbers(f"PH2.0 UL2725 的 {SAMPLE_PART} 有客訴嗎"), [SAMPLE_PART])
    ok &= _check("part_numbers 不把 CAR 編號當料號",
                 part_numbers(f"{GOLD} 的根本原因"), [])

    print("\n[12] 速度（不落地的依據）")
    t = time.perf_counter()
    search("彈高", top_k=8)
    ms = (time.perf_counter() - t) * 1000
    ok &= _check(f"全表文字檢索 < 1000ms（實際 {ms:.0f}ms）", ms < 1000, True)

    print("\n[13] 問句 -> 過濾器")
    today = date(2026, 10, 20)
    ok &= _check("今年5月 -> 當月區間、沒有剩餘文字",
                 parse_question("今年5月有什麼客訴單?", today),
                 ("", {"since": "2026-05-01", "until": "2026-06-01"}))
    ok &= _check("西區廠 2025 年之後 -> 廠區 + since",
                 parse_question("西區廠 2025 年之後的客訴單有哪些？各自結案了嗎？", today),
                 ("", {"since": "2025-01-01", "factory": "W"}))
    ok &= _check("「成立嗎」是在問、不是過濾器",
                 "result" in parse_question("這些客訴成立嗎？", today)[1], False)
    ok &= _check("「不成立的」-> 不成立（不可被「成立」吃掉）",
                 parse_question("不成立的客訴", today)[1].get("result"), "客訴不成立")
    ok &= _check("多段關鍵字保留兩段",
                 parse_question("有哪些客訴是端子變形或彈高偏規格下限造成的？", today)[0],
                 "端子變形 彈高偏規格下限")
    t, f = parse_question("今年5月有什麼客訴單?", date(2026, 7, 1))
    ok &= _check("拆成過濾器後查得到單（整句當 LIKE 會是 0）",
                 search(t, top_k=1, **f)["total"] > 0, True)

    print(f"\n{'=' * 62}")
    print(f"{'全部通過' if ok else '有測項失敗'}　共耗時 {time.perf_counter() - t0:.1f}s")
    return ok


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="客訴抱怨單(8D) 查詢")
    ap.add_argument("car_no", nargs="?", help="CAR 編號")
    ap.add_argument("--selftest", action="store_true", help="回歸測試")
    ap.add_argument("--part", help="料號反查客訴")
    ap.add_argument("--search", help="自由文字")
    ap.add_argument("--stats", action="store_true", help="統計")
    ap.add_argument("--customer")
    ap.add_argument("--factory")
    ap.add_argument("--result")
    ap.add_argument("--work-type", dest="work_type")
    ap.add_argument("--state")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--top-k", type=int, default=guard.MAX_HITS)
    a = ap.parse_args()

    def dump(o: Any) -> None:
        print(json.dumps(o, ensure_ascii=False, indent=2, default=str))

    f = {k: getattr(a, k) for k in
         ("customer", "factory", "result", "work_type", "state", "since", "until")
         if getattr(a, k)}

    if a.selftest:
        sys.exit(0 if selftest() else 1)
    if a.part:
        dump(by_part(a.part))
    elif a.stats:
        dump(stats(**f))
    elif a.search is not None:
        dump(search(a.search, top_k=a.top_k, **f))
    elif a.car_no:
        dump(detail(a.car_no))
    elif f:
        dump(search("", top_k=a.top_k, **f))
    else:
        ap.error("要給 CAR 編號，或用 --part / --search / --stats / --selftest")


if __name__ == "__main__":
    main()
