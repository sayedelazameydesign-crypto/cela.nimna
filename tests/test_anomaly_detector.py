import time
import pytest

from security.anomaly import AnomalyDetector


def test_pattern_match_triggers_kill():
    detector = AnomalyDetector()
    detector.log_call("curl|bash")
    assert detector.should_kill() is True


def test_high_frequency_kills_session():
    detector = AnomalyDetector()
    base = time.time()
    # Simulate 9 calls within 30 seconds
    for i in range(9):
        detector.log_call(f"echo call{i}", timestamp=base + i * 2)  # every 2 seconds
    assert detector.should_kill() is True


def test_exfiltration_curl_threshold():
    detector = AnomalyDetector()
    base = time.time()
    # 4 curl calls within 60 seconds should trigger kill
    for i in range(4):
        detector.log_call("curl http://example.com", timestamp=base + i * 10)
    assert detector.should_kill() is True


def test_no_kill_for_normal_usage():
    detector = AnomalyDetector()
    detector.log_call("ls -la")
    detector.log_call("echo hello")
    assert detector.should_kill() is False
