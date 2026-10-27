"""欄位 ↔ 中文標籤的單一事實來源。

8D 表單的 Discipline 文字欄，**欄位名稱與實際內容錯位一格**：

    欄位名                  表單 label              實際內容
    containment_action      3.Root cause            D3 根本原因
    root_cause              4.Containment plan      D4 臨時對策

label 是對的，欄位名是錯的。照欄位名「修正」會讓 D3 與 D4 對調，而且不會報錯
（兩段都是通順的中文對策）。所以 SQL 別名、卡片區塊標題、system 提示全部從
這裡生成，`python -m src.car --selftest` 第 [3] 段以內容斷言守住這個對照。
"""
from __future__ import annotations

from typing import Any

# Discipline 3~8：(中文標籤, 欄位名, 填答率%)。順序即表單順序，第二欄不可照名字修正。
D_FIELDS: tuple[tuple[str, str, float], ...] = (
    ("D3 根本原因",     "containment_action", 68.6),   # 名字寫 containment
    ("D4 臨時對策",     "root_cause",         66.3),   # 名字寫 root_cause
    ("D5 永久改善對策", "verification",       66.3),
    ("D6 效果確認",     "corrective_actions", 63.4),
    ("D7 再發防止",     "prevent_recurrence", 60.5),
    ("D8 結果",         "closure_result",     32.4),
)

# SQL 別名用（去掉空格）
D_ALIAS: tuple[tuple[str, str], ...] = tuple(
    (label.replace(" ", ""), col) for label, col, _ in D_FIELDS
)

# ---------------------------------------------------------------------------
# 清單（plm.list -> plm.list_value）
#
# 繁中 label 就在 list_value 自己的 label_zh 欄，一律 COALESCE(label_zh, label_en, value)，
# 順序不可顛倒：部分值的英文 label_en 是 NULL。
#
# 兩個廠區清單不同名、不同碼，不可互換（用錯清單 JOIN 不會報錯，只會變空白）：
#     生產廠區      working_factory                 -> prod_factory   南區 = 'S'
#     填單/責任廠區 creator_factory / error_factory -> site_factory   南區 = 'X'
# ---------------------------------------------------------------------------
LIST_OF: dict[str, str] = {
    "work_type":       "complaint_type",
    "result":          "complaint_result",
    "working_factory": "prod_factory",
    "creator_factory": "site_factory",
    "error_factory":   "site_factory",
    "error_code1":     "error_code_l1",
    "error_code2":     "error_code_l2",
    "error_code3":     "error_code_l3",
}

CAR_RESULT: dict[str, str] = {"Yes": "客訴成立", "No": "客訴不成立"}

WORK_TYPE: dict[str, str] = {
    "NPI": "NPI(開發階段)抱怨",
    "CAR": "MP(量產階段)抱怨",
    "CAF": "RMA申請.覆判單",
}

WORKING_FACTORY: dict[str, str] = {
    "N": "F01_北區廠", "E": "F02_東區廠", "S": "F03_南區廠", "W": "F04_西區廠",
    "O1": "F05_海外一廠", "P": "OUT_(外購件)", "O2": "F06_海外二廠",
}

# 退役代碼：清單裡已經沒有、但舊單仍在用的值（海外廠拆成 O1/O2 之前的 'O'）。
# 因此生產廠區的解析是三層：清單 label -> working_factory_label（快取）-> 裸代碼。
# 只用清單會漏掉這批舊單；只用快取會漏掉快取沒填的單（快取欄位約 84% 有值）。
RETIRED_FACTORY: dict[str, str] = {
    "O": "F05_海外廠（舊代碼，拆成 O1/O2 之前）",
}

# 顯示與過濾用（現行 + 退役）。WORKING_FACTORY 本身只鏡像 DB 的現行清單。
WORKING_FACTORY_ALL: dict[str, str] = {**WORKING_FACTORY, **RETIRED_FACTORY}

SEQ_FACTORY: dict[str, str] = {
    "N": "N:北區", "E": "E:東區", "X": "X:南區", "W": "W:西區",
    "O": "O:海外", "P": "P:外購",
}

# 'Released' 在這張表是「已結案」不是「已發行」（約八成的單是這個狀態）。
STATE: dict[str, str] = {
    "Released":    "已結案",
    "QA Tracking": "品保追蹤中",
    "QA Check":    "品保確認中",
    "In Review":   "審核中",
    "New":         "新建立",
    "Cancelled":   "已取消",
}

# ---------------------------------------------------------------------------
# 語料基準值（展示用的虛構數字）。被 system 提示、工具 description、
# /api/health 與 selftest 引用，集中一份。
# ---------------------------------------------------------------------------
CORPUS: dict[str, Any] = {
    "record_type": "complaint",
    "record_type_label": "Corrective Action Request (8D)",
    "table": "plm.complaint",
    "rows": 320,
    "date_from": "2022-06-01",
    "date_to": "2026-06-30",
    "text_chars": 201_350,
    "undecided": 32,                 # result 為 NULL = 尚未判定
    "fill": {
        "result": 90.0,
        "qa_owner": 90.0,
        "error_factory": 3.1,        # 責任廠區 —— 幾乎全空
        "error_vendor": 1.3,         # 責任單位(供應商) —— 幾乎全空
        "part_list_text": 98.1,
    },
    "work_type_prefix_mismatch": 3,  # work_type 與編號前綴不一致的張數
    "retired_factory": {"rows": 11, "cache_rescued": 9},
}

# mv_list（error_code1/2/3）以逗號分隔；清單值本身不含逗號，split(',') 是安全的。
MV_SEP = ","


def split_mv(v: str | None) -> list[str]:
    """mv_list 欄位 -> 值清單。空的回空 list（不是 [""]）。"""
    if not v:
        return []
    return [t.strip() for t in v.split(MV_SEP) if t.strip()]


def split_part_list(v: str | None) -> list[str]:
    """`part_list` 是多行文字，一行一個料號。"""
    if not v:
        return []
    out: list[str] = []
    for line in v.replace("\r", "\n").split("\n"):
        t = line.strip()
        if t and t not in out:
            out.append(t)
    return out


def as_bool(v: Any) -> bool:
    """PLM 旗標轉 bool。

    PLM 的旗標是字串 '0'/'1'，而 `bool('0')` 是 True；SQL 算出來的則是 int。
    兩種型別都會出現，這支兩種都吃。
    """
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() not in ("", "0")
    return bool(v)
