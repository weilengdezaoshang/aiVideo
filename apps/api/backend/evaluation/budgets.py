"""共享单写者预算账本(技术方案 §13.2/§6.3,验收 BUDGET-01..04)。

纪律:
- 原子检查:已结算 + 未结预留 + 本次最坏预留 ≤ 上限;检查与持久化预留必须在
  同一把锁内完成,发出任何真实请求之前先落盘(§6.2)。
- 锁按账本文件路径注册为进程级共享:同一 scope 的多个 BudgetLedger 实例
  (例如 API 进程内重复构造)共享同一把锁,实例级 threading.Lock 不再是边界。
- run 账本可挂载项目总账本:检查与预留按"文件路径排序"一次性取得两把锁,
  两个日志都落盘成功才算预留成功;不出现"run 预留成功、项目预算没扣"的部分状态。
- 金额一律以 Decimal(str(x)) 参与比较与累加,JSON 层保留 float 兼容旧数据,
  避免 0.1+0.2 类浮点误差突破上限。
- 请求已发出(sent)但结果未知:预留保持 reserved/unknown,绝不自动释放或盲目重发。
- 重试是新的预留(BUDGET-04);并发争用下两个调用不能花同一份余额(BUDGET-01)。
- usage/费用未知保持 null,不冒充 0(§13.1);预留(估算口径)与实际结算分字段记录。
"""

from __future__ import annotations

import json
import math
import threading
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from ..common import now

# 预留状态机:reserved --send--> sent --结果--> settled | unknown;reserved --取消--> released。
# 进程重启后 reserved/sent 都按 unknown 处理(结果不可知,占用预算等待显式对账)。
RESERVATION_STATUSES = ("reserved", "sent", "settled", "unknown", "released")

# 结算口径:billed=服务商账单,provider_reported=响应 usage 折算,estimated=价格快照估算。
SETTLE_BASES = ("billed", "provider_reported", "estimated")

_LEDGER_LOCKS: dict[str, threading.RLock] = {}
# 可重入:open() 持有注册表锁构造实例时,__init__ 会再次登记同一把账本锁。
_LEDGER_LOCKS_GUARD = threading.RLock()
# 同进程内每个账本文件的唯一实例:执行中的预留/结算内存态不被重复构造的实例分叉。
_LEDGER_INSTANCES: dict[str, "BudgetLedger"] = {}


def ledger_lock(path: Path) -> threading.RLock:
    """按账本文件路径共享的进程级锁;同一 scope 的所有实例互斥。"""
    key = str(Path(path).resolve())
    with _LEDGER_LOCKS_GUARD:
        if key not in _LEDGER_LOCKS:
            _LEDGER_LOCKS[key] = threading.RLock()
        return _LEDGER_LOCKS[key]


def _acquire_for(*paths: Path) -> list[threading.RLock]:
    """按路径排序获取多把账本锁,避免不同 (run, project) 组合死锁。"""
    locks = sorted({ledger_lock(path) for path in paths}, key=lambda lock: id(lock))
    for lock in locks:
        lock.acquire()
    return locks


class BudgetExhausted(ValueError):
    """结构化业务错误 code=BUDGET_EXHAUSTED(§14.4):不滥用 500,不自动转付费。"""

    code = "BUDGET_EXHAUSTED"


class PriceUnknown(ValueError):
    """code=PRICE_UNKNOWN:硬预算模式下拒绝无法限定上界的调用。"""

    code = "PRICE_UNKNOWN"


def check_amount(value, field: str, allow_none: bool = False):
    """金额/次数校验:非负、有限、可比较;非法输入直接拒绝,不静默夹紧。"""
    if value is None:
        if allow_none:
            return None
        raise PriceUnknown(f"{field} 不能为空")
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise PriceUnknown(f"{field} 必须是数值,收到 {type(value).__name__}")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PriceUnknown(f"{field} 不是合法数值:{value!r}") from exc
    if not amount.is_finite():
        raise PriceUnknown(f"{field} 必须是有限数值,收到 {value!r}")
    if amount < 0:
        raise PriceUnknown(f"{field} 不能为负数,收到 {value!r}")
    return amount


