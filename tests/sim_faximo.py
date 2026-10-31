"""faximoSilver Web-API（REST V2）への送信依頼・結果の問い合わせを、通信をまねて確かめる（実際には送らない）。"""
import base64, json, os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("STORE", "local")
os.environ.setdefault("INVOICE_TRN", "")
os.environ.setdefault("MOCK_RESULT", str(ROOT / "tests" / "fixtures" / "mihon-chintai.json"))
import delivery, usage

delivery.FAXIMO_ID, delivery.FAXIMO_PASSWORD, delivery.FAX_ENABLED = "otowa-test", "pw-test", True
admin = []
usage.notify_admin = lambda subj, text, urgent=True: admin.append(subj)


class R:
    def __init__(self, j, code=200):
        self.status_code, self._j = code, j
    def json(self):
        return self._j


calls = []
queue = []
def fake_post(url, json=None, headers=None, timeout=None):
    calls.append(("POST", url, json, headers))
    return queue.pop(0)
def fake_get(url, params=None, headers=None, timeout=None):
    calls.append(("GET", url, params, headers))
    return queue.pop(0)
delivery.requests.post, delivery.requests.get = fake_post, fake_get

print("=== 送信依頼：日本語ヘッダーで受付 ===")
queue.append(R({"result": "000000", "idxcnt": "0000036273813"}))
pid = delivery._fax_api_send("03-3577-5600", b"%PDF-1.4 test", delivery.fax_sender("サンプル不動産株式会社"))
m, url, body, h = calls[-1]
print(pid, url, body["headerinfo"], body["subject"])
assert pid == "0000036273813" and url == "https://rest.faximo.jp/snd/v2/request.json"
assert body["sendto"] == [{"faxno": "0335775600"}] and body["retrynum"] == 2
assert body["userkey"].isalnum() and len(body["subject"]) <= 50
assert base64.b64decode(body["Attachment"][0]["attachmentdata"]) == b"%PDF-1.4 test"
assert base64.b64decode(h["X-Auth"]).decode() == "otowa-test:pw-test" and len(h["X-Processkey"]) <= 20
assert body["headerinfo"].startswith("サンプル不動産株式会社（1分資料経由）") and len(body["headerinfo"]) <= 80

print("=== 日本語ヘッダーが通らない → 半角ヘッダーで送り直す ===")
queue += [R({"result": "001100"}), R({"result": "000000", "idxcnt": "0000000000002"})]
pid = delivery._fax_api_send("0312345678", b"x", "テスト（1分資料経由）")
print(pid, calls[-1][2]["headerinfo"])
assert pid == "0000000000002" and calls[-1][2]["headerinfo"] == delivery.FAX_HEADER_ASCII and not admin

print("=== 認証エラー → None・運営に知らせる ===")
queue.append(R({"result": "001001"}))
assert delivery._fax_api_send("0312345678", b"x", "a") is None and admin, admin
print(admin[-1]); admin.clear()

print("=== 長い会社名でも80文字に収める ===")
assert len(delivery.fax_header("あ" * 200)) <= 80

print("=== 結果の読み替え ===")
def st(row, result="000000"):
    queue.append(R({"result": result, "count": 1 if row else 0, "SendResultList": [row] if row else []}))
    return delivery._fax_api_status("0000036273813")
assert st({"status": "0"}) == ("sending", "")
assert st(None) == ("sending", "")
assert st({"status": "5", "pagenum": "2"}) == ("sent", "")
assert st({"status": "9202", "statusdetail": "30012"}) == ("failed", "busy")
assert st({"status": "9202", "statusdetail": "40014"}) == ("failed", "invalid")
assert st({"status": "9202", "statusdetail": ""}) == ("failed", "")
assert not admin
assert st({"status": "9209"}) == ("failed", "") and len(admin) == 1
assert st({"status": "9208"}) == ("sent", "") and st({"status": "9208"}) == ("sent", "") and len(admin) == 2  # 上限警告は1日1回
m, url, params, h = calls[-1]
assert url.endswith("/snd/v2/record_query.json") and params == {"idxcnt": "0000036273813"}
queue.append(R({}, 500)); assert delivery._fax_api_status("x") == ("sending", "")
print("OK")
