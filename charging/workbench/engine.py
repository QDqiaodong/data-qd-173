"""收款核销与充电准入工作台的纯状态机。

设计目标（完整契约见 ``docs/workbench.md``）：

* 工作台与计量结算引擎完全独立：输入的 ``source_state`` 只读，从不写回；
  工作台有自己的可恢复 JSON 状态（:func:`apply_round` 的父/子状态）。
* 一轮输入先做凭据导入与策略登记，然后按次序执行金融操作，再按轮末欠款
  失效尚未启动的许可，最后按次序执行服务操作。整轮任一步失败都抛出
  :class:`WorkbenchError`，父状态与输入快照均不被修改。
* 金额一律为整数 cents；所有分配/撤回量为非负整数，且每次操作的实际变化
  总量必须为正。
* 许可只是站点的服务准入凭据，不预占金钱；时钟只能由每轮
  ``evaluation_at`` 显式推进，不读取机器当前时间。
"""

from copy import deepcopy
from datetime import datetime, timedelta

STATE_VERSION = 1

FINANCE_OPS = ("receive", "allocate", "chargeback")
SERVICE_OPS = ("grant", "begin", "finish")
ALL_OPS = FINANCE_OPS + SERVICE_OPS


class WorkbenchError(ValueError):
    """一轮输入未通过校验或触发业务边界。

    ``code`` 为稳定的机器可读错误码（CLI 退出码 2 时打印），``message``
    为人类可读说明。
    """

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _require(cond, code, message):
    if not cond:
        raise WorkbenchError(code, message)


# ---------------------------------------------------------------------------
# 基础类型校验
# ---------------------------------------------------------------------------

def _dt(value, code="invalid_round", what="timestamp"):
    _require(isinstance(value, str), code, f"{what} 必须是带显式偏移的 ISO 时间字符串")
    try:
        d = datetime.fromisoformat(value)
    except ValueError as exc:
        raise WorkbenchError(code, f"{what} 无法解析: {value!r}") from exc
    _require(d.tzinfo is not None and d.utcoffset() is not None, code,
             f"{what} 必须带显式 UTC 偏移: {value!r}")
    return d


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(value, code, message):
    _require(_is_int(value), code, message)


def _require_nonneg_int(value, code, message):
    _require(_is_int(value) and value >= 0, code, message)


def _require_pos_int(value, code, message):
    _require(_is_int(value) and value > 0, code, message)


def _require_str(value, code, message):
    _require(isinstance(value, str) and value != "", code, message)


def _require_object(value, code, message):
    _require(isinstance(value, dict), code, message)


# ---------------------------------------------------------------------------
# 初始状态
# ---------------------------------------------------------------------------

def initial_state():
    """返回空工作台状态。"""
    return {
        "version": STATE_VERSION,
        "last_evaluation_at": None,
        # 凭据事实（只追加、身份不可变）
        "lines": {},          # line_id -> {line_id, session_id, period_id, charge_cents, account_id, due_at}
        "adjustments": {},    # adjustment_id -> {adjustment_id, line_id, amount_cents}
        "session_accounts": {},   # session_id -> account_id（固定映射）
        "period_due_at": {},      # period_id -> due_at（固定映射）
        # 策略
        "policies": {},       # account_id -> {debt_limit_cents, grace_seconds}
        # 金融
        "receipts": {},       # receipt_id -> 收款对象
        # 许可
        "permits": {},        # permit_id -> 许可对象
        "used_sessions": {},  # session_id -> permit_id
        # 幂等与回执
        "operations": {},     # operation_id -> 操作归档（含 kind/payload/receipt/round_id）
    }


# ---------------------------------------------------------------------------
# 凭据导入
# ---------------------------------------------------------------------------

