"""Codec tests — قرار S301 (تدقيق 2026-09-25): JSON موقّع ببادئة J1، لا pickle إطلاقًا.

الأدلة الأصلية موثقة في docs/REPO-AUDIT-2026-09-25.md § decision-S301:
الموضع الوحيد للكتابة يخزّن dict من 4 حقول نصية → JSON مباشر بلا غلاف قيم،
وكل بايتات بلا بادئة J1 (بما فيها مخلفات pickle القديمة) = miss لا يُفكّ تسلسلها.
"""
import pickle

from nimna.vision.cache import _dumps, _loads


def test_json_roundtrip_with_j1_framing():
    value = {"a": [1, 2.5, None, True], "b": "نص عربي"}
    raw = _dumps(value)
    assert raw[:2] == b"J1"
    assert _loads(raw) == value


def test_malicious_global_opcode_is_a_miss_never_executed():
    """بايتات تحتوي opcode GLOBAL حرفيًا (posix.system) — لو فُكّ تسلسلها بـpickle
    لشغّلت أمرًا. القراءة الصارمة تعيدها miss بلا تنفيذ ولا استثناء.
    (اختبار أحمر موثق: على الكود القديم كان يُنفَّذ فعلًا — انظر سجل الدفعة 5)."""
    malicious = b"cposix\nsystem\np0\n(Vid\np1\ntp2\nRp3\n."
    assert _loads(malicious) is None


def test_legacy_plain_pickle_is_a_miss_not_unpickled():
    """مخلفات الإنتاج قبل الإصلاح (pickle سليم): تُعامل miss — الاستبدال مقصود،
    والتكلفة صفرًا عمليًا (TTL ≤ 600 ثانية → إعادة وصف واحدة)."""
    assert _loads(pickle.dumps({"old": 1})) == None  # noqa: E711


def test_vision_cache_redis_path_roundtrip_via_stub():
    """تكامل كامل عبر VisionCache بعميل Redis مزيّف: يكتب J1+JSON ويقرأه،
    والبايتات التالفة = miss مع عدّادات سليمة."""
    class _FakeRedis:
        def __init__(self):
            self.store = {}

        def setex(self, k, ttl, raw):
            self.store[k] = raw

        def get(self, k):
            return self.store.get(k)

    from nimna.vision import cache as cache_mod

    vc = cache_mod.VisionCache.__new__(cache_mod.VisionCache)
    vc.ttl = 60
    vc.redis_url = "stub"
    vc._redis = _FakeRedis()
    vc._enabled = True

    value = {"b64": "QUJD", "mime": "image/png", "caption": "شرح", "hash": "abc123"}
    vc.set(b"imgbytes", 800, 600, value)
    assert vc.get(b"imgbytes", 800, 600) == value
    # المخلفات القديمة تُحقن مباشرة في المخزن → miss لا crash
    k = cache_mod._key(b"imgbytes", 800, 600)
    vc._redis.store[k] = pickle.dumps({"legacy": True})
    assert vc.get(b"imgbytes", 800, 600) is None
