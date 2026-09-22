"""统一外部调用入口(技术方案 §13.2):预算预留 → 标记发送 → 结算。

所有真实外部 HTTP(生成、翻译、轮询、下载、裁判)都经由本 transport 进入,
先持久化预留、标记 sent 再发出请求;请求发出后任何异常都视为结果未知,
预留保留,绝不盲目重试(§6.3/BUDGET-02)。结算口径为价格快照估算
(basis=estimated),服务商账单导入对账后升级为 billed,不互相冒充。
"""

from __future__ import annotations

import httpx

from .budgets import BudgetLedger, PriceUnknown

PRICEABLE_MARKERS = (
    ("/images/generations", "generation"),
    ("multimodal-generation", "generation"),
    ("video-synthesis", "generation"),
    ("/chat/completions", "translate"),
    ("/tasks", "poll"),
)

# 结果下载:已付费产物的取回(GET),不产生新增计费;仍计入调用次数与预留审计(0 费用预留)。
DOWNLOAD_CALL_SITE = "download"


def classify_call_site(request: httpx.Request) -> str:
    """预算调用点分类:按上游路径标记;裁判等共用路径形状的调用由调用方显式声明。

    未命中标记的 GET(结果媒体/产物下载)归为 download,按零费用预留,
    避免硬预算模式把"取回已付费产物"误拒为 PRICE_UNKNOWN。
    """
    path = request.url.path
    for marker, site in PRICEABLE_MARKERS:
        if marker in path:
            return site
    if request.method == "GET":
        return DOWNLOAD_CALL_SITE
    return "unknown"


class CallContext:
    """runner 在 trial 间更新当前 trialId,供账本与录制关联(单写者、顺序执行)。

    match_trial_id 供回放匹配使用:回放 run 的 trialId 与源录制不同,
    匹配时以源 run 的 trialId 为键(由 runner 按 (caseId, repetitionIndex) 映射)。
    """

    def __init__(self):
        self.run_id: str = ""
        self.trial_id: str | None = None
        self.match_trial_id: str | None = None
        self.attempt_index = 0
        self.current_call_site: str | None = None  # 当前调用点;经 context 传递,不写请求头
        self._judge_calls = 0
        self.charges: list[dict] = []  # 每次结算的 (callSite, reservationId, amount, basis, trialId)

    def next_judge_attempt(self) -> int:
        """裁判每次真实调用(含修复重试)都是独立 attempt、独立预留。"""
        self._judge_calls += 1
        return self._judge_calls - 1


class _BudgetedCore:
    """预留 → 标记发送 → 结算的公共实现;异步/同步 transport 各自适配 httpx 接口。"""

    def __init__(self, ledger: BudgetLedger, context: CallContext):
        self.ledger = ledger
        self.context = context

    def _worst_cost(self, call_site: str) -> float | None:

        if call_site == DOWNLOAD_CALL_SITE:
            # 下载已付费产物不产生新增费用;0 费用预留保证审计完整且不触发 PRICE_UNKNOWN。
            return 0.0
        prices = self.ledger.policy.prices
        worst = prices.get(call_site)
        if worst is None and not self.ledger.policy.estimated:
            raise PriceUnknown(
                f"调用 {call_site} 未登记价格且无法确定上界,硬预算模式拒绝发送(BUDGET-03)"
            )
        return worst

    def _before_send(self, request, call_site: str):
        # 调用点经 CallContext 传递给录制层;绝不写入请求头,内部标记不得发往真实上游(§16)。
        self.context.current_call_site = call_site
        reservation = self.ledger.reserve(
            run_id=self.context.run_id,
            call_site=call_site,
            worst_cost=self._worst_cost(call_site),
            trial_id=self.context.trial_id,
            attempt_index=self.context.attempt_index,
        )
        self.ledger.mark_sent(reservation.reservationId)
        return reservation

    def _settle_ok(self, reservation) -> None:
        self.ledger.settle(
            reservation.reservationId,
            reservation.reservedAmount,
            basis="estimated",
            note="价格快照估算;等待账单对账",
        )
        self.context.charges.append(
            {
                "callSite": reservation.callSite,
                "reservationId": reservation.reservationId,
                "amount": reservation.reservedAmount,
                "currency": reservation.currency,
                "basis": "estimated",
                "trialId": self.context.trial_id,
            }
        )


class BudgetedTransport(httpx.AsyncBaseTransport):
    """预算网关(异步):reserve(先落盘) → mark_sent → inner.send → settle / settle_unknown。

    reserve/mark_sent 抛出的 PriceUnknown/BudgetExhausted 原样上抛:
    硬预算模式下调用在发出前被拒绝,由调用方记为结构化错误,不转付费。
    """

    def __init__(self, inner, ledger: BudgetLedger, context: CallContext):
        self.inner = inner
        self.ledger = ledger
        self.context = context
        self._core = _BudgetedCore(ledger, context)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        call_site = classify_call_site(request)
        reservation = self._core._before_send(request, call_site)
        try:
            response = await self.inner.handle_async_request(request)
            await response.aread()
        except Exception as exc:
            self.ledger.settle(
                reservation.reservationId, None, note=f"发送后异常:{type(exc).__name__}"
            )
            raise
        self._core._settle_ok(reservation)
        return response


class BudgetedSyncTransport(httpx.BaseTransport):
    """预算网关(同步):供 vlm 裁判的 httpx.Client 使用,语义与异步版一致。

    每次真实请求(含修复重试)独立预留;attempt_index 来自 CallContext 的裁判计数。
    call_site 显式指定:裁判端点(/chat/completions)与翻译共用路径形状,
    路径标记无法区分,必须由调用方声明,否则会错记为 translate 的价格。
    """

    def __init__(self, inner, ledger: BudgetLedger, context: CallContext, call_site: str | None = None):
        self.inner = inner
        self.ledger = ledger
        self.context = context
        self.call_site = call_site  # None:按路径/方法分类;显式传入供裁判等共用路径形状的调用
        self._core = _BudgetedCore(ledger, context)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        call_site = self.call_site or classify_call_site(request)
        reservation = self._core._before_send(request, call_site)
        try:
            response = self.inner.handle_request(request)
        except Exception as exc:
            self.ledger.settle(
                reservation.reservationId, None, note=f"发送后异常:{type(exc).__name__}"
            )
            raise
        self._core._settle_ok(reservation)
        return response
