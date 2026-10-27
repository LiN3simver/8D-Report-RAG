"""8D 客訴單 Agent API :8700。

    .venv/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8700

把 PLM 的客訴抱怨單(8D)（表單類型 `complaint`）變成可檢索、可被上游 agent 呼叫的一層。
不落地：每次請求直接查來源庫，資料永遠是現況。

端點
----
    GET  /api/complaint/{car_no}      一張單的全部欄位 + 料號 + 通知人員
    GET  /api/part/{part_number}      料號反查客訴史
    GET  /api/search                  自由文字 + 過濾（?q= &customer= &factory= …）
    GET  /api/stats                   張數 / 成立率 / 各種分佈
    GET  /api/health                  來源庫 / LLM / 語料現況

    GET  /a2a/card | tools | health | peers
    POST /a2a/tool/{name}             get_part_complaints / get_complaint /
                                      search_complaints / complaint_stats
    POST /a2a/search                  統一檢索（回 {hits, raw}）
    POST /a2a/message                 統一對話（SSE）
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query

from src import car, db, fields, guard

app = FastAPI(title="8D 客訴單 Agent", version="0.1")


def _ok(result: dict[str, Any]) -> dict[str, Any]:
    """查不到就 404，並帶出 `tried`，讓呼叫端分得出是輸入錯還是服務壞。"""
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail={
            "error": result.get("error"),
            "input": result.get("input"),
            "tried": result.get("tried"),
            "note": result.get("note"),
        })
    return result


_FILTERS = ("customer", "factory", "result", "work_type", "state", "since", "until")


@app.get("/api/complaint/{car_no}")
def get_complaint(car_no: str) -> dict:
    """一張客訴單的全部欄位（含 D3~D8、是否成立、負責品保、關聯料號、通知人員）。"""
    return _ok(car.detail(car_no))


@app.get("/api/part/{part_number:path}")
def get_part_complaints(part_number: str) -> dict:
    """料號反查客訴史。兩條路 UNION，每一筆帶 matched_by 說明命中方式。

    查無紀錄時回 200 + total=0 而不是 404：「語料裡沒有客訴」是有效答案，
    回 404 會讓上游的委派迴圈把它當成失敗。
    """
    return _ok(car.by_part(part_number))


@app.get("/api/search")
def search(
    q: str = Query("", description="自由文字，掃 16 個欄位（含 D3~D8）"),
    customer: str | None = Query(None, description="客戶名稱片段，例 範例電腦"),
    factory: str | None = Query(None, description="生產廠區，例 西區 / W / F04_西區廠"),
    result: str | None = Query(None, description="客訴成立 / 客訴不成立 / 未判定"),
    work_type: str | None = Query(None, description="申請種類"),
    state: str | None = Query(None, description="狀態，例 已結案"),
    since: str | None = Query(None, description="建立日期起 YYYY-MM-DD"),
    until: str | None = Query(None, description="建立日期迄 YYYY-MM-DD"),
    top_k: int = Query(guard.MAX_HITS, ge=1, le=guard.MAX_ROWS),
) -> dict:
    """自由文字 + 結構化過濾。認不出來的過濾值會列在 `ignored` 並在 `notes` 警告。"""
    f = {k: v for k, v in locals().items() if k in _FILTERS and v}
    return _ok(car.search(q, top_k=top_k, **f))


@app.get("/api/stats")
def stats(
    customer: str | None = None,
    factory: str | None = None,
    result: str | None = None,
    work_type: str | None = None,
    state: str | None = None,
    since: str | None = None,
    until: str | None = None,
    part_number: str | None = Query(None, description="只算這顆料號"),
    top_customers: int = Query(10, ge=1, le=100),
) -> dict:
    """張數 / 成立率 / 狀態 / 廠區 / 申請種類 / 客戶 / 異常歸類 分佈。

    成立率的分母是「已判定的張數」，不含未判定。
    """
    loc = locals()
    f = {k: v for k, v in loc.items() if k in (*_FILTERS, "part_number") and v}
    return _ok(car.stats(top_customers=top_customers, **f))


@app.get("/api/health")
def health() -> dict:
    """來源庫 + LLM + 語料現況。來源庫連不上 = 這個 agent 不能用。"""
    src_ok, src_why = db.available()
    c = fields.CORPUS
    out: dict[str, Any] = {
        "plm": {
            "ok": src_ok,
            "detail": src_why,
            "source": db.source(),
            "note": "本 agent 的全部能力來源。掛掉 = 不能用。",
            "is_test_db": db.is_test_db(),
        },
        "corpus": {
            "record_type": c["record_type"],
            "table": c["table"],
            "rows": c["rows"],
            "range": f"{c['date_from']} ~ {c['date_to']}",
            "text_chars": c["text_chars"],
            "landed": False,
            "attachments": "不提供（PLM 上有 8D 報告附件，本 agent 不下載也不解析）",
        },
        "limits": {
            "max_rows": guard.MAX_ROWS,
            "max_hits": guard.MAX_HITS,
            "statement_timeout_s": guard.STATEMENT_TIMEOUT,
            "login_timeout_s": guard.LOGIN_TIMEOUT,
        },
    }
    try:
        from agentkit import gateway

        from src import a2a
    except ImportError as e:
        out["a2a"] = {"ok": False, "detail": f"agentkit 未安裝（{e}）"}
        return out

    status, detail = a2a.SPEC.status()
    llm_ok, llm_why = gateway.available()
    return {
        "agent_id": a2a.AGENT_ID,
        "status": status,
        "detail": detail,
        **out,
        "llm": {"ok": llm_ok, "detail": llm_why,
                "note": "只影響文字摘要。掛掉時檢索與工具照樣可用。"},
        "blocked_by": a2a.BLOCKED_BY if not src_ok else [],
    }


# --------------------------------------------------------------------------
# A2A（需要 agentkit；未安裝時 REST API 仍可獨立運作）
# --------------------------------------------------------------------------
try:
    from src import a2a as _a2a

    _a2a.mount(app)
except Exception as _e:  # pragma: no cover
    print(f"[a2a] 未掛載（{type(_e).__name__}: {_e}）")
