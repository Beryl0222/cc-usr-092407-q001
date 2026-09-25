"""市场举报奖励领域核心。

职责：
- 在隔离身份信息的前提下接收线索、证据补充与关联案件，承办人员只接触别名；
- 认定最先有效贡献与后来出现的独立关键贡献，重复举报不予奖励；
- 案件进入可奖励阶段后，按违法类别、举报等级、罚没结果和当时生效的规则版本
  自动生成奖励建议金额（三级物质奖励 + 精神奖励，内部举报加成，百万封顶）；
- 二十万元以上自动转财政会签，承办人不得批准自己的建议；
- 撤回、重复举报、行政复议和判决变化一律以追加决定调整，旧结论继续保留；
  追加决定生效时按锁定规则重算应得额：追回以真实已付净额为限，未付差额
  只取消待付、绝不生成负支付；应得调增部分转为待付，由支付执行人经正常
  支付流程发放。结算幂等，请求重放与失败恢复不会重复补付或追回。

金额单位为元，一律取整（四舍五入到元）。日期使用 ISO 格式（YYYY-MM-DD），
按字典序比较即可，不涉及时区。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import date


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class DomainError(Exception):
    """领域规则被违反。"""


class NotFoundError(DomainError):
    """引用的对象不存在。"""


class PermissionDenied(DomainError):
    """操作者角色或身份不允许该操作。"""


class InvalidStateError(DomainError):
    """当前状态不允许该操作。"""


# ---------------------------------------------------------------------------
# 角色与常量
# ---------------------------------------------------------------------------

ROLE_INTAKE = "intake_officer"          # 举报中心受理员（可接触身份）
ROLE_HANDLER = "case_handler"           # 案件承办人（只接触别名）
ROLE_REVIEWER = "reward_reviewer"       # 奖励审核人
ROLE_FINANCE = "finance_cosigner"       # 财政会签人
ROLE_PAYER = "payment_officer"          # 支付执行人
ROLE_AUDITOR = "audit_viewer"           # 审计查看人（可查身份访问台账）

CONTRIBUTION_FIRST = "最先有效贡献"
CONTRIBUTION_KEY = "独立关键贡献"
CONTRIBUTION_SUPPLEMENT = "补充贡献"
CONTRIBUTION_DUPLICATE = "重复举报"

GRADE_LABELS = {1: "一级举报", 2: "二级举报", 3: "三级举报"}

DECISION_PROPOSED = "待奖励审核"
DECISION_PENDING_COSIGN = "待财政会签"
DECISION_EFFECTIVE = "已生效"
DECISION_REJECTED = "已驳回"

ADJUST_PENDING_REVIEW = "待调整审核"
ADJUST_PENDING_COSIGN = "待调整会签"
ADJUST_EFFECTIVE = "已生效"
ADJUST_REJECTED = "已驳回"

ADJUST_KINDS = ("withdrawal", "duplicate", "reconsideration", "judgment")
ADJUST_KIND_LABELS = {
    "withdrawal": "举报人撤回",
    "duplicate": "重复举报确认",
    "reconsideration": "行政复议变化",
    "judgment": "司法判决变化",
}

# 待办审批的中文说明，供逐人说明视图使用
PENDING_LABELS = {
    DECISION_PROPOSED: "奖励审核",
    DECISION_PENDING_COSIGN: "财政会签",
    ADJUST_PENDING_REVIEW: "调整审核",
    ADJUST_PENDING_COSIGN: "调整财政会签",
}


# ---------------------------------------------------------------------------
# 规则版本
# ---------------------------------------------------------------------------

DEFAULT_RULES = [
    {
        "version": "2023-01",
        "effective_from": "2023-01-01",
        "label": "2023 版举报奖励规则",
        # 三级举报：按罚没款比例、每级最低保障（元）
        "rates": {1: 0.05, 2: 0.03, 3: 0.01},
        "grade_floor": {1: 5000, 2: 3000, 3: 1000},
        # 无罚没款结案：按等级定额（元）
        "no_penalty_amount": {1: 5000, 2: 3000, 3: 1000},
        # 内部举报（被举报人内部人员）加成
        "insider_multiplier": 1.5,
        # 单案物质奖励上限（元）
        "cap": 1_000_000,
        # 达到该金额（元）须会同财政部门确定
        "cosign_threshold": 200_000,
    },
    {
        "version": "2026-01",
        "effective_from": "2026-01-01",
        "label": "2026 版举报奖励规则",
        "rates": {1: 0.06, 2: 0.04, 3: 0.02},
        "grade_floor": {1: 6000, 2: 4000, 3: 2000},
        "no_penalty_amount": {1: 8000, 2: 5000, 3: 3000},
        "insider_multiplier": 1.5,
        "cap": 1_000_000,
        "cosign_threshold": 200_000,
    },
]

# 违法类别 -> 严重度系数（0.5 ~ 1.0），在等级区间内调节金额
DEFAULT_CATEGORY_SEVERITY = {
    "食品药品安全": 1.0,
    "特种设备安全": 1.0,
    "产品质量": 0.8,
    "价格违法": 0.7,
    "不正当竞争": 0.7,
    "广告违法": 0.6,
    "无照经营": 0.5,
}
DEFAULT_SEVERITY = 0.6


def _round_yuan(amount):
    """金额四舍五入到元，返回 int。"""
    return int(round(float(amount) + 1e-9))


# ---------------------------------------------------------------------------
# 身份库：真实身份与业务数据物理隔离
# ---------------------------------------------------------------------------

class IdentityVault:
    """保存举报人的真实身份信息。

    业务表（举报、案件、决定、日志）只允许出现别名；
    只有 intake_officer / audit_viewer 角色可以读取身份，且每次读取都记台账。
    """

    def __init__(self):
        self._records = {}   # alias -> {"identity": ..., "disclosed": bool}
        self.access_log = []  # 身份访问台账（审计用，本身不含身份内容）

    def store(self, alias, identity):
        self._records[alias] = {"identity": identity}

    def has(self, alias):
        return alias in self._records

    def reveal(self, alias, actor_id, actor_role, reason):
        if actor_role not in (ROLE_INTAKE, ROLE_AUDITOR):
            raise PermissionDenied("该角色无权查看举报人身份")
        if alias not in self._records:
            raise NotFoundError(f"别名 {alias} 无身份登记")
        self.access_log.append({
            "alias": alias,
            "actor": actor_id,
            "role": actor_role,
            "reason": reason,
        })
        return self._records[alias]["identity"]


# ---------------------------------------------------------------------------
# 举报奖励中心
# ---------------------------------------------------------------------------

class RewardCenter:
    """举报接收、贡献认定、奖励建议、审批会签、支付与追加调整的核心。"""

    def __init__(self, rules=None, category_severity=None, today=None):
        # 规则按生效日升序保存，便于“当时生效”的查找
        self.rules = sorted(rules or DEFAULT_RULES, key=lambda r: r["effective_from"])
        self.category_severity = dict(category_severity or DEFAULT_CATEGORY_SEVERITY)
        self._today = today or (lambda: date.today().isoformat())

        self.vault = IdentityVault()
        self.reports = {}       # alias -> report
        self.cases = {}         # case_id -> case
        self.decisions = {}     # decision_id -> 奖励建议（决定）
        self.adjustments = {}   # adjustment_id -> 追加决定
        self.commendations = {} # commendation_id -> 精神奖励
        self.payments = []      # 支付记录（含追回，金额为负）
        self.event_log = []     # 普通办案日志（只含别名，绝不含身份）

        self._case_seq = 0
        self._report_seq = 0
        self._decision_seq = 0
        self._adjust_seq = 0
        self._commend_seq = 0

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _log(self, event, **fields):
        """普通办案日志：只允许别名级信息。"""
        entry = {"event": event, "date": self._today()}
        entry.update(fields)
        self.event_log.append(entry)

    def rule_version_on(self, day):
        """返回指定日期当天生效的规则版本。"""
        chosen = None
        for rule in self.rules:
            if rule["effective_from"] <= day:
                chosen = rule
        if chosen is None:
            raise DomainError(f"{day} 之前没有生效的奖励规则")
        return chosen

    def _severity_of(self, violation_category):
        return self.category_severity.get(violation_category, DEFAULT_SEVERITY)

    def _next_id(self, prefix, seq_name):
        seq = getattr(self, seq_name) + 1
        setattr(self, seq_name, seq)
        return f"{prefix}-{seq:04d}"

    # ------------------------------------------------------------------
    # 1. 举报接收（身份隔离）
    # ------------------------------------------------------------------

    def intake_report(self, actor_id, actor_role, violation_category, facts,
                      received_at=None, identity=None, is_insider=False):
        """接收一条举报线索。

        - 身份信息（如提供）只进身份库，业务侧只保留别名；
        - 匿名举报不强制提供身份，发放领取码用于日后领奖；
        - 返回 (alias, case_id, claim_code)。
        """
        if actor_role != ROLE_INTAKE:
            raise PermissionDenied("只有举报中心受理员可以登记举报")
        if not violation_category:
            raise DomainError("必须填写违法类别")
        facts = [f for f in (facts or []) if f]
        if not facts:
            raise DomainError("必须至少提供一条违法事实线索")

        self._report_seq += 1
        alias = f"RPT-{self._report_seq:04d}"
        received_at = received_at or self._today()

        # 匿名或实名，都生成领取码；实名举报的身份只入身份库
        claim_code = secrets.token_hex(4)
        if identity:
            self.vault.store(alias, dict(identity))

        report = {
            "alias": alias,
            "violation_category": violation_category,
            "facts": list(facts),
            "supplements": [],
            "received_at": received_at,
            "is_insider": bool(is_insider),
            "is_anonymous": not bool(identity),
            "claim_code_hash": hashlib.sha256(claim_code.encode()).hexdigest(),
            "case_id": None,
            "contribution": None,      # 认定后写入贡献类别
            "grade": None,             # 认定后写入举报等级 1/2/3
            "withdrawn": False,
            "duplicate_of": None,
        }
        self.reports[alias] = report

        # 关联案件：同类别且尚未办结的案件自动关联，否则新建
        # （迟到举报在奖励确定前仍应并入原案，保障多人举报先后顺序认定）
        case_id = None
        for case in self.cases.values():
            if (case["violation_category"] == violation_category
                    and case["status"] != "已办结"):
                case_id = case["case_id"]
                break
        if case_id is None:
            case_id = self._open_case(violation_category)
        self.link_report_to_case(alias, case_id)

        self._log("举报登记", alias=alias, case_id=case_id,
                  violation_category=violation_category)
        return alias, case_id, claim_code

    def _open_case(self, violation_category):
        self._case_seq += 1
        case_id = f"C-{self._case_seq:04d}"
        self.cases[case_id] = {
            "case_id": case_id,
            "violation_category": violation_category,
            "status": "已登记",
            "aliases": [],
            "penalty_amount": None,
            "closed_at": None,
            "reward_stage_at": None,
            "rule_version": None,   # 进入可奖励阶段时锁定
        }
        return case_id

    def link_report_to_case(self, alias, case_id):
        report = self._report(alias)
        case = self._case(case_id)
        if report["case_id"] == case_id:
            return
        if report["case_id"] is not None:
            old = self.cases[report["case_id"]]
            if alias in old["aliases"]:
                old["aliases"].remove(alias)
        report["case_id"] = case_id
        if alias not in case["aliases"]:
            case["aliases"].append(alias)
        self._log("关联案件", alias=alias, case_id=case_id)

    def add_supplement(self, alias, facts, added_at=None):
        """举报人补充证据材料（仍只按别名记录）。"""
        report = self._report(alias)
        facts = [f for f in (facts or []) if f]
        if not facts:
            raise DomainError("补充材料不能为空")
        entry = {"facts": list(facts), "added_at": added_at or self._today()}
        report["supplements"].append(entry)
        report["facts"].extend(facts)
        self._log("证据补充", alias=alias, case_id=report["case_id"])
        return entry

    # ------------------------------------------------------------------
    # 2. 案件流程
    # ------------------------------------------------------------------

    def close_case(self, case_id, penalty_amount, closed_at=None):
        """结案并登记罚没款结果（可以为 0）。"""
        case = self._case(case_id)
        if penalty_amount is None or penalty_amount < 0:
            raise DomainError("罚没款金额必须是不小于 0 的数字")
        case["penalty_amount"] = _round_yuan(penalty_amount)
        case["closed_at"] = closed_at or self._today()
        case["status"] = "已结案"
        self._log("案件结案", case_id=case_id, penalty_amount=case["penalty_amount"])

    def enter_reward_stage(self, case_id, entered_at=None):
        """案件进入可奖励阶段：锁定当时生效的规则版本。"""
        case = self._case(case_id)
        if case["status"] != "已结案":
            raise InvalidStateError("只有已结案的案件才能进入可奖励阶段")
        entered_at = entered_at or self._today()
        rule = self.rule_version_on(entered_at)
        case["status"] = "可奖励"
        case["reward_stage_at"] = entered_at
        case["rule_version"] = rule["version"]
        self._log("进入可奖励阶段", case_id=case_id, rule_version=rule["version"])
        return rule["version"]

    # ------------------------------------------------------------------
    # 3. 贡献认定（最先有效 / 独立关键 / 重复）
    # ------------------------------------------------------------------

    def assess_contributions(self, case_id, assessments, actor_id, actor_role):
        """对案件下各举报进行贡献认定。

        assessments: [{"alias":..., "grade":1|2|3, "new_facts":[...],
                       "key_contribution":bool, "duplicate":bool}]
        规则：
        - 时间最早且未撤回的举报是“最先有效贡献”；
        - 之后的举报若带来未被覆盖的新事实，记“独立关键贡献”（可奖励），
          否则记“重复举报”（不予奖励）；
        - 等级只接受 1/2/3。
        """
        if actor_role != ROLE_INTAKE:
            raise PermissionDenied("贡献认定由举报中心统一作出")
        case = self._case(case_id)
        if case["status"] != "可奖励":
            raise InvalidStateError("案件尚未进入可奖励阶段")

        by_alias = {a["alias"]: a for a in assessments}
        ordered = sorted(
            (self.reports[al] for al in case["aliases"]),
            key=lambda r: (r["received_at"], r["alias"]),
        )
        covered = set()
        results = {}
        first_valid_done = False
        for report in ordered:
            alias = report["alias"]
            assess = by_alias.get(alias, {})
            grade = assess.get("grade")
            if grade not in (1, 2, 3):
                grade = 3
            # 新颖事实 = 举报材料事实 + 认定时补充列明的新事实
            novelty = (set(report["facts"]) | set(assess.get("new_facts") or [])) \
                - covered
            is_duplicate = bool(assess.get("duplicate"))
            is_key = bool(assess.get("key_contribution"))

            if report["withdrawn"]:
                contribution = None  # 已撤回，不参与认定
            elif not first_valid_done and not is_duplicate:
                contribution = CONTRIBUTION_FIRST
                first_valid_done = True
                covered.update(report["facts"])
            elif is_duplicate:
                contribution = CONTRIBUTION_DUPLICATE
                report["duplicate_of"] = self._first_valid_alias(case)
            elif is_key or novelty:
                # 后来者带来未被覆盖的新事实，即独立关键贡献
                contribution = CONTRIBUTION_KEY
                covered.update(novelty)
            else:
                contribution = CONTRIBUTION_SUPPLEMENT

            report["contribution"] = contribution
            report["grade"] = grade if contribution in (
                CONTRIBUTION_FIRST, CONTRIBUTION_KEY) else None
            results[alias] = contribution
            self._log("贡献认定", alias=alias, case_id=case_id,
                      contribution=contribution or "已撤回")
        return results

    def _first_valid_alias(self, case, exclude=None):
        ordered = sorted(
            (self.reports[al] for al in case["aliases"]),
            key=lambda r: (r["received_at"], r["alias"]),
        )
        for report in ordered:
            if report["alias"] == exclude:
                continue
            if report["contribution"] == CONTRIBUTION_FIRST:
                return report["alias"]
        return None

    # ------------------------------------------------------------------
    # 4. 奖励建议（金额由规则引擎生成，禁止手填）
    # ------------------------------------------------------------------

    def compute_amount(self, rule, grade, penalty_amount, is_insider, severity):
        """按规则版本计算物质奖励金额（纯函数，便于单测）。"""
        if penalty_amount and penalty_amount > 0:
            base = penalty_amount * rule["rates"][grade] * severity
            base = max(base, rule["grade_floor"][grade])
        else:
            base = rule["no_penalty_amount"][grade]
        if is_insider:
            base *= rule["insider_multiplier"]
        return min(_round_yuan(base), rule["cap"])

    def propose_rewards(self, case_id, handler_id, actor_role):
        """为案件下具备奖励资格的举报生成奖励建议。

        - 只有“最先有效贡献”和“独立关键贡献”具备物质奖励资格；
        - 金额按案件锁定的规则版本自动计算，任何人不得手填；
        - 返回本批次建议 id 列表。
        """
        if actor_role != ROLE_HANDLER:
            raise PermissionDenied("奖励建议由案件承办人发起")
        case = self._case(case_id)
        if case["status"] != "可奖励":
            raise InvalidStateError("案件尚未进入可奖励阶段")
        if not case["rule_version"]:
            raise InvalidStateError("案件尚未锁定规则版本")
        rule = self._rule_by_version(case["rule_version"])
        severity = self._severity_of(case["violation_category"])

        made = []
        for alias in case["aliases"]:
            report = self.reports[alias]
            if report["contribution"] not in (CONTRIBUTION_FIRST, CONTRIBUTION_KEY):
                continue
            if report["withdrawn"]:
                continue
            if self._open_decision_for(alias, case_id) is not None:
                continue  # 已有在途或生效决定，不重复建议
            amount = self.compute_amount(
                rule, report["grade"], case["penalty_amount"],
                report["is_insider"], severity)
            needs_cosign = amount >= rule["cosign_threshold"]
            decision = {
                "decision_id": self._next_id("D", "_decision_seq"),
                "kind": "initial",
                "case_id": case_id,
                "alias": alias,
                "grade": report["grade"],
                "contribution": report["contribution"],
                "is_insider": report["is_insider"],
                "penalty_amount": case["penalty_amount"],
                "severity": severity,
                "rule_version": rule["version"],
                "amount": amount,
                "needs_cosign": needs_cosign,
                "status": DECISION_PROPOSED,
                "proposed_by": handler_id,
                "reviewed_by": None,
                "cosigned_by": None,
                "created_at": self._today(),
                "superseded_by": None,  # 被追加决定取代时记录，但原决定保留
            }
            self.decisions[decision["decision_id"]] = decision
            made.append(decision["decision_id"])
            self._log("奖励建议", alias=alias, case_id=case_id,
                      decision_id=decision["decision_id"], amount=amount)
        if not made:
            raise DomainError("本案没有具备奖励资格的举报")
        return made

    def _open_decision_for(self, alias, case_id):
        for d in self.decisions.values():
            if (d["alias"] == alias and d["case_id"] == case_id
                    and d["status"] != DECISION_REJECTED):
                return d
        return None

    def _rule_by_version(self, version):
        for rule in self.rules:
            if rule["version"] == version:
                return rule
        raise NotFoundError(f"规则版本不存在：{version}")

    # ------------------------------------------------------------------
    # 5. 审批与会签（职责分离）
    # ------------------------------------------------------------------

    def approve_decision(self, decision_id, reviewer_id, actor_role, approve=True):
        """奖励审核。审核人不得是建议人本人。"""
        if actor_role != ROLE_REVIEWER:
            raise PermissionDenied("只有奖励审核人可以审核")
        d = self._decision(decision_id)
        if d["status"] != DECISION_PROPOSED:
            raise InvalidStateError("该决定不在待审核状态")
        if self.reports[d["alias"]]["withdrawn"]:
            raise InvalidStateError("举报人已撤回，该建议不能再生效")
        if reviewer_id == d["proposed_by"]:
            raise PermissionDenied("承办人不得批准自己提出的奖励建议")
        if not approve:
            d["status"] = DECISION_REJECTED
            d["reviewed_by"] = reviewer_id
            self._log("建议驳回", alias=d["alias"], decision_id=decision_id)
            return d["status"]
        d["reviewed_by"] = reviewer_id
        if d["needs_cosign"]:
            d["status"] = DECISION_PENDING_COSIGN
        else:
            d["status"] = DECISION_EFFECTIVE
        self._log("建议审核通过", alias=d["alias"], decision_id=decision_id,
                  status=d["status"])
        return d["status"]

    def cosign_decision(self, decision_id, finance_id, actor_role, agree=True):
        """财政会签：二十万元以上的决定必须经此环节。"""
        if actor_role != ROLE_FINANCE:
            raise PermissionDenied("只有财政会签人可以会签")
        d = self._decision(decision_id)
        if d["status"] != DECISION_PENDING_COSIGN:
            raise InvalidStateError("该决定不在待财政会签状态")
        if finance_id in (d["proposed_by"], d["reviewed_by"]):
            raise PermissionDenied("会签人不得与建议人、审核人为同一人")
        if not agree:
            d["status"] = DECISION_REJECTED
            d["cosigned_by"] = finance_id
            self._log("会签退回", alias=d["alias"], decision_id=decision_id)
            return d["status"]
        d["cosigned_by"] = finance_id
        d["status"] = DECISION_EFFECTIVE
        self._log("会签通过", alias=d["alias"], decision_id=decision_id)
        return d["status"]

    # ------------------------------------------------------------------
    # 6. 支付
    # ------------------------------------------------------------------

    def pay_decision(self, decision_id, payer_id, actor_role, amount=None,
                     claim_code=None, paid_at=None, request_id=None):
        """对已生效决定执行支付。

        - 支付上限 = 生效应得额（含已生效追加决定）− 实付净额；
        - 匿名举报须出示领取码；支付记录只写别名，不写身份；
        - 提供 request_id 时，同一请求重放返回首次支付记录，不重复入账。
        """
        if actor_role != ROLE_PAYER:
            raise PermissionDenied("只有支付执行人可以登记支付")
        d = self._decision(decision_id)
        if request_id is not None:
            for p in self.payments:
                if p.get("request_id") == request_id:
                    return p  # 请求重放：同一请求只产生一次结果
        if d["status"] != DECISION_EFFECTIVE:
            raise InvalidStateError("只有已生效的决定可以支付")
        report = self.reports[d["alias"]]
        if report["withdrawn"]:
            raise InvalidStateError("举报人已撤回，不得支付")
        if self._inflight_adjustment(d):
            raise InvalidStateError("存在在途追加决定，待办结后再支付")
        remaining = self.decision_balance(decision_id)["payable_amount"]
        if report["is_anonymous"]:
            if not claim_code:
                raise PermissionDenied("匿名举报须凭领取码支付")
            digest = hashlib.sha256(claim_code.encode()).hexdigest()
            if digest != report["claim_code_hash"]:
                raise PermissionDenied("领取码校验失败")
        pay_amount = _round_yuan(amount if amount is not None else remaining)
        if pay_amount <= 0 or pay_amount > remaining:
            raise DomainError(f"可支付余额为 {max(remaining, 0)} 元")
        record = {
            "payment_id": f"P-{len(self.payments) + 1:04d}",
            "decision_id": decision_id,
            "alias": d["alias"],
            "case_id": d["case_id"],
            "amount": pay_amount,
            "paid_by": payer_id,
            "paid_at": paid_at or self._today(),
            "note": "支付",
            "request_id": request_id,
        }
        self.payments.append(record)
        self._log("奖励支付", alias=d["alias"], case_id=d["case_id"],
                  decision_id=decision_id, amount=pay_amount)
        return record

    def _paid_amount(self, decision_id):
        """某决定的实付净额（支付 − 追回）。"""
        return sum(p["amount"] for p in self.payments
                   if p["decision_id"] == decision_id)

    def decision_balance(self, decision_id):
        """某决定的结算余额：应得 / 已付 / 追回 / 实付净额 / 待付。

        已付只统计真实支付（正向记录），追回单列（负向记录的绝对值），
        待付 = 应得 − 实付净额，且只在决定已生效时才有待付。
        """
        d = self._decision(decision_id)
        tail = self._tail_adjustment(d)
        effective = tail["new_amount"] if tail else d["amount"]
        paid = 0
        clawed = 0
        for p in self.payments:
            if p["decision_id"] != decision_id:
                continue
            if p["amount"] >= 0:
                paid += p["amount"]
            else:
                clawed -= p["amount"]
        net = paid - clawed
        payable = effective - net if d["status"] == DECISION_EFFECTIVE else 0
        return {
            "effective_amount": effective,
            "paid_total": paid,
            "clawed_back_total": clawed,
            "net_paid": net,
            "payable_amount": max(payable, 0),
        }

    # ------------------------------------------------------------------
    # 7. 追加决定（撤回 / 重复 / 复议 / 判决），旧结论保留
    # ------------------------------------------------------------------

    def withdraw_report(self, alias, actor_id, actor_role, withdrawn_at=None):
        """举报人撤回：标记线索，并对已有决定生成调减为 0 的追加决定。

        先校验全部生效决定都不存在在途追加决定，再统一标记撤回，
        避免部分决定挂上调整、部分遗漏的中间态。
        """
        if actor_role != ROLE_INTAKE:
            raise PermissionDenied("撤回登记由举报中心办理")
        report = self._report(alias)
        if report["withdrawn"]:
            raise InvalidStateError("该举报已撤回")
        effective = [d for d in self.decisions.values()
                     if d["alias"] == alias and d["status"] == DECISION_EFFECTIVE]
        for d in effective:
            if self._inflight_adjustment(d):
                raise InvalidStateError("存在在途追加决定，须先办结才能登记撤回")
        report["withdrawn"] = True
        self._log("举报撤回", alias=alias, case_id=report["case_id"])
        made = []
        for d in self.decisions.values():
            if d["alias"] != alias:
                continue
            if d["status"] in (DECISION_PROPOSED, DECISION_PENDING_COSIGN):
                # 在途建议因撤回而终止，无需支付，也不产生追加决定
                d["status"] = DECISION_REJECTED
                self._log("建议因撤回终止", alias=alias,
                          decision_id=d["decision_id"])
            elif d["status"] == DECISION_EFFECTIVE:
                made.append(self._make_adjustment(
                    d, "withdrawal", 0, "举报人撤回，奖励调减为 0",
                    proposed_by=actor_id, changed_at=withdrawn_at))
        return made

    def adjust_decision(self, decision_id, kind, actor_id, actor_role,
                        new_penalty_amount=None, reason="", changed_at=None,
                        request_id=None):
        """对生效决定作出追加决定（复议、判决变化、重复确认等）。

        - 重算一律使用原决定锁定的规则版本，保证跨生效日的一致性；
        - 原决定不被修改，只标记 superseded_by；
        - 追加决定同样走审核/会签流程；
        - 提供 request_id 时，同一请求重放返回首次的追加决定，不重复立案。
        """
        if kind not in ADJUST_KINDS:
            raise DomainError(f"不支持的调整类型：{kind}")
        if actor_role != ROLE_HANDLER:
            raise PermissionDenied("追加决定由案件承办人发起")
        d = self._decision(decision_id)
        if request_id is not None:
            for a in self.adjustments.values():
                if a.get("request_id") == request_id:
                    return a["adjustment_id"]  # 请求重放：只产生一次结果
        if d["status"] != DECISION_EFFECTIVE:
            raise InvalidStateError("只有已生效的决定可以调整")
        if self.reports[d["alias"]]["withdrawn"]:
            raise InvalidStateError("举报已撤回，不再产生新的追加决定")
        if self._inflight_adjustment(d):
            raise InvalidStateError("该决定已有在途追加决定，须先办结")
        tail = self._tail_adjustment(d)
        base_amount = tail["new_amount"] if tail is not None else d["amount"]

        if kind == "duplicate":
            new_amount, note = 0, "确认为重复举报，奖励调减为 0"
        elif kind in ("reconsideration", "judgment"):
            if new_penalty_amount is None:
                raise DomainError("复议或判决变化必须提供新的罚没款金额")
            if new_penalty_amount < 0:
                raise DomainError("罚没款金额必须是不小于 0 的数字")
            rule = self._rule_by_version(d["rule_version"])  # 关键：用旧规则重算
            new_amount = self.compute_amount(
                rule, d["grade"], _round_yuan(new_penalty_amount),
                d["is_insider"], d["severity"])
            note = (f"{ADJUST_KIND_LABELS[kind]}：罚没款调整为 "
                    f"{_round_yuan(new_penalty_amount)} 元，"
                    f"按 {rule['version']} 规则重算")
        else:  # withdrawal 一般由 withdraw_report 触发，这里兜底
            new_amount, note = 0, "举报人撤回，奖励调减为 0"
        if reason:
            note = f"{note}；{reason}" if note else reason
        return self._make_adjustment(d, kind, new_amount, note,
                                     proposed_by=actor_id, changed_at=changed_at,
                                     base_amount=base_amount,
                                     request_id=request_id)

    def _make_adjustment(self, decision, kind, new_amount, note,
                         proposed_by, changed_at=None, base_amount=None,
                         request_id=None):
        rule = self._rule_by_version(decision["rule_version"])
        if self._inflight_adjustment(decision):
            raise InvalidStateError("该决定已有在途追加决定，须先办结")
        chain = self._adjustment_chain(decision)
        if base_amount is None:
            tail = self._tail_adjustment(decision)
            base_amount = tail["new_amount"] if tail else decision["amount"]
        # 追加决定链：指向上一节点（原决定或上一道追加决定），形成完整沿革
        prev_id = chain[-1]["adjustment_id"] if chain else None
        adj = {
            "adjustment_id": self._next_id("ADJ", "_adjust_seq"),
            "kind": kind,
            "kind_label": ADJUST_KIND_LABELS[kind],
            "decision_id": decision["decision_id"],
            "prev_adjustment_id": prev_id,
            "case_id": decision["case_id"],
            "alias": decision["alias"],
            "old_amount": base_amount,
            "new_amount": new_amount,
            "delta": new_amount - base_amount,
            "rule_version": decision["rule_version"],
            "needs_cosign": new_amount >= rule["cosign_threshold"],
            "status": ADJUST_PENDING_REVIEW,
            "reason": note,
            "proposed_by": proposed_by,
            "reviewed_by": None,
            "cosigned_by": None,
            "created_at": changed_at or self._today(),
            "request_id": request_id,
            "settlement": None,  # 生效时写入结算结果，重放直接返回
        }
        self.adjustments[adj["adjustment_id"]] = adj
        # 只在首次被调整时留痕；原决定记录始终保留不删改
        if decision.get("superseded_by") is None:
            decision["superseded_by"] = adj["adjustment_id"]
        self._log("追加决定", alias=decision["alias"], case_id=decision["case_id"],
                  adjustment_id=adj["adjustment_id"], kind=kind,
                  old_amount=base_amount, new_amount=new_amount)
        return adj["adjustment_id"]

    def _adjustment_chain(self, decision):
        """按链路顺序返回某决定的全部追加决定（含在途与驳回）。"""
        nodes = [a for a in self.adjustments.values()
                 if a["decision_id"] == decision["decision_id"]]
        by_id = {a["adjustment_id"]: a for a in nodes}
        heads = [a for a in nodes if not a["prev_adjustment_id"]]
        ordered = []

        def walk(adj):
            ordered.append(adj)
            children = [a for a in nodes
                        if a["prev_adjustment_id"] == adj["adjustment_id"]]
            for child in sorted(children, key=lambda a: a["adjustment_id"]):
                walk(child)
        for head in sorted(heads, key=lambda a: a["adjustment_id"]):
            walk(head)
        return ordered

    def _tail_adjustment(self, decision):
        """链上最后一个“已生效”的追加决定；没有则返回 None。"""
        tail = None
        for adj in self._adjustment_chain(decision):
            if adj["status"] == ADJUST_EFFECTIVE:
                tail = adj
        return tail

    def _inflight_adjustment(self, decision):
        """该决定是否存在在途（待审核/待会签）的追加决定。"""
        return any(
            a["status"] in (ADJUST_PENDING_REVIEW, ADJUST_PENDING_COSIGN)
            for a in self._adjustment_chain(decision))

    def review_adjustment(self, adjustment_id, reviewer_id, actor_role, approve=True):
        if actor_role != ROLE_REVIEWER:
            raise PermissionDenied("只有奖励审核人可以审核追加决定")
        adj = self._adjustment(adjustment_id)
        if adj["status"] != ADJUST_PENDING_REVIEW:
            raise InvalidStateError("该追加决定不在待审核状态")
        if reviewer_id == adj["proposed_by"]:
            raise PermissionDenied("承办人不得批准自己提出的追加决定")
        if not approve:
            adj["status"] = ADJUST_REJECTED
            adj["reviewed_by"] = reviewer_id
            self._on_adjustment_rejected(adj)
            self._log("追加决定驳回", alias=adj["alias"],
                      adjustment_id=adjustment_id)
            return adj["status"]
        adj["reviewed_by"] = reviewer_id
        if adj["needs_cosign"]:
            adj["status"] = ADJUST_PENDING_COSIGN
        else:
            self._effect_adjustment(adj)
        self._log("追加决定审核通过", alias=adj["alias"],
                  adjustment_id=adjustment_id, status=adj["status"])
        return adj["status"]

    def cosign_adjustment(self, adjustment_id, finance_id, actor_role, agree=True):
        if actor_role != ROLE_FINANCE:
            raise PermissionDenied("只有财政会签人可以会签")
        adj = self._adjustment(adjustment_id)
        if adj["status"] != ADJUST_PENDING_COSIGN:
            raise InvalidStateError("该追加决定不在待财政会签状态")
        if finance_id in (adj["proposed_by"], adj["reviewed_by"]):
            raise PermissionDenied("会签人不得与建议人、审核人为同一人")
        if not agree:
            adj["status"] = ADJUST_REJECTED
            adj["cosigned_by"] = finance_id
            self._on_adjustment_rejected(adj)
            self._log("追加决定会签退回", alias=adj["alias"],
                      adjustment_id=adjustment_id)
            return adj["status"]
        adj["cosigned_by"] = finance_id
        self._effect_adjustment(adj)
        self._log("追加决定会签通过", alias=adj["alias"],
                  adjustment_id=adjustment_id)
        return adj["status"]

    def _on_adjustment_rejected(self, adj):
        """追加决定被驳回：旧结论维持；撤回登记随之撤销，举报恢复有效。"""
        if adj["kind"] == "withdrawal":
            report = self.reports[adj["alias"]]
            if report["withdrawn"]:
                report["withdrawn"] = False
                self._log("撤回未获认可，举报恢复有效", alias=adj["alias"],
                          case_id=adj["case_id"])

    def _effect_adjustment(self, adj):
        """追加决定生效：状态流转 + 资金结算（幂等，重放不产生二次结果）。"""
        if adj["status"] == ADJUST_EFFECTIVE:
            return adj["settlement"]
        adj["status"] = ADJUST_EFFECTIVE
        report = self.reports[adj["alias"]]
        if adj["kind"] == "withdrawal":
            report["withdrawn"] = True
        elif adj["kind"] == "duplicate":
            # 重复确认生效后，举报人不再具备奖励资格
            report["contribution"] = CONTRIBUTION_DUPLICATE
            report["grade"] = None
            if not report["duplicate_of"]:
                report["duplicate_of"] = self._first_valid_alias(
                    self.cases[adj["case_id"]], exclude=adj["alias"])
        return self._settle_adjustment(adj)

    def _settle_adjustment(self, adj):
        """追加决定生效时的资金结算（幂等）。

        结算边界：
        - 应得额已按锁定规则重算（new_amount）；
        - 实付净额超过新应得额的部分登记追回（负向资金记录），
          追回以真实已付为限；
        - 未付差额只取消待付，不生成负支付；
        - 应得调增部分转为待付，由支付执行人经正常支付流程发放，
          系统不替支付执行人把未付款记成已付。
        结算结果保存在追加决定上，重复生效、请求重放或失败恢复
        都直接返回既有结果，不会重复补付或追回。
        """
        if adj["settlement"] is not None:
            return adj["settlement"]
        net_paid = self._paid_amount(adj["decision_id"])
        payable_before = max(0, adj["old_amount"] - net_paid)
        clawback = max(0, net_paid - adj["new_amount"])
        if clawback > 0:
            self.payments.append({
                "payment_id": f"P-{len(self.payments) + 1:04d}",
                "decision_id": adj["decision_id"],
                "adjustment_id": adj["adjustment_id"],
                "alias": adj["alias"],
                "case_id": adj["case_id"],
                "amount": -clawback,
                "paid_by": "system-adjustment",
                "paid_at": self._today(),
                "note": "追回",
            })
            self._log("奖励追回", alias=adj["alias"], case_id=adj["case_id"],
                      adjustment_id=adj["adjustment_id"], amount=clawback)
        net_after = net_paid - clawback
        payable_after = max(0, adj["new_amount"] - net_after)
        adj["settlement"] = {
            "effective_amount": adj["new_amount"],
            "clawed_back": clawback,
            "cancelled_payable": max(0, payable_before - payable_after),
            "net_paid": net_after,
            "payable_amount": payable_after,
        }
        return adj["settlement"]

    # ------------------------------------------------------------------
    # 8. 精神奖励（与物质奖励并行）
    # ------------------------------------------------------------------

    def grant_commendation(self, alias, level, reason, actor_id, actor_role):
        """颁发精神奖励（通报表扬/荣誉证书/锦旗），与物质奖励互不影响。"""
        if actor_role != ROLE_INTAKE:
            raise PermissionDenied("精神奖励由举报中心统一登记")
        report = self._report(alias)
        if level not in ("通报表扬", "荣誉证书", "锦旗"):
            raise DomainError("精神奖励等级须为：通报表扬/荣誉证书/锦旗")
        commend = {
            "commendation_id": self._next_id("CMT", "_commend_seq"),
            "alias": alias,
            "case_id": report["case_id"],
            "level": level,
            "reason": reason,
            "granted_by": actor_id,
            "granted_at": self._today(),
        }
        self.commendations[commend["commendation_id"]] = commend
        self._log("精神奖励", alias=alias, case_id=report["case_id"], level=level)
        return commend

    # ------------------------------------------------------------------
    # 9. 逐人说明视图（资格 / 待办审批 / 实际支付）
    # ------------------------------------------------------------------

    def explain_case(self, case_id):
        """按别名逐人说明：资格、奖励结论、待办审批、实际支付。"""
        case = self._case(case_id)
        people = []
        ordered_aliases = sorted(
            case["aliases"],
            key=lambda al: (self.reports[al]["received_at"], al))
        for alias in ordered_aliases:
            report = self.reports[alias]
            people.append(self._explain_person(case, report))
        return {
            "case_id": case_id,
            "violation_category": case["violation_category"],
            "status": case["status"],
            "penalty_amount": case["penalty_amount"],
            "rule_version": case["rule_version"],
            "reporters": people,
        }

    def _explain_person(self, case, report):
        alias = report["alias"]
        eligible = (report["contribution"] in (CONTRIBUTION_FIRST, CONTRIBUTION_KEY)
                    and not report["withdrawn"])
        if report["withdrawn"]:
            eligibility = "已撤回，不具备奖励资格"
        elif report["contribution"] == CONTRIBUTION_DUPLICATE:
            eligibility = "重复举报，不予物质奖励"
        elif report["contribution"] in (CONTRIBUTION_FIRST, CONTRIBUTION_KEY):
            eligibility = f"具备奖励资格（{report['contribution']}）"
        elif report["contribution"] is None:
            eligibility = "尚未认定"
        else:
            eligibility = "补充贡献，不单独奖励"

        decisions = [d for d in self.decisions.values()
                     if d["alias"] == alias and d["case_id"] == case["case_id"]]
        decisions.sort(key=lambda d: d["decision_id"])
        current = next((d for d in decisions if d["status"] != DECISION_REJECTED),
                       None)

        chain = self._adjustment_chain(current) if current else []
        adjustments = list(chain)

        pending = []
        for d in decisions:
            if d["status"] in (DECISION_PROPOSED, DECISION_PENDING_COSIGN):
                pending.append({"item": d["decision_id"],
                                "stage": PENDING_LABELS[d["status"]],
                                "proposed_amount": d["amount"]})
        for a in adjustments:
            if a["status"] in (ADJUST_PENDING_REVIEW, ADJUST_PENDING_COSIGN):
                pending.append({"item": a["adjustment_id"],
                                "stage": PENDING_LABELS[a["status"]],
                                "proposed_amount": a["new_amount"]})

        # 资金台账分列：应得 / 已付 / 追回 / 待付 / 仍待处理，互不混记
        if current is not None:
            balance = self.decision_balance(current["decision_id"])
            effective_amount = balance["effective_amount"]
        else:
            balance = {"paid_total": 0, "clawed_back_total": 0,
                       "net_paid": 0, "payable_amount": 0}
            effective_amount = None
        history = sorted(
            (p for p in self.payments
             if p["alias"] == alias and p["case_id"] == case["case_id"]),
            key=lambda p: p["payment_id"])
        commendations = [c for c in self.commendations.values()
                         if c["alias"] == alias]

        return {
            "alias": alias,
            "received_at": report["received_at"],
            "contribution": report["contribution"],
            "grade": report["grade"],
            "grade_label": GRADE_LABELS.get(report["grade"]),
            "eligibility": eligibility,
            "eligible": eligible,
            "current_decision": (
                None if current is None else {
                    "decision_id": current["decision_id"],
                    "amount": current["amount"],
                    "status": current["status"],
                    "rule_version": current["rule_version"],
                }),
            "effective_amount": effective_amount,
            "paid_total": balance["paid_total"],
            "clawed_back_total": balance["clawed_back_total"],
            "net_paid": balance["net_paid"],
            "payable_amount": balance["payable_amount"],
            "pending_amount": sum(item["proposed_amount"] for item in pending),
            "adjustments": [{
                "adjustment_id": a["adjustment_id"],
                "kind_label": a["kind_label"],
                "old_amount": a["old_amount"],
                "new_amount": a["new_amount"],
                "status": a["status"],
                "reason": a["reason"],
                "settlement": a["settlement"],
            } for a in adjustments],
            "pending_approvals": pending,
            "payment_history": [{
                "payment_id": p["payment_id"],
                "amount": p["amount"],
                "kind": p.get("note") or ("支付" if p["amount"] > 0 else "追回"),
                "paid_by": p["paid_by"],
                "paid_at": p["paid_at"],
                "adjustment_id": p.get("adjustment_id"),
            } for p in history],
            "commendations": [
                {"level": c["level"], "granted_at": c["granted_at"]}
                for c in commendations],
        }

    # ------------------------------------------------------------------
    # 10. 对外视图：承办人 / 对外材料 / 普通日志均不含身份
    # ------------------------------------------------------------------

    def case_file_for_handler(self, case_id, actor_id, actor_role):
        """承办人办案视图：只有别名与办案所需信息。"""
        if actor_role not in (ROLE_HANDLER, ROLE_REVIEWER, ROLE_FINANCE,
                              ROLE_PAYER, ROLE_INTAKE, ROLE_AUDITOR):
            raise PermissionDenied("无权查看案件材料")
        case = self._case(case_id)
        reports = []
        for alias in case["aliases"]:
            r = self.reports[alias]
            reports.append({
                "alias": alias,
                "violation_category": r["violation_category"],
                "facts": list(r["facts"]),
                "supplements": [dict(s) for s in r["supplements"]],
                "received_at": r["received_at"],
                "contribution": r["contribution"],
                "grade": r["grade"],
                "withdrawn": r["withdrawn"],
            })
        return {
            "case_id": case_id,
            "violation_category": case["violation_category"],
            "status": case["status"],
            "penalty_amount": case["penalty_amount"],
            "rule_version": case["rule_version"],
            "reports": reports,
        }

    def public_case_material(self, case_id):
        """对外材料：只含公开字段，举报人以别名+贡献标注出现。"""
        case = self._case(case_id)
        return {
            "case_id": case["case_id"],
            "violation_category": case["violation_category"],
            "status": case["status"],
            "reports": [{
                "alias": self.reports[al]["alias"],
                "contribution": self.reports[al]["contribution"],
            } for al in case["aliases"]],
        }

    def ordinary_case_log(self, case_id):
        """普通办案日志（只含别名级条目）。"""
        return [dict(e) for e in self.event_log
                if e.get("case_id") == case_id]

    def reveal_identity(self, alias, actor_id, actor_role, reason):
        """查看身份的唯一入口：受角色限制且全程留痕。"""
        identity = self.vault.reveal(alias, actor_id, actor_role, reason)
        return identity

    def identity_access_log(self, actor_role):
        if actor_role != ROLE_AUDITOR:
            raise PermissionDenied("只有审计查看人可以查阅身份访问台账")
        return [dict(e) for e in self.vault.access_log]

    # ------------------------------------------------------------------
    # 内部取数
    # ------------------------------------------------------------------

    def _report(self, alias):
        try:
            return self.reports[alias]
        except KeyError:
            raise NotFoundError(f"举报不存在：{alias}")

    def _case(self, case_id):
        try:
            return self.cases[case_id]
        except KeyError:
            raise NotFoundError(f"案件不存在：{case_id}")

    def _decision(self, decision_id):
        try:
            return self.decisions[decision_id]
        except KeyError:
            raise NotFoundError(f"奖励决定不存在：{decision_id}")

    def _adjustment(self, adjustment_id):
        try:
            return self.adjustments[adjustment_id]
        except KeyError:
            raise NotFoundError(f"追加决定不存在：{adjustment_id}")


def basic_check():
    """供 service.py --check 使用的基础自检。"""
    center = RewardCenter(today=lambda: "2026-09-22")
    assert center.rule_version_on("2026-09-22")["version"] == "2026-01"
    assert center.rule_version_on("2025-12-31")["version"] == "2023-01"
    rule = center.rule_version_on("2026-09-22")
    # 百万封顶
    assert center.compute_amount(rule, 1, 100_000_000, False, 1.0) == 1_000_000
    # 内部举报加成
    assert center.compute_amount(rule, 1, 100_000, True, 1.0) == 9_000
    # 无罚没款定额
    assert center.compute_amount(rule, 2, 0, False, 1.0) == 5_000
    return True