def _import_credentials(state, snapshot):
    """把一轮凭据快照合并进工作副本 ``state``。

    只读取 ``source_state.closed_lines`` 与 ``source_state.adjustments``
    中题面列明的业务字段；其余字段（含内部 ``reflected_charge_cents``）
    一律不参与应收身份比较。
    """
    _require_object(snapshot, "invalid_import", "import 必须是 JSON 对象")
    source = snapshot.get("source_state")
    _require_object(source, "invalid_import", "import.source_state 必须是 JSON 对象")
    raw_lines = source.get("closed_lines")
    raw_adjustments = source.get("adjustments")
    _require(isinstance(raw_lines, dict), "invalid_import",
             "source_state.closed_lines 必须是以 line_id 为键的对象")
    _require(isinstance(raw_adjustments, list), "invalid_import",
             "source_state.adjustments 必须是数组")

    # 调用方提供的固定归属/到期映射
    session_accounts = snapshot.get("session_accounts", {})
    period_due_at = snapshot.get("period_due_at", {})
    _require(isinstance(session_accounts, dict) and isinstance(period_due_at, dict),
             "invalid_import", "session_accounts / period_due_at 必须是对象")
    for sid, account_id in session_accounts.items():
        _require_str(sid, "invalid_import", "session_accounts 的键必须是非空字符串")
        _require_str(account_id, "invalid_import", f"session {sid!r} 的 account_id 必须是非空字符串")
    for pid, due in period_due_at.items():
        _require_str(pid, "invalid_import", "period_due_at 的键必须是非空字符串")
        _dt(due, "invalid_import", f"period {pid!r} 的 due_at")

    # 固定映射：已接受的身份映射不可改；本轮映射与既有状态冲突即整轮拒绝
    merged_session_accounts = dict(state["session_accounts"])
    for sid, account_id in session_accounts.items():
        if sid in merged_session_accounts:
            _require(merged_session_accounts[sid] == account_id, "credential_conflict",
                     f"session_id {sid!r} 的 account_id 已固定为 {merged_session_accounts[sid]!r}，"
                     f"不能改为 {account_id!r}")
        else:
            merged_session_accounts[sid] = account_id
    merged_period_due = dict(state["period_due_at"])
    for pid, due in period_due_at.items():
        if pid in merged_period_due:
            _require(merged_period_due[pid] == due, "credential_conflict",
                     f"period_id {pid!r} 的 due_at 已固定为 {merged_period_due[pid]!r}，"
                     f"不能改为 {due!r}")
        else:
            merged_period_due[pid] = due

    # 先抽取快照中的 adjustment 身份并做批内/历史身份校验
    snapshot_adj = {}
    for adj in raw_adjustments:
        _require_object(adj, "invalid_import", "adjustments 的每项必须是对象")
        adj_id = adj.get("adjustment_id")
        line_id = adj.get("line_id")
        amount = adj.get("amount_cents")
        _require_str(adj_id, "invalid_import", "adjustment 缺少非空 adjustment_id")
        _require_str(line_id, "invalid_import", f"adjustment {adj_id!r} 缺少非空 line_id")
        _require_int(amount, "invalid_import", f"adjustment {adj_id!r} 的 amount_cents 必须是整数")
        identity = {"adjustment_id": adj_id, "line_id": line_id, "amount_cents": amount}
        _require(adj_id not in snapshot_adj, "invalid_import",
                 f"快照内 adjustment_id {adj_id!r} 重复")
        snapshot_adj[adj_id] = identity
        if adj_id in state["adjustments"]:
            _require(state["adjustments"][adj_id] == identity, "credential_conflict",
                     f"adjustment_id {adj_id!r} 已以不同内容存在，凭据身份不可修改")

    # 调整引用的 line 必须存在于（合并后的）line 全集
    snapshot_line_ids = set(raw_lines.keys())
    for adj_id, identity in snapshot_adj.items():
        _require(identity["line_id"] in snapshot_line_ids or identity["line_id"] in state["lines"],
                 "unknown_line_reference",
                 f"adjustment {adj_id!r} 引用未知 line_id {identity['line_id']!r}")

    # 先前导入的全部 line 凭据必须仍然出现在快照中，且身份金额不可改
    for lid, existing in state["lines"].items():
        _require(lid in raw_lines, "missing_credential",
                 f"凭据快照缺失先前已导入的 line_id {lid!r}，整轮拒绝")
    # 先前导入的全部 adjustment 也必须仍然出现（同一身份）
    for adj_id, existing in state["adjustments"].items():
        _require(adj_id in snapshot_adj, "missing_credential",
                 f"凭据快照缺失先前已导入的 adjustment_id {adj_id!r}，整轮拒绝")
        _require(snapshot_adj[adj_id] == existing, "credential_conflict",
                 f"adjustment_id {adj_id!r} 在快照中内容变化")

    # 逐 line 校验与建单
    new_lines = {}
    for lid, raw in raw_lines.items():
        _require_str(lid, "invalid_import", "closed_lines 的键必须是非空字符串")
        _require_object(raw, "invalid_import", f"closed_lines[{lid!r}] 必须是对象")
        session_id = raw.get("session_id")
        period_id = raw.get("period_id")
        charge = raw.get("charge_cents")
        _require_str(session_id, "invalid_import", f"line {lid!r} 缺少非空 session_id")
        _require_str(period_id, "invalid_import", f"line {lid!r} 缺少非空 period_id")
        _require_int(charge, "invalid_import", f"line {lid!r} 的 charge_cents 必须是整数")
        # 新凭据必须能解析出固定客户与到期日
        account_id = merged_session_accounts.get(session_id)
        due_at = merged_period_due.get(period_id)
        if lid not in state["lines"]:
            _require(account_id is not None, "missing_mapping",
                     f"新凭据 line {lid!r} 的 session_id {session_id!r} 缺少 account_id 映射")
            _require(due_at is not None, "missing_mapping",
                     f"新凭据 line {lid!r} 的 period_id {period_id!r} 缺少 due_at 映射")
        identity = {
            "line_id": lid,
            "session_id": session_id,
            "period_id": period_id,
            "charge_cents": charge,
            "account_id": account_id,
            "due_at": due_at,
        }
        if lid in state["lines"]:
            # 既有身份金额、归属、到期日不可改
            _require(state["lines"][lid] == identity, "credential_conflict",
                     f"line_id {lid!r} 的既有凭据身份/金额/归属/到期日被修改")
        new_lines[lid] = identity

    # 提交：替换映射与 line 全集，合并 adjustments
    state["session_accounts"] = merged_session_accounts
    state["period_due_at"] = merged_period_due
    state["lines"] = new_lines
    state["adjustments"].update(snapshot_adj)


