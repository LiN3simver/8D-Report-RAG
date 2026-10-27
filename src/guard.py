"""逾時、列數上限與失敗預算的單一設定來源。

客訴主表是一張平表、本 agent 也沒有下游，所以沒有遞迴上限與斷路器 ——
留著沒作用的上限值，只會讓人以為它有在作用。

失敗採「N 次就停」而不是退避重試：這裡的失敗（連不上、編號不存在）
都不是再試一次就會好的那種。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

# 登入逾時：伺服器關機時，決定等幾秒才知道連不上。
LOGIN_TIMEOUT = 10

# 查詢逾時。正常查詢在 50ms 內，15 秒是「用不到，但不會有查詢永遠掛著」。
STATEMENT_TIMEOUT = 15

# 列數硬上限：避免一句 LIKE N'%%' 把整庫倒進 LLM context。
MAX_ROWS = 1_000

# 一次 /a2a/search 最多回幾張卡（每張含 D3~D8 六段長文字）。
MAX_HITS = 8

MAX_FAILS = 3


class BudgetExhausted(RuntimeError):
    """失敗次數用完。帶著 `tried`，讓錯誤訊息說得出試過哪些步驟、各自怎麼失敗。"""

    def __init__(self, tried: list[dict[str, Any]]) -> None:
        self.tried = tried
        steps = "、".join(t["step"] for t in tried if not t.get("ok"))
        super().__init__(
            f"連續失敗 {sum(1 for t in tried if not t.get('ok'))} 次後停止（{steps}）"
        )


class Budget:
    """一次請求的失敗預算，第 MAX_FAILS 次失敗就終止。

        b = Budget()
        row = b.attempt("編號精確", lambda: _one(SQL_DETAIL, [no]))
        row = row or b.attempt("編號正規化", lambda: _one(SQL_DETAIL, [_norm(no)]))

    「查無結果」（fn 回 None）也算一次失敗：這條路走不通，換下一條。
    """

    def __init__(self, max_fails: int = MAX_FAILS) -> None:
        self.max_fails = max_fails
        self.fails = 0
        self.tried: list[dict[str, Any]] = []

    @property
    def exhausted(self) -> bool:
        return self.fails >= self.max_fails

    def attempt(self, step: str, fn: Callable[[], T | None]) -> T | None:
        """跑一次 fn。成功回結果，失敗記一筆並回 None。

        用完預算時丟例外而不是回傳值，讓呼叫端不可能忘記檢查而繼續往下打。
        """
        if self.exhausted:
            raise BudgetExhausted(self.tried)
        try:
            out = fn()
        except Exception as e:  # noqa: BLE001
            self._fail(step, f"{type(e).__name__}: {e}")
            return None
        if out is None:
            self._fail(step, "查無結果")
            return None
        self.tried.append({"step": step, "ok": True})
        return out

    def _fail(self, step: str, why: str) -> None:
        self.fails += 1
        self.tried.append({"step": step, "ok": False, "error": why})
        if self.exhausted:
            raise BudgetExhausted(self.tried)
