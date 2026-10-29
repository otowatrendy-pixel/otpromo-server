"""マイソク1枚から読み取る内容と、生成する文章の型。

Claude にはこの型どおりの JSON を返してもらう（structured outputs）。
金額の計算はモデルにさせず、costs.py で決まった式で行う。
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class Money(BaseModel):
    """マイソクの記載をそのまま残しつつ、計算用の数字も持つ。"""
    記載: Optional[str] = Field(None, description="募集図面に書いてある表記そのまま。書いていなければ null")
    金額円: Optional[int] = Field(None, description="円で書いてあれば整数。書いていなければ null")
    ヶ月: Optional[float] = Field(None, description="『1ヶ月』のように月数で書いてあればその数。なければ null")


class OtherCost(BaseModel):
    項目: str
    記載: str
    金額円: Optional[int] = None
    時期: Literal["入居時", "月額", "更新時", "退去時", "不明"] = Field(
        "入居時", description="いつ払う費用か。初期費用の合計には「入居時」だけを入れる")


class Choice(BaseModel):
    """1枚の図面に、区画や使い方によって金額が2通り以上書いてあるときの、その1つ分。"""
    名前: str = Field(description="図面に書いてある区別の言葉（例：住居として、事務所として、店舗①、2階）")
    賃料: Money = Field(default_factory=Money)
    管理費_共益費: Money = Field(default_factory=Money)
    敷金: Money = Field(default_factory=Money)
    礼金: Money = Field(default_factory=Money)
    保証金: Money = Field(default_factory=Money)


class Facts(BaseModel):
    資料の種類: Literal["マイソク", "マイソクではない", "読み取れない"] = Field(
        "マイソク", description="物件の募集図面なら「マイソク」。物件資料でない写真は「マイソクではない」。ぼやけ・暗い・一部しか写っていないなどで主な項目が読めなければ「読み取れない」")
    取引: Literal["賃貸", "賃貸以外"] = Field(description="賃貸専門のサービス。売買などの募集図面なら「賃貸以外」")
    用途: Literal["居住用", "事業用", "不明"] = Field(
        "不明", description="住まい用なら居住用。店舗・事務所・倉庫など事業向けなら事業用。どちらも賃貸として資料を作る")
    物件種別: Optional[str] = Field(None, description="マンション、アパート、戸建、店舗、事務所など")
    物件名: Optional[str] = None
    所在地: Optional[str] = None
    交通: List[str] = Field(default_factory=list, description="『〇〇線 〇〇駅 徒歩5分』の形で1路線1件")
    間取り: Optional[str] = None
    専有面積: Optional[str] = None
    建物面積: Optional[str] = None
    土地面積: Optional[str] = None
    築年月: Optional[str] = None
    構造: Optional[str] = None
    所在階_階建: Optional[str] = None
    向き: Optional[str] = None
    賃料: Money = Field(default_factory=Money)
    管理費_共益費: Money = Field(default_factory=Money)
    敷金: Money = Field(default_factory=Money)
    礼金: Money = Field(default_factory=Money)
    保証金: Money = Field(default_factory=Money, description="事業用に多い。敷金と別に書いてあれば")
    償却: Optional[str] = Field(None, description="保証金・敷金の償却や敷引の記載そのまま（例：償却2ヶ月、解約時20%）")
    賃料_消費税: Literal["税込", "税別", "非課税", "記載なし"] = Field(
        "記載なし", description="賃料・共益費の金額が税込か税別か。図面に書いてあるときだけ税込・税別・非課税")
    更新料: Optional[str] = None
    保証会社: Optional[str] = Field(None, description="加入要否・費用の記載をそのまま")
    火災保険: Optional[str] = None
    契約期間: Optional[str] = None
    入居時期: Optional[str] = None
    # 共通
    設備: List[str] = Field(default_factory=list)
    条件_備考: List[str] = Field(default_factory=list, description="ペット可、二人入居可、フリーレントなど")
    その他の費用: List[OtherCost] = Field(default_factory=list, description="鍵交換、消毒、24時間サポートなど費用の記載")
    元付業者: Optional[str] = None
    元付業者_電話: Optional[str] = Field(None, description="帯などに書いてある元付業者のTEL")
    元付業者_FAX: Optional[str] = Field(None, description="帯などに書いてある元付業者のFAX。内見申込書FAXの宛先に使う")
    号室: Optional[str] = Field(None, description="部屋番号の数字や記号だけ（例：203）。「号室」は付けない。物件名と分けて書いてあれば")
    取引態様: Optional[str] = None
    仲介手数料_記載: Optional[str] = Field(None, description="手数料の負担・配分の記載そのまま（例：借主100%、当方不払い）。なければ null")
    広告転載: Optional[str] = Field(None, description="『広告転載可』『不可』など記載のまま。なければ null")
    読み取りに自信がない項目: List[str] = Field(default_factory=list)
    条件が複数: List[Choice] = Field(
        default_factory=list,
        description="賃料・管理費・敷金・礼金・保証金が、区画や使い方によって2通り以上書いてあるときだけ、その2〜4通り。1通りなら空")
    選んだ条件: Optional[int] = Field(None, description="AIは入れない（利用者が選んだ条件の番号）")


def _points(v):
    """保存用の [{"x":…, "y":…}] を [[x, y]] に戻す（Firestore は配列の中に配列を入れられないため）。"""
    if isinstance(v, list):
        return [[p.get("x"), p.get("y")] if isinstance(p, dict) else p for p in v]
    return v


class Band(BaseModel):
    """元付業者の会社情報の帯（ふつうは下端）の位置。画像の高さを1とした割合。"""
    見つかった: bool
    上端_写真: Optional[List[List[float]]] = Field(
        None, description="写真のとき、帯の上の縁の左端と右端の位置。元の画像の幅・高さを1とした[x, y]を2つ")
    上端: Optional[float] = None
    下端: Optional[float] = None

    _pts = field_validator("上端_写真", mode="before")(staticmethod(_points))


class Paper(BaseModel):
    """スマホで撮った写真のときの、用紙（画面に映った図面なら図面のページ）の四隅。"""
    写真: bool = Field(False, description="机の上の紙やパソコンの画面をカメラで撮った写真なら true。PDFやスキャンなら false")
    四隅: Optional[List[List[float]]] = Field(
        None, description="写真のとき、用紙の四隅の位置。画像の幅と高さを1とした[x, y]を左上・右上・右下・左下の順に4つ。用紙の外の机・画面の余白・枠は含めない")


    _pts = field_validator("四隅", mode="before")(staticmethod(_points))

class Point(BaseModel):
    見出し: str = Field(description="15字程度")
    説明: str = Field("", description="60字程度。お客様にそのまま話せる言葉で")
    根拠: str = Field("", description="募集図面のどの記載にもとづくか")


class QA(BaseModel):
    質問: str
    参考回答: str
    要確認: bool = Field(description="募集図面に答えが書いておらず、元付業者などへの確認が必要なら true")


class Result(BaseModel):
    事実: Facts
    用紙: Paper = Field(default_factory=Paper)
    帯: Band = Field(default_factory=lambda: Band(見つかった=False))
    帯の外の業者向け情報: List[str] = Field(
        default_factory=list,
        description="帯の外（備考欄など）に書かれた、元付業者と客付業者のあいだだけの情報を、図面に書いてあるとおり1行ずつ。なければ空")
    おすすめポイント: List[Point] = Field(default_factory=list, description="3〜5個")
    想定QA: List[QA] = Field(default_factory=list, description="8〜10個")


def for_store(r: "Result") -> dict:
    """Firestore に保存できる形の dict（四隅などの [[x, y]] を [{"x", "y"}] にする）。Result.model_validate で戻せる。"""
    import json
    d = json.loads(r.model_dump_json())
    for parent, key in ((d.get("用紙") or {}, "四隅"), (d.get("帯") or {}, "上端_写真")):
        if isinstance(parent.get(key), list):
            parent[key] = [{"x": p[0], "y": p[1]} if isinstance(p, list) and len(p) >= 2 else p for p in parent[key]]
    return d
