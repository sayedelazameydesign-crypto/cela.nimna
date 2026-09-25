"""Regression — قرار decision-D من تقرير التدقيق 2026-09-25.

السياق: أُزيل مسار t5_policy_adapter من arena suite (هجرة P1-T7 — العبور عبر
gateway واحد)، وحُذف المتغير اليتيم policy_stats من scripts/run_arena_suite.py.
هذا الاختبار يمنع انقراض المقياس: بوابة سياسة T5 يجب أن تُصدر إحصاءات DENY/ALLOW
عند الرفض والسماح — عبر أي مسار يعيد توظيف المحول مستقبلًا.

الأدلة الأصلية: TEST-A/TEST-B موثقتان حرفيًا في
docs/REPO-AUDIT-2026-09-25.md § decision-D.

ملاحظة أمانة: السياسات والكتالوج هنا هما كائنا السويت الحقيقيان (مستوردان من
السكربت نفسه عبر _suite_policy_and_catalog — لا نسخة طبق الأصل). الوصف المسجل
مسبار أدنى يطابق مخطط sandbox.command؛ من يحرق البناء كله يحمّل مسؤولية
إعادة توصيف الاختبار لا حذفه.
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "run_arena_suite.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_arena_suite_gate_stats", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_gate_emits_stats_on_both_paths():
    rs = _load_module()
    from nimna.execution.policy import t5_policy_adapter
    from nimna.execution.tool_registry import (
        AuthorizationDecision,
        InvocationStatus,
        ToolDescriptor,
        ToolRegistry,
        invoke,
    )

    # كائنا السويت الحقيقيان — لا نسخة
    catalog, policy = rs._suite_policy_and_catalog()

    stats: dict = {}
    adapter = t5_policy_adapter(policy, stats=stats)

    registry = ToolRegistry()
    registry.register(ToolDescriptor(
        tool_id="sandbox.command", version="1.0.0",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object",
                       "properties": {"status": {"type": "string"}},
                       "required": ["status"]},
        capabilities=("shell",), side_effects=("process",), risk_level="HIGH",
        handler=lambda args: {"status": "EXECUTED", "exit_code": 0}))

    def _grant(descriptor, arguments):
        return AuthorizationDecision(True, "operator consent")

    # مسار الرفض: مورد خارج الحوزة → القاعدة deny-outside-workspace
    denied = invoke(registry, "sandbox.command", {"resource": "system/etc/passwd"},
                    granted_capabilities=("shell",),
                    capability_resolver=lambda d: catalog.resolve(("shell",)),
                    policy=adapter, authorizer=_grant)
    assert denied.status is InvocationStatus.POLICY_DENIED

    # مسار السماح: مورد داخل الحوزة → القاعدة allow-sandbox-shell
    allowed = invoke(registry, "sandbox.command", {"resource": "workspace"},
                     granted_capabilities=("shell",),
                     capability_resolver=lambda d: catalog.resolve(("shell",)),
                     policy=adapter, authorizer=_grant)
    assert allowed.status is InvocationStatus.EXECUTED

    # المقياس الذي أودى بـ policy_stats: ينبغي أن يبقى حيًا عبر أي هجرة قادمة
    assert stats.get("DENY", 0) >= 1 and stats.get("ALLOW", 0) >= 1