def _register_policies(state, entries):
    """策略登记：首次使用账户前登记；不可变，同内容重复登记可复用。"""
    _require(isinstance(entries, list), "invalid_policy", "register_policies 必须是数组")
    for entry in entries:
        _require_object(entry, "invalid_policy", "策略登记项必须是对象")
        account_id = entry.get("account_id")
        limit = entry.get("debt_limit_cents")
        grace = entry.get("grace_seconds")
        _require_str(account_id, "invalid_policy", "策略登记缺少非空 account_id")
        _require_nonneg_int(limit, "invalid_policy",
                            f"account {account_id!r} 的 debt_limit_cents 必须是非负整数")
        _require_nonneg_int(grace, "invalid_policy",
                            f"account {account_id!r} 的 grace_seconds 必须是非负整数")
        policy = {"debt_limit_cents": limit, "grace_seconds": grace}
        if account_id in state["policies"]:
            _require(state["policies"][account_id] == policy, "policy_conflict",
                     f"account {account_id!r} 的策略已登记且不可修订")
        else:
            state["policies"][account_id] = policy


# ---------------------------------------------------------------------------
# 金融派生
# ---------------------------------------------------------------------------

def _adjustments_by_line(state):
    index = {}
    for adj in state["adjustments"].values():
        index.setdefault(adj["line_id"], []).append(adj)
    return index


def _line_receivable(state, adj_index, line_id):
    """当前应收额 = 原始 charge_cents + 全部关联有符号 adjustment。"""
    line = state["lines"][line_id]
    return line["charge_cents"] + sum(a["amount_cents"] for a in adj_index.get(line_id, ()))


def _line_writeoff(state, line_id):
    """该单当前有效核销（已扣除撤回部分）。"""
    total = 0
    for receipt in state["receipts"].values():
        for item in receipt["allocations"]:
            if item["line_id"] == line_id:
                total += item["amount_cents"] - item["withdrawn_cents"]
    return total


def _receipt_unallocated(receipt):
    """收款当前仍可分配的未分配额。"""
    allocated = sum(a["amount_cents"] for a in receipt["allocations"])
    return receipt["amount_cents"] - allocated - receipt["withdrawn_unallocated_cents"]


def _receipt_effective(receipt):
    """当前仍挂在单据上的有效核销总额。"""
    return sum(a["amount_cents"] - a["withdrawn_cents"] for a in receipt["allocations"])


# ---------------------------------------------------------------------------
# 金融操作
# ---------------------------------------------------------------------------

def _check_op_header(state, op, expected_kinds):
    _require_object(op, "invalid_operation", "操作必须是对象")
    op_id = op.get("operation_id")
    kind = op.get("op")
    _require_str(op_id, "invalid_operation", "操作缺少非空 operation_id")
    _require(kind in expected_kinds, "invalid_operation",
             f"操作 {op_id!r} 的 op 必须是 {expected_kinds} 之一，收到 {kind!r}")
    archived = state["operations"].get(op_id)
    return op_id, kind, archived


def _replay_receipt(state, archived, op_id, kind, payload):
    _require(archived["kind"] == kind, "operation_conflict",
             f"operation_id {op_id!r} 已用于 {archived['kind']!r}，不能再作为 {kind!r}")
    _require(archived["payload"] == payload, "operation_conflict",
             f"operation_id {op_id!r} 以不同业务载荷重试，内容冲突，整轮拒绝")
    return dict(archived["receipt"], replayed=True)


def _archive(state, op_id, kind, payload, receipt, round_id):
    state["operations"][op_id] = {
        "kind": kind,
        "payload": deepcopy(payload),
        "receipt": deepcopy(receipt),
        "round_id": round_id,
    }


