"""faximo のテスト送信（Cloud Shell 用・標準ライブラリだけ）。会社のFAXへ1枚送り、結果が出るまで待って表示する。
使い方: python3 tools/fax_test.py [FAX番号]（省略時は 0335775600＝音羽トレンディの事務所FAX）
ログインID・パスワードは Secret Manager の FAXIMO_ID・FAXIMO_PASSWORD から読む（画面には出さない）。"""
import base64, json, secrets, subprocess, sys, time, urllib.parse, urllib.request

URL = "https://rest.faximo.jp/snd/v2"
to = "".join(c for c in (sys.argv[1] if len(sys.argv) > 1 else "0335775600") if c.isdigit())


def secret(name):
    return subprocess.run(["gcloud", "secrets", "versions", "access", "latest", f"--secret={name}"],
                          capture_output=True, text=True, check=True).stdout.strip()


auth = base64.b64encode(f"{secret('FAXIMO_ID')}:{secret('FAXIMO_PASSWORD')}".encode()).decode()


def call(method, path, body=None):
    h = {"Content-Type": "application/json", "X-Auth": auth, "X-Processkey": secrets.token_hex(10)}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def pdf():
    """A4・1枚の英字だけのPDF（日本語は相手FAXの上の1行＝ヘッダーで確かめる）。"""
    text = b"BT /F1 28 Tf 72 700 Td (1pun-shiryo FAX test) Tj 0 -50 Td /F1 16 Tf (Server connection test. Please discard.) Tj ET"
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(text) + text + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x)


body = {"sendto": [{"faxno": to}], "userkey": "otptest" + secrets.token_hex(4),
        "headerinfo": "株式会社音羽トレンディ（1分資料経由） @d@ @t@ P.@p@", "retrynum": 2,
        "subject": "1分資料 接続テスト",
        "Attachment": [{"attachmentname": "test.pdf", "attachmentdata": base64.b64encode(pdf()).decode()}]}
j = call("POST", "/request.json", body)
header = "日本語"
if j.get("result") in ("001100", "021007"):
    print("日本語のヘッダーは受け付けられませんでした。半角のヘッダーで送り直します。")
    body["headerinfo"], body["subject"], header = "1pun-shiryo @d@ @t@ P.@p@", "1pun-shiryo test", "半角"
    j = call("POST", "/request.json", body)
if j.get("result") != "000000":
    sys.exit(f"送信依頼が受け付けられませんでした（結果コード {j.get('result')}）。001001 ならIDかパスワードが違います。")
idx = j["idxcnt"]
print(f"受け付けられました（明細番号 {idx}、ヘッダー：{header}）。結果を待っています…")
for _ in range(30):
    time.sleep(20)
    q = call("GET", "/record_query.json?" + urllib.parse.urlencode({"idxcnt": idx}))
    rows = q.get("SendResultList") or []
    st = str(rows[0].get("status")) if rows else "0"
    if st != "0":
        print({"5": "★ 送信完了。事務所のFAXに1枚届いているか、上の1行の表示も見てください。"}.get(
            st, f"送信できませんでした（status {st} / 詳細 {rows[0].get('statusdetail', '')}）"))
        break
else:
    print("10分待っても結果が出ません。faximo の管理画面の送信履歴を見てください。")
