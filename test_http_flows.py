"""HTTP 端到端：三类业务情形走完整接口，并验证响应不泄露身份。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import service
from service import Handler


def actor(actor_id, role):
    return {"id": actor_id, "role": role}


class HttpFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        service.reset_center()

    def post(self, path, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(f"{self.base_url}{path}", data=data, method="POST",
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def get(self, path, **query):
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        with urlopen(url, timeout=3) as resp:
            return resp.status, json.load(resp)

    # ------------------------------------------------------------------

    def test_three_reporters_flow_over_http(self):
        def report(facts, day):
            status, body = self.post("/reports/intake", {
                "actor": actor("intake-1", "intake_officer"),
                "violation_category": "食品药品安全",
                "facts": facts, "received_at": day,
                "identity": {"name": "真实姓名-不应出现在任何响应里"}})
            self.assertEqual(status, 201)
            return body["alias"], body["case_id"]

        a1, case_id = report(["事实A"], "2026-02-01")
        a2, c2 = report(["事实A"], "2026-02-03")
        a3, c3 = report(["事实B"], "2026-02-10")
        self.assertEqual((c2, c3), (case_id, case_id))

        self.post("/cases/close", {
            "case_id": case_id, "penalty_amount": 5_000_000,
            "at": "2026-03-01"})
        _, stage = self.post("/cases/reward-stage", {
            "case_id": case_id, "at": "2026-03-05"})
        self.assertEqual(stage["rule_version"], "2026-01")

        _, assessed = self.post("/cases/assess", {
            "actor": actor("intake-1", "intake_officer"),
            "case_id": case_id,
            "assessments": [
                {"alias": a1, "grade": 1, "new_facts": ["事实A"]},
                {"alias": a2, "grade": 1, "duplicate": True},
                {"alias": a3, "grade": 2, "new_facts": ["事实B"],
                 "key_contribution": True},
            ]})
        self.assertEqual(assessed["contributions"][a1], "最先有效贡献")
        self.assertEqual(assessed["contributions"][a2], "重复举报")
        self.assertEqual(assessed["contributions"][a3], "独立关键贡献")

        _, proposed = self.post("/rewards/propose", {
            "actor": actor("handler-1", "case_handler"), "case_id": case_id})
        self.assertEqual(len(proposed["decision_ids"]), 2)

        # 承办人批准自己的建议：403
        status_self, _ = self.post("/rewards/approve", {
            "actor": actor("handler-1", "reward_reviewer"),
            "decision_id": proposed["decision_ids"][0]})
        self.assertEqual(status_self, 403)

        for did in proposed["decision_ids"]:
            status, body = self.post("/rewards/approve", {
                "actor": actor("reviewer-1", "reward_reviewer"),
                "decision_id": did})
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "待财政会签")
            status, body = self.post("/rewards/cosign", {
                "actor": actor("finance-1", "finance_cosigner"),
                "decision_id": did})
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "已生效")
            self.post("/rewards/pay", {
                "actor": actor("payer-1", "payment_officer"),
                "decision_id": did})

        _, explain = self.get(f"/cases/{case_id}/explain")
        by_alias = {r["alias"]: r for r in explain["reporters"]}
        self.assertEqual(by_alias[a1]["paid_total"], 300_000)
        self.assertEqual(by_alias[a3]["paid_total"], 200_000)
        self.assertFalse(by_alias[a2]["eligible"])
        self.assertEqual(by_alias[a2]["paid_total"], 0)

        # 所有对外/办案视图都不得出现真实姓名
        _, case_file = self.get(f"/cases/{case_id}/file",
                                actor="h-1", role="case_handler")
        _, public = self.get(f"/cases/{case_id}/public")
        _, log = self.get(f"/cases/{case_id}/log")
        for view in (explain, case_file, public, log):
            self.assertNotIn("真实姓名", json.dumps(view, ensure_ascii=False))

        # 承办人无权查看身份（403），受理员查看留痕
        status_forbidden, _ = self.post("/identity/reveal", {
            "actor": actor("h-1", "case_handler"), "alias": a1,
            "reason": "想看看"})
        self.assertEqual(status_forbidden, 403)
        status_ok, revealed = self.post("/identity/reveal", {
            "actor": actor("intake-1", "intake_officer"), "alias": a1,
            "reason": "核实联系方式"})
        self.assertEqual(status_ok, 200)
        self.assertIn("identity", revealed)
        _, access = self.get("/identity/access-log", role="audit_viewer")
        self.assertEqual(len(access["events"]), 1)

    def test_no_penalty_anonymous_flow(self):
        status, body = self.post("/reports/intake", {
            "actor": actor("intake-1", "intake_officer"),
            "violation_category": "广告违法",
            "facts": ["发布违法医疗广告"], "received_at": "2026-04-01"})
        self.assertEqual(status, 201)
        alias, case_id, code = body["alias"], body["case_id"], body["claim_code"]
        self.assertTrue(body["hint"])

        self.post("/cases/close", {
            "case_id": case_id, "penalty_amount": 0, "at": "2026-05-01"})
        self.post("/cases/reward-stage", {
            "case_id": case_id, "at": "2026-05-05"})
        self.post("/cases/assess", {
            "actor": actor("intake-1", "intake_officer"),
            "case_id": case_id,
            "assessments": [
                {"alias": alias, "grade": 3,
                 "new_facts": ["发布违法医疗广告"]}]})
        _, proposed = self.post("/rewards/propose", {
            "actor": actor("handler-1", "case_handler"), "case_id": case_id})
        did = proposed["decision_ids"][0]
        self.post("/rewards/approve", {
            "actor": actor("reviewer-1", "reward_reviewer"),
            "decision_id": did})

        # 无领取码 / 错码：403
        self.assertEqual(self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did})[0], 403)
        self.assertEqual(self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "claim_code": "deadbeef"})[0], 403)
        status, paid = self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "claim_code": code})
        self.assertEqual(status, 200)
        self.assertEqual(paid["amount"], 3000)  # 2026 版三级无罚没款定额

    def test_cross_effective_date_reconsideration(self):
        _, body = self.post("/reports/intake", {
            "actor": actor("intake-1", "intake_officer"),
            "violation_category": "食品药品安全",
            "facts": ["事实A"], "received_at": "2025-11-01",
            "identity": {"name": "王五-不外露"}})
        alias, case_id = body["alias"], body["case_id"]
        self.post("/cases/close", {
            "case_id": case_id, "penalty_amount": 5_000_000,
            "at": "2025-12-10"})
        self.post("/cases/reward-stage", {
            "case_id": case_id, "at": "2025-12-15"})
        self.post("/cases/assess", {
            "actor": actor("intake-1", "intake_officer"),
            "case_id": case_id,
            "assessments": [{"alias": alias, "grade": 1,
                             "new_facts": ["事实A"]}]})
        _, proposed = self.post("/rewards/propose", {
            "actor": actor("handler-1", "case_handler"), "case_id": case_id})
        did = proposed["decision_ids"][0]
        self.post("/rewards/approve", {
            "actor": actor("reviewer-1", "reward_reviewer"),
            "decision_id": did})
        self.post("/rewards/cosign", {
            "actor": actor("finance-1", "finance_cosigner"),
            "decision_id": did})
        self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did})

        # 2026 年发起复议，仍按 2023 版规则重算
        status, adj = self.post("/rewards/adjust", {
            "actor": actor("handler-1", "case_handler"),
            "decision_id": did, "kind": "reconsideration",
            "new_penalty_amount": 3_000_000, "at": "2026-03-01"})
        self.assertEqual(status, 201)
        self.assertEqual(adj["old_amount"], 250_000)
        self.assertEqual(adj["new_amount"], 150_000)
        self.assertFalse(adj["needs_cosign"])

        # 追加决定同样禁止自审
        self.assertEqual(self.post("/rewards/adjustment/approve", {
            "actor": actor("handler-1", "reward_reviewer"),
            "adjustment_id": adj["adjustment_id"]})[0], 403)
        status, approved = self.post("/rewards/adjustment/approve", {
            "actor": actor("reviewer-2", "reward_reviewer"),
            "adjustment_id": adj["adjustment_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(approved["status"], "已生效")

        _, explain = self.get(f"/cases/{case_id}/explain")
        person = explain["reporters"][0]
        self.assertEqual(person["effective_amount"], 150_000)
        # 已付 25 万、追回 10 万分列，实付净额 15 万
        self.assertEqual(person["paid_total"], 250_000)
        self.assertEqual(person["clawed_back_total"], 100_000)
        self.assertEqual(person["net_paid"], 150_000)
        self.assertEqual(person["payable_amount"], 0)
        self.assertEqual(person["adjustments"][0]["kind_label"], "行政复议变化")
        # 旧结论保留
        self.assertEqual(person["current_decision"]["amount"], 250_000)

    def test_downward_adjustment_before_payment_cancels_payable_only(self):
        """复议调减时尚未付款：不产生负支付，只取消待付。"""
        _, body = self.post("/reports/intake", {
            "actor": actor("intake-1", "intake_officer"),
            "violation_category": "价格违法",
            "facts": ["事实A"], "received_at": "2026-02-01"})
        alias, case_id = body["alias"], body["case_id"]
        self.post("/cases/close", {
            "case_id": case_id, "penalty_amount": 1_000_000,
            "at": "2026-03-01"})
        self.post("/cases/reward-stage", {
            "case_id": case_id, "at": "2026-03-05"})
        self.post("/cases/assess", {
            "actor": actor("intake-1", "intake_officer"),
            "case_id": case_id,
            "assessments": [{"alias": alias, "grade": 2,
                             "new_facts": ["事实A"]}]})
        _, proposed = self.post("/rewards/propose", {
            "actor": actor("handler-1", "case_handler"), "case_id": case_id})
        did = proposed["decision_ids"][0]
        self.post("/rewards/approve", {
            "actor": actor("reviewer-1", "reward_reviewer"),
            "decision_id": did})
        # 100 万 ×4%×0.7 = 2.8 万生效，尚未支付
        _, adj = self.post("/rewards/adjust", {
            "actor": actor("handler-1", "case_handler"),
            "decision_id": did, "kind": "reconsideration",
            "new_penalty_amount": 100_000})
        status, approved = self.post("/rewards/adjustment/approve", {
            "actor": actor("reviewer-2", "reward_reviewer"),
            "adjustment_id": adj["adjustment_id"]})
        self.assertEqual(status, 200)
        # 10 万 ×4%×0.7 = 2800，低于等级保底 4000 → 新应得 4000
        # 结算：无追回、取消待付 2.4 万、剩余待付 4000
        self.assertEqual(approved["settlement"]["clawed_back"], 0)
        self.assertEqual(approved["settlement"]["cancelled_payable"], 24_000)
        self.assertEqual(approved["settlement"]["payable_amount"], 4_000)

        _, explain = self.get(f"/cases/{case_id}/explain")
        person = explain["reporters"][0]
        self.assertEqual(person["effective_amount"], 4_000)
        self.assertEqual(person["paid_total"], 0)
        self.assertEqual(person["clawed_back_total"], 0)
        self.assertEqual(person["net_paid"], 0)
        self.assertEqual(person["payable_amount"], 4_000)
        # 资金台账中不得出现任何负支付
        self.assertEqual(
            [p for p in person["payment_history"] if p["amount"] < 0], [])

    def test_upward_adjustment_becomes_payable_without_auto_payment(self):
        """复议调增：系统不自动补付，转为待付由支付执行人发放。"""
        _, body = self.post("/reports/intake", {
            "actor": actor("intake-1", "intake_officer"),
            "violation_category": "食品药品安全",
            "facts": ["事实A"], "received_at": "2026-02-01"})
        alias, case_id = body["alias"], body["case_id"]
        self.post("/cases/close", {
            "case_id": case_id, "penalty_amount": 3_000_000,
            "at": "2026-03-01"})
        self.post("/cases/reward-stage", {
            "case_id": case_id, "at": "2026-03-05"})
        self.post("/cases/assess", {
            "actor": actor("intake-1", "intake_officer"),
            "case_id": case_id,
            "assessments": [{"alias": alias, "grade": 1,
                             "new_facts": ["事实A"]}]})
        _, proposed = self.post("/rewards/propose", {
            "actor": actor("handler-1", "case_handler"), "case_id": case_id})
        did = proposed["decision_ids"][0]
        self.post("/rewards/approve", {
            "actor": actor("reviewer-1", "reward_reviewer"),
            "decision_id": did})
        self.post("/rewards/cosign", {
            "actor": actor("finance-1", "finance_cosigner"),
            "decision_id": did})
        # 300 万 ×6% = 18 万，先付 10 万（部分付款）
        status, paid = self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "amount": 100_000,
            "claim_code": body["claim_code"]})
        self.assertEqual(status, 200)
        self.assertEqual(paid["payable_remaining"], 80_000)
        # 复议后罚没款增至 500 万 → 应得 30 万（≥20 万须再走会签）
        _, adj = self.post("/rewards/adjust", {
            "actor": actor("handler-1", "case_handler"),
            "decision_id": did, "kind": "reconsideration",
            "new_penalty_amount": 5_000_000})
        self.assertTrue(adj["needs_cosign"])
        # 待会签期间不得支付
        self.assertEqual(self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did})[0], 409)
        self.post("/rewards/adjustment/approve", {
            "actor": actor("reviewer-2", "reward_reviewer"),
            "adjustment_id": adj["adjustment_id"]})
        # 会签人不得与建议/审核同人
        self.assertEqual(self.post("/rewards/adjustment/cosign", {
            "actor": actor("reviewer-2", "finance_cosigner"),
            "adjustment_id": adj["adjustment_id"]})[0], 403)
        status, cosigned = self.post("/rewards/adjustment/cosign", {
            "actor": actor("finance-2", "finance_cosigner"),
            "adjustment_id": adj["adjustment_id"]})
        self.assertEqual(status, 200)
        # 生效时无自动补付记录：差额 20 万转为待付
        self.assertEqual(cosigned["settlement"]["clawed_back"], 0)
        self.assertEqual(cosigned["settlement"]["payable_amount"], 200_000)
        # 会签请求重放不会二次结算
        replay_status, replay = self.post("/rewards/adjustment/cosign", {
            "actor": actor("finance-2", "finance_cosigner"),
            "adjustment_id": adj["adjustment_id"]})
        self.assertEqual(replay_status, 409)
        _, explain = self.get(f"/cases/{case_id}/explain")
        person = explain["reporters"][0]
        self.assertEqual(person["paid_total"], 100_000)
        self.assertEqual(person["clawed_back_total"], 0)
        self.assertEqual(person["payable_amount"], 200_000)
        # 补付经正常支付流程发放
        _, second_pay = self.post("/rewards/pay", {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "claim_code": body["claim_code"]})
        self.assertEqual(second_pay["amount"], 200_000)
        self.assertEqual(second_pay["payable_remaining"], 0)

    def test_request_replay_only_settles_once(self):
        """同一 request_id 重放：追加决定只立一次、支付只发一次。"""
        _, body = self.post("/reports/intake", {
            "actor": actor("intake-1", "intake_officer"),
            "violation_category": "广告违法",
            "facts": ["事实A"], "received_at": "2026-02-01"})
        alias, case_id = body["alias"], body["case_id"]
        self.post("/cases/close", {
            "case_id": case_id, "penalty_amount": 0, "at": "2026-03-01"})
        self.post("/cases/reward-stage", {"case_id": case_id,
                                          "at": "2026-03-05"})
        self.post("/cases/assess", {
            "actor": actor("intake-1", "intake_officer"),
            "case_id": case_id,
            "assessments": [{"alias": alias, "grade": 3,
                             "new_facts": ["事实A"]}]})
        _, proposed = self.post("/rewards/propose", {
            "actor": actor("handler-1", "case_handler"), "case_id": case_id})
        did = proposed["decision_ids"][0]
        self.post("/rewards/approve", {
            "actor": actor("reviewer-1", "reward_reviewer"),
            "decision_id": did})
        # 支付请求重放：只产生一笔支付
        payload = {
            "actor": actor("payer-1", "payment_officer"),
            "decision_id": did, "request_id": "pay-req-1",
            "claim_code": body["claim_code"]}
        _, first = self.post("/rewards/pay", payload)
        _, replay = self.post("/rewards/pay", payload)
        self.assertEqual(first["payment_id"], replay["payment_id"])
        # 调整请求重放：只产生一个追加决定
        adj_payload = {
            "actor": actor("handler-1", "case_handler"),
            "decision_id": did, "kind": "duplicate",
            "request_id": "adj-req-1"}
        _, adj1 = self.post("/rewards/adjust", adj_payload)
        _, adj2 = self.post("/rewards/adjust", adj_payload)
        self.assertEqual(adj1["adjustment_id"], adj2["adjustment_id"])
        self.post("/rewards/adjustment/approve", {
            "actor": actor("reviewer-2", "reward_reviewer"),
            "adjustment_id": adj1["adjustment_id"]})
        _, explain = self.get(f"/cases/{case_id}/explain")
        person = explain["reporters"][0]
        self.assertEqual(len(person["payment_history"]), 2)  # 1 支付 + 1 追回
        self.assertEqual(person["paid_total"], 3_000)
        self.assertEqual(person["clawed_back_total"], 3_000)
        self.assertEqual(person["payable_amount"], 0)

    def test_unknown_route_and_bad_json(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        req = Request(f"{self.base_url}/reports/intake",
                      data=b"{bad json", method="POST",
                      headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(req, timeout=2)
        self.assertEqual(error.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