def _do_receive(state, op, round_id, adj_index):
    op_id, kind, archived = _check_op_header(state, op, ("receive",))
    payload = {k: v for k, v in op.items() if k not in ("operation_id", "op")}
    if archived is not None:
        return _replay_receipt(state, archived, op_id, kind, payload)

    receipt_id = op.get("receipt_id")
    account_id = op.get("account_id")
    amount = op.get("amount_cents")
    allocations = op.get("allocations", [])
    _require_str(receipt_id, "invalid_operation", "receive 缺少非空 receipt_id")
    _require_str(account_id, "invalid_operation", f"receive {receipt_id!r} 缺少非空 account_id")
    _require_pos_int(amount, "invalid_operation",
                     f"receive {receipt_id!r} 的 amount_cents 必须是正整数")
    _require(isinstance(allocations, list), "invalid_operation",
             f"receive {receipt_id!r} 的 allocations 必须是数组")
    _require(receipt_id not in state["receipts"], "duplicate_receipt",
             f"receipt_id {receipt_id!r} 已登记，不能用第二次 receive 重复登记")

    line_amounts = {}
    for item in allocations:
        _require_object(item, "invalid_operation", "receive 分配项必须是对象")
        line_id = item.get("line_id")
        cents = item.get("amount_cents")
        _require_str(line_id, "invalid_operation", "receive 分配项缺少非空 line_id")
        _require_pos_int(cents, "invalid_operation",
                         f"receive {receipt_id!r} 对 line {line_id!r} 的分配必须是正整数")
        _require(line_id in state["lines"], "unknown_line",
                 f"receive {receipt_id!r} 引用未知 line_id {line_id!r}")
        _require(state["lines"][line_id]["account_id"] == account_id, "cross_account",
                 f"receive {receipt_id!r} 不能核销其他客户的 line {line_id!r}")
        line_amounts[line_id] = line_amounts.get(line_id, 0) + cents
    total_allocated = sum(line_amounts.values())
    _require(total_allocated <= amount, "over_allocation",
             f"receive {receipt_id!r} 的分配之和 {total_allocated} 超过到账金额 {amount}")

    receipt = {
        "receipt_id": receipt_id,
        "account_id": account_id,
        "amount_cents": amount,
        "withdrawn_unallocated_cents": 0,
        "allocations": [
            {"line_id": lid, "amount_cents": cents, "withdrawn_cents": 0}
            for lid, cents in sorted(line_amounts.items())
        ],
    }
    state["receipts"][receipt_id] = receipt
    out_receipt = {
        "op": "receive", "operation_id": op_id, "replayed": False,
        "receipt_id": receipt_id, "account_id": account_id,
        "amount_cents": amount,
        "allocated_cents": total_allocated,
        "unallocated_cents": amount - total_allocated,
        "allocations": deepcopy(receipt["allocations"]),
    }
    _archive(state, op_id, kind, payload, out_receipt, round_id)
    return out_receipt


def _do_allocate(state, op, round_id):
    op_id, kind, archived = _check_op_header(state, op, ("allocate",))
    payload = {k: v for k, v in op.items() if k not in ("operation_id", "op")}
    if archived is not None:
        return _replay_receipt(state, archived, op_id, kind, payload)

    receipt_id = op.get("receipt_id")
    allocations = op.get("allocations")
    _require_str(receipt_id, "invalid_operation", "allocate 缺少非空 receipt_id")
    _require(receipt_id in state["receipts"], "unknown_receipt",
             f"allocate 引用未知 receipt_id {receipt_id!r}")
    receipt = state["receipts"][receipt_id]
    _require(isinstance(allocations, list) and allocations, "invalid_operation",
             f"allocate {receipt_id!r} 需要非空 allocations 数组")

    line_amounts = {}
    total = 0
    for item in allocations:
        _require_object(item, "invalid_operation", "allocate 分配项必须是对象")
        line_id = item.get("line_id")
        cents = item.get("amount_cents")
        _require_str(line_id, "invalid_operation", "allocate 分配项缺少非空 line_id")
        _require_pos_int(cents, "invalid_operation",
                         f"allocate {receipt_id!r} 对 line {line_id!r} 的分配必须是正整数")
        _require(line_id in state["lines"], "unknown_line",
                 f"allocate {receipt_id!r} 引用未知 line_id {line_id!r}")
        _require(state["lines"][line_id]["account_id"] == receipt["account_id"], "cross_account",
                 f"receipt {receipt_id!r} 不能核销其他客户的 line {line_id!r}")
        line_amounts[line_id] = line_amounts.get(line_id, 0) + cents
        total += cents

    available = _receipt_unallocated(receipt)
    _require(total > 0, "invalid_operation", f"allocate {receipt_id!r} 的实际变化总量必须为正")
    _require(total <= available, "over_allocation",
             f"allocate {receipt_id!r} 拟分配 {total}，超过现有未分配额 {available}")

    existing = {a["line_id"]: a for a in receipt["allocations"]}
    for lid, cents in sorted(line_amounts.items()):
        if lid in existing:
            existing[lid]["amount_cents"] += cents
        else:
            receipt["allocations"].append(
                {"line_id": lid, "amount_cents": cents, "withdrawn_cents": 0})

    out_receipt = {
        "op": "allocate", "operation_id": op_id, "replayed": False,
        "receipt_id": receipt_id, "allocated_cents": total,
        "unallocated_cents": _receipt_unallocated(receipt),
        "allocations": deepcopy(receipt["allocations"]),
    }
    _archive(state, op_id, kind, payload, out_receipt, round_id)
    return out_receipt


