"""売買の販売図面1枚から読み取る内容と、生成する文章の型（売買版）。

賃貸版の schema.py とは別のファイルにして、賃貸版の動きを変えない。
帯・用紙・帯の外の業者向け情報・おすすめポイント・想定Q&A の形は賃貸版と同じにして、
render.obikae（帯替え）・hide.apply（白塗り）・scan（写真補正）をそのまま使えるようにする。

金額の計算（購入諸費用）はモデルにさせず、別のファイル（諸費用の計算）で決まった式で行う。
そのため、計算に要る事実（価格の内訳・消費税の扱い・面積・築年月・管理費等・固定資産税など）は
図面に書いてあるとおりに受け取る。
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# 賃貸版と同じ形のもの（帯・用紙・おすすめポイント・想定Q&A）はそのまま使う
from schema import Band, Money, Paper, Point, QA, _points  # noqa: F401

物件種別の選択肢 = Literal["区分マンション", "戸建", "土地", "収益一棟", "収益区分", "事業用", "その他", "不明"]


class Section(BaseModel):
    """1枚の図面に、区画・号棟・号室ごとに価格が2通り以上書いてあるときの、その1つ分。"""
    名前: str = Field(description="図面に書いてある区別の言葉（例：A区画、1号棟、302号室）")
    価格: Money = Field(default_factory=Money)
    土地面積: Optional[str] = None
    建物面積_専有面積: Optional[str] = None
    間取り: Optional[str] = None


class SaleFacts(BaseModel):
    資料の種類: Literal["マイソク", "マイソクではない", "読み取れない"] = Field(
        "マイソク", description="物件の販売図面なら「マイソク」。物件資料でない写真は「マイソクではない」。ぼやけ・暗い・一部しか写っていないなどで主な項目（価格・所在地）が読めなければ「読み取れない」")
    取引: Literal["売買", "賃貸", "不明"] = Field(
        description="価格（◯◯万円）で売る図面なら売買。賃料・敷金・礼金の図面なら賃貸。判断できなければ不明")
    物件種別: 物件種別の選択肢 = Field(
        "不明", description="区分マンション・戸建（中古・新築とも）・土地（古家付きを含む）・収益一棟（アパート・マンション一棟・ビル一棟）・収益区分（賃貸中で利回りの書いてある区分マンション）・事業用（店舗・事務所・倉庫・工場）")
    新築: Optional[bool] = Field(None, description="新築・未入居・建築中なら true、中古なら false、土地や判断できなければ null")
    物件名: Optional[str] = None
    号室: Optional[str] = Field(None, description="部屋番号の数字や記号だけ（例：302）")
    所在地: Optional[str] = Field(None, description="図面の表記どおり（地番・住居表示の別があればそのまま）")
    交通: List[str] = Field(default_factory=list, description="『〇〇線 〇〇駅 徒歩5分』の形で1路線1件。バス便はそのまま")
    # 価格
    価格: Money = Field(default_factory=Money, description="売買価格。『3,980万円』→ 記載そのまま、金額円 39800000")
    価格_消費税: Literal["税込", "税別", "非課税", "記載なし"] = Field(
        "記載なし", description="価格に消費税が含まれるか。『税込』『うち消費税◯円』『消費税込』は税込。『税別』『＋消費税』は税別。書いていなければ記載なし")
    価格_うち消費税: Money = Field(default_factory=Money, description="『うち消費税◯万円』『（内消費税等◯円）』と書いてあれば")
    価格_うち土地: Money = Field(default_factory=Money, description="土地価格が分けて書いてあれば")
    価格_うち建物: Money = Field(default_factory=Money, description="建物価格が分けて書いてあれば（税込・税別は記載どおり）")
    # 区分マンションの月々の費用
    管理費: Money = Field(default_factory=Money, description="月額")
    修繕積立金: Money = Field(default_factory=Money, description="月額")
    その他の月額費用: List[str] = Field(default_factory=list, description="管理組合費・インターネット使用料・町内会費・駐車場使用料など、月額の記載をそのまま")
    修繕積立基金: Money = Field(default_factory=Money, description="新築などで購入時に一度払う修繕積立基金")
    管理準備金: Money = Field(default_factory=Money, description="購入時に一度払う管理準備金")
    管理形態: Optional[str] = Field(None, description="全部委託・一部委託・自主管理、管理人の勤務形態（日勤・常駐・巡回）などの記載そのまま")
    管理会社: Optional[str] = None
    総戸数: Optional[str] = None
    所在階_階建: Optional[str] = None
    向き: Optional[str] = Field(None, description="バルコニーの向き、または主な採光面")
    # 建物
    間取り: Optional[str] = None
    専有面積: Optional[str] = Field(None, description="区分マンション。壁芯・内法・登記の別が書いてあればそのまま（例：65.12㎡（壁芯））")
    バルコニー面積: Optional[str] = None
    建物面積: Optional[str] = Field(None, description="戸建・一棟。延床面積。各階の内訳があればそのまま")
    築年月: Optional[str] = Field(None, description="図面の表記どおり（例：1998年3月、平成10年3月築、2026年12月完成予定）")
    構造: Optional[str] = None
    認定住宅: Optional[str] = Field(None, description="『長期優良住宅』『低炭素住宅』『ZEH』『住宅性能評価』など、図面に明記があるときだけ記載そのまま")
    買取再販: Optional[str] = Field(None, description="『買取再販』『宅建業者が買い取ってリフォームした住宅』など、図面に明記があるときだけ記載そのまま")
    リフォーム: List[str] = Field(default_factory=list, description="リフォーム・リノベーションの時期と内容の記載（例：2024年 キッチン・浴室交換）")
    # 土地
    土地面積: Optional[str] = Field(None, description="公簿・実測の別があればそのまま")
    敷地持分: Optional[str] = Field(None, description="区分マンションの敷地権の持分・敷地面積の記載があればそのまま")
    私道負担: Optional[str] = Field(None, description="『私道負担 約12㎡』『無』など記載そのまま")
    セットバック: Optional[str] = Field(None, description="『セットバック要 約5㎡』『無』など記載そのまま")
    土地権利: Optional[str] = Field(None, description="所有権・借地権（旧法・普通・定期）・地上権など記載そのまま。区分マンションの敷地権も")
    借地の内容: Optional[str] = Field(None, description="借地なら地代・期間・更新料・譲渡承諾料などの記載そのまま")
    地目: Optional[str] = None
    都市計画: Optional[str] = Field(None, description="市街化区域・市街化調整区域・非線引き など")
    用途地域: Optional[str] = None
    建ぺい率: Optional[str] = None
    容積率: Optional[str] = None
    その他の法令制限: List[str] = Field(default_factory=list, description="防火地域・高度地区・日影規制・風致地区・地区計画・土砂災害警戒区域など、記載そのまま")
    接道: Optional[str] = Field(None, description="『北側 公道 幅員4.0m 接道5.2m』のように、方向・公道私道・幅員・間口の記載そのまま")
    道路種別: Optional[str] = Field(None, description="建築基準法の道路の種類（42条1項1号、42条2項など）の記載そのまま")
    再建築: Optional[str] = Field(None, description="『再建築不可』『再建築可（但し…）』など、再建築についての記載そのまま。書いていなければ null")
    建築条件: Optional[str] = Field(None, description="土地の『建築条件付』『建築条件なし』の記載そのまま")
    建築確認番号: Optional[str] = Field(None, description="新築・未完成物件の建築確認番号")
    # 取引
    現況: Optional[str] = Field(None, description="居住中・空家・空室・賃貸中・更地・古家ありなど記載そのまま")
    引渡し: Optional[str] = Field(None, description="『相談』『即時』『2027年3月下旬』など記載そのまま")
    駐車場: Optional[str] = None
    設備: List[str] = Field(default_factory=list)
    条件_備考: List[str] = Field(default_factory=list, description="ペット可・リフォーム済・告知事項あり・管理規約で禁止のことなど、お客様に関わる記載")
    告知事項: Optional[str] = Field(None, description="『告知事項あり』『心理的瑕疵あり』『事故物件』など、図面にあれば記載そのまま。なければ null")
    契約不適合責任: Optional[str] = Field(None, description="『契約不適合責任免責』『引渡しから2年』など記載そのまま（旧来の『瑕疵担保』の表記もここ）")
    固定資産税_都市計画税: Optional[str] = Field(None, description="年額の記載があればそのまま")
    固定資産税評価額: Optional[str] = Field(None, description="土地・建物の評価額の記載があればそのまま（まれ）")
    評価額_土地: Money = Field(default_factory=Money, description="土地の固定資産税評価額（円）。『固定資産税評価額：土地 90,206,550円』→ 金額円 90206550")
    評価額_建物: Money = Field(default_factory=Money, description="建物の固定資産税評価額（円）")
    評価額_年度: Optional[str] = Field(None, description="評価額の年度の記載そのまま（例：2025年、令和7年度）")
    評価額_土地は持分: Optional[bool] = Field(None, description="区分マンションの土地の評価額が、この部屋の持分の額と書いてあれば true、敷地全体の額と書いてあれば false、わからなければ null")
    # 収益物件
    満室想定年収: Money = Field(default_factory=Money)
    現況年収: Money = Field(default_factory=Money)
    表面利回り: Optional[str] = Field(None, description="図面に書いてある利回りの表記そのまま（満室想定・現況の別も）")
    賃貸状況: Optional[str] = Field(None, description="『全8室中7室賃貸中』など記載そのまま")
    # 元付業者（帯の中の情報。物件確認・内見依頼のFAXの宛先に使う）
    元付業者: Optional[str] = None
    元付業者_電話: Optional[str] = Field(None, description="帯などに書いてある元付業者のTEL")
    元付業者_FAX: Optional[str] = Field(None, description="帯などに書いてある元付業者のFAX。物件確認・内見依頼のFAXの宛先に使う")
    元付業者_免許: Optional[str] = Field(None, description="帯の免許番号（例：東京都知事(3)第12345号）")
    物件番号: Optional[str] = Field(None, description="レインズの物件番号・元付業者の物件番号の記載そのまま（例：レインズ 100123456789）。物件確認・内見依頼のFAXに載せる。お客様向けの図面からは白で消す")
    取引態様: Optional[str] = Field(None, description="図面に書いてある取引態様そのまま（売主・代理・専任媒介・一般媒介・専属専任媒介・仲介など）")
    売主: Optional[str] = Field(None, description="図面に売主の名前・種類（『売主：見本ホーム株式会社』『売主 個人』など）が書いてあればそのまま。なければ null")
    売主_連絡先: List[str] = Field(default_factory=list, description="図面に書いてある売主の電話・FAX・メール・URLを、書いてあるとおり1件ずつ（例：TEL 03-0000-3333）。お客様向けの図面からは白で消す")
    広告転載: Optional[str] = Field(None, description="『広告転載可』『広告不可』『ネット掲載不可』『広告は要承諾』など、広告についての記載そのまま。なければ null")
    仲介手数料_記載: Optional[str] = Field(None, description="お客様（買主）の手数料の負担に関わる記載だけ（例：仲介手数料不要）。業者間の配分（分かれ・片手・両手など）は入れない")
    読み取りに自信がない項目: List[str] = Field(default_factory=list)
    区画が複数: List[Section] = Field(
        default_factory=list,
        description="区画・号棟・号室ごとに価格が2通り以上書いてあるときだけ、その2〜6通り。1通りなら空")
    選んだ区画: Optional[int] = Field(None, description="AIは入れない（利用者が選んだ区画の番号）")


class SaleResult(BaseModel):
    事実: SaleFacts
    用紙: Paper = Field(default_factory=Paper)
    帯: Band = Field(default_factory=lambda: Band(見つかった=False))
    帯の外の業者向け情報: List[str] = Field(
        default_factory=list,
        description="帯の外（備考欄など）に書かれた、元付業者と客付業者のあいだだけの情報を、図面に書いてあるとおり1行ずつ。なければ空")
    おすすめポイント: List[Point] = Field(default_factory=list, description="3〜5個")
    想定QA: List[QA] = Field(default_factory=list, description="8〜10個")
    # AIは入れない。読み取りのあとに決まった決まりで足す、スタッフ向けの注意（社内向けにだけ出す）
    スタッフへの注意: List[str] = Field(default_factory=list, description="AIは入れない。空のままにする")


def for_store(r: "SaleResult") -> dict:
    """Firestore に保存できる形の dict。SaleResult.model_validate で戻せる。"""
    import json
    d = json.loads(r.model_dump_json())
    for parent, key in ((d.get("用紙") or {}, "四隅"), (d.get("帯") or {}, "上端_写真")):
        if isinstance(parent.get(key), list):
            parent[key] = [{"x": p[0], "y": p[1]} if isinstance(p, list) and len(p) >= 2 else p for p in parent[key]]
    return d
