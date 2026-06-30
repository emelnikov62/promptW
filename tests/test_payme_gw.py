"""Юнит-тесты чистой логики Payme (без БД). Запуск: python tests/test_payme_gw.py"""
import base64, os, sys
os.environ["PAYME_MERCHANT_ID"] = "MID123"
os.environ["PAYME_KEY_TEST"] = "testkey"
os.environ["PAYME_MODE"] = "test"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import payme_gw as p

def test_build_checkout_url():
    url = p.build_checkout_url("ord-1", 16900, "https://t.me/promptW_bot/app", "uz")
    assert url.startswith("https://checkout.paycom.uz/"), url
    raw = base64.b64decode(url.rsplit("/", 1)[1]).decode()
    assert "m=MID123" in raw and "ac.order_id=ord-1" in raw
    assert "a=1690000" in raw                      # 16900 сум -> тийины
    assert "l=uz" in raw

def test_verify_auth_ok_and_fail():
    good = "Basic " + base64.b64encode(b"Paycom:testkey").decode()
    bad  = "Basic " + base64.b64encode(b"Paycom:wrong").decode()
    assert p.verify_auth(good) is True
    assert p.verify_auth(bad) is False
    assert p.verify_auth("") is False

def test_available():
    assert p.payme_available() is True

if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok:", name)
    print("ALL PASS")