def _do_chargeback(state, op, round_id):
    op_id, kind, archived = _check_op_header(state, op, ("chargeback",))
    payload = {k: v for k, v in op.items() if k not in ("operation_id", "op")}
    if archived is not None:
        return _replay_receipt(state, archived, op_id, kind, payload)

    receipt_id = op.get("receipt_id")
    withdrawals = op.get("withdrawals", [])
    unallocated_cents = op.get("unallocated_cents", 0)
    _require_str(receipt_id, "invalid_operation", "chargeback 缺少非空 receipt_id")
    _require(receipt_id in state["receipts"], "unknown_receipt",
             f"chargeback 引用未知 receipt_id {receipt_id!r}")
    receipt = state["receipts"][receipt_id]
    _require(isinstance(withdrawals, list), "invalid_operation",
             f"chargeback {receipt_id!r} 的 withdrawals 必须是数组")
    _require_nonneg_int(unallocated_cents, "invalid_operation",
                        f"chargeback {receipt_id!r} 的 unallocated_cents 必须是非负整数")

    line_withdraw = {}
    for item in withdrawals:
        _require_object(item, "invalid_operation", "chargeback 撤回项必须是对象")
        line_id = item.get("line_id")
        cents = item.get("amount_cents")
        _require_str(line_id, "invalid_operation", "chargeback 撤回项缺少非空 line_id")
        _require_pos_int(cents, "invalid_operation",
                         f"chargeback {receipt_id!r} 对 line {line_id!r} 的撤回必须是正整数")
        allocation = next((a for a in receipt["allocations"] if a["line_id"] == line_id), None)
        _require(allocation is not None, "unknown_allocation",
                 f"receipt {receipt_id!r} 对 line {line_id!r} 没有可撤回的核销")
        line_withdraw[line_id] = line_withdraw.get(line_id, 0) + cents

    # 累计撤回不得超过各自仍有效的分配/未分配余额
    total_line_withdraw = 0
    for lid, cents in line_withdraw.items():
        allocation = next(a for a in receipt["allocations"] if a["line_id"] == lid)
        effective = allocation["amount_cents"] - allocation["withdrawn_cents"]
        _require(cents <= effective, "over_chargeback",
                 f"chargeback {receipt_id!r} 撤回 line {lid!r} {cents}，"
                 f"超过其仍有效核销 {effective}")
        total_line_withdraw += cents
    _require(unallocated_cents <= _receipt_unallocated(receipt), "over_chargeback",
             f"chargeback {receipt_id!r} 撤回未分配 {unallocated_cents}，"
             f"超过现有未分配额 {_receipt_unallocated(receipt)}")
    total_change = total_line_withdraw + unallocated_cents
    _require(total_change > 0, "invalid_operation",
             f"chargeback {receipt_id!r} 的实际变化总量必须为正")

    for lid, cents in line_withdraw.items():
        allocation = next(a for a in receipt["allocations"] if a["line_id"] == lid)
        allocation["withdrawn_cents"] += cents
    receipt["withdrawn_unallocated_cents"] += unallocated_cents

    out_receipt = {
        "op": "chargeback", "operation_id": op_id, "replayed": False,
        "receipt_id": receipt_id,
        "withdrawn_allocated_cents": total_line_withdraw,
        "withdrawn_unallocated_cents": unallocated_cents,
        "unallocated_cents": _receipt_unallocated(receipt),
        "effective_writeoff_cents": _receipt_effective(receipt),
    }
    _archive(state, op_id, kind, payload, out_receipt, round_id)
    return out_receipt


# ---------------------------------------------------------------------------
# 欠款与许可
# ---------------------------------------------------------------------------

def _account_lines(state, account_id):
    return [lid for lid in sorted(state["lines"])
            if state["lines"][lid]["account_id"] == account_id]


def _overdue_debt(state, adj_index, account_id, evaluation_at):
    """只汇总 evaluation_at 严格晚于 due_at + grace_seconds 的正欠款。"""
    policy = state["policies"].get(account_id)
    _require(policy is not None, "unknown_policy",
             f"account {account_id!r} 尚未登记 debt_limit/grace 策略")
    debt = 0
    overdue_lines = []
    for lid in _account_lines(state, account_id):
        line = state["lines"][lid]
        deadline = _dt(line["due_at"]) + timedelta(seconds=policy["grace_seconds"])
        receivable = _line_receivable(state, adj_index, lid)
        writeoff = _line_writeoff(state, lid)
        owed = max(receivable - writeoff, 0)
        if evaluation_at > deadline and owed > 0:
            debt += owed
            overdue_lines.append(lid)
    return debt, policy, overdue_lines


def _invalidate_pending(state, account_id, reason, evaluation_at):
    """把该客户所有尚未启动的 issued 许可不可逆地标记失效。"""
    changed = []
    for permit_id in sorted(state["permits"]):
        permit = state["permits"][permit_id]
        if permit["account_id"] != account_id:
            continue
        if permit["status"] == "issued":
            permit["status"] = "invalidated"
            permit["invalidated_reason"] = reason
            permit["invalidated_at"] = evaluation_at.isoformat()
            changed.append(permit_id)
    return changed


def _expire_pending(state, evaluation_at):
    """到达 expires_at 的未启动许可失效（过期原因与欠款原因可区分）。"""
    changed = []
    for permit_id in sorted(state["permits"]):
        permit = state["permits"][permit_id]
        if permit["status"] == "issued" and evaluation_at >= _dt(permit["expires_at"]):
            permit["status"] = "invalidated"
            permit["invalidated_reason"] = "expired"
            permit["invalidated_at"] = evaluation_at.isoformat()
            changed.append(permit_id)
    return changed