class BudgetPolicy(BaseModel):
    currency: str = "CNY"
    maxCost: float | None = None  # None 表示未限定(仅允许显式声明估算预算时)
    maxExternalCalls: int | None = None
    estimated: bool = False  # True 表示明确标记的估算预算(无硬保证,§13.2)
    prices: dict[str, float] = Field(default_factory=dict)  # 价格快照:callSite → 最坏单次成本
    priceVersion: str = "1"  # 计价版本:变更单价必须更换版本,账单可追溯对账

    @field_validator("maxCost")
    @classmethod
    def _max_cost(cls, value: float | None) -> float | None:
        if value is not None:
            check_amount(value, "maxCost")
        return value

    @field_validator("maxExternalCalls")
    @classmethod
    def _max_calls(cls, value: int | None) -> int | None:
        if value is not None and (value < 0):
            raise PriceUnknown("maxExternalCalls 不能为负数")
        return value

    @field_validator("prices")
    @classmethod
    def _prices(cls, value: dict[str, float]) -> dict[str, float]:
        for site, price in value.items():
            check_amount(price, f"价格 {site}")
        return value


class Reservation(BaseModel):
    reservationId: str
    scopeId: str
    runId: str
    trialId: str | None = None
    callSite: str
    attemptIndex: int = 0
    status: str = "reserved"  # reserved -> sent -> settled | unknown;reserved -> released
    reservedAmount: float | None
    settledAmount: float | None = None
    settledBasis: str | None = None  # billed | provider_reported | estimated
    usage: dict | None = None  # 服务商报告的原始 usage(可追溯对账)
    currency: str = "CNY"
    createdAt: str = Field(default_factory=now)
    settledAt: str | None = None
    note: str | None = None


class LedgerSummary(BaseModel):
    scopeId: str
    settled: float = 0.0
    settledBilled: float = 0.0  # 实际账单口径
    settledEstimated: float = 0.0  # 估算/服务商报告口径(不冒充账单)
    outstandingReserved: float = 0.0
    unknownCount: int = 0
    totalCalls: int = 0
    currency: str = "CNY"
    maxCost: float | None = None
    maxExternalCalls: int | None = None
    priceVersion: str = "1"


