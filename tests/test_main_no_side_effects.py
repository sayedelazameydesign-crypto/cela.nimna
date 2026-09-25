"""Regression للخلل P1-1 (تدقيق 2026-09-25): استيراد nimna.__main__ كان ينفّذ الـCLI.

قبل الإصلاح كان `sys.exit(main())` بلا حارس `__name__` — أي استيراد برمجي
(IDE، مولدات توثيق، فاحصات) يقتل العملية بـ SystemExit.
"""
import subprocess
import sys


def test_import_main_does_not_execute_cli():
    """الاستيراد البرمجي يجب ألا يُطلق SystemExit (الخلل P1-1 الأصلي)."""
    import nimna.__main__  # noqa: F401


def test_module_invocation_still_works():
    """python -m nimna يجب أن يعمل بعد الإصلاح."""
    result = subprocess.run(
        [sys.executable, "-m", "nimna", "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