def _do_grant(state, op, round_id, evaluation_at, adj_index):
    op_id, kind, archived = _check_op_header(state, op, ("grant",))
    payload = {k: v for k, v in op.items() if k not in ("operation_id", "op")}
    if archived is not None:
        return _replay_receipt(state, archived, op_id, kind, payload)

    permit_id = op.get("permit_id")
    account_id = op.get("account_id")
    station_id = op.get("station_id")
    expires_at = op.get("expires_at")
    _require_str(permit_id, "invalid_operation", "grant 缺少非空 permit_id")
    _require_str(account_id, "invalid_operation", f"grant {permit_id!r} 缺少非空 account_id")
    _require_str(station_id, "invalid_operation", f"grant {permit_id!r} 缺少非空 station_id")
    _dt(expires_at, "invalid_operation", f"grant {permit_id!r} 的 expires_at")
    _require(permit_id not in state["permits"], "duplicate_permit",
             f"permit_id {permit_id!r} 已存在")
    _require(account_id in state["policies"], "unknown_policy",
             f"grant {permit_id!r} 的 account {account_id!r} 尚未登记策略")
    _require(_dt(expires_at) > evaluation_at, "permit_expired",
             f"grant {permit_id!r} 的 expires_at 必须严格晚于本轮 evaluation_at")

    debt, policy, overdue_lines = _overdue_debt(state, adj_index, account_id, evaluation_at)
    if debt > policy["debt_limit_cents"]:
        # 可记录、可重试的业务拒绝结果；不创建许可
        out_receipt = {
            "op": "grant", "operation_id": op_id, "replayed": False,
            "permit_id": permit_id, "account_id": account_id, "station_id": station_id,
            "granted": False, "denied_reason": "debt_limit_exceeded",
            "overdue_debt_cents": debt, "debt_limit_cents": policy["debt_limit_cents"],
        }
        _archive(state, op_id, kind, payload, out_receipt, round_id)
        return out_receipt

    permit = {
        "permit_id": permit_id,
        "account_id": account_id,
        "station_id": station_id,
        "expires_at": expires_at,
        "issued_at": evaluation_at.isoformat(),
        "status": "issued",
        "invalidated_reason": None,
        "invalidated_at": None,
        "session_id": None,
        "started_at": None,
        "finished_at": None,
    }
    state["permits"][permit_id] = permit
    out_receipt = {
        "op": "grant", "operation_id": op_id, "replayed": False,
        "permit_id": permit_id, "account_id": account_id, "station_id": station_id,
        "granted": True, "expires_at": expires_at,
    }
    _archive(state, op_id, kind, payload, out_receipt, round_id)
    return out_receipt


def _do_begin(state, op, round_id, evaluation_at, adj_index):
    op_id, kind, archived = _check_op_header(state, op, ("begin",))
    payload = {k: v for k, v in op.items() if k not in ("operation_id", "op")}
    if archived is not None:
        return _replay_receipt(state, archived, op_id, kind, payload)

    permit_id = op.get("permit_id")
    station_id = op.get("station_id")
    session_id = op.get("session_id")
    _require_str(permit_id, "invalid_operation", "begin 缺少非空 permit_id")
    _require_str(station_id, "invalid_operation", f"begin {permit_id!r} 缺少非空 station_id")
    _require_str(session_id, "invalid_operation", f"begin {permit_id!r} 缺少非空 session_id")
    _require(permit_id in state["permits"], "unknown_permit",
             f"begin 引用未知 permit_id {permit_id!r}")
    permit = state["permits"][permit_id]
    _require(permit["status"] == "issued", "permit_not_usable",
             f"permit {permit_id!r} 当前状态为 {permit['status']!r}，不能启动"
             + (f"（{permit.get('invalidated_reason')}）" if permit.get("invalidated_reason") else ""))
    _require(permit["station_id"] == station_id, "station_mismatch",
             f"permit {permit_id!r} 只能在站点 {permit['station_id']!r} 使用，"
             f"不能在 {station_id!r} 启动")
    _require(evaluation_at < _dt(permit["expires_at"]), "permit_expired",
             f"permit {permit_id!r} 已过期，不能启动")
    _require(session_id not in state["used_sessions"], "session_conflict",
             f"session_id {session_id!r} 已被另一许可使用")

    debt, policy, _ = _overdue_debt(state, adj_index, permit["account_id"], evaluation_at)
    _require(debt <= policy["debt_limit_cents"], "debt_not_qualified",
             f"account {permit['account_id']!r} 当前逾期欠款 {debt} 超过限额 "
             f"{policy['debt_limit_cents']}，同轮失格客户的 begin 不得成功")

    permit["status"] = "started"
    permit["session_id"] = session_id
    permit["started_at"] = evaluation_at.isoformat()
    state["used_sessions"][session_id] = permit_id
    out_receipt = {
        "op": "begin", "operation_id": op_id, "replayed": False,
        "permit_id": permit_id, "session_id": session_id,
        "station_id": station_id, "started": True,
    }
    _archive(state, op_id, kind, payload, out_receipt, round_id)
    return out_receipt