class BudgetLedger:
    """run/项目级账本;同一 scope 全部实例共享一把进程锁,JSONL 追加落盘(单写者)。

    进程模型:本仓库为单进程部署(AGENTS.md),跨实例互斥由共享文件锁保证;
    跨进程写入不属于支持模型,多进程部署必须改为外部锁或数据库(§11.2)。
    生产读写统一走 open():同进程内同一账本只保留一个实例,执行中的
    reserved/sent 状态不会被并发构造的只读实例误判为"进程中断"。
    """

    def __init__(self, store: Path, scope_id: str, policy: BudgetPolicy | None = None):
        self.scope_id = scope_id
        self.policy = policy or BudgetPolicy(maxCost=0.0)
        self.file = store / "budgets" / f"{scope_id}.jsonl"
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = ledger_lock(self.file)
        self._reservations: dict[str, Reservation] = {}
        self._call_count = 0
        self._consumed_lines = 0
        self._load()

    @classmethod
    def open(cls, store: Path, scope_id: str, policy: BudgetPolicy | None = None) -> "BudgetLedger":
        """返回该账本文件在进程内的唯一实例;已存在时沿用其策略与内存态。

        open() 是生产读写的推荐入口:执行中的 reserved/sent 状态不会因只读端点
        重复构造实例而被误判为"进程中断"。即便绕开 open() 直接构造,实例间也
        通过 _sync() 增量读取共享日志保持一致。
        """
        key = str((store / "budgets" / f"{scope_id}.jsonl").resolve())
        with _LEDGER_LOCKS_GUARD:
            existing = _LEDGER_INSTANCES.get(key)
            if existing is not None:
                return existing
            instance = cls(store, scope_id, policy)
            _LEDGER_INSTANCES[key] = instance
            return instance

    # ---------- 持久化 ----------

    def _append(self, entry: dict) -> None:
        import os

        with self.file.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._consumed_lines += 1  # 自身追加的行不再被 _sync 重复消费

    def _apply_entry(self, entry: dict) -> None:
        kind = entry.get("kind")
        if kind == "reserve":
            self._call_count += 1
            self._reservations[entry["reservationId"]] = Reservation.model_validate(
                {k: v for k, v in entry.items() if k != "kind"}
            )
        elif kind == "settle":
            reservation = self._reservations[entry["reservationId"]]
            reservation.status = "settled"
            reservation.settledAmount = entry.get("settledAmount")
            reservation.settledBasis = entry.get("settledBasis", "estimated")
            reservation.usage = entry.get("usage")
            reservation.settledAt = entry.get("settledAt")
            reservation.note = entry.get("note")
        elif kind == "mark_sent":
            reservation = self._reservations[entry["reservationId"]]
            reservation.status = "sent"
        elif kind == "settle_unknown":
            reservation = self._reservations[entry["reservationId"]]
            reservation.status = "unknown"
            reservation.settledAt = entry.get("settledAt")
            reservation.note = entry.get("note")
        elif kind == "release":
            reservation = self._reservations[entry["reservationId"]]
            reservation.status = "released"
            reservation.settledAt = entry.get("settledAt")
            reservation.note = entry.get("note")

    def _consume_lines(self, lines: list[str]) -> None:
        for line in lines:
            if not line.strip():
                continue
            self._consumed_lines += 1
            self._apply_entry(json.loads(line))

    def _sync(self) -> None:
        """增量读取日志新增行(调用方需已持有账本锁):同进程多实例保持一致。"""
        if not self.file.is_file():
            return
        lines = self.file.read_text(encoding="utf-8").splitlines()
        pending = lines[self._consumed_lines:]
        if pending:
            self._consume_lines(pending)

    def _load(self) -> None:
        if not self.file.is_file():
            return
        self._consume_lines(self.file.read_text(encoding="utf-8").splitlines())
        # 崩溃恢复(BUDGET-02):仍为 reserved/sent 的条目说明进程曾消失。
        # reserved:预留已落盘但发送标记不明,一律按结果未知处理,等待显式对账;
        # sent:请求已发出,结果必然未知。绝不删除、不释放、不自动结算。
        for reservation in self._reservations.values():
            if reservation.status in {"reserved", "sent"}:
                reservation.status = "unknown"
                reservation.note = "进程中断,上游结果未知;保留预留等待对账"

    def _totals(self) -> tuple[Decimal, Decimal, int, Decimal, Decimal]:
        settled = Decimal("0")
        outstanding = Decimal("0")
        unknown = 0
        billed = Decimal("0")
        estimated = Decimal("0")
        for reservation in self._reservations.values():
            if reservation.status == "settled":
                amount = check_amount(reservation.settledAmount or 0, "settledAmount")
                settled += amount
                if reservation.settledBasis == "billed":
                    billed += amount
                else:
                    estimated += amount
            elif reservation.status == "reserved":
                outstanding += check_amount(reservation.reservedAmount or 0, "reservedAmount")
            elif reservation.status == "unknown":
                unknown += 1
                outstanding += check_amount(reservation.reservedAmount or 0, "reservedAmount")
        return settled, outstanding, unknown, billed, estimated

    # ---------- 核心操作 ----------

    def _price_for(self, call_site: str):
        price = self.policy.prices.get(call_site)
        return None if price is None else check_amount(price, f"价格 {call_site}")

    def reserve(
        self, run_id: str, call_site: str, worst_cost, trial_id=None, attempt_index=0
    ) -> Reservation:
        """原子检查并预留;必须发生在真实请求发出之前。worst_cost=None 且硬预算 → 拒绝。"""
        with self._lock:
            self._sync()
            return self._reserve_locked(
                run_id, call_site, worst_cost, trial_id=trial_id, attempt_index=attempt_index
            )

    def _reserve_locked(self, run_id, call_site, worst_cost, trial_id=None, attempt_index=0):
        if self.policy.maxCost is None and not self.policy.estimated:
            raise PriceUnknown("预算未限定上限且未声明估算模式,拒绝发起外部调用")
        worst = check_amount(worst_cost, f"调用 {call_site} 的最坏成本", allow_none=True)
        if worst is None:
            raise PriceUnknown(f"调用 {call_site} 无可信计费上界,硬预算模式拒绝(BUDGET-03)")
        settled, outstanding, unknown, _, _ = self._totals()
        projected = settled + outstanding + worst
        if self.policy.maxCost is not None:
            cap = check_amount(self.policy.maxCost, "maxCost")
            if projected > cap:
                raise BudgetExhausted(
                    f"预算不足:已结算 {settled} + 未结预留 {outstanding} + 本次最坏 "
                    f"{worst} = {projected} 超过上限 {cap}"
                    + (f";另有 {unknown} 笔未知调用待对账" if unknown else "")
                )
        if (
            self.policy.maxExternalCalls is not None
            and self._call_count + 1 > self.policy.maxExternalCalls
        ):
            raise BudgetExhausted(
                f"外部调用次数达到上限 {self.policy.maxExternalCalls}(BUDGET-04)"
            )
        reservation = Reservation(
            reservationId=f"res-{uuid.uuid4().hex[:16]}",
            scopeId=self.scope_id,
            runId=run_id,
            trialId=trial_id,
            callSite=call_site,
            attemptIndex=attempt_index,
            reservedAmount=float(worst),
            currency=self.policy.currency,
        )
        self._call_count += 1
        self._reservations[reservation.reservationId] = reservation
        self._append({"kind": "reserve", **reservation.model_dump()})
        return reservation

    def reserve_chained(
        self,
        run_id: str,
        call_site: str,
        worst_cost,
        parent: "BudgetLedger",
        trial_id=None,
        attempt_index=0,
    ) -> Reservation:
        """run + 项目两级预算的原子预留:任一检查失败则双双拒绝,不产生部分预留。

        两本账的锁按固定顺序获取;日志落盘在同一临界区内先后完成。
        """
        if parent is self:
            raise ValueError("项目账本不能与 run 账本相同")
        locks = _acquire_for(self.file, parent.file)
        try:
            self._sync()
            parent._sync()
            # 先在两本账上各自预检,再统一落盘;预检全部通过才写第一条日志。
            run_reservation = self._reserve_locked(
                run_id, call_site, worst_cost, trial_id=trial_id, attempt_index=attempt_index
            )
            try:
                parent._reserve_locked(
                    run_id, call_site, worst_cost, trial_id=trial_id, attempt_index=attempt_index
                )
            except Exception:
                # 回滚 run 账本刚写入的预留:补一条 release,保持两本账一致。
                self._release_locked(run_reservation.reservationId, note="项目预算拒绝,回滚预留")
                raise
            return run_reservation
        finally:
            for lock in locks:
                lock.release()

    def mark_sent(self, reservation_id: str) -> Reservation:
        """请求即将发出:置 sent 并落盘;崩溃后 sent 按 unknown 对账。"""
        with self._lock:
            reservation = self._reservations[reservation_id]
            self._sync()
            if reservation.status != "reserved":
                raise ValueError(f"预留状态为 {reservation.status},不能标记发送")
            reservation.status = "sent"
            self._append({"kind": "mark_sent", "reservationId": reservation_id, "at": now()})
            return reservation

    def settle(
        self,
        reservation_id: str,
        actual_cost=None,
        note=None,
        basis: str = "estimated",
        usage: dict | None = None,
    ) -> Reservation:
        """请求返回后结算;actual_cost=None 表示无法确定,转入 unknown 并保留预留。

        basis 区分口径:billed(账单)/provider_reported(usage 折算)/estimated(价格快照)。
        """
        if basis not in SETTLE_BASES:
            raise ValueError(f"未知结算口径:{basis}")
        with self._lock:
            self._sync()
            reservation = self._reservations[reservation_id]
            if reservation.status == "settled":
                return reservation
            if reservation.status == "released":
                raise ValueError("已释放的预留不能结算(请求从未发出)")
            if actual_cost is None:
                reservation.status = "unknown"
                reservation.note = note or "上游结果未知,预留保留(不盲目重试)"
                self._append(
                    {
                        "kind": "settle_unknown",
                        "reservationId": reservation_id,
                        "settledAt": now(),
                        "note": reservation.note,
                    }
                )
            else:
                amount = check_amount(actual_cost, "结算金额")
                reservation.status = "settled"
                reservation.settledAmount = float(amount)
                reservation.settledBasis = basis
                reservation.usage = usage
                reservation.settledAt = now()
                if note:
                    reservation.note = note
                self._append(
                    {
                        "kind": "settle",
                        "reservationId": reservation_id,
                        "settledAmount": float(amount),
                        "settledBasis": basis,
                        "usage": usage,
                        "settledAt": reservation.settledAt,
                        "note": note,
                    }
                )
            return reservation

    def release(self, reservation_id: str, note=None) -> Reservation:
        """仅允许释放确认从未发出的请求(发送前取消);已发出的一律走 settle/unknown。"""
        with self._lock:
            self._sync()
            return self._release_locked(reservation_id, note=note)

    def _release_locked(self, reservation_id: str, note=None) -> Reservation:
        reservation = self._reservations[reservation_id]
        if reservation.status not in {"reserved"}:
            raise ValueError(f"预留状态为 {reservation.status},不能释放(可能已发出请求)")
        reservation.status = "released"
        reservation.note = note
        self._append({"kind": "release", "reservationId": reservation_id, "settledAt": now()})
        return reservation

    def import_billing(self, reservation_id: str, billed_cost, raw: dict | None = None) -> Reservation:
        """服务商账单导入对账:只有 unknown/settled(非 billed)允许补录 billed 口径。"""
        with self._lock:
            self._sync()
            reservation = self._reservations[reservation_id]
            if reservation.status not in {"unknown", "settled"}:
                raise ValueError(f"预留状态为 {reservation.status},不能导入账单")
            amount = check_amount(billed_cost, "账单金额")
            reservation.status = "settled"
            reservation.settledAmount = float(amount)
            reservation.settledBasis = "billed"
            if raw is not None:
                reservation.usage = {"billed": raw}
            reservation.settledAt = now()
            reservation.note = "账单导入对账"
            self._append(
                {
                    "kind": "settle",
                    "reservationId": reservation_id,
                    "settledAmount": float(amount),
                    "settledBasis": "billed",
                    "usage": reservation.usage,
                    "settledAt": reservation.settledAt,
                    "note": "账单导入对账",
                }
            )
            return reservation

    def summary(self) -> LedgerSummary:
        with self._lock:
            self._sync()
            settled, outstanding, unknown, billed, estimated = self._totals()
            return LedgerSummary(
                scopeId=self.scope_id,
                settled=float(settled),
                settledBilled=float(billed),
                settledEstimated=float(estimated),
                outstandingReserved=float(outstanding),
                unknownCount=unknown,
                totalCalls=self._call_count,
                currency=self.policy.currency,
                maxCost=self.policy.maxCost,
                maxExternalCalls=self.policy.maxExternalCalls,
                priceVersion=self.policy.priceVersion,
            )

    def reservations(self) -> list[Reservation]:
        with self._lock:
            self._sync()
            return list(self._reservations.values())


def is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
