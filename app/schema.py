"""マイソク1枚から読み取る内容と、生成する文章の型。

Claude にはこの型どおりの JSON を返してもらう（structured outputs）。
金額の計算はモデルにさせず、costs.py で決まった式で行う。
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class Money(BaseModel):
    """マイソクの記載をそのまま残しつつ、計算用の数字も持つ。"""
    記載: Optional[str] = Field(None, description="マイソクに書いてある表記そのまま。書いていなければ null")
    金額円: Optional[int] = Field(None, description="円で書いてあれば整数。書いていなければ null")
    ヶ月: Optional[float] = Field(None, description="『1ヶ月』のように月数で書いてあればその数。なければ null")


class OtherCost(BaseModel):
    項目: str
    記載: str
    金額円: Optional[int] = None


class Facts(BaseModel):
    資料の種類: Literal["マイソク", "マイソクではない", "読み取れない"] = Field(
        "マイソク", description="物件の募集図面なら「マイソク」。物件資料でない写真は「マイソクではない」。ぼやけ・暗い・一部しか写っていないなどで主な項目が読めなければ「読み取れない」")
    用途: Literal["居住用", "事業用", "不明"] = Field(
        "居住用", description="マンション・アパート・戸建などは居住用。事務所・店舗・倉庫・駐車場などは事業用")
    取引: Literal["賃貸", "賃貸以外"] = Field(description="賃貸専門のサービス。売買などのマイソクなら「賃貸以外」")
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
    保証金_償却: Optional[str] = None
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
    元付業者_電話: Optional[str] = Field(None, description="帯に書いてある元付業者のTEL")
    元付業者_FAX: Optional[str] = Field(None, description="帯に書いてある元付業者のFAX（内見申込書の送り先に使う）")
    取引態様: Optional[str] = None
    広告転載: Optional[str] = Field(None, description="『広告転載可』『不可』など記載のまま。なければ null")
    読み取りに自信がない項目: List[str] = Field(default_factory=list)


class Band(BaseModel):
    """元付業者の会社情報の帯（ふつうは下端）の位置。画像の高さを1とした割合。"""
    見つかった: bool
    上端: Optional[float] = None
    下端: Optional[float] = None


class Point(BaseModel):
    見出し: str = Field(description="15字程度")
    説明: str = Field("", description="60字程度。お客様にそのまま話せる言葉で")
    根拠: str = Field("", description="マイソクのどの記載にもとづくか")


class QA(BaseModel):
    質問: str
    参考回答: str
    要確認: bool = Field(description="マイソクに答えが書いておらず、元付業者などへの確認が必要なら true")


class Result(BaseModel):
    事実: Facts
    帯: Band = Field(default_factory=lambda: Band(見つかった=False))
    おすすめポイント: List[Point] = Field(default_factory=list, description="3〜5個")
    想定QA: List[QA] = Field(default_factory=list, description="8〜10個")