def _do_finish(state, op, round_id, evaluation_at):
    op_id, kind, archived = _check_op_header(state, op, ("finish",))
    payload = {k: v for k, v in op.items() if k not in ("operation_id", "op")}
    if archived is not None:
        return _replay_receipt(state, archived, op_id, kind, payload)

    permit_id = op.get("permit_id")
    session_id = op.get("session_id")
    _require_str(permit_id, "invalid_operation", "finish 缺少非空 permit_id")
    _require_str(session_id, "invalid_operation", f"finish {permit_id!r} 缺少非空 session_id")
    _require(permit_id in state["permits"], "unknown_permit",
             f"finish 引用未知 permit_id {permit_id!r}")
    permit = state["permits"][permit_id]
    _require(permit["status"] == "started", "permit_not_started",
             f"permit {permit_id!r} 当前状态为 {permit['status']!r}，只有已启动会话可结束")
    _require(permit["session_id"] == session_id, "session_mismatch",
             f"finish 的 session_id {session_id!r} 不是 permit {permit_id!r} 实际启动的 "
             f"{permit['session_id']!r}")

    permit["status"] = "finished"
    permit["finished_at"] = evaluation_at.isoformat()
    out_receipt = {
        "op": "finish", "operation_id": op_id, "replayed": False,
        "permit_id": permit_id, "session_id": session_id, "finished": True,
    }
    _archive(state, op_id, kind, payload, out_receipt, round_id)
    return out_receipt


# ---------------------------------------------------------------------------
# 结果渲染
# ---------------------------------------------------------------------------

def _render(state, evaluation_at, round_id, finance_receipts, service_receipts,
            invalidated_this_round, adj_index):
    # 逐单视图
    lines_view = []
    for lid in sorted(state["lines"]):
        line = state["lines"][lid]
        receivable = _line_receivable(state, adj_index, lid)
        writeoff = _line_writeoff(state, lid)
        lines_view.append({
            "line_id": lid,
            "account_id": line["account_id"],
            "session_id": line["session_id"],
            "period_id": line["period_id"],
            "due_at": line["due_at"],
            "receivable_cents": receivable,
            "writeoff_cents": writeoff,
            "debt_cents": max(receivable - writeoff, 0),
            "credit_cents": max(writeoff - receivable, 0),
        })

    # 逐收款视图与守恒分解
    receipts_view = []
    for rid in sorted(state["receipts"]):
        receipt = state["receipts"][rid]
        effective = _receipt_effective(receipt)
        unallocated = _receipt_unallocated(receipt)
        withdrawn_alloc = sum(a["withdrawn_cents"] for a in receipt["allocations"])
        chargeback_total = withdrawn_alloc + receipt["withdrawn_unallocated_cents"]
        # 守恒：有效核销 + 未分配 + 累计退单 == 原收款金额
        assert effective + unallocated + chargeback_total == receipt["amount_cents"]
        receipts_view.append({
            "receipt_id": rid,
            "account_id": receipt["account_id"],
            "received_cents": receipt["amount_cents"],
            "effective_writeoff_cents": effective,
            "unallocated_cents": unallocated,
            "withdrawn_allocated_cents": withdrawn_alloc,
            "withdrawn_unallocated_cents": receipt["withdrawn_unallocated_cents"],
            "chargeback_total_cents": chargeback_total,
            "conservation": {
                "effective_writeoff_cents": effective,
                "unallocated_cents": unallocated,
                "chargeback_total_cents": chargeback_total,
                "received_cents": receipt["amount_cents"],
                "balanced": effective + unallocated + chargeback_total == receipt["amount_cents"],
            },
            "allocations": deepcopy(receipt["allocations"]),
        })

    # 账户逾期欠款与当前准入原因
    accounts = sorted(set(state["policies"]) |
                      {line["account_id"] for line in state["lines"].values()})
    accounts_view = []
    for account_id in accounts:
        policy = state["policies"].get(account_id)
        entry = {"account_id": account_id, "policy": policy}
        if policy is None:
            entry.update({"registered": False, "admission": "policy_missing",
                          "overdue_debt_cents": None, "debt_limit_cents": None,
                          "grace_seconds": None, "overdue_line_ids": []})
        else:
            debt, _, overdue_lines = _overdue_debt(state, adj_index, account_id, evaluation_at)
            qualified = debt <= policy["debt_limit_cents"]
            entry.update({
                "registered": True,
                "overdue_debt_cents": debt,
                "debt_limit_cents": policy["debt_limit_cents"],
                "grace_seconds": policy["grace_seconds"],
                "overdue_line_ids": overdue_lines,
                "admission": "qualified" if qualified else "debt_limit_exceeded",
            })
        accounts_view.append(entry)

    # 许可当前生命周期
    permits_view = [deepcopy(state["permits"][pid]) for pid in sorted(state["permits"])]

    # 本轮全部回执（金融在前、服务在后，保持输入次序分组）
    receipts = list(finance_receipts) + list(service_receipts)

    return {
        "round_id": round_id,
        "evaluation_at": evaluation_at.isoformat(),
        "lines": lines_view,
        "receipts": receipts_view,
        "accounts": accounts_view,
        "permits": permits_view,
        "invalidated_this_round": invalidated_this_round,
        "receipts_this_round": receipts,
    }


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------

def apply_round(parent_state, round_input):
    """在 ``parent_state``（首轮可为 ``None``）上应用一轮工作台输入。

    返回 ``(next_state, result)``。任何校验/业务失败抛 :class:`WorkbenchError`，
    父状态与输入的结算 source_state 均不被修改。
    """
    _require_object(round_input, "invalid_round", "round 输入必须是 JSON 对象")
    round_id = round_input.get("round_id")
    evaluation_raw = round_input.get("evaluation_at")
    _require_str(round_id, "invalid_round", "round 缺少非空 round_id")
    evaluation_at = _dt(evaluation_raw, "invalid_round", "evaluation_at")

    state = deepcopy(parent_state) if parent_state is not None else initial_state()
    _require(isinstance(state, dict) and state.get("version") == STATE_VERSION,
             "invalid_state", "不支持的工作台状态版本")
    if state["last_evaluation_at"] is not None:
        _require(evaluation_at >= _dt(state["last_evaluation_at"]), "clock_moved_backwards",
                 f"evaluation_at 不得倒退：上一轮 {state['last_evaluation_at']!r}，"
                 f"本轮 {evaluation_raw!r}")

    finance_ops = round_input.get("finance_operations", [])
    service_ops = round_input.get("service_operations", [])
    _require(isinstance(finance_ops, list) and isinstance(service_ops, list),
             "invalid_round", "finance_operations / service_operations 必须是数组")

    # 阶段 0：本轮 operation_id 批内唯一（不同列表也算同一身份空间）
    seen = set()
    for op in finance_ops + service_ops:
        _require_object(op, "invalid_operation", "操作必须是对象")
        op_id = op.get("operation_id")
        _require_str(op_id, "invalid_operation", "操作缺少非空 operation_id")
        _require(op_id not in seen, "operation_conflict",
                 f"本轮 operation_id {op_id!r} 重复")
        seen.add(op_id)
        _require(op.get("op") in ALL_OPS, "invalid_operation",
                 f"operation_id {op_id!r} 的 op 非法: {op.get('op')!r}")
    for op in finance_ops:
        _require(op["op"] in FINANCE_OPS, "invalid_phase",
                 f"operation_id {op['operation_id']!r} 是服务操作，必须放入 service_operations")
    for op in service_ops:
        _require(op["op"] in SERVICE_OPS, "invalid_phase",
                 f"operation_id {op['operation_id']!r} 是金融操作，必须放入 finance_operations")

    # 阶段 1：凭据导入（可选；显式 null 视为结构错误）
    if "import" in round_input:
        _require(round_input["import"] is not None, "invalid_import",
                 "import 为 null 时应直接省略该字段")
        _import_credentials(state, round_input["import"])

    # 阶段 2：策略登记（可选，先于金融操作生效）
    if "register_policies" in round_input:
        _require(round_input["register_policies"] is not None, "invalid_policy",
                 "register_policies 为 null 时应直接省略该字段")
        _register_policies(state, round_input["register_policies"])

    adj_index = _adjustments_by_line(state)

    # 阶段 3：金融操作按输入次序执行
    finance_receipts = []
    for op in finance_ops:
        kind = op["op"]
        if kind == "receive":
            finance_receipts.append(_do_receive(state, op, round_id, adj_index))
        elif kind == "allocate":
            finance_receipts.append(_do_allocate(state, op, round_id))
        else:
            finance_receipts.append(_do_chargeback(state, op, round_id))

    # 阶段 4：按轮末欠款失效尚未启动的许可，再处理到期失效
    invalidated_this_round = []
    for account_id in sorted(set(state["policies"]) |
                             {line["account_id"] for line in state["lines"].values()}):
        if account_id not in state["policies"]:
            continue
        debt, policy, _ = _overdue_debt(state, adj_index, account_id, evaluation_at)
        if debt > policy["debt_limit_cents"]:
            for pid in _invalidate_pending(state, account_id, "debt_limit_exceeded",
                                           evaluation_at):
                invalidated_this_round.append(
                    {"permit_id": pid, "reason": "debt_limit_exceeded",
                     "at": evaluation_at.isoformat()})
    for pid in _expire_pending(state, evaluation_at):
        invalidated_this_round.append(
            {"permit_id": pid, "reason": "expired", "at": evaluation_at.isoformat()})

    # 阶段 5：服务操作按输入次序执行
    service_receipts = []
    for op in service_ops:
        kind = op["op"]
        if kind == "grant":
            service_receipts.append(
                _do_grant(state, op, round_id, evaluation_at, adj_index))
        elif kind == "begin":
            service_receipts.append(
                _do_begin(state, op, round_id, evaluation_at, adj_index))
        else:
            service_receipts.append(_do_finish(state, op, round_id, evaluation_at))

    state["last_evaluation_at"] = evaluation_at.isoformat()
    result = _render(state, evaluation_at, round_id, finance_receipts,
                     service_receipts, invalidated_this_round, adj_index)
    return state, result
